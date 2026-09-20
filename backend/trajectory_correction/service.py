"""Application service for correction sessions."""

from __future__ import annotations

import json
import hashlib
import uuid
import shutil
from copy import deepcopy
from contextlib import contextmanager
from functools import wraps
from pathlib import Path
from typing import Any
from urllib.parse import quote

from PIL import Image

from .assets import resolve_asset
from .constants import CORRECTION_EXPORTS_DIR, CORRECTION_INPUTS_DIR, FIXED_SOURCE_ID, PROJECT_ROOT, ensure_correction_dirs
from .draft_store import list_sessions, load_session, new_session_id, save_session, update_session, storage_root, utc_now, session_batch_id
from ..data_store import ArtifactStore, RecordStore, RevisionConflict
from ..batch_lifecycle import ensure_batch_active, is_batch_active
from .session_state import identified, row_index, reconcile, reviews_for_group, session_fingerprint, fingerprint
from .exporter import export_full_dataset_workbook, export_session_workbook
from .quality_selection import (
    QualitySelectionUnavailable,
    filter_snapshot,
    top1_selection_for_run,
)
from .workbook import load_snapshot, parse_action, has_workbook_json
from ..stage_artifacts import read_workbook_payload, write_payload_workbook
from ..trajectory_data import _format_manual_actions_box
from ..trajectories_tree.tree_builder import parse_bbox


def _session_or_raise(session_id: str) -> dict[str, Any]:
    session = load_session(session_id)
    if session is None:
        raise FileNotFoundError("修正会话不存在")
    return session


def _source_for_session(session: dict[str, Any], *, for_export: bool = False) -> tuple[Path, Path]:
    frozen = session.get("source_snapshot")
    if isinstance(frozen, dict):
        path = _frozen_input_path(session, str(frozen.get("workbook", "")))
        if session.get("workbook_payload") and for_export:
            path = path.with_name("export_input.xlsx")
            data = session["workbook_payload"]
            path.parent.mkdir(parents=True, exist_ok=True)
            write_payload_workbook(path, data)
            path.with_suffix(".json").write_text(json.dumps(data, ensure_ascii=False, default=str), encoding="utf-8")
        if for_export and not session.get("workbook_payload"):
            if frozen.get("workbook_json_sha256"):
                structured = path.with_suffix(".json")
                if not structured.is_file():
                    raise FileNotFoundError("修正完整表格 JSON 快照不存在，请重新创建会话")
                if hashlib.sha256(structured.read_bytes()).hexdigest() != frozen["workbook_json_sha256"]:
                    raise ValueError("修正输入 JSON 快照校验失败")
            else:
                raise ValueError("修正会话缺少完整 JSON 输入校验信息")
        from .assets import registered_asset_root, trajectory_source
        raw_root = frozen.get("raw_root")
        return path, registered_asset_root(str(raw_root), storage_root()) if raw_root else trajectory_source()
    raise ValueError("修正会话缺少冻结 JSON 输入，请重新创建会话")


def _frozen_input_path(session: dict[str, Any], filename: str) -> Path:
    if not filename or Path(filename).name != filename:
        raise ValueError("修正输入快照文件名无效")
    return CORRECTION_INPUTS_DIR / str(session["session_id"]) / filename


def _snapshot(session: dict[str, Any]) -> dict[str, Any]:
    if isinstance(session.get("snapshot_payload"), dict):
        return deepcopy(session["snapshot_payload"])
    frozen = session.get("source_snapshot")
    if isinstance(frozen, dict):
        path = _frozen_input_path(session, str(frozen.get("json", "")))
        if not path.is_file():
            raise FileNotFoundError("修正步骤 JSON 快照不存在，请重新创建会话")
        if hashlib.sha256(path.read_bytes()).hexdigest() != frozen.get("sha256"):
            raise ValueError("修正输入快照校验失败")
        return json.loads(path.read_text(encoding="utf-8"))
    raise ValueError("修正会话缺少冻结 JSON 输入，请重新创建会话")


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
    row["original_action"] = edit.get("original_actions", row.get("actions", ""))
    row["original_actions_box"] = baseline_box
    row["bbox_edited"] = str(row.get("actions_box", "")) != str(row["original_actions_box"])
    row["bbox_source"] = str(edit.get("bbox_source", "manual" if row["bbox_edited"] else "original"))
    row["thought"] = str(row.get("original_thought", ""))
    row["summary_source"] = "original"
    row["thought_source"] = "original"
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
            and (not cot.get("source_fingerprint") or cot.get("bbox_hash") == bbox_hash)
            and (not cot.get("source_fingerprint") or cot["source_fingerprint"] == base.get("source_fingerprint"))
        ):
            for field in ("summary", "thought"):
                if cot.get(field):
                    row[field] = str(cot[field])
                    row[f"{field}_source"] = "generated"
                    row["cot_status"] = "generated"
            row["cot_summary"] = str(cot.get("summary") or "")
            row["cot_action_hash"] = action_hash
            row["cot_bbox_hash"] = bbox_hash
            row["cot_generated_at"] = cot.get("generated_at")
    # Apply each explicit manual value last, including an empty string.
    # Editing one text field must not discard the other generated field.
    for field in ("summary", "thought"):
        if field in edit:
            row[field] = str(edit[field])
            row[f"{field}_source"] = "manual"
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
    reviews = reviews_for_group(session, _snapshot(session), group)
    rows = [_overlay_row(session_id, session, row) for row in group["rows"]]
    active = [row for row in rows if not row["deleted"]]
    return {
        "group_id": group["group_id"],
        "task_id": group.get("task_id", ""),
        "pending_review": bool(reviews) or group.get("task_id") in session.get("stale_tasks", []),
        "pending_review_count": len(reviews),
        "can_adopt_review": bool(reviews) and all(item["can_adopt"] for item in reviews),
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
        "batch_id": session.get("storage_batch_id") or session.get("batch_id") or session.get("tree_run_id"),
        "storage_revision": session.get("storage_revision"),
        "pending_review_count": len(session.get("pending_review", {})),
        "source": _session_source_info(session),
        "tree_run_id": session.get("tree_run_id"),
        "selection": session.get("selection"),
        "created_at": session.get("created_at"),
        "updated_at": session.get("updated_at"),
        "row_count": snapshot["row_count"],
        "group_count": len(groups),
        "groups": groups,
        "exports": session.get("exports", []),
    }


def _session_source_info(session: dict[str, Any]) -> dict[str, Any]:
    workbook, root = _source_for_session(session)
    return {"source_id": FIXED_SOURCE_ID, "name": "当前批次标注表", "kind": "annotated_workbook",
            "relative_path": workbook.relative_to(storage_root()).as_posix(),
            "size_bytes": workbook.stat().st_size if workbook.is_file() else 0, "package_root": root.name}


def sessions() -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for session in list_sessions():
        if session.get("archived") or not is_batch_active(session_batch_id(session), storage_root()):
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
                "batch_id": public["batch_id"],
                "pending_review_count": public["pending_review_count"],
                "stale_task_count": len(session.get("stale_tasks", [])),
                "created_at": public["created_at"],
                "updated_at": public["updated_at"],
                "group_count": public["group_count"],
                "row_count": public["row_count"],
                "export_count": len(public["exports"]),
            }
        )
    return result


def published_tree_run_ids() -> set[str]:
    """Legacy run labels belonging to batches closed by publication."""
    return {str(session.get("tree_run_id")) for session in list_sessions()
            if session.get("tree_run_id") and not is_batch_active(session_batch_id(session), storage_root())}


@contextmanager
def _input_for_batch(identifier: str):
    from .quality_selection import current_selection_input, source_for_tree_run
    store = ArtifactStore(storage_root())
    if store.get(identifier, "04_tree") is not None:
        with current_selection_input(identifier) as value:
            yield value
    else:
        path, assets = source_for_tree_run(identifier)
        yield path, assets, {}


def create_session(tree_run_id: str | None = None, *, batch_id: str | None = None) -> dict[str, Any]:
    identifier = str(batch_id or tree_run_id or "").strip()
    if not identifier:
        raise ValueError("请选择业务批次")
    from .quality_selection import correction_batch_id
    ensure_batch_active(correction_batch_id(identifier), storage_root())
    selection = top1_selection_for_run(identifier)
    selected_batch = str(selection.get("storage_batch_id") or identifier)
    store = ArtifactStore(storage_root())
    with store.batch_lock(selected_batch):
        ensure_batch_active(selected_batch, storage_root())
        ensure_correction_dirs()
        selection = top1_selection_for_run(identifier)
        if str(selection.get("storage_batch_id") or identifier) != selected_batch:
            raise ValueError("批次来源已更新，请重新选择")
        with _input_for_batch(identifier) as (workbook_path, package_root, current_tree):
            full_snapshot = load_snapshot(workbook_path, asset_root=package_root, source_kind="annotated_workbook")
            snapshot = identified(filter_snapshot(full_snapshot, selection))
            raw_payload = read_workbook_payload(workbook_path)
            table_payload = {key: deepcopy(raw_payload[key]) for key in ("schema_version", "columns", "sheets") if key in raw_payload}
            source_key = fingerprint({"table": table_payload, "selection": selection.get("selected_trajectories"),
                                      "tasks": selection.get("task_fingerprints", {})})
            existing = next((item for item in list_sessions() if not item.get("archived")
                and str(item.get("storage_batch_id") or item.get("batch_id") or item.get("tree_run_id")) == selected_batch), None)
            if existing is not None:
                if existing.get("input_fingerprint") == source_key and not existing.get("stale_tasks"):
                    return _public_session(existing, _snapshot(existing))
                old_snapshot = _snapshot(existing)
                reconcile(existing, old_snapshot, snapshot, selection.get("task_fingerprints", {}))
                session = existing
            else:
                session_id = new_session_id()
                session = {"session_id": session_id, "source_id": FIXED_SOURCE_ID,
                    "source_kind": "annotated_workbook", "created_at": utc_now(),
                    "row_edits": {}, "cot": {}, "group_exports": {}, "exports": []}
                reconcile(session, {"groups": []}, snapshot, selection.get("task_fingerprints", {}))
            session.update(batch_id=selected_batch, storage_batch_id=selected_batch,
                tree_run_id=str(selection.get("tree_run_id") or identifier), selection=selection,
                workbook_payload=table_payload, input_fingerprint=source_key, published=False)
            input_dir = CORRECTION_INPUTS_DIR / session["session_id"]
            input_dir.mkdir(parents=True, exist_ok=True)
            # The database owns the current draft; these files only serve Excel export.
            path = input_dir / "source.xlsx"
            write_payload_workbook(path, table_payload)
            table_bytes = json.dumps(table_payload, ensure_ascii=False, indent=2, default=str).encode()
            (input_dir / "source.json").write_bytes(table_bytes)
            snapshot_bytes = json.dumps(session["snapshot_payload"], ensure_ascii=False, indent=2).encode()
            (input_dir / "snapshot.json").write_bytes(snapshot_bytes)
            session["source_snapshot"] = {"workbook": "source.xlsx", "json": "snapshot.json",
                "raw_root": str(package_root.resolve()), "sha256": hashlib.sha256(snapshot_bytes).hexdigest(),
                "workbook_json_sha256": hashlib.sha256(table_bytes).hexdigest(),
                "workbook_sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
            save_session(session)
            return _public_session(session, _snapshot(session))


def _check_revision(session: dict, expected_revision: int | None) -> None:
    if expected_revision is not None and session.get("storage_revision") != expected_revision:
        raise RevisionConflict("批次已更新，请刷新后重试")


def on_batch_tasks_invalidated(batch_id: str, task_ids: list[str], root: Path | None = None) -> None:
    """Keep manual work for review and invalidate only the changed tasks."""
    data_root = Path(root or storage_root())
    records = RecordStore(data_root)
    store = ArtifactStore(data_root)
    affected = set(task_ids)
    with store.batch_lock(batch_id):
        ensure_batch_active(batch_id, data_root)
        for session in records.list("correction_sessions"):
            if session.get("archived") or str(session.get("storage_batch_id") or session.get("batch_id") or session.get("tree_run_id")) != batch_id:
                continue
            snapshot = session.get("snapshot_payload")
            if not snapshot:
                input_path = data_root / "system" / "trajectory_correction" / "inputs" / session["session_id"] / str(session.get("source_snapshot", {}).get("json", "snapshot.json"))
                if input_path.is_file():
                    snapshot = json.loads(input_path.read_text(encoding="utf-8"))
                elif data_root.resolve() == storage_root().resolve():
                    snapshot = _snapshot(session)
                else:
                    raise ValueError("无法读取待失效修正草稿的来源，请先迁移")
            snapshot = identified(snapshot)
            impacted = affected & {group["task_id"] for group in snapshot["groups"]}
            if not impacted:
                continue
            pending = session.setdefault("pending_review", {})
            for row in row_index(snapshot).values():
                if row["task_id"] not in affected:
                    continue
                edit = session.setdefault("row_edits", {}).pop(str(row["excel_row"]), None)
                session.setdefault("cot", {}).pop(str(row["excel_row"]), None)
                if edit:
                    pending[row["step_key"]] = {"step_key": row["step_key"], "task_id": row["task_id"],
                        "step": row.get("step"), "image": row.get("image", ""), "baseline": row,
                        "changes": edit, "reason": "来源已更新，人工修改待复核"}
            session["snapshot_payload"] = snapshot
            session["stale_tasks"] = sorted(set(session.get("stale_tasks", [])) | impacted)
            session["updated_at"] = utc_now()
            records.put("correction_sessions", session["session_id"], session, expected_revision=session.get("storage_revision"))
        for stage in ("06_correction", "07_cot"):
            ref = store.get(batch_id, stage)
            if ref is None:
                continue
            payload = store.read_payload(ref)
            groups = payload.get("groups", [])
            removed_rows = {str(row["excel_row"]) for group in groups
                if str(group.get("task_id") or (group.get("rows") or [{}])[0].get("task_id", "")) in affected for row in group.get("rows", [])}
            payload["groups"] = [group for group in groups if str(group.get("task_id") or (group.get("rows") or [{}])[0].get("task_id", "")) not in affected]
            for key in ("row_edits", "cot"):
                payload[key] = {row: val for row, val in payload.get(key, {}).items() if row not in removed_rows}
            if removed_rows:
                payload["stale_tasks"] = sorted(set(payload.get("stale_tasks", [])) | affected)
            refs = [item for item in (store.get(batch_id, "04_tree"), store.get(batch_id, "05_quality")) if item]
            workbooks = {item["name"]: store.resolve_file(ref, item["name"]) for item in ref.get("files", [])
                         if item["name"].endswith(".xlsx")} if not removed_rows else {}
            new_ref = store.publish(batch_id, stage, payload, source_refs=refs, workbooks=workbooks,
                          metadata={**ref.get("metadata", {}), **({"invalidated_tasks": sorted(affected)} if removed_rows else {})})
            if not removed_rows:
                # Only the lineage changed; existing valid exports retain their bytes.
                for current in records.list("correction_sessions"):
                    if current.get("archived") or str(current.get("storage_batch_id") or current.get("tree_run_id")) != batch_id:
                        continue
                    changed = False
                    for export in current.get("exports", []):
                        if export.get("artifact", {}).get("stage") == stage and export["artifact"].get("version") == ref["version"]:
                            export["artifact"] = new_ref
                            changed = True
                    if changed:
                        records.put("correction_sessions", current["session_id"], current, expected_revision=current.get("storage_revision"))


def review_group(session_id: str, group_id: str, decision: str, expected_revision: int | None = None) -> dict:
    session = _session_or_raise(session_id)
    snapshot = _snapshot(session)
    group = next((item for item in snapshot["groups"] if item["group_id"] == group_id or item.get("legacy_group_id") == group_id), None)
    if group is None:
        raise KeyError(group_id)
    def apply(current):
        _check_revision(current, expected_revision)
        reviews = reviews_for_group(current, snapshot, group)
        if decision not in {"adopt", "discard"}:
            raise ValueError("复核决定必须为 adopt 或 discard")
        if decision == "adopt" and any(not item["can_adopt"] for item in reviews):
            raise ValueError("原步骤已不属于当前轨迹，不能将旧编辑套用到其他步骤")
        rows = row_index(snapshot)
        for item in reviews:
            if decision == "adopt":
                row = rows[item["step_key"]]
                changes = deepcopy(item["changes"])
                if "actions" in changes:
                    changes["original_actions"] = row.get("actions", "")
                current.setdefault("row_edits", {})[str(row["excel_row"])] = {**changes, **current.get("row_edits", {}).get(str(row["excel_row"]), {})}
                current.setdefault("cot", {}).pop(str(row["excel_row"]), None)
            current.setdefault("pending_review", {}).pop(item["step_key"], None)
    session = update_session(session_id, apply)
    return {"session": _public_session(session, snapshot), "group": _group_summary(session_id, session, group),
            "storage_revision": session.get("storage_revision")}


def get_session(session_id: str) -> dict[str, Any]:
    session = _session_or_raise(session_id)
    batch_id = str(session.get("storage_batch_id") or session.get("batch_id") or session.get("tree_run_id"))
    if ArtifactStore(storage_root()).get(batch_id, "04_tree") is not None:
        try:
            return create_session(batch_id=batch_id)
        except QualitySelectionUnavailable:
            pass  # Keep old edits visible while the changed task is reprocessed.
    return _public_session(session, _snapshot(session))


def get_groups(session_id: str) -> list[dict[str, Any]]:
    session = _session_or_raise(session_id)
    snapshot = _snapshot(session)
    return [_group_summary(session_id, session, group) for group in snapshot["groups"]]


def get_group(session_id: str, group_id: str) -> dict[str, Any]:
    session = _session_or_raise(session_id)
    snapshot = _snapshot(session)
    group = next((item for item in snapshot["groups"] if item["group_id"] == group_id or item.get("legacy_group_id") == group_id), None)
    if group is None:
        raise KeyError(group_id)
    return {
        **_group_summary(session_id, session, group),
        "rows": [_overlay_row(session_id, session, row) for row in group["rows"]],
        "pending_reviews": reviews_for_group(session, snapshot, group),
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
            "task_id": group.get("task_id", ""),
            "pending_review": _group_summary(session_id, session, group)["pending_review"],
            "pending_review_count": _group_summary(session_id, session, group)["pending_review_count"],
            "task": group["task"],
            "rows": [
                {
                    "task_id": str(row.get("task_id") or group.get("task_id") or str(row.get("image", "")).replace("\\", "/").split("/", 1)[0]),
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
    return {"session_id": session_id, "batch_id": session.get("storage_batch_id") or session.get("tree_run_id"), "storage_revision": session.get("storage_revision"), "pending_review_count": len(session.get("pending_review", {})), "groups": groups}


def patch_row(session_id: str, excel_row: int, payload: dict[str, Any]) -> dict[str, Any]:
    session = _session_or_raise(session_id)
    snapshot = _snapshot(session)
    group, base = _base_row(snapshot, excel_row)
    expected_revision = payload.pop("expected_revision", None)
    def apply(session: dict[str, Any]) -> None:
        _check_revision(session, expected_revision)
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
            # Saving the original text (or clearing it) is still an explicit
            # choice and must take precedence over any generated value.
            edit[field] = value

        if payload.get("deleted") is not None:
            if bool(payload["deleted"]):
                edit["deleted"] = True
            else:
                edit.pop("deleted", None)

        if not edit:
            session.setdefault("row_edits", {}).pop(str(excel_row), None)
    session = update_session(session_id, apply)
    return {
        "group": _group_summary(session_id, session, group),
        "row": _overlay_row(session_id, session, base),
        "storage_revision": session.get("storage_revision"),
    }


def patch_group_export(session_id: str, group_id: str, export: bool, expected_revision: int | None = None) -> dict[str, Any]:
    session = _session_or_raise(session_id)
    snapshot = _snapshot(session)
    group = next((item for item in snapshot["groups"] if item["group_id"] == group_id or item.get("legacy_group_id") == group_id), None)
    if group is None:
        raise KeyError(group_id)
    def apply(current):
        _check_revision(current, expected_revision)
        if export and (_group_summary(session_id, current, group)["pending_review"]):
            raise ValueError("待复核任务不能加入有效导出")
        current.setdefault("group_exports", {})[group["group_id"]] = bool(export)
    session = update_session(session_id, apply)
    return {**_group_summary(session_id, session, group), "storage_revision": session.get("storage_revision")}


def publish_stage_snapshot(session: dict[str, Any], snapshot: dict[str, Any], stage: str, *, workbook: Path | None = None) -> dict[str, Any]:
    """Publish the batch's valid steps, including deleted/unexported rows."""
    batch_id = session_batch_id(session)
    with ArtifactStore(storage_root()).batch_lock(batch_id):
        ensure_batch_active(batch_id, storage_root())
        return _publish_stage_snapshot(session, snapshot, stage, workbook=workbook)


def _publish_stage_snapshot(session: dict[str, Any], snapshot: dict[str, Any], stage: str, *, workbook: Path | None = None) -> dict[str, Any]:
    session, snapshot = _export_view(session, snapshot)
    groups = []
    table = []
    for group in snapshot["groups"]:
        rows = [_overlay_row(session["session_id"], session, row) for row in group["rows"]]
        groups.append({**_group_summary(session["session_id"], session, group), "rows": rows})
        for row in rows:
            table.append({"任务": group["task"], "轨迹": group["meta_task"], "源行号": row["excel_row"],
                          "步骤": row["step"], **row.get("values", {}), "action": row["actions"],
                          "summary": row.get("summary", ""), "thought": row.get("thought", ""),
                          "actions_box": row.get("actions_box", ""), "sop": row.get("sop", ""),
                          "deleted": row["deleted"], "group_export": groups[-1]["export"],
                          "summary_source": row["summary_source"], "thought_source": row["thought_source"],
                          "original_action": row.get("original_action", ""), "original_summary": row.get("original_summary", ""),
                          "original_thought": row.get("original_thought", ""), "original_actions_box": row.get("original_actions_box", ""),
                          "人工修改": session.get("row_edits", {}).get(str(row["excel_row"]), {}),
                          "COT元数据": session.get("cot", {}).get(str(row["excel_row"]), {})})
    payload = {"schema_version": 2, "session_id": session["session_id"], "tree_run_id": session.get("tree_run_id"),
               "batch_id": session.get("storage_batch_id") or session.get("tree_run_id") or session["session_id"],
               "storage_revision": session.get("storage_revision"), "selection": session.get("selection"),
               "groups": groups, "row_edits": session.get("row_edits", {}), "cot": session.get("cot", {}),
               "task_fingerprints": session.get("task_fingerprints", {}), "content_fingerprint": session.get("export_fingerprint") or session_fingerprint(session)}
    store = ArtifactStore(storage_root())
    batch_id = str(session.get("storage_batch_id") or session.get("tree_run_id") or session["session_id"])
    lineage = [ref for ref in (store.get(batch_id, "04_tree"), store.get(batch_id, "05_quality")) if ref]
    return store.publish(
        str(session.get("storage_batch_id") or session.get("tree_run_id") or session["session_id"]), stage, payload,
        tables={"完整步骤": table}, workbooks={"full_dataset.xlsx": workbook} if workbook else None,
        source_refs=[{"kind": "correction_session", "id": session["session_id"], "revision": session.get("storage_revision")},
                     {"kind": "trajectory_tree", "id": session.get("tree_run_id")}] + lineage,
        metadata={"session_id": session["session_id"], "row_count": len(table)})


@contextmanager
def active_session_lock(session_id: str):
    session = _session_or_raise(session_id)
    batch_id = session_batch_id(session)
    with ArtifactStore(storage_root()).batch_lock(batch_id):
        ensure_batch_active(batch_id, storage_root())
        yield _session_or_raise(session_id)


def _locked_session(function):
    @wraps(function)
    def locked(session_id, *args, **kwargs):
        with active_session_lock(session_id):
            return function(session_id, *args, **kwargs)
    return locked


def _export_view(session: dict, snapshot: dict) -> tuple[dict, dict]:
    """Exclude stale tasks, retaining a whole source-shaped table for ready tasks."""
    value = deepcopy(session)
    value["export_fingerprint"] = session.get("export_fingerprint") or session_fingerprint(session)
    invalid = set(session.get("stale_tasks", [])) | {
        item["task_id"] for item in session.get("pending_review", {}).values()}
    identified_snapshot = identified(snapshot)
    valid = {group["task_id"] for group in identified_snapshot["groups"] if group["task_id"] not in invalid}
    if not valid:
        raise ValueError("当前没有已完成复核的任务可导出")
    groups = [deepcopy(group) for group in identified_snapshot["groups"] if group["task_id"] in valid]
    table = value.get("workbook_payload")
    mapping = {}
    if table:
        from ..trajectory_context import row_task_id
        table = deepcopy(table)
        active = next(iter(table.get("sheets", {})), None)
        for name, rows in table.get("sheets", {}).items():
            kept = []
            for old_number, row in enumerate(rows, 2):
                if row_task_id(row) not in valid:
                    continue
                if name == active:
                    mapping[str(old_number)] = str(len(kept) + 2)
                kept.append(row)
            table["sheets"][name] = kept
        value["workbook_payload"] = table
    else:
        mapping = {str(row["excel_row"]): str(row["excel_row"]) for group in groups for row in group["rows"]}
    for group in groups:
        for row in group["rows"]:
            if str(row["excel_row"]) not in mapping:
                raise ValueError("有效修正步骤无法关联完整数据表")
            row["excel_row"] = int(mapping[str(row["excel_row"])])
    for field in ("row_edits", "cot", "bbox_baselines"):
        value[field] = {mapping[row]: deepcopy(edit) for row, edit in session.get(field, {}).items() if row in mapping}
    # Source group choices can use legacy or stable IDs; carry them by task.
    old_groups = {str(group.get("task_id") or identified({"groups": [group]})["groups"][0]["task_id"]): group for group in snapshot["groups"]}
    value["group_exports"] = {group["group_id"]: bool(session.get("group_exports", {}).get(
        old_groups[group["task_id"]]["group_id"], old_groups[group["task_id"]].get("export", False))) for group in groups}
    value["pending_review"] = {}
    value["stale_tasks"] = []
    value["task_fingerprints"] = {task: item for task, item in session.get("task_fingerprints", {}).items() if task in valid}
    value["selection"] = {**session.get("selection", {}), "tasks": [item for item in session.get("selection", {}).get("tasks", []) if item.get("task_id") in valid]}
    frozen = {**snapshot, "groups": groups, "row_count": sum(len(group["rows"]) for group in groups)}
    value["snapshot_payload"] = frozen
    return value, frozen


def _existing_export(session: dict, kind: str) -> dict | None:
    current_key = session_fingerprint(session)
    store = ArtifactStore(storage_root())
    for record in session.get("exports", []):
        if str(record.get("kind") or "selected") != kind or record.get("content_fingerprint") != current_key:
            continue
        ref = record.get("artifact") or {}
        if not all(ref.get(key) for key in ("batch_id", "stage", "version")) or store.get(ref["batch_id"], ref["stage"], ref["version"]) is None:
            continue
        pending_refs = [ref]
        visited = set()
        valid_sources = True
        while pending_refs:
            source = pending_refs.pop()
            key = tuple(source.get(name) for name in ("batch_id", "stage", "version"))
            if not all(key) or key in visited:
                continue
            visited.add(key)
            current_source = store.get(*key)
            if current_source is None:
                valid_sources = False
                break
            pending_refs.extend(current_source.get("source_refs", []))
        if not valid_sources:
            continue
        filename = str(record.get("filename", ""))
        if Path(filename).name != filename:
            continue
        path = CORRECTION_EXPORTS_DIR / session["session_id"] / filename
        if path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == record.get("sha256"):
            return {**record, "storage_revision": session.get("storage_revision"), "reused": True}
    return None


def _remember_export(session_id: str, history: dict) -> dict:
    replaced = []
    kind = history.get("kind", "selected")
    def apply(current):
        replaced.extend(item for item in current.get("exports", []) if str(item.get("kind") or "selected") == kind)
        current["exports"] = [history] + [item for item in current.get("exports", []) if str(item.get("kind") or "selected") != kind]
    session = update_session(session_id, apply)
    directory = (CORRECTION_EXPORTS_DIR / session_id).resolve()
    for item in replaced:
        filename = str(item.get("filename", ""))
        candidate = (directory / filename).resolve()
        if filename != history["filename"] and Path(filename).name == filename and candidate.parent == directory:
            candidate.unlink(missing_ok=True)
    return session


@_locked_session
def publish_cot_snapshot(session_id: str, expected_revision: int | None = None) -> dict[str, Any]:
    session = _session_or_raise(session_id)
    _check_revision(session, expected_revision)
    session, snapshot = _export_view(session, _snapshot(session))
    workbook_path, _ = _source_for_session(session, for_export=True)
    output_dir = CORRECTION_EXPORTS_DIR / session_id
    output_dir.mkdir(parents=True, exist_ok=True)
    result = export_full_dataset_workbook(workbook_path=workbook_path, snapshot=snapshot, session=session,
                                          output_dir=output_dir, export_id=uuid.uuid4().hex[:16])
    generated = output_dir / result["filename"]
    try:
        return publish_stage_snapshot(session, snapshot, "07_cot", workbook=generated)
    finally:
        generated.unlink(missing_ok=True)


@_locked_session
def export_session(session_id: str, expected_revision: int | None = None) -> dict[str, Any]:
    session = _session_or_raise(session_id)
    _check_revision(session, expected_revision)
    existing = _existing_export(session, "selected")
    if existing is not None:
        return existing
    session, snapshot = _export_view(session, _snapshot(session))
    workbook_path, _ = _source_for_session(session, for_export=True)
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
        "kind": "selected",
        "summary": result["summary"],
        "content_fingerprint": session["export_fingerprint"],
        "selection": session.get("selection"),
        "filename": result["filename"],
        "sha256": hashlib.sha256((output_dir / result["filename"]).read_bytes()).hexdigest(),
        "created_at": utc_now(),
        "download_url": f"/api/correction/sessions/{session_id}/exports/{quote(result['filename'], safe='')}" ,
        "sheets": result["sheets"],
    }
    history["artifact"] = publish_stage_snapshot(session, snapshot, "06_correction")
    _remember_export(session_id, history)
    return {**history, "summary": result["summary"], "storage_revision": load_session(session_id).get("storage_revision")}


@_locked_session
def export_dataset_session(session_id: str, expected_revision: int | None = None) -> dict[str, Any]:
    """Publish a full source-shaped workbook with all persisted corrections overlaid."""
    session = _session_or_raise(session_id)
    _check_revision(session, expected_revision)
    existing = _existing_export(session, "full_dataset")
    if existing is not None:
        return existing
    session, snapshot = _export_view(session, _snapshot(session))
    workbook_path, _ = _source_for_session(session, for_export=True)
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
        "summary": result["summary"],
        "content_fingerprint": session["export_fingerprint"],
        "selection": session.get("selection"),
        "kind": "full_dataset",
        "filename": result["filename"],
        "sha256": hashlib.sha256((output_dir / result["filename"]).read_bytes()).hexdigest(),
        "created_at": utc_now(),
        "download_url": (
            f"/api/correction/sessions/{session_id}/exports/"
            f"{quote(result['filename'], safe='')}"
        ),
        "sheets": result["sheets"],
    }
    publish_stage_snapshot(session, snapshot, "06_correction")
    history["artifact"] = publish_stage_snapshot(session, snapshot, "07_cot", workbook=output_dir / result["filename"])
    _remember_export(session_id, history)
    return {**history, "summary": result["summary"], "storage_revision": load_session(session_id).get("storage_revision")}


def download_export(session_id: str, filename: str) -> Path:
    session = _session_or_raise(session_id)
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
    record = next((item for item in session.get("exports", []) if item.get("filename") == filename), None)
    if record is None or not record.get("sha256") or hashlib.sha256(path.read_bytes()).hexdigest() != record["sha256"]:
        raise ValueError("导出文件与已保存版本的 SHA256 不一致，请重新导出")
    return path


def session_asset(session_id: str, image_value: str) -> Path:
    session = _session_or_raise(session_id)
    _, package_root = _source_for_session(session)
    return resolve_asset(package_root, image_value)

# Upstream invalidation and interactive writes share the same batch lock.
patch_row = _locked_session(patch_row)
patch_group_export = _locked_session(patch_group_export)
review_group = _locked_session(review_group)

# These are workbench entry points; historical downloads/assets remain read-only.
get_session = _locked_session(get_session)
get_groups = _locked_session(get_groups)
get_group = _locked_session(get_group)
get_cot = _locked_session(get_cot)
