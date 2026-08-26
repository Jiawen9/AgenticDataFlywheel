"""Tests for score aggregation strategies."""

from __future__ import annotations

import pytest

from adarubric.core.models import (
    DimensionScore,
    DynamicRubric,
    EvalDimension,
    StepEvaluation,
)
from adarubric.evaluator.aggregator import (
    ConfidenceNormalizedAggregator,
    WeightedMeanAggregator,
)


def _criteria() -> dict[int, str]:
    return {
        1: "Clearly poor execution",
        2: "Weak execution with major gaps",
        3: "Acceptable execution with some gaps",
        4: "Strong execution with minor gaps",
        5: "Excellent execution",
    }


def _rubric() -> DynamicRubric:
    return DynamicRubric(
        task_id="t1",
        dimensions=[
            EvalDimension(
                name="D1",
                description="First dimension for aggregation behavior testing",
                weight=1.0,
                scoring_criteria=_criteria(),
            ),
            EvalDimension(
                name="D2",
                description="Second dimension for aggregation behavior testing",
                weight=1.0,
                scoring_criteria=_criteria(),
            ),
        ],
    )


def test_confidence_normalized_treats_low_confidence_as_low_evidence() -> None:
    rubric = _rubric()
    steps = [
        StepEvaluation(
            step_id=0,
            dimension_scores=[
                DimensionScore(dimension_name="D1", score=5, confidence=1.0),
                DimensionScore(dimension_name="D2", score=3, confidence=0.2),
            ],
        ),
        StepEvaluation(
            step_id=1,
            dimension_scores=[
                DimensionScore(dimension_name="D1", score=3, confidence=0.2),
                DimensionScore(dimension_name="D2", score=5, confidence=1.0),
            ],
        ),
    ]

    old_dims, old_global = WeightedMeanAggregator().aggregate_steps(steps, rubric)
    new_dims, new_global = ConfidenceNormalizedAggregator().aggregate_steps(steps, rubric)

    assert old_dims["D1"] == pytest.approx(2.8)
    assert old_dims["D2"] == pytest.approx(2.8)
    assert old_global == pytest.approx(2.8)

    assert new_dims["D1"] == pytest.approx(4.6667, abs=1e-4)
    assert new_dims["D2"] == pytest.approx(4.6667, abs=1e-4)
    assert new_global == pytest.approx(4.6667, abs=1e-4)


def test_confidence_normalized_returns_zero_without_dimension_evidence() -> None:
    rubric = _rubric()
    steps = [
        StepEvaluation(
            step_id=0,
            dimension_scores=[
                DimensionScore(dimension_name="D1", score=5, confidence=0.0),
            ],
        )
    ]

    dims, global_score = ConfidenceNormalizedAggregator().aggregate_steps(steps, rubric)

    assert dims["D1"] == 0.0
    assert dims["D2"] == 0.0
    assert global_score == 0.0
