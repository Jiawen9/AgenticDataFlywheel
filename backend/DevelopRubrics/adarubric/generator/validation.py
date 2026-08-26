"""Validation utilities for generated dynamic rubrics.

The validator implements the three automated checks described in the
AdaRubric paper:

1. dimension names are non-overlapping by cosine distance;
2. rubric weights sum to 1 within tolerance;
3. all five scoring levels are populated.
"""

from __future__ import annotations

import math
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from adarubric.core.models import DynamicRubric, TaskDescription


@dataclass(frozen=True)
class RubricValidationIssue:
    """A single validation failure for a generated rubric."""

    code: str
    message: str
    context: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RubricValidationResult:
    """Validation result with all discovered issues."""

    valid: bool
    issues: list[RubricValidationIssue] = field(default_factory=list)

    def summary(self) -> str:
        if self.valid:
            return "valid"
        return "; ".join(f"{issue.code}: {issue.message}" for issue in self.issues)


class EmbeddingProvider(ABC):
    """Embeds rubric dimension names for overlap checks."""

    @abstractmethod
    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        """Return one embedding vector per input text."""


class OpenAIEmbeddingProvider(EmbeddingProvider):
    """Embedding provider for OpenAI-compatible embeddings APIs."""

    def __init__(
        self,
        *,
        model: str = "text-embedding-3-small",
        api_key: str | None = None,
        base_url: str | None = None,
    ) -> None:
        try:
            from openai import AsyncOpenAI
        except ImportError as exc:
            raise ImportError(
                "openai package is required for OpenAIEmbeddingProvider. "
                "Install with: pip install 'adarubric[openai]'"
            ) from exc

        self.model = model
        self._client = AsyncOpenAI(
            api_key=api_key or os.environ.get("OPENAI_API_KEY"),
            base_url=base_url,
        )

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        response = await self._client.embeddings.create(model=self.model, input=texts)
        return [list(item.embedding) for item in response.data]

    async def close(self) -> None:
        await self._client.close()


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 1.0
    return dot / (norm_a * norm_b)


class RubricValidator:
    """Validate generated rubrics using AdaRubric paper checks."""

    def __init__(
        self,
        embedding_provider: EmbeddingProvider,
        *,
        min_cosine_distance: float = 0.3,
        weight_sum_tolerance: float = 0.01,
    ) -> None:
        self._embedding_provider = embedding_provider
        self.min_cosine_distance = min_cosine_distance
        self.weight_sum_tolerance = weight_sum_tolerance

    async def validate(
        self,
        rubric: DynamicRubric,
        task: TaskDescription | None = None,
    ) -> RubricValidationResult:
        issues: list[RubricValidationIssue] = []
        if task is not None and rubric.task_id != task.task_id:
            issues.append(
                RubricValidationIssue(
                    code="task_id_mismatch",
                    message=f"rubric task_id {rubric.task_id!r} does not match {task.task_id!r}",
                    context={"rubric_task_id": rubric.task_id, "task_id": task.task_id},
                )
            )

        issues.extend(self._validate_weight_sum(rubric))
        issues.extend(self._validate_scoring_levels(rubric))
        issues.extend(await self._validate_dimension_overlap(rubric))
        return RubricValidationResult(valid=not issues, issues=issues)

    def _validate_weight_sum(self, rubric: DynamicRubric) -> list[RubricValidationIssue]:
        total = rubric.total_weight
        if abs(total - 1.0) <= self.weight_sum_tolerance:
            return []
        return [
            RubricValidationIssue(
                code="weight_sum",
                message=(
                    f"dimension weights must sum to 1.0 within "
                    f"{self.weight_sum_tolerance:.2%}; got {total:.6f}"
                ),
                context={"total_weight": total},
            )
        ]

    @staticmethod
    def _validate_scoring_levels(rubric: DynamicRubric) -> list[RubricValidationIssue]:
        issues: list[RubricValidationIssue] = []
        expected = {1, 2, 3, 4, 5}
        for dim in rubric.dimensions:
            keys = set(dim.scoring_criteria)
            if keys != expected:
                issues.append(
                    RubricValidationIssue(
                        code="scoring_levels",
                        message=f"{dim.name} scoring_criteria keys must be exactly 1-5",
                        context={"dimension": dim.name, "keys": sorted(keys)},
                    )
                )
                continue

            empty_levels = [
                level
                for level, criteria in dim.scoring_criteria.items()
                if not str(criteria).strip()
            ]
            if empty_levels:
                issues.append(
                    RubricValidationIssue(
                        code="empty_scoring_level",
                        message=f"{dim.name} has empty scoring levels: {empty_levels}",
                        context={"dimension": dim.name, "levels": empty_levels},
                    )
                )
        return issues

    async def _validate_dimension_overlap(
        self,
        rubric: DynamicRubric,
    ) -> list[RubricValidationIssue]:
        if len(rubric.dimensions) < 2:
            return []

        names = rubric.dimension_names
        embeddings = await self._embedding_provider.embed_texts(names)
        if len(embeddings) != len(names):
            return [
                RubricValidationIssue(
                    code="embedding_count",
                    message="embedding provider returned a different number of vectors",
                    context={"expected": len(names), "actual": len(embeddings)},
                )
            ]

        issues: list[RubricValidationIssue] = []
        for i, left_name in enumerate(names):
            for j in range(i + 1, len(names)):
                right_name = names[j]
                distance = 1.0 - _cosine_similarity(embeddings[i], embeddings[j])
                if distance <= self.min_cosine_distance:
                    issues.append(
                        RubricValidationIssue(
                            code="dimension_overlap",
                            message=(
                                f"{left_name!r} and {right_name!r} are too similar "
                                f"(cosine distance={distance:.3f})"
                            ),
                            context={
                                "left": left_name,
                                "right": right_name,
                                "cosine_distance": distance,
                                "min_cosine_distance": self.min_cosine_distance,
                            },
                        )
                    )
        return issues
