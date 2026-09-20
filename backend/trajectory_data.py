"""Read task, trajectory, and immutable tree-run data for the web application."""

from __future__ import annotations

import json
import re
import threading
import tempfile
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

from openpyxl import load_workbook
from PIL import Image

from .batch_operations import active_batch_lock
from .batch_lifecycle import is_batch_active
from .trajectories_tree.tree_builder import parse_action
from .data_store import DATA_ROOT, ArtifactStore
from .trajectory_context import (resolve_batch_context, row_task_id, row_trajectory_id,
    row_identity_metadata, annotation_batch_lock, AnnotationVersionConflict, TrajectoryBatchContext)


BACKEND_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = BACKEND_DIR.parent
WORKSPACE_DIR = DATA_ROOT / "system"
TRAJECTORY_ROOT = DATA_ROOT / "raw" / "rollout_trajectories"
ANNOTATED_XLSX = WORKSPACE_DIR / "preprocessing" / "annotated_trajectories.xlsx"
TREE_RUNS_DIR = WORKSPACE_DIR / "trajectory_tree_runs"
TREE_JOBS_DIR = WORKSPACE_DIR / "trajectory_tree_jobs"
QUALITY_JOBS_DIR = WORKSPACE_DIR / "trajectory_quality_jobs"
QUALITY_RESULTS_DIR = WORKSPACE_DIR / "trajectory_quality_results"

GOAL_RE = re.compile(
    r"\*\*原始目标\*\*\s*[:：]\s*(.*?)(?=\r?\n\s*\r?\n|\Z)",
    re.DOTALL,
)
STEP_RE = re.compile(r"step(\d+)", re.IGNORECASE)
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
BBOX_WRITE_LOCK = threading.Lock()


@dataclass(frozen=True)
class TaskMetadata:
    task_id: str
    goal: str
    warning: str
    first_trajectory: str


def _trajectory_number(task_id: str, path: Path) -> int | None:
    match = re.fullmatch(re.escape(task_id) + r"-(\d+)", path.name)
    return int(match.group(1)) if match else None


def first_trajectory_dir(task_dir: Path) -> Path | None:
    candidates = [
        (number, child)
        for child in task_dir.iterdir()
        if child.is_dir()
        and (number := _trajectory_number(task_dir.name, child)) is not None
    ]
    return min(candidates, key=lambda item: item[0])[1] if candidates else None


def _message_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(item.get("text", ""))
            for item in content
            if isinstance(item, dict) and item.get("type") == "text"
        )
    return ""


def extract_original_goal(request_path: Path) -> str:
    payload = json.loads(request_path.read_text(encoding="utf-8-sig"))
    for message in payload.get("messages", []):
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        match = GOAL_RE.search(_message_text(message.get("content")))
        if match:
            return " ".join(match.group(1).strip().splitlines())
    return ""


def discover_tasks(trajectory_root: Path = TRAJECTORY_ROOT) -> dict[str, TaskMetadata]:
    if not trajectory_root.is_dir():
        return {}
    tasks: dict[str, TaskMetadata] = {}
    for task_dir in sorted(
        (path for path in trajectory_root.iterdir() if path.is_dir()),
        key=lambda path: path.name.casefold(),
    ):
        first = first_trajectory_dir(task_dir)
        if first is None:
            continue
        request_path = first / "turn001_orch_model_request.json"
        warning = ""
        goal = ""
        if request_path.is_file():
            try:
                goal = extract_original_goal(request_path)
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                warning = f"无法读取原始目标：{exc}"
        else:
            warning = f"缺少 {request_path.name}"
        if not goal:
            goal = task_dir.name
            warning = warning or "未找到 **原始目标**，已回退为任务 ID"
        tasks[task_dir.name] = TaskMetadata(
            task_id=task_dir.name,
            goal=goal,
            warning=warning,
            first_trajectory=first.name,
        )
    return tasks


def task_id_from_resource(value: str) -> str:
    normalized = value.replace("\\", "/").strip("/")
    return normalized.split("/", 1)[0] if normalized else ""


def _step_number(image: str, fallback: int) -> int:
    match = STEP_RE.search(Path(image).name)
    return int(match.group(1)) if match else fallback


def asset_url(relative_path: str, batch_id: str | None = None, annotation_version: str | None = None) -> str:
    normalized = relative_path.replace("\\", "/").strip("/")
    url = "/api/assets/" + quote(normalized, safe="/")
    if batch_id:
        url += "?batch_id=" + quote(batch_id, safe="")
        if annotation_version:
            url += "&annotation_version=" + quote(annotation_version, safe="")
    return url


def _load_annotated_trajectories(xlsx_path: Path = ANNOTATED_XLSX, *, payload: dict[str, Any] | None = None,
                                 batch_id: str | None = None, annotation_version: str | None = None) -> dict[str, list[dict[str, Any]]]:
    """Read current steps exclusively from the configured JSON snapshot."""
    from .stage_artifacts import read_workbook_payload, structured_input_exists
    if payload is None:
        if not structured_input_exists(xlsx_path):
            if xlsx_path.is_file():
                raise FileNotFoundError(f"Required JSON snapshot is missing: {xlsx_path.with_suffix('.json')}")
            return {}
        payload = read_workbook_payload(xlsx_path)
    name = next(iter(payload["sheets"]))
    rows = payload["sheets"][name]
    required = {"image", "xml", "action", "summary", "actions_box"}
    headers = set(payload.get("columns", {}).get(name) or (rows[0] if rows else {}))
    missing = sorted(required - headers)
    if missing:
        raise ValueError(f"Excel 缺少必要列：{', '.join(missing)}")
    grouped = {}
    for row_index, row in enumerate(rows, 2):
        trajectory = row_trajectory_id(row)
        image = str(row.get("image") or "").strip()
        if not trajectory or not image:
            continue
        task_id = row_task_id(row)
        steps = grouped.setdefault(task_id, {}).setdefault(trajectory, [])
        action_text = str(row.get("action") or "")
        steps.append({
            "step": _step_number(image, len(steps) + 1), "excel_row": row_index,
            "image": image, "image_url": asset_url(image, batch_id, annotation_version),
            **row_identity_metadata(row),
            "xml": str(row.get("xml") or "").strip(),
            "action_text": action_text, "action": parse_action(action_text),
            "action_summary": str(row.get("summary") or ""),
            "actions_box": str(row.get("actions_box") or ""),
        })
    return {task_id: [{"trajectory_id": trajectory, "step_count": len(steps),
                      **row_identity_metadata(steps[0]),
                      "steps": sorted(steps, key=lambda step: (step["step"], step["excel_row"]))}
                     for trajectory, steps in sorted(trajectories.items())]
            for task_id, trajectories in grouped.items()}

def trajectory_summaries(
    task_id: str,
    xlsx_path: Path = ANNOTATED_XLSX,
    *, batch_id: str | None = None, annotation_version: str | None = None, data_root: Path | None = None,
) -> list[dict[str, Any]]:
    """Return only trajectory names and counts for the task list UI."""
    return load_trajectory_index(xlsx_path, batch_id=batch_id, annotation_version=annotation_version, data_root=data_root).get(task_id, [])


def _load_trajectory_index(xlsx_path: Path = ANNOTATED_XLSX, *, payload: dict[str, Any] | None = None) -> dict[str, list[dict[str, Any]]]:
    """Read IDs without parsing actions; JSON remains usable without its Excel."""
    from .stage_artifacts import read_workbook_payload, structured_input_exists
    if payload is None:
        if not structured_input_exists(xlsx_path):
            if xlsx_path.is_file():
                raise FileNotFoundError(f"Required JSON snapshot is missing: {xlsx_path.with_suffix('.json')}")
            return {}
        payload = read_workbook_payload(xlsx_path)
    name = next(iter(payload["sheets"]))
    rows = payload["sheets"][name]
    headers = payload.get("columns", {}).get(name) or (rows[0] if rows else {})
    if "image" not in headers:
        raise ValueError("Excel 缺少必要列：image")
    counts = {}
    identities = {}
    for row in rows:
        trajectory = row_trajectory_id(row)
        task_id = row_task_id(row)
        if trajectory and task_id:
            values = counts.setdefault(task_id, {})
            values[trajectory] = values.get(trajectory, 0) + 1
            identities[(task_id, trajectory)] = row_identity_metadata(row)
    return {task_id: [{"trajectory_id": trajectory, "step_count": count, **identities[(task_id, trajectory)]}
                     for trajectory, count in sorted(items.items())]
            for task_id, items in counts.items()}

def _load_annotated_trajectory(task_id: str, trajectory_id: str, xlsx_path: Path = ANNOTATED_XLSX) -> dict[str, Any] | None:
    return next((item for item in _load_annotated_trajectories(xlsx_path).get(task_id, [])
                 if item["trajectory_id"] == trajectory_id), None)

def _format_manual_actions_box(
    action: dict[str, Any], bbox: tuple[int, int, int, int]
) -> str:
    kind = str(action.get("action", "")).lower()
    tagged = f"<bbox>[{','.join(str(value) for value in bbox)}]</bbox>"
    if kind == "click":
        return f"click(bbox={tagged})"
    if kind == "long_press":
        return f"long_press(bbox={tagged})"
    if kind == "swipe":
        start = action.get("start_coordinate")
        end = action.get("end_coordinate")
        if not isinstance(start, list) or len(start) < 2 or not isinstance(end, list) or len(end) < 2:
            raise ValueError("swipe 缺少起止坐标")
        dx = float(end[0]) - float(start[0])
        dy = float(end[1]) - float(start[1])
        direction = (
            ("left" if dx < 0 else "right")
            if abs(dx) > abs(dy)
            else ("up" if dy < 0 else "down")
        )
        return f"swipe_screen(bbox={tagged}, direction={direction})"
    raise ValueError(f"动作 {kind or 'unknown'} 不支持 bbox")


def update_action_bbox(
    task_id: str,
    trajectory_id: str,
    step: int,
    excel_row: int,
    bbox: tuple[int, int, int, int],
    action_override: dict[str, Any] | None = None,
    *,
    xlsx_path: Path = ANNOTATED_XLSX,
    trajectory_root: Path = TRAJECTORY_ROOT,
    batch_id: str | None = None, expected_annotation_version: str | None = None,
    data_root: Path | None = None,
) -> str | dict[str, Any]:
    """Validate and atomically persist a manually redrawn action bbox."""
    if batch_id is not None:
        return _update_batch_bbox(batch_id, task_id, trajectory_id, step, excel_row, bbox,
                                  action_override, expected_annotation_version, data_root)
    if xlsx_path == ANNOTATED_XLSX:
        xlsx_path = resolve_annotated_path(task_id)
    from .stage_artifacts import (structured_input_exists, read_workbook_payload, write_payload_workbook,
                                  publish_workbook, store_root)
    if not structured_input_exists(xlsx_path):
        raise FileNotFoundError("标注轨迹 JSON 不存在")
    if excel_row < 2:
        raise ValueError("无效的 Excel 行号")
    x1, y1, x2, y2 = (int(value) for value in bbox)
    if x1 < 0 or y1 < 0 or x2 <= x1 or y2 <= y1:
        raise ValueError("bbox 必须是有效的 [x1,y1,x2,y2]")

    with BBOX_WRITE_LOCK:
        temporary_path = xlsx_path.with_name(f".{xlsx_path.stem}.bbox-edit.tmp{xlsx_path.suffix}")
        current = read_workbook_payload(xlsx_path)
        from .stage_artifacts import fingerprint, sidecar_path
        source_file = sidecar_path(xlsx_path)
        previous = current.get("source_ref") or {"kind": "annotation_snapshot", "path": str(source_file), "sha256": fingerprint(source_file)}
        with active_batch_lock(str(previous.get("batch_id") or "manual-annotation"), data_root or store_root(xlsx_path)):
            write_payload_workbook(temporary_path, current)
            workbook = load_workbook(temporary_path)
            try:
                sheet = workbook.active
                headers = {
                    str(cell.value).strip(): cell.column
                    for cell in sheet[1]
                    if cell.value is not None
                }
                required = {"文件夹名", "image", "action", "actions_box"}
                missing = sorted(required - headers.keys())
                if missing:
                    raise ValueError(f"Excel 缺少必要列：{', '.join(missing)}")
                if excel_row > sheet.max_row:
                    raise ValueError("Excel 行号超出范围")

                row_trajectory = str(sheet.cell(excel_row, headers["文件夹名"]).value or "").strip()
                image_value = str(sheet.cell(excel_row, headers["image"]).value or "").strip()
                if row_trajectory != trajectory_id:
                    raise ValueError("Excel 行与轨迹不匹配")
                if task_id_from_resource(image_value) != task_id:
                    raise ValueError("Excel 行与任务不匹配")
                if _step_number(image_value, -1) != step:
                    raise ValueError("Excel 行与 step 不匹配")

                image_path = resolve_image_asset(image_value, trajectory_root)
                with Image.open(image_path) as image:
                    width, height = image.size
                if x2 > width or y2 > height:
                    raise ValueError(f"bbox 超出截图范围 {width}x{height}")

                action_text = str(sheet.cell(excel_row, headers["action"]).value or "")
                actions_box = _format_manual_actions_box(action_override or parse_action(action_text), (x1, y1, x2, y2))
                sheet.cell(excel_row, headers["actions_box"]).value = actions_box
                if temporary_path.exists():
                    temporary_path.unlink()
                workbook.save(temporary_path)
                workbook.close()
                temporary_path.replace(xlsx_path)
                publish_workbook(xlsx_path, batch_id=str(previous.get("batch_id") or "manual-annotation"),
                                 data_root=data_root or store_root(xlsx_path), stage="02_annotation", source_refs=[previous] if previous else [],
                                 metadata={"manual_bbox": {"task_id": task_id, "trajectory_id": trajectory_id, "step": step}})
                return actions_box
            finally:
                workbook.close()
                if temporary_path.exists():
                    temporary_path.unlink()


def task_summaries(
    trajectory_root: Path = TRAJECTORY_ROOT,
    xlsx_path: Path = ANNOTATED_XLSX,
    *, batch_id: str | None = None, annotation_version: str | None = None, data_root: Path | None = None,
) -> list[dict[str, Any]]:
    if batch_id is not None:
        context = resolve_batch_context(batch_id, annotation_version, data_root)
        metadata = batch_task_metadata(context)
        annotated = _load_trajectory_index(payload=context.payload)
    else:
        metadata = discover_tasks(trajectory_root)
        annotated = load_trajectory_index(xlsx_path)
    values: list[dict[str, Any]] = []
    for task_id, item in metadata.items():
        trajectories = annotated.get(task_id, [])
        values.append(
            {
                "task_id": task_id,
                "goal": item.goal,
                "warning": item.warning,
                "first_trajectory": item.first_trajectory,
                "trajectory_count": len(trajectories),
                "step_count": sum(value["step_count"] for value in trajectories),
                "annotated": bool(trajectories),
            }
        )
    return values


def resolve_image_asset(relative_path: str, root: Path = TRAJECTORY_ROOT, *, batch_id: str | None = None,
                        annotation_version: str | None = None, data_root: Path | None = None) -> Path:
    if batch_id is not None:
        root = resolve_batch_context(batch_id, annotation_version, data_root).raw_root
    normalized = relative_path.replace("\\", "/").lstrip("/")
    candidate = (root / Path(normalized)).resolve()
    resolved_root = root.resolve()
    try:
        candidate.relative_to(resolved_root)
    except ValueError as exc:
        raise ValueError("资源路径超出轨迹目录") from exc
    if candidate.suffix.lower() not in IMAGE_EXTENSIONS:
        raise ValueError("仅允许访问轨迹图片")
    if not candidate.is_file():
        raise FileNotFoundError(relative_path)
    return candidate


def list_tree_runs(runs_dir: Path = TREE_RUNS_DIR) -> list[dict[str, Any]]:
    if runs_dir.resolve() == TREE_RUNS_DIR.resolve():
        from .batch_results import list_current_tree_batches
        return [item for item in list_current_tree_batches(runs_dir.parent.parent)
                if is_batch_active(item["batch_id"], runs_dir.parent.parent)]
    if not runs_dir.is_dir():
        return []
    runs: list[dict[str, Any]] = []
    for directory in runs_dir.iterdir():
        manifest_path = directory / "manifest.json"
        if directory.is_dir() and not directory.name.startswith(".") and manifest_path.is_file():
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            if isinstance(manifest, dict) and manifest.get("run_id") == directory.name:
                if is_batch_active(str(manifest.get("batch_id") or manifest["run_id"]), runs_dir.parent.parent):
                    runs.append(manifest)
    return sorted(runs, key=lambda item: str(item.get("completed_at", "")), reverse=True)


def find_tree_run(run_id: str, runs_dir: Path = TREE_RUNS_DIR) -> dict[str, Any] | None:
    if runs_dir.resolve() == TREE_RUNS_DIR.resolve():
        from .batch_results import resolve_current_batch_id, current_tree_batch
        data_root = runs_dir.parent.parent
        batch_id = resolve_current_batch_id(run_id, data_root)
        return current_tree_batch(batch_id, data_root) if batch_id else None
    return next((item for item in list_tree_runs(runs_dir) if item.get("run_id") == run_id), None)


def resolve_tree_run_dir(run_id: str, runs_dir: Path = TREE_RUNS_DIR) -> Path:
    candidate = (runs_dir / run_id).resolve()
    if not candidate.is_relative_to(runs_dir.resolve()):
        raise ValueError("无效建树批次编号")
    return candidate


def annotated_sources(xlsx_path: Path = ANNOTATED_XLSX) -> list[Path]:
    return [xlsx_path]


def resolve_annotated_path(task_id: str | None = None) -> Path:
    from .stage_artifacts import structured_input_exists
    for path in reversed(annotated_sources()):
        if structured_input_exists(path) and (task_id is None or task_id in _load_trajectory_index(path)):
            return path
    return ANNOTATED_XLSX


def load_annotated_trajectories(xlsx_path: Path = ANNOTATED_XLSX, *, batch_id: str | None = None,
                                annotation_version: str | None = None, data_root: Path | None = None) -> dict[str, list[dict[str, Any]]]:
    if batch_id is not None:
        context = resolve_batch_context(batch_id, annotation_version, data_root)
        return _load_annotated_trajectories(payload=context.payload, batch_id=batch_id,
                                           annotation_version=context.annotation_version)
    merged = {}
    for path in annotated_sources(xlsx_path):
        merged.update(_load_annotated_trajectories(path))
    return merged


def load_trajectory_index(xlsx_path: Path = ANNOTATED_XLSX, *, batch_id: str | None = None,
                          annotation_version: str | None = None, data_root: Path | None = None) -> dict[str, list[dict[str, Any]]]:
    if batch_id is not None:
        context = resolve_batch_context(batch_id, annotation_version, data_root)
        return _load_trajectory_index(payload=context.payload)
    merged = {}
    for path in annotated_sources(xlsx_path):
        merged.update(_load_trajectory_index(path))
    return merged


def load_annotated_trajectory(task_id: str, trajectory_id: str, xlsx_path: Path = ANNOTATED_XLSX, *,
                             batch_id: str | None = None, annotation_version: str | None = None,
                             data_root: Path | None = None) -> dict[str, Any] | None:
    if batch_id is not None:
        return next((item for item in load_annotated_trajectories(batch_id=batch_id,
                    annotation_version=annotation_version, data_root=data_root).get(task_id, [])
                    if item["trajectory_id"] == trajectory_id), None)
    for path in reversed(annotated_sources(xlsx_path)):
        result = _load_annotated_trajectory(task_id, trajectory_id, path)
        if result is not None:
            return result
    return None


def batch_task_metadata(context: TrajectoryBatchContext) -> dict[str, TaskMetadata]:
    """Discover labels from this snapshot only, including old new-store batches."""
    tasks = {}
    for rows in context.payload["sheets"].values():
        for row in rows:
            task_id = row_task_id(row)
            if task_id in tasks:
                continue
            goal = context.task_goals.get(task_id, "")
            warning = ""
            if not goal:
                request = (context.raw_root / str(row["image"]).replace("\\", "/")).parent / "turn001_orch_model_request.json"
                if request.is_file():
                    try:
                        goal = extract_original_goal(request)
                    except (OSError, ValueError):
                        warning = "无法读取原始目标"
            tasks[task_id] = TaskMetadata(task_id, goal or task_id, warning,
                                          str(row.get("source_trajectory_id") or row.get("文件夹名") or row_trajectory_id(row)))
    return tasks


def _update_batch_bbox(batch_id, task_id, trajectory_id, step, excel_row, bbox,
                       action_override, expected_version, data_root):
    from .stage_artifacts import write_payload_workbook
    if not expected_version:
        raise AnnotationVersionConflict("修改标框必须提供当前 annotation_version")
    root = Path(data_root or DATA_ROOT).resolve()
    with active_batch_lock(batch_id, root):
        context = resolve_batch_context(batch_id, root=root)
        if context.annotation_version != expected_version:
            raise AnnotationVersionConflict("标框版本已更新，请刷新后重试")
        if len(bbox) != 4:
            raise ValueError("bbox 必须包含四个整数")
        x1, y1, x2, y2 = (int(value) for value in bbox)
        if x1 < 0 or y1 < 0 or x2 <= x1 or y2 <= y1:
            raise ValueError("bbox 必须是有效的 [x1,y1,x2,y2]")
        payload = deepcopy(context.payload)
        name = next(iter(payload["sheets"]))
        rows = payload["sheets"][name]
        if excel_row < 2 or excel_row - 2 >= len(rows):
            raise ValueError("无效的 Excel 行号")
        row = rows[excel_row - 2]
        if row_task_id(row) != task_id or row_trajectory_id(row) != trajectory_id or _step_number(str(row.get("image")), -1) != step:
            raise ValueError("行与任务、轨迹或步骤不匹配")
        image = resolve_image_asset(str(row["image"]), context.raw_root)
        with Image.open(image) as screenshot:
            width, height = screenshot.size
        if x2 > width or y2 > height:
            raise ValueError(f"bbox 超出截图范围 {width}x{height}")
        value = _format_manual_actions_box(action_override or parse_action(str(row["action"])), (x1, y1, x2, y2))
        if row.get("actions_box") == value:
            return {"actions_box": value, "batch_id": batch_id,
                    "annotation_version": context.annotation_version, "annotation_ref": context.annotation_ref}
        row["actions_box"] = value
        temporary_root = root / "tmp" / "annotation_edits"
        temporary_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=temporary_root) as directory:
            path = Path(directory) / "annotated_trajectories.xlsx"
            write_payload_workbook(path, payload)
            from .batch_results import invalidation_record_entry, drain_batch_invalidations
            store = ArtifactStore(root)
            artifact = store.publish_many(batch_id, [{"stage": "02_annotation", "payload": payload,
                "workbooks": {path.name: path}, "source_refs": context.annotation_ref.get("source_refs", []),
                "metadata": {**context.annotation_ref.get("metadata", {}), "raw_root": str(context.raw_root),
                             "manual_bbox": {"task_id": task_id, "trajectory_id": trajectory_id, "step": step}},
                "expected_version": context.annotation_version}],
                record_entries=[invalidation_record_entry(store, batch_id, "annotation", [task_id])])[0]
        drain_batch_invalidations(batch_id, root)
        return {"actions_box": value, "batch_id": batch_id,
                "annotation_version": artifact["version"], "annotation_ref": artifact}
