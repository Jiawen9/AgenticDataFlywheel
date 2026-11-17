"""Pure display projections; never authorize editing or publication from these."""
from __future__ import annotations

import hashlib
import json


def mapping(value):
    return value if isinstance(value, dict) else {}


def records(value):
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def count(value):
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError, OverflowError):
        return 0


def project_session(payload: dict) -> dict:
    snapshot = mapping(payload.get("snapshot_payload"))
    groups = records(snapshot.get("groups"))
    selection = mapping(payload.get("selection"))
    tasks = records(selection.get("tasks"))
    exports = records(payload.get("exports"))
    latest = max((item for item in exports if item.get("kind") in {"full_dataset", "corrected_selection"}),
                 key=lambda item: str(item.get("created_at", "")), default={})
    tasks = records(mapping(latest.get("selection") or selection).get("tasks"))
    sheets = mapping(latest.get("sheets"))
    value = {key: payload.get(key) for key in ("session_id", "source_id", "tree_run_id", "created_at", "updated_at", "archived", "published")}
    value.update(batch_id=payload.get("storage_batch_id") or payload.get("batch_id") or payload.get("tree_run_id"),
        pending_review_count=len(mapping(payload.get("pending_review"))), stale_task_count=len(payload.get("stale_tasks") or []) if isinstance(payload.get("stale_tasks"), list) else 0,
        group_count=len(groups) if groups else count(selection.get("selected_trajectory_count") or len(tasks)),
        row_count=count(snapshot.get("row_count")), export_count=len(exports),
        task_count=len({str(item.get("task_id") or index) for index, item in enumerate(tasks)}), trajectory_count=sum(count(item.get("trajectory_count") or 1) for item in tasks),
        step_count=sum(count(number) for number in sheets.values()),
        latest_excel={"filename": latest.get("filename", ""), "created_at": latest.get("created_at"), "rows": sum(count(number) for number in sheets.values()), "path": ""},
        ready=bool(latest), reason="" if latest else "尚未导出所选完整轨迹或完整数据集 Excel",
        readiness="cached", status="published" if payload.get("published") else "ready")
    # Hashing the authoritative draft remains part of create/export, not list reads.
    # A positive cached hint never bypasses the existing publication checks.
    if value["pending_review_count"] or value["stale_task_count"]:
        value.update(ready=False, reason="来源或人工修改待复核")
    pending_tasks = (set(payload.get("stale_tasks") or []) if isinstance(payload.get("stale_tasks"), list) else set()) | {item.get("task_id") for item in mapping(payload.get("pending_review")).values() if isinstance(item, dict)}
    edits, cot, baselines = mapping(payload.get("row_edits")), mapping(payload.get("cot")), mapping(payload.get("bbox_baselines"))
    edited = generated = eligible = 0
    for group in groups:
        for row in records(group.get("rows")):
            number = str(row.get("excel_row"))
            edit = mapping(edits.get(number))
            needed = "actions" in edit or str(edit.get("actions_box", row.get("actions_box", ""))) != str(baselines.get(number, row.get("actions_box", "")))
            if needed and not edit.get("deleted"):
                edited += 1
                current = mapping(cot.get(number))
                action = row.get("action")
                try:
                    if "actions" in edit:
                        action = json.loads(edit["actions"])
                    action_hash = hashlib.sha256(json.dumps(action, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
                    bbox_hash = hashlib.sha256(str(edit.get("actions_box", row.get("actions_box", ""))).encode()).hexdigest()
                    valid = (current.get("content_tag") == "thought_summary" and current.get("action_hash") == action_hash
                             and (not current.get("source_fingerprint") or (current.get("bbox_hash") == bbox_hash and current.get("source_fingerprint") == row.get("source_fingerprint"))))
                    generated += int(valid and bool(current.get("thought") or current.get("summary")) and "summary" not in edit and "thought" not in edit)
                except (ValueError, TypeError):
                    pass
                eligible += int(group.get("task_id") not in pending_tasks)
    value.update(cot_row_count=edited, cot_generated_count=generated, eligible_cot_row_count=eligible)
    return value


def page_values(items: list, *, offset=0, limit=10, total=None, revision=None) -> dict:
    """Envelope for already scoped detail metadata, without any row bodies."""
    count = len(items) if total is None else total
    return {"items": items[offset:offset + limit], "latest": [], "total": count,
        "filtered_total": count, "scope_counts": {"total": count, "active": 0, "abnormal": 0},
        "offset": offset, "limit": limit, "has_more": offset + limit < count,
        "index_state": "ready", "counts_complete": True, "storage_revision": revision}
