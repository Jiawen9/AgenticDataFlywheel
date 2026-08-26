"""Tests for generated rubric validation."""

from __future__ import annotations

import json
from typing import TypeVar

import pytest
from pydantic import BaseModel

from adarubric.core.exceptions import RubricGenerationError
from adarubric.core.models import DynamicRubric, EvalDimension, TaskDescription
from adarubric.generator.llm_generator import LLMRubricGenerator
from adarubric.generator.validation import EmbeddingProvider, RubricValidator
from adarubric.llm.base import LLMClient

T = TypeVar("T", bound=BaseModel)


class StaticEmbeddingProvider(EmbeddingProvider):
    def __init__(self, vectors: dict[str, list[float]] | None = None) -> None:
        self.vectors = vectors or {}

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        return [self.vectors.get(text, [1.0, 0.0, 0.0]) for text in texts]


class SequenceLLMClient(LLMClient):
    def __init__(self, responses: list[str]) -> None:
        self.responses = responses
        self.call_count = 0

    async def generate_structured(
        self,
        messages: list[dict[str, str]],
        response_model: type[T],
        *,
        temperature: float = 0.0,
        max_tokens: int = 4096,
    ) -> T:
        index = min(self.call_count, len(self.responses) - 1)
        self.call_count += 1
        return response_model.model_validate_json(self.responses[index])

    async def generate_text(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.0,
        max_tokens: int = 4096,
    ) -> str:
        self.call_count += 1
        return "ok"


def _criteria(prefix: str) -> dict[int, str]:
    return {
        1: f"{prefix} fundamentally broken behavior",
        2: f"{prefix} weak behavior",
        3: f"{prefix} acceptable baseline behavior",
        4: f"{prefix} strong behavior",
        5: f"{prefix} exemplary behavior",
    }


def _rubric(*, weights: tuple[float, float] = (0.5, 0.5)) -> DynamicRubric:
    return DynamicRubric(
        task_id="task-1",
        dimensions=[
            EvalDimension(
                name="SearchQuality",
                description="Measures how well the agent searches for required information",
                weight=weights[0],
                scoring_criteria=_criteria("search"),
            ),
            EvalDimension(
                name="RecommendationGrounding",
                description="Measures whether recommendations are grounded in evidence",
                weight=weights[1],
                scoring_criteria=_criteria("recommendation"),
            ),
        ],
        generation_rationale="Covers search and recommendation quality.",
    )


@pytest.mark.asyncio
async def test_rubric_validator_accepts_valid_rubric() -> None:
    validator = RubricValidator(
        StaticEmbeddingProvider(
            {
                "SearchQuality": [1.0, 0.0, 0.0],
                "RecommendationGrounding": [0.0, 1.0, 0.0],
            }
        )
    )

    result = await validator.validate(_rubric(), TaskDescription(task_id="task-1", instruction="x"))

    assert result.valid
    assert result.issues == []


@pytest.mark.asyncio
async def test_rubric_validator_rejects_bad_weight_sum() -> None:
    validator = RubricValidator(
        StaticEmbeddingProvider(
            {
                "SearchQuality": [1.0, 0.0, 0.0],
                "RecommendationGrounding": [0.0, 1.0, 0.0],
            }
        )
    )

    result = await validator.validate(_rubric(weights=(0.5, 0.7)))

    assert not result.valid
    assert any(issue.code == "weight_sum" for issue in result.issues)


@pytest.mark.asyncio
async def test_rubric_validator_rejects_empty_scoring_level() -> None:
    rubric = _rubric()
    criteria = dict(rubric.dimensions[0].scoring_criteria)
    criteria[3] = " "
    rubric.dimensions[0].scoring_criteria = criteria
    validator = RubricValidator(
        StaticEmbeddingProvider(
            {
                "SearchQuality": [1.0, 0.0, 0.0],
                "RecommendationGrounding": [0.0, 1.0, 0.0],
            }
        )
    )

    result = await validator.validate(rubric)

    assert not result.valid
    assert any(issue.code == "empty_scoring_level" for issue in result.issues)


@pytest.mark.asyncio
async def test_rubric_validator_rejects_overlapping_dimensions() -> None:
    validator = RubricValidator(
        StaticEmbeddingProvider(
            {
                "SearchQuality": [1.0, 0.0, 0.0],
                "RecommendationGrounding": [0.95, 0.05, 0.0],
            }
        )
    )

    result = await validator.validate(_rubric())

    assert not result.valid
    assert any(issue.code == "dimension_overlap" for issue in result.issues)


@pytest.mark.asyncio
async def test_generator_retries_until_rubric_passes_validation() -> None:
    invalid = _rubric(weights=(0.5, 0.7)).model_dump_json()
    valid = _rubric().model_dump_json()
    client = SequenceLLMClient([invalid, valid])
    validator = RubricValidator(
        StaticEmbeddingProvider(
            {
                "SearchQuality": [1.0, 0.0, 0.0],
                "RecommendationGrounding": [0.0, 1.0, 0.0],
            }
        )
    )
    generator = LLMRubricGenerator(client, rubric_validator=validator, max_validation_attempts=10)

    rubric = await generator.generate(TaskDescription(task_id="task-1", instruction="x"))

    assert rubric.total_weight == pytest.approx(1.0)
    assert client.call_count == 2


@pytest.mark.asyncio
async def test_generator_raises_after_validation_attempt_limit() -> None:
    invalid = _rubric(weights=(0.5, 0.7)).model_dump_json()
    client = SequenceLLMClient([invalid])
    validator = RubricValidator(
        StaticEmbeddingProvider(
            {
                "SearchQuality": [1.0, 0.0, 0.0],
                "RecommendationGrounding": [0.0, 1.0, 0.0],
            }
        )
    )
    generator = LLMRubricGenerator(client, rubric_validator=validator, max_validation_attempts=2)

    with pytest.raises(RubricGenerationError):
        await generator.generate(TaskDescription(task_id="task-1", instruction="x"))

    assert client.call_count == 2


def test_prompt_requires_weights_sum_to_one() -> None:
    from adarubric.generator.prompts import RUBRIC_GENERATION_SYSTEM

    prompt = RUBRIC_GENERATION_SYSTEM.format(num_dimensions=5)
    assert "sum to 1.0" in prompt
    assert json.dumps({"weight": 0.2})
