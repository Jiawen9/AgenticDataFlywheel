"""Request models for the trajectory-correction API."""

from __future__ import annotations

from pydantic import BaseModel, Field


class CreateSessionRequest(BaseModel):
    tree_run_id: str = Field(min_length=1)


class RowPatchRequest(BaseModel):
    sop: str | None = None
    actions: str | None = None
    deleted: bool | None = None


class ExportStateRequest(BaseModel):
    export: bool
