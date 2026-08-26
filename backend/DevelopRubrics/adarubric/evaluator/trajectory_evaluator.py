"""LLM-powered trajectory evaluator."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from pydantic import BaseModel, Field, model_validator

from adarubric.core.exceptions import EvaluationError
from adarubric.core.models import (
    DimensionScore,
    DynamicRubric,
    StepEvaluation,
    Trajectory,
    TrajectoryEvaluation,
)
from adarubric.evaluator.aggregator import AggregationStrategy, WeightedMeanAggregator
from adarubric.evaluator.base import TrajectoryEvaluatorBase
from adarubric.evaluator.prompts import (
    EVALUATION_SYSTEM,
    EVALUATION_USER,
    format_trajectory_steps,
)
from adarubric.llm.base import LLMClient

logger = logging.getLogger(__name__)

_RATIONALE_MAX_CHARS = 300
_STEP_SUMMARY_MAX_CHARS = 240


# ---------------------------------------------------------------------------
# Intermediate Pydantic model for parsing LLM evaluation output
# ---------------------------------------------------------------------------


class _DimensionScoreRaw(BaseModel):
    dimension_name: str
    score: int = Field(ge=1, le=5)
    confidence: float = Field(ge=0.0, le=1.0, default=1.0)
    rationale: str = ""


class _StepEvalRaw(BaseModel):
    step_id: int
    dimension_scores: list[_DimensionScoreRaw]
    step_quality_summary: str = ""

    @model_validator(mode="before")
    @classmethod
    def _normalize_misplaced_fields(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data

        dimension_scores = data.get("dimension_scores")
        if not isinstance(dimension_scores, list):
            return data

        normalized_scores: list[Any] = []
        summary = data.get("step_quality_summary", "")
        for item in dimension_scores:
            if not isinstance(item, dict):
                continue
            if "dimension_name" in item and "score" in item:
                normalized_scores.append(item)
                continue
            if not summary and "step_quality_summary" in item:
                summary = item["step_quality_summary"]

        normalized = dict(data)
        normalized["dimension_scores"] = normalized_scores
        normalized["step_quality_summary"] = summary
        return normalized


class _EvaluationResponse(BaseModel):
    trajectory_id: str = ""
    task_id: str = ""
    step_evaluations: list[_StepEvalRaw]


def _truncate_text(text: str, max_chars: int) -> str:
    text = text.strip()
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 3].rstrip() + "..."


# ---------------------------------------------------------------------------
# Evaluator
# ---------------------------------------------------------------------------


class LLMTrajectoryEvaluator(TrajectoryEvaluatorBase):
    """Evaluates trajectories by prompting an LLM with the rubric.

    Parameters
    ----------
    client : LLMClient
        LLM backend for evaluation.
    aggregator : AggregationStrategy | None
        Strategy for computing global scores from step-level scores.
        Defaults to :class:`WeightedMeanAggregator`.
    max_concurrent : int
        Maximum concurrent evaluations in batch mode.
    """

    def __init__(
        self,
        client: LLMClient,
        *,
        aggregator: AggregationStrategy | None = None,
        max_concurrent: int = 5,
        max_tokens: int = 8192,
    ) -> None:
        self._client = client
        self._aggregator = aggregator or WeightedMeanAggregator()
        self._default_max_concurrent = max(1, max_concurrent)
        self._max_tokens = max_tokens
        self._semaphore = asyncio.Semaphore(self._default_max_concurrent)

    def _build_messages(
        self,
        trajectory: Trajectory,
        rubric: DynamicRubric,
        task_instruction: str,
        target_step_ids: set[int] | None = None,
    ) -> list[dict[str, str]]:
        rubric_data: dict[str, Any] = {
            "dimensions": [
                {
                    "name": d.name,
                    "description": d.description,
                    "weight": d.weight,
                    "scoring_criteria": {str(k): v for k, v in d.scoring_criteria.items()},
                }
                for d in rubric.dimensions
            ]
        }

        steps_data = [
            {
                "step_id": s.step_id,
                "action": s.action,
                "action_input": s.action_input
                if isinstance(s.action_input, str)
                else json.dumps(s.action_input),
                "action_input_data": s.action_input,
                "observation": s.observation,
            }
            for s in trajectory.steps
        ]

        if target_step_ids is None:
            evaluation_scope = "Evaluate every listed step."
        else:
            evaluation_scope = (
                "The listed trajectory contains both context steps and target steps. "
                f"Return step_evaluations only for these target step_ids: "
                f"{sorted(target_step_ids)}. Do not output evaluations for context-only "
                "steps. Use context steps only to understand prior state and the immediate "
                "next observation; do not merge context-step actions or outcomes into a "
                "target step's score."
            )

        user_content = EVALUATION_USER.format(
            rubric_json=json.dumps(rubric_data, indent=2),
            instruction=task_instruction,
            evaluation_scope=evaluation_scope,
            trajectory_text=format_trajectory_steps(
                steps_data,
                target_step_ids=target_step_ids,
            ),
        )

        return [
            {"role": "system", "content": EVALUATION_SYSTEM},
            {"role": "user", "content": user_content},
        ]

    def _convert_raw(
        self,
        raw: _EvaluationResponse,
        trajectory: Trajectory,
        rubric: DynamicRubric,
        target_step_ids: set[int] | None = None,
    ) -> TrajectoryEvaluation:
        """Convert parsed LLM output into the canonical evaluation model."""
        by_step: dict[int, _StepEvalRaw] = {}
        for raw_step in raw.step_evaluations:
            if raw_step.step_id in by_step:
                logger.warning(
                    "Duplicate evaluation for step_id=%d; keeping last occurrence",
                    raw_step.step_id,
                )
            by_step[raw_step.step_id] = raw_step

        expected_ids = target_step_ids or {s.step_id for s in trajectory.steps}
        missing = expected_ids - by_step.keys()
        extra = by_step.keys() - expected_ids
        if missing:
            logger.warning(
                "Missing LLM evaluation for trajectory %s step_ids=%s",
                trajectory.trajectory_id,
                sorted(missing),
            )
        if extra:
            logger.warning(
                "LLM returned evaluations for unknown step_ids=%s (trajectory %s)",
                sorted(extra),
                trajectory.trajectory_id,
            )

        ordered_ids = sorted(by_step.keys() & expected_ids)
        step_evals: list[StepEvaluation] = []
        for raw_step in (by_step[i] for i in ordered_ids):
            valid_dims = rubric.dimension_names
            dropped = [
                ds.dimension_name
                for ds in raw_step.dimension_scores
                if ds.dimension_name not in valid_dims
            ]
            if dropped:
                logger.warning(
                    "Step %d: ignoring %d unrecognised dimension(s): %s",
                    raw_step.step_id,
                    len(dropped),
                    dropped,
                )
            dim_scores = [
                DimensionScore(
                    dimension_name=ds.dimension_name,
                    score=ds.score,
                    confidence=ds.confidence,
                    rationale=_truncate_text(ds.rationale, _RATIONALE_MAX_CHARS),
                )
                for ds in raw_step.dimension_scores
                if ds.dimension_name in valid_dims
            ]
            step_evals.append(
                StepEvaluation(
                    step_id=raw_step.step_id,
                    dimension_scores=dim_scores,
                    step_quality_summary=_truncate_text(
                        raw_step.step_quality_summary,
                        _STEP_SUMMARY_MAX_CHARS,
                    ),
                )
            )

        dim_globals, overall = self._aggregator.aggregate_steps(step_evals, rubric)

        return TrajectoryEvaluation(
            trajectory_id=trajectory.trajectory_id,
            task_id=trajectory.task_id,
            rubric_used=rubric,
            step_evaluations=step_evals,
            dimension_global_scores=dim_globals,
            global_score=overall,
        )

    async def evaluate(
        self,
        trajectory: Trajectory,
        rubric: DynamicRubric,
        *,
        temperature: float = 0.0,
        task_instruction: str = "",
        max_tokens: int | None = None,
        target_step_ids: set[int] | None = None,
    ) -> TrajectoryEvaluation:
        messages = self._build_messages(
            trajectory,
            rubric,
            task_instruction,
            target_step_ids=target_step_ids,
        )
        budget = max_tokens if max_tokens is not None else self._max_tokens

        try:
            raw = await self._client.generate_structured(
                messages,
                _EvaluationResponse,
                temperature=temperature,
                max_tokens=budget,
            )
        except Exception as exc:
            raise EvaluationError(
                f"Failed to evaluate trajectory {trajectory.trajectory_id}: {exc}",
                context={
                    "trajectory_id": trajectory.trajectory_id,
                    "num_steps": len(trajectory.steps),
                },
            ) from exc

        result = self._convert_raw(raw, trajectory, rubric, target_step_ids=target_step_ids)

        logger.info(
            "Evaluated trajectory %s: global_score=%.3f (%d steps, %d dimensions)",
            trajectory.trajectory_id,
            result.global_score,
            len(result.step_evaluations),
            len(rubric.dimensions),
        )
        return result

    async def evaluate_batch(
        self,
        trajectories: list[Trajectory],
        rubric: DynamicRubric,
        *,
        temperature: float = 0.0,
        task_instruction: str = "",
        max_tokens: int | None = None,
        max_concurrent: int | None = None,
    ) -> list[TrajectoryEvaluation]:
        """Evaluate multiple trajectories concurrently with bounded parallelism."""
        budget = max_tokens if max_tokens is not None else self._max_tokens
        conc = (
            max(1, max_concurrent) if max_concurrent is not None else self._default_max_concurrent
        )
        sem = asyncio.Semaphore(conc)

        async def _eval_one(traj: Trajectory) -> TrajectoryEvaluation:
            async with sem:
                return await self.evaluate(
                    traj,
                    rubric,
                    temperature=temperature,
                    task_instruction=task_instruction,
                    max_tokens=budget,
                )

        return list(await asyncio.gather(*[_eval_one(t) for t in trajectories]))
