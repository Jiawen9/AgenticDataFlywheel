"""Tests for evaluator and aggregation strategies."""

from __future__ import annotations

import json

import pytest

from adarubric.core.models import (
    DimensionScore,
    DynamicRubric,
    EvalDimension,
    StepEvaluation,
)
from adarubric.evaluator.aggregator import (
    GeometricMeanAggregator,
    MinScoreAggregator,
    WeightedMeanAggregator,
)
from adarubric.evaluator.trajectory_evaluator import LLMTrajectoryEvaluator

from .conftest import MockLLMClient


def _make_rubric(weights: list[float] | None = None) -> DynamicRubric:
    ws = weights or [1.0, 1.0]
    dims = []
    for i, w in enumerate(ws):
        dims.append(
            EvalDimension(
                name=f"Dim{i}",
                description=f"Test dimension number {i} for aggregation testing",
                weight=w,
                scoring_criteria={1: "1", 2: "2", 3: "3", 4: "4", 5: "5"},
            )
        )
    return DynamicRubric(task_id="test", dimensions=dims)


def _make_steps(scores_per_step: list[list[tuple[str, int, float]]]) -> list[StepEvaluation]:
    steps = []
    for i, step_scores in enumerate(scores_per_step):
        dim_scores = [
            DimensionScore(dimension_name=name, score=score, confidence=conf)
            for name, score, conf in step_scores
        ]
        steps.append(StepEvaluation(step_id=i, dimension_scores=dim_scores))
    return steps


class TestWeightedMeanAggregator:
    def test_uniform_weights(self):
        rubric = _make_rubric([1.0, 1.0])
        steps = _make_steps(
            [
                [("Dim0", 4, 1.0), ("Dim1", 2, 1.0)],
            ]
        )
        agg = WeightedMeanAggregator()
        dim_globals, overall = agg.aggregate_steps(steps, rubric)
        assert dim_globals["Dim0"] == 4.0
        assert dim_globals["Dim1"] == 2.0
        assert overall == 3.0

    def test_non_uniform_weights(self):
        rubric = _make_rubric([2.0, 1.0])
        steps = _make_steps(
            [
                [("Dim0", 5, 1.0), ("Dim1", 2, 1.0)],
            ]
        )
        agg = WeightedMeanAggregator()
        _, overall = agg.aggregate_steps(steps, rubric)
        assert overall == pytest.approx(4.0, abs=0.01)

    def test_multi_step_averaging(self):
        rubric = _make_rubric([1.0])
        steps = _make_steps(
            [
                [("Dim0", 2, 1.0)],
                [("Dim0", 4, 1.0)],
            ]
        )
        agg = WeightedMeanAggregator()
        dim_globals, overall = agg.aggregate_steps(steps, rubric)
        assert dim_globals["Dim0"] == 3.0
        assert overall == 3.0

    def test_recency_decay(self):
        rubric = _make_rubric([1.0])
        steps = _make_steps(
            [
                [("Dim0", 2, 1.0)],
                [("Dim0", 4, 1.0)],
            ]
        )
        agg = WeightedMeanAggregator(recency_decay=2.0)
        dim_globals, _ = agg.aggregate_steps(steps, rubric)
        assert dim_globals["Dim0"] > 3.0

    def test_confidence_weighting(self):
        rubric = _make_rubric([1.0])
        steps = _make_steps(
            [
                [("Dim0", 4, 0.5)],
            ]
        )
        agg = WeightedMeanAggregator()
        dim_globals, _ = agg.aggregate_steps(steps, rubric)
        assert dim_globals["Dim0"] == 2.0

    def test_empty_steps(self):
        rubric = _make_rubric()
        agg = WeightedMeanAggregator()
        dim_globals, overall = agg.aggregate_steps([], rubric)
        assert dim_globals == {}
        assert overall == 0.0


class TestGeometricMeanAggregator:
    def test_equal_scores(self):
        rubric = _make_rubric([1.0, 1.0])
        steps = _make_steps(
            [
                [("Dim0", 3, 1.0), ("Dim1", 3, 1.0)],
            ]
        )
        agg = GeometricMeanAggregator()
        _, overall = agg.aggregate_steps(steps, rubric)
        assert overall == pytest.approx(3.0, abs=0.01)

    def test_penalizes_low_outlier(self):
        rubric = _make_rubric([1.0, 1.0])
        steps = _make_steps(
            [
                [("Dim0", 5, 1.0), ("Dim1", 1, 1.0)],
            ]
        )
        agg_geo = GeometricMeanAggregator()
        agg_arith = WeightedMeanAggregator()
        _, geo = agg_geo.aggregate_steps(steps, rubric)
        _, arith = agg_arith.aggregate_steps(steps, rubric)
        assert geo < arith


class TestMinScoreAggregator:
    def test_takes_minimum(self):
        rubric = _make_rubric([1.0, 1.0])
        steps = _make_steps(
            [
                [("Dim0", 5, 1.0), ("Dim1", 2, 1.0)],
            ]
        )
        agg = MinScoreAggregator()
        _, overall = agg.aggregate_steps(steps, rubric)
        assert overall == 2.0

    def test_all_same(self):
        rubric = _make_rubric([1.0, 1.0])
        steps = _make_steps(
            [
                [("Dim0", 3, 1.0), ("Dim1", 3, 1.0)],
            ]
        )
        agg = MinScoreAggregator()
        _, overall = agg.aggregate_steps(steps, rubric)
        assert overall == 3.0


@pytest.mark.asyncio
async def test_llm_evaluator_max_tokens(
    mock_llm_client: MockLLMClient,
    sample_trajectory,
    sample_rubric,
):
    ev = LLMTrajectoryEvaluator(mock_llm_client, max_tokens=3333)
    await ev.evaluate(sample_trajectory, sample_rubric)
    assert mock_llm_client.last_max_tokens == 3333
    await ev.evaluate(sample_trajectory, sample_rubric, max_tokens=4444)
    assert mock_llm_client.last_max_tokens == 4444


@pytest.mark.asyncio
async def test_llm_evaluator_does_not_send_thoughts(
    mock_llm_client: MockLLMClient,
    sample_trajectory,
    sample_rubric,
):
    ev = LLMTrajectoryEvaluator(mock_llm_client)
    await ev.evaluate(sample_trajectory, sample_rubric)

    prompt_text = "\n".join(message["content"] for message in mock_llm_client.last_messages)
    assert "**Thought**" not in prompt_text
    for step in sample_trajectory.steps:
        assert step.thought not in prompt_text


@pytest.mark.asyncio
async def test_llm_evaluator_uses_summary_only_history_for_target_step(sample_trajectory, sample_rubric):
    sample_trajectory.steps[0].action_input["summary"] = "search suppliers"
    raw = """
    {
      "trajectory_id": "traj",
      "task_id": "task",
      "step_evaluations": [
        {
          "step_id": 0,
          "dimension_scores": [
            {"dimension_name": "SearchStrategyQuality", "score": 1, "confidence": 1.0},
            {"dimension_name": "DataExtractionAccuracy", "score": 1, "confidence": 1.0}
          ],
          "step_quality_summary": "Extra context step."
        },
        {
          "step_id": 1,
          "dimension_scores": [
            {"dimension_name": "SearchStrategyQuality", "score": 4, "confidence": 1.0},
            {"dimension_name": "DataExtractionAccuracy", "score": 5, "confidence": 1.0}
          ],
          "step_quality_summary": "Target step."
        }
      ]
    }
    """
    client = MockLLMClient({"_EvaluationResponse": raw})
    ev = LLMTrajectoryEvaluator(client)

    result = await ev.evaluate(sample_trajectory, sample_rubric, target_step_ids={1})

    prompt_text = "\n".join(message["content"] for message in client.last_messages)
    assert "Return step_evaluations only for these target step_ids: [1]" in prompt_text
    assert "**History**:" in prompt_text
    assert "step0:search suppliers" in prompt_text
    assert "--- Step 0 [CONTEXT ONLY - DO NOT SCORE] ---" not in prompt_text
    assert sample_trajectory.steps[0].observation not in prompt_text
    assert "steel pipe suppliers Germany" not in prompt_text
    assert "--- Step 1 [TARGET - SCORE THIS STEP] ---" in prompt_text
    assert [step.step_id for step in result.step_evaluations] == [1]


@pytest.mark.asyncio
async def test_llm_evaluator_truncates_overlong_rationales(sample_trajectory, sample_rubric):
    long_rationale = "The agent opened the app correctly. " * 20
    long_summary = "This step contains an overly verbose quality summary. " * 10
    raw = json.dumps(
        {
            "trajectory_id": "traj",
            "task_id": "task",
            "step_evaluations": [
                {
                    "step_id": 0,
                    "dimension_scores": [
                        {
                            "dimension_name": "SearchStrategyQuality",
                            "score": 4,
                            "confidence": 1.0,
                            "rationale": long_rationale,
                        },
                        {
                            "dimension_name": "DataExtractionAccuracy",
                            "score": 4,
                            "confidence": 1.0,
                            "rationale": long_rationale,
                        },
                    ],
                    "step_quality_summary": long_summary,
                }
            ],
        }
    )
    client = MockLLMClient({"_EvaluationResponse": raw})
    ev = LLMTrajectoryEvaluator(client)

    result = await ev.evaluate(sample_trajectory, sample_rubric, target_step_ids={0})

    score = result.step_evaluations[0].dimension_scores[0]
    assert len(score.rationale) <= 300
    assert score.rationale.endswith("...")
    assert len(result.step_evaluations[0].step_quality_summary) <= 240


@pytest.mark.asyncio
async def test_llm_evaluator_recovers_misplaced_step_quality_summary(
    sample_trajectory,
    sample_rubric,
):
    raw = json.dumps(
        {
            "trajectory_id": "traj",
            "task_id": "task",
            "step_evaluations": [
                {
                    "step_id": 0,
                    "dimension_scores": [
                        {
                            "dimension_name": "SearchStrategyQuality",
                            "score": 4,
                            "confidence": 1.0,
                            "rationale": "Relevant action.",
                        },
                        {
                            "dimension_name": "DataExtractionAccuracy",
                            "score": 3,
                            "confidence": 0.8,
                            "rationale": "Partial evidence.",
                        },
                        {
                            "step_quality_summary": "The step is useful but incomplete.",
                        },
                    ],
                }
            ],
        }
    )
    client = MockLLMClient({"_EvaluationResponse": raw})
    ev = LLMTrajectoryEvaluator(client)

    result = await ev.evaluate(sample_trajectory, sample_rubric, target_step_ids={0})

    step_eval = result.step_evaluations[0]
    assert len(step_eval.dimension_scores) == 2
    assert step_eval.step_quality_summary == "The step is useful but incomplete."
