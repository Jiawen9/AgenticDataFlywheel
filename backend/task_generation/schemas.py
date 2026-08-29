"""API schemas for task generation."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class KnowledgeJobRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    app: str | None = None
    scene: str | None = None
    capability: str | None = None
    sub_capability: str | None = None
    generate_per_sub_capability: int = Field(default=5, ge=1, le=100)


class InputMetadata(BaseModel):
    input_id: str
    original_filename: str
    size_bytes: int
    created_at: str
    status: Literal["ready"]


class SceneMatchJobRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input_id: str = Field(min_length=1, max_length=64)


class VariantJobRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scene_match_job_id: str = Field(min_length=1, max_length=64)
    generate_n: int = Field(default=10, ge=1, le=100)
