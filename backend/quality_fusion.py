"""Deterministic trajectory fusion and occurrence-aware tree values.

These values supplement Rubric scores; they never replace observed step scores.
"""
from __future__ import annotations

import math
from copy import deepcopy

SCORING_VERSION = "checklist_fusion_v1"
LAMBDA = 0.5


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def fuse(evaluation: dict, checklist: dict, *, definition_digest: str | None = None) -> dict:
    reasons = []
    g = evaluation.get("global_score")
    if not _finite(g) or not 1 <= g <= 5:
        reasons.append("rubric_score_unavailable")
    if evaluation.get("status") in {"error", "invalid", "incomplete"}:
        reasons.append("rubric_incomplete")
    if evaluation.get("invalid_reasons") or evaluation.get("no_evidence_dimensions"):
        reasons.append("rubric_evidence_invalid")
    dimensions = evaluation.get("dimension_global_scores")
    if isinstance(dimensions, dict) and any(not _finite(value) or not 1 <= value <= 5 for value in dimensions.values()):
        reasons.append("rubric_evidence_invalid")
    if checklist.get("status") != "complete":
        reasons.append("checklist_incomplete")
        # A failed invocation has no report to validate. Do not turn one known
        # execution failure into invented definition/count/verdict mismatches.
        return {"version": 1, "lambda": LAMBDA, "score": None, "valid": False,
                "invalid_reasons": list(dict.fromkeys(reasons))}
    definition = checklist.get("definition_digest")
    if not isinstance(definition, str) or not definition or definition_digest and definition != definition_digest:
        reasons.append("checklist_definition_mismatch")
    if checklist.get("source") not in {"gold", "llm"}:
        reasons.append("checklist_source_unavailable")
    items = checklist.get("items")
    if not isinstance(items, list) or not items or any(not isinstance(item, dict) for item in items):
        items = []
        reasons.append("invalid_checklist_items")
    ids = [item.get("id") for item in items]
    if any(not isinstance(ident, str) or not ident for ident in ids) or len(set(map(str, ids))) != len(ids):
        reasons.append("invalid_checklist_ids")
    checked = [item for item in items if item.get("checked") is True and item.get("result") in {"passed", "failed"}]
    passed = sum(item["result"] == "passed" for item in checked)
    failed = len(checked) - passed
    for field, expected in (("total", len(items)), ("passed", passed), ("failed", failed), ("unchecked", len(items) - len(checked))):
        if type(checklist.get(field)) is not int or checklist[field] != expected:
            reasons.append("inconsistent_checklist_counts")
    if len(checked) != len(items):
        reasons.append("unchecked_conditions")
    if checklist.get("verdict") not in {"success", "partial_success", "fail"}:
        reasons.append("checklist_verdict_unavailable")
    valid = not reasons
    return {"version": 1, "lambda": LAMBDA, "score": (passed + LAMBDA * g / 5) / (len(items) + LAMBDA) if valid else None,
            "valid": valid, "invalid_reasons": list(dict.fromkeys(reasons))}


def rank_trajectories(evaluations: dict, workbook_order: list[str]) -> list[str]:
    """F ordering without rounding; workbook order is the sole tie breaker."""
    order = {ident: index for index, ident in enumerate(workbook_order)}
    valid = [ident for ident in workbook_order if evaluations.get(ident, {}).get("fusion", {}).get("valid")]
    return sorted(valid, key=lambda ident: (-evaluations[ident]["checklist"]["passed"],
                                          -evaluations[ident]["global_score"], order[ident]))


def node_values(tree: dict, evaluations: dict, trajectory_ids: list[str]) -> dict:
    all_ids = set(trajectory_ids)
    values = {}

    def walk(node, root=False):
        identities = all_ids if root else {str(row.get("trajectory", row.get("trajectory_id"))) for row in node.get("occurrences", [])
                                          if isinstance(row, dict) and row.get("trajectory", row.get("trajectory_id")) is not None}
        identities &= all_ids
        scores = [evaluations[ident]["fusion"]["score"] for ident in identities
                  if evaluations.get(ident, {}).get("fusion", {}).get("valid") is True
                  and _finite(evaluations[ident]["fusion"].get("score"))]
        value = {"q": sum(scores) / len(scores) if scores else None, "advantage": None,
                 "scored_count": len(scores), "total_count": len(identities),
                 "status": "complete" if scores and len(scores) == len(identities) else "partial" if scores else "unavailable"}
        values[str(node["id"])] = value
        children = node.get("children", [])
        child_values = [walk(child) for child in children]
        if child_values and all(item["q"] is not None for item in child_values):
            mean = sum(item["q"] for item in child_values) / len(child_values)
            std = math.sqrt(sum((item["q"] - mean) ** 2 for item in child_values) / len(child_values))
            for item in child_values:
                item["advantage"] = (item["q"] - mean) / (std + 1e-8)
        return value

    walk(tree, True)
    return values


def enrich_task(task: dict, tree: dict, checklists: dict, workbook_order: list[str], definition_digest: str | None = None) -> dict:
    result = deepcopy(task)
    evaluations = result["evaluations"]
    standards = {(str(row.get("definition_digest")), str(row.get("total")), str(row.get("source"))) for row in checklists.values()
                 if row.get("status") == "complete"}
    for ident, evaluation in evaluations.items():
        checklist = deepcopy(checklists.get(ident) or {"status": "error", "error": "checklist_result_missing"})
        evaluation["checklist"] = checklist
        evaluation["fusion"] = fuse(evaluation, checklist, definition_digest=definition_digest)
        if len(standards) > 1:
            evaluation["fusion"].update(valid=False, score=None)
            evaluation["fusion"]["invalid_reasons"].append("mixed_task_checklist_standards")
    result.update(scoring_version=SCORING_VERSION, checklist_definition_digest=definition_digest,
                  trajectory_order=list(workbook_order), node_scores=node_values(tree, evaluations, workbook_order),
                  fusion_order=rank_trajectories(evaluations, workbook_order))
    result["fusion_complete"] = all(evaluations.get(ident, {}).get("fusion", {}).get("valid") for ident in workbook_order)
    result["fusion_eligible_count"] = len(result["fusion_order"])
    return result


def completion_summary(report: dict, task_ids: list[str] | None = None) -> dict:
    """Separate committed/viewable evidence from complete fusion results."""
    selected = set(task_ids or [])
    tasks = [task for task in report.get("tasks", [])
             if not selected or task.get("task_id") in selected]
    fusion_tasks = [task for task in tasks if task.get("scoring_version") == SCORING_VERSION]
    incomplete = [task for task in fusion_tasks if not task.get("fusion_complete")]
    missing = sorted(selected - {task.get("task_id") for task in tasks}) if "tasks" in report else []
    available = any(task.get("evaluations") or task.get("results_available") for task in tasks)
    result = {"result_complete": not incomplete and not missing,
              "results_available": available,
              "partial_results_available": bool(incomplete or missing) and available,
              "fusion_eligible_count": sum(int(task.get("fusion_eligible_count") or 0) for task in fusion_tasks)}
    if not incomplete and not missing:
        return result
    reasons = [f"任务 {task}：缺少本次评分结果" for task in missing[:3]]
    failure = None
    for task in incomplete:
        if len(reasons) >= 3:
            break
        if task.get("primary_error") and not task.get("evaluations"):
            reasons.append(f"任务 {task.get('task_id')}：{task['primary_error']}")
        for ident, row in (task.get("evaluations") or {}).items():
            if (row.get("fusion") or {}).get("valid"):
                continue
            checklist = row.get("checklist") or {}
            error = checklist.get("error") or row.get("error")
            if not error:
                error = "评分缺项：" + "、".join((row.get("fusion") or {}).get("invalid_reasons") or ["评分尚未完成"])
            reasons.append(f"任务 {task.get('task_id')} / 轨迹 {ident}：{error}")
            failure = failure or checklist.get("failure")
            if len(reasons) == 3:
                break
        if len(reasons) == 3:
            break
    from .pipeline_diagnostics import sanitize
    from .pipeline_retry_errors import failure_from_payload
    result["error"] = str(sanitize("质检未完整完成，已有结果已保留：" + "；".join(reasons or ["部分轨迹尚无有效融合评分"])))[:2400]
    result["failure"] = failure_from_payload(failure or {"category": "unknown", "code": "quality_result_incomplete"})
    return result
