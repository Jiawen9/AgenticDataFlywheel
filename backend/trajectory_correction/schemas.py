"""Request models for the trajectory-correction API."""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class CreateSessionRequest(BaseModel):
    tree_run_id: str = Field(min_length=1)


class RowPatchRequest(BaseModel):
    sop: Optional[str] = None
    actions: Optional[str] = None
    deleted: Optional[bool] = None


class ExportStateRequest(BaseModel):
    export: bool
