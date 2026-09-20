"""Stable step identities and lossless reconciliation of a batch's one draft."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json

from .quality_selection import _group_task_id


def fingerprint(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), default=str).encode()).hexdigest()


def identified(snapshot: dict) -> dict:
    result = deepcopy(snapshot)
    seen = set()
    for group in result.get("groups", []):
        task_id = _group_task_id(group) or str(group.get("task", ""))
        group["task_id"] = task_id
        group.setdefault("legacy_group_id", group.get("group_id"))
        group["group_id"] = "group_" + fingerprint([task_id, str(group.get("meta_task", ""))])[:20]
        for row in group.get("rows", []):
            image = str(row.get("image", "")).replace("\\", "/").strip("/")
            row["task_id"] = task_id
            row["step_key"] = fingerprint([task_id, str(group.get("meta_task", "")), image or str(row.get("step", ""))])
            if row["step_key"] in seen:
                raise ValueError("修正输入包含重复步骤身份")
            seen.add(row["step_key"])
            # Ignore positional and derived UI fields when comparing inputs.
            base = {key: value for key, value in row.items() if key not in
                    {"excel_row", "step_key", "source_fingerprint", "image_url"}}
            row["source_fingerprint"] = fingerprint(base)
    result["row_count"] = sum(len(group.get("rows", [])) for group in result.get("groups", []))
    return result


def row_index(snapshot: dict) -> dict[str, dict]:
    return {row["step_key"]: row for group in identified(snapshot).get("groups", []) for row in group.get("rows", [])}


def reconcile(session: dict, old_snapshot: dict, new_snapshot: dict, task_fingerprints: dict | None = None) -> dict:
    """Remap only identical identities; changed manual inputs wait for review."""
    snapshot = identified(new_snapshot)
    ready_tasks = {group["task_id"] for group in snapshot.get("groups", [])}
    remaining_stale = set(session.get("stale_tasks", [])) - ready_tasks
    missing_groups = [deepcopy(group) for group in identified(old_snapshot).get("groups", [])
                      if group["task_id"] in remaining_stale]
    for group in missing_groups:
        group["stale"] = True
        for row in group.get("rows", []):
            row["excel_row"] = -abs(int(row["excel_row"]))
    snapshot["groups"].extend(missing_groups)
    snapshot["row_count"] = sum(len(group.get("rows", [])) for group in snapshot["groups"])
    old_rows, new_rows = row_index(old_snapshot), row_index(snapshot)
    pending = deepcopy(session.get("pending_review", {}))
    edits, cot, baselines = {}, {}, {}
    stale = set(session.get("stale_tasks", []))
    previous_fingerprints = session.get("task_fingerprints", {})
    next_fingerprints = task_fingerprints or {}
    for key, old in old_rows.items():
        old_number = str(old["excel_row"])
        edit = session.get("row_edits", {}).get(old_number, {})
        generated = session.get("cot", {}).get(old_number, {})
        new = new_rows.get(key)
        same = new is not None and old["source_fingerprint"] == new["source_fingerprint"]
        task_id = old["task_id"]
        same_task = (task_id not in stale and (not previous_fingerprints.get(task_id)
            or not next_fingerprints.get(task_id)
            or previous_fingerprints[task_id] == next_fingerprints[task_id]))
        if same and same_task:
            if edit:
                edits[str(new["excel_row"])] = deepcopy(edit)
            if generated:
                cot[str(new["excel_row"])] = deepcopy(generated)
        elif edit:
            pending[key] = {"step_key": key, "task_id": task_id,
                "step": old.get("step"), "image": old.get("image", ""),
                "baseline": deepcopy(old), "changes": deepcopy(edit),
                "reason": "来源已更新，人工修改待复核"}
    for key, row in new_rows.items():
        baselines[str(row["excel_row"])] = row.get("actions_box", "")
    old_groups = {_group_task_id(group): group for group in old_snapshot.get("groups", [])}
    exports = {}
    for group in snapshot.get("groups", []):
        old_group = old_groups.get(group["task_id"], {})
        exports[group["group_id"]] = bool(session.get("group_exports", {}).get(
            old_group.get("group_id"), old_group.get("export", group.get("export", False))))
    session.update(snapshot_payload=snapshot, row_edits=edits, cot=cot,
                   bbox_baselines=baselines, pending_review=pending, group_exports=exports,
                   task_fingerprints=next_fingerprints, stale_tasks=sorted(remaining_stale))
    return session


def reviews_for_group(session: dict, snapshot: dict, group: dict) -> list[dict]:
    current = row_index(snapshot)
    task_id = _group_task_id(group)
    return [{**deepcopy(item), "can_adopt": key in current and item.get("task_id") not in session.get("stale_tasks", [])}
            for key, item in session.get("pending_review", {}).items()
            if str(item.get("task_id", "")) == task_id]


def session_fingerprint(session: dict) -> str:
    return fingerprint({key: session.get(key) for key in (
        "snapshot_payload", "source_snapshot", "task_fingerprints", "row_edits", "cot",
        "group_exports", "pending_review", "stale_tasks")})


def migrate_session_identity(session: dict, snapshot: dict, *, batch_id: str | None = None,
                             table_payload: dict | None = None, task_fingerprints: dict | None = None) -> dict:
    """Pure migration: preserve every edit/COT while attaching stable identities."""
    result = deepcopy(session)
    before = deepcopy(snapshot)
    current = identified(snapshot)
    batch_id = str(batch_id or session.get("storage_batch_id") or session.get("tree_run_id"))
    result.update(batch_id=batch_id, storage_batch_id=batch_id, tree_run_id=batch_id,
                  snapshot_payload=current, task_fingerprints=deepcopy(task_fingerprints or {}),
                  pending_review=deepcopy(session.get("pending_review", {})), stale_tasks=[],
                  published=bool(session.get("published") or session.get("published_release_id")))
    choices = session.get("group_exports", {})
    result["group_exports"] = {new["group_id"]: bool(choices.get(old["group_id"], old.get("export", False)))
                               for old, new in zip(before.get("groups", []), current.get("groups", []))}
    selection = deepcopy(session.get("selection") or {})
    selection.update(tree_run_id=batch_id, run_id=batch_id, storage_batch_id=batch_id,
                     task_fingerprints=deepcopy(task_fingerprints or {}))
    selection["selected_trajectories"] = {group["task_id"]: group["meta_task"] for group in current["groups"]}
    result["selection"] = selection
    if table_payload is not None:
        table = {key: deepcopy(table_payload[key]) for key in ("schema_version", "columns", "sheets") if key in table_payload}
        result["workbook_payload"] = table
        result["input_fingerprint"] = fingerprint({"table": table, "selection": selection["selected_trajectories"],
                                                   "tasks": result["task_fingerprints"]})
    if session.get("published") or session.get("published_release_id"):
        result["published_content_fingerprint"] = session_fingerprint(result)
    # Existing export bytes stay unchanged and may already be release-owned.
    for export in result.get("exports", []):
        export.setdefault("content_fingerprint", session_fingerprint(result))
    return result
