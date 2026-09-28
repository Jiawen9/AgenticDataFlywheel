"""Server-local Rollout import endpoints; preview and copy use FastAPI workers."""
from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field

from .batch_lifecycle import BatchPublishedError, ensure_batch_active
from .collection_runs import CollectionRunError
from .data_store import RevisionConflict
from .rollout_imports import RolloutImportStore


def _no_cache(response: Response):
    response.headers["Cache-Control"] = "no-store"


router = APIRouter(prefix="/api/rollout-imports", tags=["rollout-imports"], dependencies=[Depends(_no_cache)])
_store = None


def configure_rollout_import_store(store: RolloutImportStore):
    global _store
    _store = store


def _service():
    if _store is None:
        raise HTTPException(status_code=503, detail="Rollout 导入服务尚未启动")
    return _store


class TaskOverride(BaseModel):
    model_config = ConfigDict(extra="forbid")
    collection_case_id: str
    task: str = ""
    app: str | None = None
    scene: str | None = None
    capability: str | None = None


class PreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_path: str = Field(max_length=4096)
    batch_id: str = Field(max_length=128)
    name: str | None = Field(default=None, max_length=200)
    app: str | None = None
    scene: str | None = None
    capability: str | None = None
    task_overrides: list[TaskOverride] = Field(default_factory=list)


class CommitRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    import_id: str
    request_id: str


def _call(operation):
    try:
        return operation()
    except BatchPublishedError as exc:
        raise HTTPException(status_code=409, detail=exc.detail) from exc
    except CollectionRunError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
    except RevisionConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (ValueError, OSError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/options")
def options():
    return _call(lambda: _service().options())


@router.get("/batches/{batch_id}")
def get_batch(batch_id: str):
    def read():
        store = _service()
        ensure_batch_active(batch_id, store.root)
        value = store.get_batch(batch_id)
        if value is None:
            raise HTTPException(status_code=404, detail="Rollout 导入批次不存在")
        return value
    return _call(read)


@router.post("/preview")
def preview(body: PreviewRequest):
    return _call(lambda: _service().preview(body.model_dump()))


@router.post("")
def commit(body: CommitRequest):
    return _call(lambda: _service().commit(body.import_id, body.request_id))
