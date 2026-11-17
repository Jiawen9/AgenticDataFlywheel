"""Deterministic training samples from immutable release rows, without models.

Only tiny JSONL/report files are created. Original tables, images and coordinate
values are never rewritten. The cloud image URI uses the actual upload mapping.
"""
from collections import OrderedDict
import hashlib
import json
import math
from pathlib import Path
import re
import uuid

from openpyxl import load_workbook
from PIL import Image

from ..data_store.release_provenance import FrozenReleaseSources
from ..file_io import io_path, resolve_path
from ..export_markers import action_value
from .upload_sources import _Scope, _batch_id, _text

VERSION = "training_jsonl_v1"
SYSTEM_PROMPT = '''You are a GUI agent. You are given a task and your action history, with screenshots. You need to perform the next action to complete the task.

# Action Space
{"action": "click", "coordinate": [x, y]}
{"action": "long_press", "coordinate": [x, y]}
{"action": "type", "text": ""}
{"action": "open", "text": "app_name"}
{"action": "swipe", "start_coordinate": [x1, y1], "end_coordinate": [x2, y2]}
{"action": "system_button", "button": "button_name"} # Options: back, home, menu, enter
{"action": "wait"}
{"action": "terminate", "status": "success or failure"}
{"action": "answer", "text": "xxx"} # Use escape characters \\', \\", and \\n in text part to ensure we can parse the text in normal python string format.

# Response format
Your response must contain the following three tags. The <thought> and <action> tags must be in Chinese.
<thought>
Your reasoning process that connects the task, history, and current visual information to decide the next step.
</thought>
<tool_call>
The machine-readable tool call to be executed that must follow the Action Space strictly.
</tool_call>
<action>
A human-readable summary of the action you will perform.
</action>'''


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _sha(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def file_digest(path):
    digest = hashlib.sha256()
    with io_path(Path(path)).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def parse_action(value):
    action = action_value(value)
    allowed = {"click", "long_press", "swipe", "type", "open", "system_button", "wait", "terminate", "answer"}
    if not isinstance(action, dict) or action.get("action") not in allowed:
        raise ValueError("动作不是可解析的单个标准 action")
    kind = action["action"]
    for name in (("coordinate",) if kind in {"click", "long_press"} else ("start_coordinate", "end_coordinate") if kind == "swipe" else ()):
        point = action.get(name)
        if not isinstance(point, list) or len(point) != 2 or any(type(x) not in (int, float) or not math.isfinite(x) for x in point):
            raise ValueError(f"动作的 {name} 不是两个有限坐标")
    required = {"type": "text", "open": "text", "answer": "text", "system_button": "button", "terminate": "status"}.get(kind)
    if required and not isinstance(action.get(required), str):
        raise ValueError(f"动作缺少有效 {required}")
    _json(action)
    return action


def _description(row, action):
    original = action_value(row.get("original_action", row.get("original_actions", action)))
    changed = bool(row.get("action_edited")) or original != action
    # An unchanged source SOP describes the old action. Only a manual SOP can
    # override the description after editing that action; the projection has
    # already checked the bindings of any generated summary below.
    if _text(row.get("sop")) and (not changed or row.get("sop_source") == "manual" or row.get("sop_edited")):
        return _text(row["sop"])
    if _text(row.get("summary")) and (not changed or row.get("summary_source") in {"manual", "generated"}):
        return _text(row["summary"])
    return "执行动作：" + _json(action) if isinstance(action, dict) else "动作记录不可解析"


def effective_tool_call(row, action, image_size):
    """A missing/stale box falls back to JSON; no step is excluded for its box."""
    value = _text(row.get("actions_box"))
    kind = action["action"]
    name = {"click": "click", "long_press": "long_press", "swipe": "swipe_screen"}.get(kind)
    if not name:
        return _json(action), "not_applicable"
    match = re.search(r"<bbox>\s*\[([^\]]+)\]\s*</bbox>", value)
    valid = value.startswith(name + "(") and match is not None
    try:
        box = [float(x.strip()) for x in match.group(1).split(",")] if match else []
        width, height = image_size
        valid = valid and len(box) == 4 and all(math.isfinite(x) for x in box)
        valid = valid and 0 <= box[0] < box[2] <= width and 0 <= box[1] < box[3] <= height
    except (TypeError, ValueError, IndexError):
        valid = False
    original = action_value(row.get("original_action", row.get("original_actions", action)))
    changed = bool(row.get("action_edited")) or original != action
    if changed:
        action_hash = _sha(_json(action))
        image = str(row.get("image") or "").replace("\\", "/")
        manual_binding = row.get("bbox_action_hash") == action_hash and str(row.get("bbox_image") or "").replace("\\", "/") == image
        cot_binding = row.get("cot_action_hash") == action_hash and row.get("cot_bbox_hash") == _sha(value)
        valid = valid and (manual_binding or cot_binding)
    return (value, "bbox") if valid else (_json(action), "bbox_missing_or_stale")


def release_rows(registry, release, items):
    """Excel defines frozen scope; release-owned provenance provides row identity."""
    root = resolve_path(registry.data_root)
    frozen = FrozenReleaseSources(root, release)
    upload_dirs = {resolve_path(Path(item["source_dir"])): item for item in items if item["kind"] == "trajectory"}
    groups = OrderedDict()
    for index, file in enumerate(release.get("excel_paths", [])):
        source = release.get("source_refs", [])[index]
        batch = _batch_id(release, source, index)
        scope = _Scope(frozen, source, batch, root, file["sha256"])
        reference = source["artifact"]
        manifest = frozen.get(reference["batch_id"], reference["stage"], reference["version"])
        payload = json.loads(io_path(frozen.resolve_file(manifest, "result.json")).read_text(encoding="utf-8"))
        frozen_rows = {int(row["excel_row"]): {**group, **(row.get("values") or {}), **row}
                       for group in payload.get("groups", []) for row in group.get("rows", []) if row.get("excel_row") is not None}
        book = load_workbook(io_path(registry.resolve_project_path(file["path"])), read_only=True, data_only=False)
        try:
            for sheet in book.worksheets:
                iterator = sheet.iter_rows(values_only=True)
                headers = [_text(value) for value in next(iterator, ())]
                if "image" not in headers:
                    continue
                for number, cells in enumerate(iterator, 2):
                    excel_row = dict(zip(headers, cells))
                    if not _text(excel_row.get("image")):
                        # Preserve a missing-image row for the explicit exclusion report.
                        if not any(_text(value) for value in cells):
                            continue
                    image = str(excel_row.get("image") or "").replace("\\", "/")
                    metadata = (frozen_rows.get(number, {}) if payload.get("export_profile") == "corrected_selection_v1" else {}) or scope.images.get(image, {})
                    row = {**metadata, **excel_row}
                    row["actions"] = excel_row.get("actions", excel_row.get("action", metadata.get("actions", metadata.get("action"))))
                    row.update(source_table=index, source_sheet=sheet.title, source_excel_row=number, cot_policy=payload.get("cot_policy", "generate"), batch_id=batch)
                    try:
                        path = Path(image)
                        local = resolve_path(path if path.is_absolute() else scope.raw_root / path)
                        allowed = bool(image and local.is_relative_to(scope.raw_root))
                    except (OSError, ValueError):
                        local, allowed = scope.raw_root, False
                    # Keep nested screenshot paths and choose the most specific
                    # already-frozen upload directory, never a guessed raw root.
                    matching = [directory for directory in upload_dirs if local.is_relative_to(directory)]
                    owner = max(matching, key=lambda directory: len(directory.parts)) if matching else None
                    item = upload_dirs.get(owner) if owner is not None else None
                    row["local_image"] = str(local)
                    row["cloud_item"] = item
                    row["cloud_image_relative"] = local.relative_to(owner).as_posix() if owner is not None else ""
                    row["image_allowed"] = allowed
                    identity = str(row.get("trajectory_id") or row.get("meta_task") or row.get("source_trajectory_id") or local.parent.name)
                    key = (index, str(row.get("task_id") or ""), identity)
                    groups.setdefault(key, []).append(row)
        finally:
            book.close()
    return groups


def write_training_bundle(registry, release, items, destination, request_key):
    output = registry.data_root / "releases" / release["release_id"] / "training" / request_key[:20]
    upload_dir = output / "files"
    io_path(upload_dir).mkdir(parents=True, exist_ok=True)
    jsonl = upload_dir / "train.jsonl"
    temporary = upload_dir / ".train.jsonl.tmp"
    report = {"version": VERSION, "release_id": release["release_id"], "coordinate_policy": "preserve_source_values", "included_count": 0, "excluded_count": 0, "bbox_fallback_count": 0, "key_step_count": 0, "unknown_key_step_count": 0, "refined_count": 0, "exclusions": []}
    groups = release_rows(registry, release, items)
    with io_path(temporary).open("w", encoding="utf-8", newline="\n") as stream:
        for group_number, (identity, rows) in enumerate(groups.items(), 1):
            history = []
            history_invalid_at = None
            rows.sort(key=lambda row: (int(row.get("step") or row["source_excel_row"]), row["source_excel_row"]))
            for step_index, row in enumerate(rows):
                action = None
                reason_code = "invalid_action"
                try:
                    action = parse_action(row["actions"])
                    if history_invalid_at is not None:
                        reason_code = "history_unavailable"
                        raise ValueError(f"前序步骤 {history_invalid_at} 的动作不可解析，无法形成必要执行历史")
                    reason_code = "task_text_unavailable"
                    task_text = _text(row.get("task"))
                    if not task_text:
                        raise ValueError("缺少真实任务文本，不能用平台任务编号代替")
                    reason_code = "image_unavailable"
                    if not row["image_allowed"] or not row["cloud_item"]:
                        raise ValueError("截图不属于已上传的冻结轨迹范围或路径缺失")
                    image = Path(row["local_image"])
                    with Image.open(io_path(image)) as picture:
                        size = picture.size
                    call, box_mode = effective_tool_call(row, action, size)
                    summary = _description(row, action)
                    thought = "" if row["cot_policy"] == "skip" else _text(row.get("thought"))
                    image_uri = f"s3://{destination['bucket']}/{row['cloud_item']['bucket_path']}{row['cloud_image_relative']}"
                    sample_id = str(uuid.uuid5(uuid.NAMESPACE_URL, _json([VERSION, release["release_id"], identity, row.get("step_key") or row.get("step") or row["source_excel_row"], row["source_sheet"]])))
                    key_step = row.get("is_key_step")
                    key_step = bool(key_step) if type(key_step) in (bool, int) and key_step in (0, 1) else None
                    history_text = (chr(10).join(history) if history else "Empty") + "\n"
                    sample = {"messages": [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": f"## Task\n{task_text}\n\n## Execution History\n{history_text}\n## Current Screenshot\n<image>"}],
                        "solution": f"<thought>\n{thought}\n</thought>\n<tool_call>\n{call}\n</tool_call>\n<action>\n{summary}\n</action>\nsftcall: {_json(action)}\nuuid:{sample_id}\nmetaID:{group_number}.{step_index}",
                        "images": [image_uri], "is_key_step": key_step, "is_refined": row.get("is_refined") is True or row.get("is_refined") == 1}
                    stream.write(json.dumps(sample, ensure_ascii=False, allow_nan=False) + "\n")
                    report["included_count"] += 1
                    report["bbox_fallback_count"] += int(box_mode == "bbox_missing_or_stale")
                    report["key_step_count"] += int(key_step is True)
                    report["unknown_key_step_count"] += int(key_step is None)
                    report["refined_count"] += int(sample["is_refined"])
                except (OSError, ValueError, TypeError, KeyError) as exc:
                    report["excluded_count"] += 1
                    report["exclusions"].append({"table": identity[0] + 1, "task_id": identity[1], "trajectory_id": identity[2], "step": row.get("step"), "excel_row": row["source_excel_row"], "code": reason_code, "reason": str(exc)})
                    if action is None and history_invalid_at is None:
                        history_invalid_at = row.get("step") or step_index + 1
                # Excluding a training sample does not erase an already executed
                # action from the evidence history of subsequent samples.
                history.append(f"Step {step_index + 1}: {_description(row, action)}")
    io_path(temporary).replace(io_path(jsonl))
    report_path = output / "exclusions.json"
    io_path(report_path).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    batches = list(dict.fromkeys(release.get("batch_ids", [])))
    cloud_root = batches[0] if len(batches) == 1 else release["release_id"]
    bucket_path = "/".join(part.strip("/") for part in (destination["prefix"], cloud_root, "训练数据", release["release_id"], request_key[:12]) if part) + "/"
    return {**{key: value for key, value in report.items() if key != "exclusions"}, "status": "ready", "file_path": registry.project_path(jsonl), "report_path": registry.project_path(report_path), "sha256": file_digest(jsonl), "report_sha256": file_digest(report_path), "size_bytes": io_path(jsonl).stat().st_size, "bucket_path": bucket_path, "source_dir": str(upload_dir)}
