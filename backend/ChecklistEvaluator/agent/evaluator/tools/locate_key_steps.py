from __future__ import annotations

import hashlib
import logging

from agent.evaluator.tools.registry import EvaluatorContext

logger = logging.getLogger(__name__)


def locate_key_steps(ctx: EvaluatorContext, args: dict) -> dict:
    output: list[dict] = []
    flat = ctx.trajectory.get("actions_flat") or []
    if flat:
        actions = [
            (None, action.get("subtask_id"), index, action)
            for index, action in enumerate(flat)
        ]
    else:
        actions = [
            (pi, st.get("subtask_id"), ai, action)
            for pi, plan in enumerate(ctx.trajectory.get("plan_steps", []))
            for st in plan.get("subtasks", [])
            for ai, action in enumerate(st.get("actions", []))
        ]

    for pi, subtask_id, ai, action in actions:
        has_diff = False
        ui_before = action.get("ui_tree_before_raw") or action.get("ui_tree_before", "")
        ui_after = action.get("ui_tree_after_raw") or action.get("ui_tree_after", "")
        if ui_before and ui_after:
            try:
                has_diff = abs(_count_nodes(ui_after) - _count_nodes(ui_before)) > 2
            except Exception:
                pass

        shot_before = action.get("screenshot_before", "")
        shot_after = action.get("screenshot_after", "")
        if shot_before and shot_after:
            try:
                import os
                if os.path.exists(shot_before) and os.path.exists(shot_after):
                    with open(shot_before, "rb") as f:
                        h1 = hashlib.md5(f.read()).hexdigest()
                    with open(shot_after, "rb") as f:
                        h2 = hashlib.md5(f.read()).hexdigest()
                    if h1 != h2:
                        has_diff = True
            except Exception:
                pass

        score = 0.3
        act_type = action.get("action", {}).get("action", "")
        if act_type in ("input_text", "tap"):
            score += 0.2
        if has_diff:
            score += 0.3

        output.append({
            "plan_index": pi,
            "subtask_id": subtask_id,
            "action_index": ai,
            "global_step": action.get("global_step"),
            "score": round(score, 2),
            "reason": _reason_text(act_type, has_diff),
            "is_key": score >= 0.5,
        })

    output.sort(key=lambda x: x["score"], reverse=True)
    key_steps = [e for e in output if e["is_key"]]
    redundant = [e for e in output if not e["is_key"]]
    return {
        "key_steps": key_steps[:10],
        "redundant_steps": redundant[:10],
        "total_actions": len(output),
    }


def _count_nodes(xml_path: str) -> int:
    try:
        with open(xml_path, "r", encoding="utf-8") as f:
            content = f.read()
        return content.count("node ")
    except Exception:
        return 0


def _reason_text(act_type: str, has_diff: bool) -> str:
    parts = []
    if act_type in ("input_text", "tap"):
        parts.append(f"action={act_type} (meaningful)")
    else:
        parts.append(f"action={act_type}")
    if has_diff:
        parts.append("UI tree or screenshot changed")
    else:
        parts.append("no significant change detected")
    return "; ".join(parts)
