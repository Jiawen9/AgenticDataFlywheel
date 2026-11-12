"""HTTP control plane for durable Pipelines; GET never starts work."""
from fastapi import APIRouter, HTTPException, Request, Response, Depends, Query
from pydantic import BaseModel, ConfigDict, Field
from .data_store import RevisionConflict
from .data_store.registry import SummaryUnavailable
from .pipelines import PipelineError

def _no_cache(response: Response):
    response.headers["Cache-Control"] = "no-store"

router = APIRouter(prefix="/api/pipelines", tags=["pipelines"], dependencies=[Depends(_no_cache)])

class CreatePipelineRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: str
    name: str
    batch_id: str
    mode: str
    start_mode: str
    threshold: float | None = Field(default=None, ge=0, le=5, strict=True)
    collection_config: dict | None = None

class ControlPipelineRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int
    session_revision: int | None = None

def _manager(request):
    manager = getattr(request.app.state, "pipelines", None)
    if manager is None:
        raise HTTPException(status_code=503, detail="Pipeline 服务尚未启动")
    return manager

def _call(operation):
    try:
        return operation()
    except PipelineError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
    except RevisionConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except SummaryUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

@router.get("")
def list_pipelines(request: Request, batch_id: str | None = None, active_only: bool = False,
                   view: str | None = None, offset: int = Query(0, ge=0), limit: int = Query(10, ge=1, le=100),
                   search: str = "", status: str = "", include_published: bool = True):
    if view == "summary":
        return _call(lambda: _manager(request).summary_page(batch_id=batch_id, active_only=active_only,
            offset=offset, limit=limit, q=search, status=status, include_published=include_published))
    if view is not None:
        raise HTTPException(status_code=422, detail="未知 Pipeline 列表视图")
    return _call(lambda: {"pipelines": _manager(request).list(batch_id, active_only)})

@router.post("", status_code=201)
def create_pipeline(body: CreatePipelineRequest, request: Request):
    return _call(lambda: {"pipeline": _manager(request).create(body.model_dump())})

@router.get("/{pipeline_id}")
def get_pipeline(pipeline_id: str, request: Request):
    return _call(lambda: {"pipeline": _manager(request).get(pipeline_id)})

@router.post("/{pipeline_id}/{action}")
def control_pipeline(pipeline_id: str, action: str, body: ControlPipelineRequest, request: Request):
    if action not in {"pause", "resume", "retry", "confirm-correction", "terminate"}:
        raise HTTPException(status_code=404, detail="未知 Pipeline 操作")
    return _call(lambda: {"pipeline": _manager(request).control(pipeline_id, action, body.expected_revision,
                                                               session_revision=body.session_revision)})
