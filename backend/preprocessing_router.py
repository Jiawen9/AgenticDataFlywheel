"""HTTP entry points for batch preprocessing."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field

from .collection_runs import CollectionRunError
from .preprocessing_service import PreprocessingError


def no_cache(response: Response):
    response.headers["Cache-Control"] = "no-store"


router = APIRouter(prefix="/api/trajectory-preprocessing", tags=["trajectory-preprocessing"],
                   dependencies=[Depends(no_cache)])
_manager = None


def configure_preprocessing_manager(manager):
    global _manager
    _manager = manager


def manager():
    if _manager is None:
        raise HTTPException(503, "预处理服务尚未就绪")
    return _manager


class PreprocessingRequest(BaseModel):
    batch_id: str = Field(min_length=1, max_length=128)


def checked(operation):
    try:
        return operation()
    except (PreprocessingError, CollectionRunError) as exc:
        raise HTTPException(exc.status, str(exc)) from exc
    except (OSError, ValueError) as exc:
        raise HTTPException(409, str(exc)) from exc


@router.get("/batches")
def list_batches(view: str | None = None, offset: int = 0, limit: int = 10, q: str = "", status: str = "", batch_id: str | None = None):
    if view == "summary":
        from .stage_page_reads import batch_page
        return checked(lambda: batch_page(manager().root, offset=offset, limit=limit, q=q, status=status, batch_id=batch_id))
    return checked(lambda: {"batches": manager().batches()})


@router.post("/jobs", status_code=202)
def create_job(request: PreprocessingRequest):
    return checked(lambda: manager().submit(request.batch_id.strip()))


@router.get("/jobs")
def list_jobs(batch_id: str | None = None, view: str | None = None, offset: int = 0, limit: int = 10):
    if view == "summary":
        from .data_store import RecordStore
        return RecordStore(manager().root).summary_page("preprocessing_jobs", batch_id=batch_id, offset=offset, limit=limit)
    return checked(lambda: {"jobs": manager().list_jobs(batch_id)})


@router.get("/jobs/{job_id}")
def get_job(job_id: str):
    result = checked(lambda: manager().get(job_id))
    if result is None:
        raise HTTPException(404, "预处理作业不存在")
    return result


@router.post("/jobs/{job_id}/retry", status_code=202)
def retry_job(job_id: str):
    return checked(lambda: manager().retry(job_id))

