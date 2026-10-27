"""Read-only bulk lookup of presentation IDs. This API never reserves numbers."""
from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel, Field, model_validator

from .display_identifiers import DisplayIdentifierStore, SCOPED_KINDS

router = APIRouter(prefix="/api/display-identifiers", tags=["display-identifiers"])


class DisplayIdentifier(BaseModel):
    kind: Literal["batch", "run", "task", "trajectory", "pipeline", "job", "session", "export", "release"]
    id: str = Field(min_length=1, max_length=2048)
    scope: str = Field(default="", max_length=2048)

    @model_validator(mode="after")
    def validate_scope(self):
        if self.kind not in SCOPED_KINDS and self.scope:
            raise ValueError("仅任务和轨迹编号可以指定所属批次")
        return self


class ResolveIdentifiers(BaseModel):
    items: list[DisplayIdentifier] = Field(max_length=1000)


@router.post("/resolve")
def resolve_identifiers(request: ResolveIdentifiers):
    return {"items": DisplayIdentifierStore().resolve([item.model_dump() for item in request.items])}
