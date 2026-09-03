"""FastAPI routes for local dataset releases and mock uploads."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from .service import DatasetReleaseRegistry, default_registry
from .upload_jobs import DatasetUploadJobManager


class CreateReleaseRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    session_ids: list[str]


router = APIRouter(prefix="/api", tags=["data-publishing"])
registry: DatasetReleaseRegistry = default_registry()
upload_manager: DatasetUploadJobManager = DatasetUploadJobManager(registry)


def configure_data_publishing(
    release_registry: DatasetReleaseRegistry,
    manager: Optional[DatasetUploadJobManager] = None,
) -> None:
    global registry, upload_manager
    registry = release_registry
    upload_manager = manager or DatasetUploadJobManager(registry)


@router.get("/dataset-releases/candidates")
def dataset_release_candidates() -> dict[str, object]:
    return {"candidates": registry.candidates()}


@router.get("/dataset-releases")
def dataset_releases() -> dict[str, object]:
    return {"releases": registry.list_releases()}


@router.post("/dataset-releases", status_code=201)
def create_dataset_release(request: CreateReleaseRequest) -> dict[str, object]:
    try:
        return {"release": registry.create(request.name, request.session_ids)}
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (OSError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/dataset-releases/{release_id}")
def dataset_release(release_id: str) -> dict[str, object]:
    release = registry.get(release_id)
    if release is None:
        raise HTTPException(status_code=404, detail="数据集发布记录不存在")
    return {"release": release}


@router.get("/dataset-releases/{release_id}/excels/{index}")
def dataset_release_excel(release_id: str, index: int) -> FileResponse:
    try:
        path, filename = registry.excel_file(release_id, index)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return FileResponse(
        path,
        filename=filename,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@router.post("/dataset-releases/{release_id}/upload", status_code=202)
def upload_dataset_release(release_id: str) -> dict[str, object]:
    try:
        return {"job": upload_manager.submit(release_id)}
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (OSError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/dataset-upload-jobs/{job_id}")
def dataset_upload_job(job_id: str) -> dict[str, object]:
    job = upload_manager.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="数据集上传作业不存在")
    return {"job": job}
