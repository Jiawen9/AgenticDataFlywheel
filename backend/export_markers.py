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


def annotate_rows(groups, *, tree_payload, quality_payload, row_edits):
    """Return copied groups; a missing Q/mapping is null, not an invented zero."""
    result = deepcopy(groups)
    qualities = {str(task.get("task_id")): task for task in quality_payload.get("tasks", [])}
    trees = tree_payload.get("trees", {})
    for group in result:
        task_id = str(group.get("task_id") or "")
        trajectory = str(group.get("trajectory_id") or group.get("meta_task") or "")
        tree = trees.get(task_id) or {}
        if "tree" in tree:
            tree = tree["tree"]
        scores = qualities.get(task_id, {}).get("node_scores", {})
        expected = quality_payload.get("source_tree_hashes", {}).get(task_id)
        actual = tree_payload.get("tree_hashes", {}).get(task_id)
        if (not expected or not actual or expected != actual
                or qualities.get(task_id, {}).get("scoring_version") != SCORING_VERSION):
            scores = {}
        mapping = {}

        def walk(node, marker=False, branch_q=None, status="not_branch", parent_id=None):
            own_q = scores.get(str(node.get("id")), {}).get("q")
            if type(own_q) not in (int, float) or not math.isfinite(own_q):
                marker, status = None, "q_unavailable"
            coverage = {key: scores.get(str(node.get("id")), {}).get(key) for key in ("scored_count", "total_count", "status")}
            for occurrence in node.get("occurrences", []):
                identity = str(occurrence.get("trajectory") or occurrence.get("trajectory_id") or "")
                if identity == trajectory:
                    key = (str(occurrence.get("step", "")), str(occurrence.get("image", "")).replace("\\", "/"))
                    mapping[key] = (marker, branch_q, status, node.get("id"), parent_id, coverage)
            children = node.get("children", [])
            values = [scores.get(str(child.get("id")), {}).get("q") for child in children]
            valid = [value for value in values if type(value) in (int, float) and math.isfinite(value)]
            different = len(valid) >= 2 and max(valid) - min(valid) > 1e-8
            for child, value in zip(children, values):
                finite = type(value) in (int, float) and math.isfinite(value)
                if len(children) < 2:
                    walk(child, False, value if finite else None, parent_id=node.get("id"))
                elif not finite or (len(valid) < 2):
                    walk(child, None, value if finite else None, "q_unavailable", node.get("id"))
                elif different:
                    walk(child, True, value, "original_q_branch", node.get("id"))
                elif len(valid) != len(children):
                    walk(child, None, value, "q_incomplete", node.get("id"))
                else:
                    walk(child, False, value, "equal_q", node.get("id"))

        if tree:
            walk(tree)
        for row in group.get("rows", []):
            edit = row_edits.get(str(row.get("excel_row")), {})
            current = row.get("actions", row.get("action"))
            original = row.get("original_action", row.get("original_actions", current))
            action_changed = "actions" in edit and action_value(current) != action_value(original)
            sop = str(row.get("sop") or "")
            original_sop = str(row.get("original_sop", (row.get("values") or {}).get("sop", "")) or "")
            row["is_refined"] = bool(action_changed or ("sop" in edit and sop != original_sop))
            key = (str(row.get("step", "")), str(row.get("image", "")).replace("\\", "/"))
            found = mapping.get(key)
            if found is None:
                # Old source rows can retain the step but use a rebased path.
                matches = [value for (step, _), value in mapping.items() if step == key[0]]
                found = matches[0] if len(matches) == 1 else (None, None, "source_mapping_unavailable", None, None, {})
            row["is_key_step"], row["source_branch_q"], row["key_step_status"], row["source_node_id"], row["source_parent_node_id"], row["source_score_coverage"] = found
            row["source_tree_hash"] = actual
            row["source_tree_version"] = (tree_payload.get("artifact") or {}).get("version") or tree_payload.get("version")
            row["source_scoring_version"] = qualities.get(task_id, {}).get("scoring_version")
            row["source_quality_version"] = (quality_payload.get("artifact") or {}).get("version") or quality_payload.get("version")
    return result
