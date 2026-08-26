"""Prompt templates for trajectory evaluation."""

from __future__ import annotations

import json
from typing import Any

EVALUATION_SYSTEM = """\
You are an expert evaluator of AI agent trajectories.

Given an agent's execution trajectory and a task-specific evaluation rubric,
produce a detailed, step-by-step evaluation.

### Evaluation Protocol

1. **Read the rubric carefully.** Internalize what each scoring level (1-5)
   means for every dimension.
2. **Evaluate each target step independently.** For each target step:
   - Score it on every applicable dimension using the rubric's criteria.
   - Assign a confidence (0.0-1.0) reflecting **step-dimension applicability**:
     how directly this step provides evidence for this rubric dimension.
     Confidence is NOT how certain you are that the step is good or bad.
     A clearly bad but dimension-relevant step should receive a low score with high
     confidence. A step that does not address the dimension should receive low
     confidence, even if the trajectory is generally poor.
     Use this calibration:
       * 0.0-0.2: not applicable; the step provides almost no evidence.
       * 0.3-0.5: weakly applicable; indirect or partial evidence is present.
       * 0.6-0.8: applicable; useful but incomplete evidence is present.
       * 0.9-1.0: directly applicable; the step clearly exercises this dimension.
   - Provide a concise rationale grounding the score in observed behavior.
3. **Be calibrated.** Score 3 = acceptable execution. Reserve 5 for genuinely
   excellent steps. Score 1 only for clearly broken behavior.
4. **Be specific.** Rationales must reference concrete actions or observations,
   not vague praise/criticism.
5. **Keep output terse.** Do not include internal deliberation, uncertainty
   analysis, alternative hypotheses, or step-by-step thinking in any JSON field.
   Each rationale must be one short evidence statement, at most 18 English words
   or 30 Chinese characters. Each step_quality_summary must be one short sentence,
   at most 20 English words or 35 Chinese characters.
6. **Do not debate ambiguity.** If evidence is ambiguous, write exactly
   "Evidence ambiguous." in the rationale and choose the most conservative
   score supported by the target step. Do not explain multiple possible
   interpretations.

### Output Format

Return a JSON object with:
- trajectory_id: string
- task_id: string
- step_evaluations: array of objects, each containing:
  - step_id: integer
  - dimension_scores: array of objects, each containing:
    - dimension_name: string (must match a rubric dimension name exactly)
    - score: integer (1-5)
    - confidence: number (0.0-1.0)
    - rationale: string (one short evidence statement only)
  - step_quality_summary: string (one short sentence overall assessment of the step)\
"""

EVALUATION_USER = """\
### Evaluation Rubric

{rubric_json}

### Agent Trajectory

**Task Instruction**: {instruction}

### Evaluation Scope

{evaluation_scope}

**Steps**:
{trajectory_text}

Evaluate this trajectory against the rubric now.\
"""


def format_trajectory_steps(
    steps: list[dict[str, Any]],
    *,
    target_step_ids: set[int] | None = None,
) -> str:
    """Render trajectory steps as readable text for the LLM prompt."""
    def summary_history(before_step_id: int) -> str:
        history: list[str] = []
        for item in steps:
            if item["step_id"] >= before_step_id:
                continue
            action_input = item.get("action_input_data")
            if isinstance(action_input, str):
                try:
                    action_input = json.loads(action_input)
                except (TypeError, ValueError):
                    action_input = None
            summary = action_input.get("summary") if isinstance(action_input, dict) else None
            if summary is not None and str(summary).strip():
                history.append(f"step{item['step_id']}:{str(summary).strip()}")
        return "\n".join(history) if history else "Empty"

    parts: list[str] = []
    first_target = min(target_step_ids) if target_step_ids else None
    if first_target is not None:
        parts.append("**History**:\n" + summary_history(first_target))
    for step in steps:
        step_id = step["step_id"]
        if first_target is not None and step_id < first_target:
            continue
        if target_step_ids is None or step_id in target_step_ids:
            label = "TARGET - SCORE THIS STEP"
        else:
            label = "CONTEXT ONLY - DO NOT SCORE"
        lines = [f"--- Step {step_id} [{label}] ---"]
        if target_step_ids is None:
            lines.append("**History**:\n" + summary_history(step_id))
        lines.append(f"**Action**: {step['action']}")
        if step.get("action_input"):
            lines.append(f"**Action Input**: {step['action_input']}")
        lines.append(f"**Observation**: {step['observation']}")
        parts.append("\n".join(lines))
    return "\n\n".join(parts)
