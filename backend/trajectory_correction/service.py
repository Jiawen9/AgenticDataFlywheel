"""Application service for correction sessions."""

from __future__ import annotations

import json
import uuid
from copy import deepcopy
from pathlib import Path
from typing import Any
from urllib.parse import quote

from .assets import fixed_source, resolve_asset, source_from_id
from .constants import CORRECTION_EXPORTS_DIR, FIXED_SOURCE_ID, PROJECT_ROOT, ensure_correction_dirs
from .draft_store import list_sessions, load_session, new_session_id, save_session, utc_now
from .exporter import export_session_workbook
from .quality_selection import (
    QualitySelectionUnavailable,
    filter_snapshot,
    top1_selection_for_run,
    validate_selection_source,
)
from .workbook import load_snapshot, parse_action


def _session_or_raise(session_id: str) -> dict[str, Any]:
    session = load_session(session_id)
    if session is None:
        raise FileNotFoundError("修正会话不存在")
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


def _overlay_row(session_id: str, session: dict[str, Any], base: dict[str, Any]) -> dict[str, Any]:
    row = deepcopy(base)
    edit = session.get("row_edits", {}).get(str(base["excel_row"]), {})
    if "sop" in edit:
        row["sop"] = edit["sop"]
    if "actions" in edit:
        row["actions"] = edit["actions"]
        row["action"] = parse_action(edit["actions"])
    row["deleted"] = bool(edit.get("deleted", False))
    row["action_edited"] = "actions" in edit
    row["sop_edited"] = "sop" in edit
    row["edited"] = row["action_edited"] or row["sop_edited"] or row["deleted"]
    row["edit_status"] = "、".join(
        label
        for enabled, label in (
            (row["action_edited"], "动作已编辑"),
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


def create_session(tree_run_id: str) -> dict[str, Any]:
    ensure_correction_dirs()
    selection = top1_selection_for_run(tree_run_id)
    workbook_path, package_root = source_from_id(FIXED_SOURCE_ID)

    # A batch has one draft.  Reusing it makes the create endpoint safe to
    # retry after navigating away or refreshing the correction page.
    for existing in list_sessions():
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
