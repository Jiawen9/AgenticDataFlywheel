"""Read the task's scored tree as navigation for a correction draft."""

from collections import Counter, defaultdict

from ..batch_results import current_tree_and_quality
from ..export_markers import (
    compatible_node_scores,
    effective_refinement,
    original_branch_markers,
    step_image_identity,
)
from .draft_store import session_batch_id, storage_root
from .service import _check_revision, _snapshot, active_session_lock


def _targets(tree, groups, row_edits):
    rows = defaultdict(list)
    for group in groups:
        trajectory_id = str(group.get("meta_task") or "")
        for row in group.get("rows", []):
            if row.get("step_key") and int(row["excel_row"]) > 0:
                rows[trajectory_id, *step_image_identity(row)].append((group, row))

    occurrences = []
    stack = list(reversed(tree.get("children", [])))
    while stack:
        node = stack.pop()
        for occurrence in node.get("occurrences", []):
            trajectory_id = str(occurrence.get("trajectory") or occurrence.get("trajectory_id") or "")
            occurrences.append((str(node["id"]), (trajectory_id, *step_image_identity(occurrence))))
        stack.extend(reversed(node.get("children", [])))

    counts = Counter(identity for _, identity in occurrences)
    targets = defaultdict(list)
    for node_id, identity in occurrences:
        candidates = rows.get(identity, [])
        if counts[identity] != 1 or len(candidates) != 1:
            continue
        group, row = candidates[0]
        edit = row_edits.get(str(row["excel_row"]), {})
        deleted = bool(edit.get("deleted"))
        targets[node_id].append({
            "group_id": group["group_id"], "trajectory_id": identity[0],
            "step": row["step"], "step_key": row["step_key"], "excel_row": row["excel_row"],
            "edited": not deleted and effective_refinement(row, edit), "deleted": deleted,
        })
    return dict(targets)


def _project_task_tree(session, task_id, groups, tree_payload, quality_payload, tree_version):
    result = {
        "session_id": session["session_id"], "task_id": task_id, "storage_revision": session.get("storage_revision"),
        "tree": None, "reason": None, "node_scores": {}, "tree_version": None, "tree_hash": None,
        "quality_version": None, "scoring_version": None, "targets": {}, "key_node_ids": [],
    }
    value = tree_payload.get("trees", {}).get(task_id) or {}
    tree = value.get("tree", value)
    if not tree:
        return {**result, "reason": "该任务暂无可用轨迹树，可使用步骤列表"}
    fingerprint = session.get("task_fingerprints", {}).get(task_id)
    tree_hash = tree_payload.get("tree_hashes", {}).get(task_id)
    selected_hash = (session.get("selection") or {}).get("tree_hashes", {}).get(task_id)
    if (task_id in session.get("stale_tasks", []) or not fingerprint or not tree_hash
            or fingerprint != tree_payload.get("task_fingerprints", {}).get(task_id)
            or (selected_hash and selected_hash != tree_hash)):
        return {**result, "reason": "修正会话与当前树的来源版本无法对应，可使用步骤列表"}

    quality = next((item for item in quality_payload.get("tasks", []) if str(item.get("task_id")) == task_id), {})
    scores = compatible_node_scores(task_id, tree_payload, quality_payload, quality)
    markers = original_branch_markers(tree, scores)
    targets = _targets(tree, groups, session.get("row_edits", {}))
    if not targets:
        return {**result, "reason": "树上没有能确认身份的修正步骤，可使用步骤列表"}
    # The tree renderer needs the nodes, while the unreduced source audit is
    # loaded from the existing correction groups when a trajectory is opened.
    canvas_tree = {key: part for key, part in tree.items() if key not in {"source_trajectories", "decisions"}}
    quality_ref = quality_payload.get("artifact") or {}
    quality_version = quality.get("quality_job_id") if quality.get("result_state") == "partial" else quality_ref.get("version")
    return {**result, "tree": canvas_tree, "node_scores": scores, "tree_version": tree_version,
            "tree_hash": tree_hash, "quality_version": quality_version if scores else None,
            "scoring_version": quality.get("scoring_version") if scores else None,
            "targets": targets, "key_node_ids": [id_ for id_, marker in markers.items() if marker["is_key_step"] is True]}


def get_task_tree(session_id, task_id, expected_revision=None):
    if not task_id:
        raise ValueError("读取修正轨迹树需要 task_id")
    with active_session_lock(session_id) as session:
        _check_revision(session, expected_revision)
        snapshot = session.get("snapshot_payload") or _snapshot(session)
        groups = [group for group in snapshot["groups"] if str(group.get("task_id") or group.get("task")) == task_id]
        if not groups:
            raise KeyError(task_id)
        tree_ref, tree, quality = current_tree_and_quality(session_batch_id(session), storage_root())
    return _project_task_tree(session, task_id, groups, tree, quality, (tree_ref or {}).get("version"))
