"""Application service for correction sessions."""

from __future__ import annotations

import json
import hashlib
import uuid
from copy import deepcopy
from pathlib import Path
from typing import Any
from urllib.parse import quote

from PIL import Image

from .assets import fixed_source, resolve_asset, source_from_id
from .constants import CORRECTION_EXPORTS_DIR, FIXED_SOURCE_ID, PROJECT_ROOT, ensure_correction_dirs
from .draft_store import list_sessions, load_session, new_session_id, save_session, utc_now
from .exporter import export_full_dataset_workbook, export_session_workbook
from .quality_selection import (
    QualitySelectionUnavailable,
    filter_snapshot,
    top1_selection_for_run,
    validate_selection_source,
)
from .workbook import load_snapshot, parse_action
from ..trajectory_data import _format_manual_actions_box
from ..trajectories_tree.tree_builder import parse_bbox


def _session_or_raise(session_id: str) -> dict[str, Any]:
    session = load_session(session_id)
    if session is None:
        raise FileNotFoundError("修正会话不存在")
    if session.get("published"):
        raise FileNotFoundError("修正会话已发布，不再参与纠偏")
    return session


def _source_for_session(session: dict[str, Any]) -> tuple[Path, Path]:
    if str(session.get("source_id", "")).replace("\\", "/") != FIXED_SOURCE_ID:
        raise ValueError("修正会话必须使用项目内置标注表和质检 Top-1 结果")
    return source_from_id(str(session["source_id"]))


def _snapshot(session: dict[str, Any]) -> dict[str, Any]:
    workbook_path, asset_root = _source_for_session(session)
    snapshot = load_snapshot(
        workbook_path,
        asset_root=asset_root,
        source_kind=session.get("source_kind"),
    )
    selection = session.get("selection")
    if not isinstance(selection, dict):
        raise QualitySelectionUnavailable("旧修正草稿没有绑定质检 Top-1 结果，请重新执行质检")
    validate_selection_source(selection)
    return filter_snapshot(snapshot, selection)


def _base_row(snapshot: dict[str, Any], excel_row: int) -> tuple[dict[str, Any], dict[str, Any]]:
    for group in snapshot["groups"]:
        for row in group["rows"]:
            if int(row["excel_row"]) == excel_row:
                return group, row
    raise KeyError(excel_row)


def _edit_for(session: dict[str, Any], excel_row: int) -> dict[str, Any]:
    return session.setdefault("row_edits", {}).setdefault(str(excel_row), {})


def _validate_actions_box(
    *,
    action: dict[str, Any],
    actions_box: str,
    image: str,
    asset_root: Path,
) -> str:
    value = str(actions_box or "").strip()
    if not value:
        return ""
    bbox = parse_bbox(value)
    if bbox is None:
        raise ValueError("actions_box 必须包含有效的 bbox 坐标")
    try:
        image_path = resolve_asset(asset_root, image)
        with Image.open(image_path) as picture:
            width, height = picture.size
    except (OSError, ValueError) as exc:
        raise ValueError(f"无法读取步骤截图以校验 bbox：{exc}") from exc
    x1, y1, x2, y2 = bbox
    if x1 < 0 or y1 < 0 or x2 > width or y2 > height or x2 <= x1 or y2 <= y1:
        raise ValueError(f"bbox 超出截图范围 {width}x{height}")
    try:
        return _format_manual_actions_box(action, bbox)
    except ValueError as exc:
        raise ValueError(str(exc)) from exc


def _overlay_row(session_id: str, session: dict[str, Any], base: dict[str, Any]) -> dict[str, Any]:
    row = deepcopy(base)
    edit = session.get("row_edits", {}).get(str(base["excel_row"]), {})
    baseline_box = str(session.get("bbox_baselines", {}).get(str(base["excel_row"]), base.get("actions_box", "")))
    if "actions_box" in edit:
        row["actions_box"] = str(edit["actions_box"])
    if "sop" in edit:
        row["sop"] = edit["sop"]
    if "actions" in edit:
        row["actions"] = edit["actions"]
        row["action"] = parse_action(edit["actions"])
    row["deleted"] = bool(edit.get("deleted", False))
    row["action_edited"] = "actions" in edit
    row["sop_edited"] = "sop" in edit
    row["original_summary"] = row.get("original_summary", row.get("summary", ""))
    row["original_thought"] = row.get("original_thought", row.get("thought", ""))
    if "summary" in edit:
        row["summary"] = str(edit["summary"])
    if "thought" in edit:
        row["thought"] = str(edit["thought"])
    row["original_action"] = edit.get("original_actions", row.get("actions", ""))
    row["original_actions_box"] = baseline_box
    row["bbox_edited"] = str(row.get("actions_box", "")) != str(row["original_actions_box"])
    row["bbox_source"] = str(edit.get("bbox_source", "manual" if row["bbox_edited"] else "original"))
    row["thought"] = str(edit.get("thought", row.get("original_thought", "")))
    row["summary_source"] = "manual" if "summary" in edit else "original"
    row["thought_source"] = "manual" if "thought" in edit else "original"
    row["cot_summary"] = ""
    row["cot_status"] = "pending" if row["action_edited"] or row["bbox_edited"] else "not_needed"
    row["cot_action_hash"] = ""
    row["cot_bbox_hash"] = ""
    row["cot_generated_at"] = None
    cot = session.get("cot", {}).get(str(base["excel_row"]), {})
    if isinstance(cot, dict):
        action_hash = hashlib.sha256(json.dumps(row["action"], ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        bbox_hash = hashlib.sha256(str(row.get("actions_box", "")).encode()).hexdigest()
        if (
            cot.get("content_tag") == "thought_summary"
            and cot.get("action_hash") == action_hash
            and cot.get("summary")
        ):
            row["summary"] = str(cot.get("summary"))
            row["cot_summary"] = str(cot.get("summary"))
            row["thought"] = str(cot.get("thought", ""))
            row["summary_source"] = "generated"
            row["thought_source"] = "generated"
            row["cot_status"] = "generated"
            row["cot_action_hash"] = action_hash
            row["cot_bbox_hash"] = bbox_hash
            row["cot_generated_at"] = cot.get("generated_at")
    if row["cot_status"] != "generated" and ("summary" in edit or "thought" in edit):
        row["cot_status"] = "manual"
    row["edited"] = row["action_edited"] or row["sop_edited"] or row["bbox_edited"] or row["deleted"]
    row["edit_status"] = "、".join(
        label
        for enabled, label in (
            (row["action_edited"], "动作已编辑"),
            (row["bbox_edited"], "框已编辑"),
            ("summary" in edit or "thought" in edit, "COT 已编辑"),
            (row["sop_edited"], "SOP 已编辑"),
            (row["deleted"], "已删除"),
        )
        if enabled
    )
    row["image_url"] = (
        f"/api/correction/sessions/{quote(session_id, safe='')}/assets/"
        f"{quote(str(row['image']).replace(chr(92), '/'), safe='/')}"
    )
    return row


def _group_summary(session_id: str, session: dict[str, Any], group: dict[str, Any]) -> dict[str, Any]:
    rows = [_overlay_row(session_id, session, row) for row in group["rows"]]
    active = [row for row in rows if not row["deleted"]]
    return {
        "group_id": group["group_id"],
        "task": group["task"],
        "meta_task": group["meta_task"],
        "quality": group["quality"],
        "prefix": group["prefix"],
        "export": bool(session.get("group_exports", {}).get(group["group_id"], group["export"])),
        "row_count": len(rows),
        "active_row_count": len(active),
        "edited_row_count": sum(1 for row in rows if row["edited"]),
        "action_edit_count": sum(1 for row in rows if row["action_edited"]),
    }


def _public_session(session: dict[str, Any], snapshot: dict[str, Any]) -> dict[str, Any]:
    groups = [_group_summary(session["session_id"], session, group) for group in snapshot["groups"]]
    source_id = str(session["source_id"])
    return {
        "session_id": session["session_id"],
        "source_id": source_id,
        "source": fixed_source(),
        "tree_run_id": session.get("tree_run_id"),
        "selection": session.get("selection"),
        "created_at": session.get("created_at"),
        "updated_at": session.get("updated_at"),
        "row_count": snapshot["row_count"],
        "group_count": len(groups),
        "groups": groups,
        "exports": session.get("exports", []),
    }


def sessions() -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for session in list_sessions():
        if session.get("published"):
            continue
        if not str(session.get("tree_run_id", "")).strip():
            continue
        try:
            snapshot = _snapshot(session)
        except (FileNotFoundError, ValueError, OSError):
            continue
        public = _public_session(session, snapshot)
        result.append(
            {
                "session_id": public["session_id"],
                "source_id": public["source_id"],
                "source": public["source"],
                "tree_run_id": public["tree_run_id"],
                "created_at": public["created_at"],
                "updated_at": public["updated_at"],
                "group_count": public["group_count"],
                "row_count": public["row_count"],
                "export_count": len(public["exports"]),
            }
        )
    return result


def published_tree_run_ids() -> set[str]:
    """Tree runs whose correction drafts have already been published."""
    return {
        str(session.get("tree_run_id", ""))
        for session in list_sessions()
        if session.get("published") and str(session.get("tree_run_id", "")).strip()
    }


def create_session(tree_run_id: str) -> dict[str, Any]:
    ensure_correction_dirs()
    if str(tree_run_id) in published_tree_run_ids():
        raise ValueError("该质检批次的纠偏会话已经发布，不能重新进入纠偏")
    selection = top1_selection_for_run(tree_run_id)
    workbook_path, package_root = source_from_id(FIXED_SOURCE_ID)

    # A batch has one draft.  Reusing it makes the create endpoint safe to
    # retry after navigating away or refreshing the correction page.
    for existing in list_sessions():
        if existing.get("published"):
            continue
        if str(existing.get("tree_run_id", "")) != str(tree_run_id):
            continue
        try:
            return _public_session(existing, _snapshot(existing))
        except (FileNotFoundError, ValueError, OSError, QualitySelectionUnavailable):
            continue

    normalized_source_id = FIXED_SOURCE_ID
    source_kind = "annotated_workbook"
    full_snapshot = load_snapshot(
        workbook_path,
        asset_root=package_root,
        source_kind=source_kind,
    )
    snapshot = filter_snapshot(full_snapshot, selection)
    session_id = new_session_id()
    package_root_value = package_root.resolve().relative_to(PROJECT_ROOT.resolve()).as_posix()
    session = {
        "session_id": session_id,
        "source_id": normalized_source_id,
        "source_kind": source_kind,
        "tree_run_id": selection["tree_run_id"],
        "selection": selection,
        # This is informational session metadata.  The source is resolved
        # again through the allow-listed source ID on every request.
        "package_root": package_root_value,
        "created_at": utc_now(),
        "updated_at": utc_now(),
        "row_edits": {},
        "bbox_baselines": {str(row["excel_row"]): str(row.get("actions_box", "")) for group in snapshot["groups"] for row in group["rows"]},
        "group_exports": {group["group_id"]: bool(group["export"]) for group in snapshot["groups"]},
        "exports": [],
    }
    save_session(session)
    return _public_session(session, snapshot)


def get_session(session_id: str) -> dict[str, Any]:
    session = _session_or_raise(session_id)
    return _public_session(session, _snapshot(session))


def get_groups(session_id: str) -> list[dict[str, Any]]:
    session = _session_or_raise(session_id)
    snapshot = _snapshot(session)
    return [_group_summary(session_id, session, group) for group in snapshot["groups"]]


def get_group(session_id: str, group_id: str) -> dict[str, Any]:
    session = _session_or_raise(session_id)
    snapshot = _snapshot(session)
    group = next((item for item in snapshot["groups"] if item["group_id"] == group_id), None)
    if group is None:
        raise KeyError(group_id)
    return {
        **_group_summary(session_id, session, group),
        "rows": [_overlay_row(session_id, session, row) for row in group["rows"]],
    }


def get_cot(session_id: str) -> dict[str, Any]:
    """Return generated COT rows for a correction session."""
    session = _session_or_raise(session_id)
    snapshot = _snapshot(session)
    groups: list[dict[str, Any]] = []
    for group in snapshot["groups"]:
        rows = [_overlay_row(session_id, session, row) for row in group["rows"]]
        rows.sort(key=lambda item: int(item.get("step", 0)))
        groups.append({
            "group_id": group["group_id"],
            "trajectory_id": group["meta_task"],
            "task": group["task"],
            "rows": [
                {
                    "task_id": str(row.get("image", "").replace("\\", "/").split("/", 1)[0]),
                    "trajectory_id": group["meta_task"],
                    "excel_row": row["excel_row"],
                    "step": row["step"],
                    "image": row.get("image", ""),
                    "image_url": row.get("image_url", ""),
                    "action": row["actions"],
                    "original_action": row.get("original_action", row["actions"]),
                    "actions_box": row.get("actions_box", ""),
                    "original_actions_box": row.get("original_actions_box", row.get("actions_box", "")),
                    "original_summary": row.get("original_summary", row.get("summary", "")),
                    "summary": row.get("summary", ""),
                    "original_thought": row.get("original_thought", ""),
                    "thought": row.get("thought", ""),
                    "history": "\n".join(
                        f"Step {int(previous['step'])}: {str(previous.get('original_summary') or '').strip()}"
                        for previous in rows[:index]
                        if str(previous.get("original_summary") or "").strip() and not previous.get("deleted")
                    ),
                    "status": row.get("cot_status", "not_needed"),
                    "action_edited": bool(row.get("action_edited")),
                    "bbox_edited": bool(row.get("bbox_edited")),
                    "bbox_source": row.get("bbox_source", "original"),
                    "summary_source": row.get("summary_source", "original"),
                    "thought_source": row.get("thought_source", "original"),
                    "cot_action_hash": row.get("cot_action_hash", ""),
                    "cot_bbox_hash": row.get("cot_bbox_hash", ""),
                    "generated_at": row.get("cot_generated_at"),
                }
                for index, row in enumerate(rows)
                if (row.get("action_edited") or row.get("bbox_edited")) and not row.get("deleted")
            ],
        })
    return {"session_id": session_id, "groups": groups}


def patch_row(session_id: str, excel_row: int, payload: dict[str, Any]) -> dict[str, Any]:
    session = _session_or_raise(session_id)
    snapshot = _snapshot(session)
    group, base = _base_row(snapshot, excel_row)
    edit = _edit_for(session, excel_row)

    if payload.get("sop") is not None:
        sop = str(payload["sop"])
        if sop == base["sop"]:
            edit.pop("sop", None)
        else:
            edit["sop"] = sop

    if payload.get("actions") is not None:
        actions = str(payload["actions"])
        parsed = parse_action(actions)
        if parsed.get("action") == "unknown":
            raise ValueError("actions 必须是包含合法 action 字段的 JSON")
        if actions == base["actions"]:
            edit.pop("actions", None)
            edit.pop("original_actions", None)
        else:
            edit.setdefault("original_actions", base["actions"])
            edit["actions"] = actions
        # A changed action invalidates any COT generated for the previous
        # action.  The result remains available in the cache for audit, but
        # cannot be displayed or exported for the new action.
        session.setdefault("cot", {}).pop(str(excel_row), None)

    if payload.get("actions_box") is not None:
        actions_box = str(payload["actions_box"])
        action_value = parse_action(edit.get("actions", base["actions"]))
        if not isinstance(action_value, dict) or not action_value.get("action"):
            raise ValueError("请先保存包含合法 action 的动作")
        _, asset_root = _source_for_session(session)
        normalized_box = _validate_actions_box(
            action=action_value,
            actions_box=actions_box,
            image=str(base.get("image", "")),
            asset_root=asset_root,
        )
        baseline_box = str(session.get("bbox_baselines", {}).get(str(excel_row), base.get("actions_box", "")))
        if normalized_box == baseline_box:
            edit.pop("actions_box", None)
            edit.pop("bbox_source", None)
        else:
            edit["actions_box"] = normalized_box
            edit["bbox_source"] = "manual"
        # COT is generated from the expert action JSON and screenshot.  A bbox
        # edit does not change that semantic input, so keep the generated text.

    for field in ("summary", "thought"):
        if payload.get(field) is None:
            continue
        value = str(payload[field]).strip()
        baseline = str(base.get(field, "") or "")
        if value == baseline:
            edit.pop(field, None)
        else:
            edit[field] = value
        session.setdefault("cot", {}).pop(str(excel_row), None)

    if payload.get("deleted") is not None:
        if bool(payload["deleted"]):
            edit["deleted"] = True
        else:
            edit.pop("deleted", None)

    if not edit:
        session.setdefault("row_edits", {}).pop(str(excel_row), None)
    save_session(session)
    return {
        "group": _group_summary(session_id, session, group),
        "row": _overlay_row(session_id, session, base),
    }


def patch_group_export(session_id: str, group_id: str, export: bool) -> dict[str, Any]:
    session = _session_or_raise(session_id)
    snapshot = _snapshot(session)
    group = next((item for item in snapshot["groups"] if item["group_id"] == group_id), None)
    if group is None:
        raise KeyError(group_id)
    session.setdefault("group_exports", {})[group_id] = bool(export)
    save_session(session)
    return _group_summary(session_id, session, group)


def export_session(session_id: str) -> dict[str, Any]:
    session = _session_or_raise(session_id)
    snapshot = _snapshot(session)
    workbook_path, _ = _source_for_session(session)
    export_id = uuid.uuid4().hex[:16]
    output_dir = CORRECTION_EXPORTS_DIR / session_id
    output_dir.mkdir(parents=True, exist_ok=True)
    result = export_session_workbook(
        workbook_path=workbook_path,
        snapshot=snapshot,
        session=session,
        output_dir=output_dir,
        export_id=export_id,
    )
    history = {
        "export_id": export_id,
        "filename": result["filename"],
        "created_at": utc_now(),
        "download_url": f"/api/correction/sessions/{session_id}/exports/{quote(result['filename'], safe='')}" ,
        "sheets": result["sheets"],
    }
    session.setdefault("exports", []).insert(0, history)
    save_session(session)
    return {**history, "summary": result["summary"]}


def export_dataset_session(session_id: str) -> dict[str, Any]:
    """Publish a full source-shaped workbook with all persisted corrections overlaid."""
    session = _session_or_raise(session_id)
    snapshot = _snapshot(session)
    workbook_path, _ = _source_for_session(session)
    export_id = uuid.uuid4().hex[:16]
    output_dir = CORRECTION_EXPORTS_DIR / session_id
    output_dir.mkdir(parents=True, exist_ok=True)
    result = export_full_dataset_workbook(
        workbook_path=workbook_path,
        snapshot=snapshot,
        session=session,
        output_dir=output_dir,
        export_id=export_id,
    )
    history = {
        "export_id": export_id,
        "kind": "full_dataset",
        "filename": result["filename"],
        "created_at": utc_now(),
        "download_url": (
            f"/api/correction/sessions/{session_id}/exports/"
            f"{quote(result['filename'], safe='')}"
        ),
        "sheets": result["sheets"],
    }
    session.setdefault("exports", []).insert(0, history)
    save_session(session)
    return {**history, "summary": result["summary"]}


def download_export(session_id: str, filename: str) -> Path:
    _session_or_raise(session_id)
    safe_name = Path(filename).name
    if safe_name != filename or Path(safe_name).suffix.lower() not in {".xlsx", ".xlsm"}:
        raise ValueError("无效的导出文件名")
    path = (CORRECTION_EXPORTS_DIR / session_id / safe_name).resolve()
    try:
        path.relative_to((CORRECTION_EXPORTS_DIR / session_id).resolve())
    except ValueError as exc:
        raise ValueError("导出路径无效") from exc
    if not path.is_file():
        raise FileNotFoundError("导出文件不存在")
    return path


def session_asset(session_id: str, image_value: str) -> Path:
    session = _session_or_raise(session_id)
    _, package_root = _source_for_session(session)
    return resolve_asset(package_root, image_value)
