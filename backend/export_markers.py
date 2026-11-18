"""Pure, occurrence-aware training markers from a frozen tree and its scores.

Markers describe the original scored branch. They never assert a corrected
action has been re-evaluated and never derive importance from user selection.
"""
from copy import deepcopy
import json
import math
from .quality_fusion import SCORING_VERSION


def action_value(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            return value.strip()
    if isinstance(value, list) and len(value) == 1:
        return value[0]
    return value


def effective_action_edit(row, edit):
    """Compare the persisted Action with its source, ignoring JSON formatting."""
    baseline = edit.get("original_actions", row.get("original_action", row.get("original_actions",
                        row.get("actions", row.get("action")))))
    return "actions" in edit and action_value(edit["actions"]) != action_value(baseline)


def effective_refinement(row, edit):
    original_sop = row.get("original_sop", (row.get("values") or {}).get("sop", ""))
    sop_changed = "sop" in edit and str(edit["sop"] or "") != str(original_sop or "")
    return effective_action_edit(row, edit) or sop_changed


def compatible_node_scores(task_id, tree_payload, quality_payload, quality):
    actual = tree_payload.get("tree_hashes", {}).get(task_id)
    expected = quality_payload.get("source_tree_hashes", {}).get(task_id)
    scores = quality.get("node_scores")
    if actual and actual == expected and quality.get("scoring_version") == SCORING_VERSION and isinstance(scores, dict):
        return scores
    return {}


def _finite_q(value):
    return type(value) in (int, float) and math.isfinite(value)


def original_branch_markers(tree, scores):
    """Metadata by original node ID, used by the correction tree and exports."""
    markers = {}

    def walk(node, marker=False, branch_q=None, status="not_branch", parent_id=None):
        node_id = node.get("id")
        score = scores.get(str(node_id), {})
        if not _finite_q(score.get("q")):
            marker, status = None, "q_unavailable"
        markers[node_id] = {
            "is_key_step": marker,
            "source_branch_q": branch_q,
            "key_step_status": status,
            "source_node_id": node_id,
            "source_parent_node_id": parent_id,
            "source_score_coverage": {key: score.get(key) for key in ("scored_count", "total_count", "status")},
        }
        children = node.get("children", [])
        values = [scores.get(str(child.get("id")), {}).get("q") for child in children]
        valid = [value for value in values if _finite_q(value)]
        different = len(valid) >= 2 and max(valid) - min(valid) > 1e-8
        for child, value in zip(children, values):
            finite = _finite_q(value)
            if len(children) < 2:
                walk(child, False, value if finite else None, parent_id=node_id)
            elif not finite or len(valid) < 2:
                walk(child, None, value if finite else None, "q_unavailable", node_id)
            elif different:
                walk(child, True, value, "original_q_branch", node_id)
            elif len(valid) != len(children):
                walk(child, None, value, "q_incomplete", node_id)
            else:
                walk(child, False, value, "equal_q", node_id)

    if tree:
        walk(tree)
    return markers


def step_image_identity(row):
    return str(row.get("step", "")), str(row.get("image", "")).replace("\\", "/")


def _indexed_occurrences(tree, markers):
    by_trajectory = {}

    def visit(node):
        for occurrence in node.get("occurrences", []):
            trajectory_id = str(occurrence.get("trajectory") or occurrence.get("trajectory_id") or "")
            steps = by_trajectory.setdefault(trajectory_id, {})
            key = step_image_identity(occurrence)
            steps[key] = None if key in steps else markers[node["id"]]
        for child in node.get("children", []):
            visit(child)

    if tree:
        visit(tree)
    return by_trajectory


def annotate_rows(groups, *, tree_payload, quality_payload, row_edits):
    """Return copied groups; a missing Q/mapping is null, not an invented zero."""
    result = deepcopy(groups)
    qualities = {str(task.get("task_id")): task for task in quality_payload.get("tasks", [])}
    trees = tree_payload.get("trees", {})
    occurrences = {}
    for group in result:
        task_id = str(group.get("task_id") or "")
        trajectory = str(group.get("trajectory_id") or group.get("meta_task") or "")
        actual = tree_payload.get("tree_hashes", {}).get(task_id)
        quality = qualities.get(task_id, {})
        if task_id not in occurrences:
            value = trees.get(task_id) or {}
            tree = value.get("tree", value)
            scores = compatible_node_scores(task_id, tree_payload, quality_payload, quality)
            occurrences[task_id] = _indexed_occurrences(tree, original_branch_markers(tree, scores))
        mapping = occurrences[task_id].get(trajectory, {})
        for row in group.get("rows", []):
            edit = row_edits.get(str(row.get("excel_row")), {})
            row["is_refined"] = effective_refinement(row, edit)
            key = step_image_identity(row)
            if key in mapping:
                found = mapping[key]
            else:
                # Old source rows can retain the step but use a rebased path.
                matches = [value for (step, _), value in mapping.items() if step == key[0]]
                found = matches[0] if len(matches) == 1 else None
            row.update(found or {
                "is_key_step": None, "source_branch_q": None, "key_step_status": "source_mapping_unavailable",
                "source_node_id": None, "source_parent_node_id": None, "source_score_coverage": {},
            })
            row["source_tree_hash"] = actual
            row["source_tree_version"] = (tree_payload.get("artifact") or {}).get("version") or tree_payload.get("version")
            row["source_scoring_version"] = quality.get("scoring_version")
            row["source_quality_version"] = (quality_payload.get("artifact") or {}).get("version") or quality_payload.get("version")
    return result
