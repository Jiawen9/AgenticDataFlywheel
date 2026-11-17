"""Excel-only paired training export. No image reads, models or cloud calls."""
from collections import OrderedDict
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
from urllib.parse import urlsplit
import uuid

from openpyxl import load_workbook

from ..file_io import io_path
from .training_jsonl import SYSTEM_PROMPT, parse_action, _description

VERSION = 2


def text(value):
    return "" if value is None else str(value).strip()


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest_file(path):
    digest = hashlib.sha256()
    with io_path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def image_prefix(value=None):
    if value is None:
        from .internal_uploader import _configuration
        config = _configuration()
        value = ("s3://" + config["bucket"] + "/" + config["prefix"]) if config["bucket"] else ""
    value = text(value).replace("\\", "/")
    if not value:
        return ""
    parsed = urlsplit(value)
    if (parsed.scheme != "s3" or not parsed.netloc or parsed.username or parsed.password
            or parsed.query or parsed.fragment or ":" in parsed.netloc
            or any(ord(c) < 32 for c in value) or any(p in {".", ".."} for p in parsed.path.split("/"))):
        raise ValueError("图片前缀请填写 s3://bucket/目录，不包含查询参数或 .、.. 路径段")
    return "s3://" + parsed.netloc + "/" + "/".join(p for p in parsed.path.split("/") if p).rstrip("/") + ("/" if parsed.path.strip("/") else "")


def relative_image(value):
    value = text(value).replace("\\", "/")
    path = PurePosixPath(value)
    if not value or path.is_absolute() or any(p in {".", ".."} for p in value.split("/")) or ":" in value or any(ord(c) < 32 for c in value):
        raise ValueError("缺少有效的图片相对路径")
    return path.as_posix()


def current_box(row, action):
    """Validate binding once while projecting the final table, without images."""
    from ..export_markers import action_value
    value = text(row.get("actions_box"))
    name = {"click": "click", "long_press": "long_press", "swipe": "swipe_screen"}.get(action.get("action"))
    if not name or not value.startswith(name + "("):
        return ""
    match = re.search(r"<bbox>\s*\[([^\]]+)\]\s*</bbox>", value)
    try:
        import math
        box = [float(x.strip()) for x in match.group(1).split(",")]
        if len(box) != 4 or not all(math.isfinite(x) for x in box) or not (0 <= box[0] < box[2] and 0 <= box[1] < box[3]):
            return ""
    except (AttributeError, ValueError):
        return ""
    original = action_value(row.get("original_action", row.get("original_actions", action)))
    if row.get("action_edited") or original != action:
        action_hash = hashlib.sha256(canonical(action).encode()).hexdigest()
        manual = (row.get("bbox_action_hash") == action_hash
                  and text(row.get("bbox_image")).replace("\\", "/") == text(row.get("image")).replace("\\", "/"))
        generated = row.get("cot_action_hash") == action_hash and row.get("cot_bbox_hash") == hashlib.sha256(value.encode()).hexdigest()
        if not (manual or generated):
            return ""
    return value


def write_paired_workbook(workbook, output, report_path, prefix, *, source_id):
    """Convert only the named Excel columns; one output line per usable step."""
    prefix = image_prefix(prefix)
    base = {"version": VERSION, "image_prefix": prefix, "filename": Path(output).name,
            "report_filename": Path(report_path).name, "included_count": 0, "excluded_count": 0}
    if not prefix:
        return {**base, "status": "needs_configuration", "error": "Excel 已生成；填写云道图片前缀后可生成配套 JSONL"}
    report = {**base, "source_id": source_id, "exclusions": []}
    groups = OrderedDict()
    book = load_workbook(io_path(workbook), read_only=True, data_only=False)
    try:
        for sheet in book.worksheets:
            rows = sheet.iter_rows(values_only=True)
            headers = [text(value) for value in next(rows, ())]
            for number, cells in enumerate(rows, 2):
                if not any(text(value) for value in cells):
                    continue
                row = dict(zip(headers, cells))
                row.update(_row=number, _sheet=sheet.title)
                group = (sheet.title, text(row.get("task_id")), text(row.get("trajectory_id") or row.get("meta_task")))
                groups.setdefault(group, []).append(row)
    finally:
        book.close()
    io_path(Path(output).parent).mkdir(parents=True, exist_ok=True)
    temporary = Path(output).with_suffix(".tmp")
    with io_path(temporary).open("w", encoding="utf-8", newline="\n") as stream:
        for group_index, (identity, rows) in enumerate(groups.items(), 1):
            history = []
            invalid_history = False
            # Final workbook row order is the exported execution order. Do not
            # reorder steps using an ambiguous old source row or a display ID.
            for index, row in enumerate(rows):
                action = None
                try:
                    if not identity[1] or not identity[2]:
                        raise ValueError("缺少 task_id 或 trajectory_id，无法隔离轨迹历史")
                    action = parse_action(row.get("actions", row.get("action")))
                    if invalid_history:
                        raise ValueError("前序动作不可解析，无法生成准确执行历史")
                    task = text(row.get("task"))
                    if not task:
                        raise ValueError("缺少真实任务正文 task")
                    relative = relative_image(row.get("training_image_relative"))
                    description = _description(row, action)
                    call = current_box(row, action) or canonical(action)
                    sample_id = str(uuid.uuid5(uuid.NAMESPACE_URL, canonical([VERSION, source_id, identity, index])))
                    history_text = ("\n".join(history) if history else "Empty") + "\n"
                    sample = {"messages": [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": f"## Task\n{task}\n\n## Execution History\n{history_text}\n## Current Screenshot\n<image>"}],
                        "solution": f"<thought>\n{text(row.get('thought'))}\n</thought>\n<tool_call>\n{call}\n</tool_call>\n<action>\n{description}\n</action>\nsftcall: {canonical(action)}\nuuid:{sample_id}\nmetaID:{group_index}.{index}",
                        "images": [prefix + relative], "is_key_step": row.get("is_key_step") if row.get("is_key_step") in (True, False, 0, 1) else None,
                        "is_refined": row.get("is_refined") in (True, 1)}
                    stream.write(json.dumps(sample, ensure_ascii=False, allow_nan=False) + "\n")
                    report["included_count"] += 1
                except (ValueError, TypeError, KeyError) as exc:
                    report["excluded_count"] += 1
                    report["exclusions"].append({"sheet": identity[0], "task_id": identity[1], "trajectory_id": identity[2], "excel_row": row["_row"], "step": row.get("step"), "reason": str(exc)})
                    invalid_history = invalid_history or action is None
                history.append(f"Step {index + 1}: {_description(row, action)}")
    io_path(temporary).replace(io_path(output))
    io_path(Path(report_path).parent).mkdir(parents=True, exist_ok=True)
    io_path(report_path).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {**base, "status": "ready" if report["included_count"] else "empty",
            "included_count": report["included_count"], "excluded_count": report["excluded_count"],
            "sha256": digest_file(output), "report_sha256": digest_file(report_path),
            "size_bytes": io_path(output).stat().st_size,
            **({"error": "无可用训练样本，请查看排除报告"} if not report["included_count"] else {})}
