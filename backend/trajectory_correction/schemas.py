"""Request models for the trajectory-correction API."""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class CreateSessionRequest(BaseModel):
    batch_id: Optional[str] = Field(default=None, min_length=1)
    tree_run_id: Optional[str] = Field(default=None, min_length=1)


class RevisionRequest(BaseModel):
    expected_revision: int = Field(ge=1)


class ReviewRequest(RevisionRequest):
    decision: str


class RowPatchRequest(RevisionRequest):
    sop: Optional[str] = None
    actions: Optional[str] = None
    actions_box: Optional[str] = None
    summary: Optional[str] = None
    thought: Optional[str] = None
    deleted: Optional[bool] = None


class ExportStateRequest(RevisionRequest):
    export: bool


class CreateCotJobRequest(RevisionRequest):
    session_id: str = Field(min_length=1)
    group_ids: Optional[list[str]] = None
    row_ids: Optional[list[int]] = None
    generate_bbox: bool = False
    force_overwrite: bool = False
