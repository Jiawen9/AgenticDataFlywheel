"""FastAPI routes for dataset releases, internal uploads, and legacy mocks."""

from __future__ import annotations

from typing import Literal, Optional
import logging

from fastapi import APIRouter, HTTPException, File, Form, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from .service import DatasetReleaseRegistry, default_registry
from .upload_jobs import DatasetUploadJobManager
from .internal_jobs import InternalUploadConflict
from .external_imports import ExternalReleaseImporter, ImportFailure
from ..training_data_overview.service import TrainingOverviewManager
from ..batch_lifecycle import BatchPublishedError, BatchNotReadyError


class CreateReleaseRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    session_ids: list[str]


class ImportReleaseRequest(BaseModel):
    import_id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=120)
    request_id: str = Field(min_length=1, max_length=128)


class UploadReleaseRequest(BaseModel):
    target: Literal["internal", "mock"] | None = None


router = APIRouter(prefix="/api", tags=["data-publishing"])
registry: DatasetReleaseRegistry = default_registry()
upload_manager: DatasetUploadJobManager = DatasetUploadJobManager(registry)
overview_manager = TrainingOverviewManager(registry)


def configure_data_publishing(
    release_registry: DatasetReleaseRegistry,
    manager: Optional[DatasetUploadJobManager] = None,
) -> None:
    global registry, upload_manager, overview_manager
    overview_manager.close()
    registry = release_registry
    upload_manager = manager or DatasetUploadJobManager(registry)
    overview_manager = TrainingOverviewManager(registry)


@router.get("/dataset-releases/candidates")
def dataset_release_candidates() -> dict[str, object]:
    return {"candidates": registry.candidates()}


@router.get("/dataset-releases")
def dataset_releases() -> dict[str, object]:
    return {"releases": registry.list_releases()}


@router.post("/dataset-releases", status_code=201)
def create_dataset_release(request: CreateReleaseRequest) -> dict[str, object]:
    try:
        release = registry.create(request.name, request.session_ids)
    except (BatchPublishedError, BatchNotReadyError) as exc:
        raise HTTPException(status_code=409, detail=exc.detail) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (OSError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    try:
        overview_manager.submit(str(release["release_id"]))
    except Exception:
        # Startup reconciliation also discovers a release missed here.
        logging.getLogger(__name__).exception("发布已完成，训练数据汇总排队失败")
    return {"release": release}


def recover_dataset_imports() -> None:
    ExternalReleaseImporter(registry).recover()


@router.post("/dataset-release-imports/preview")
def preview_dataset_release_import(
    file: UploadFile = File(...),
    data_source: str = Form(default="人工采集", max_length=120),
    data_date: str = Form(...),
    app: str = Form(default="", max_length=240),
    level1: str = Form(default="", max_length=240),
    level2: str = Form(default="", max_length=240),
    sheet_name: str = Form(default="", max_length=31),
) -> dict:
    try:
        return ExternalReleaseImporter(registry).preview(
            file.filename or "", file.file, data_source=data_source, data_date=data_date,
            app=app, level1=level1, level2=level2, sheet_name=sheet_name)
    except ImportFailure as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail) from exc
    except (OSError, ValueError, TypeError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    finally:
        file.file.close()


@router.post("/dataset-releases/import", status_code=201)
def import_dataset_release(request: ImportReleaseRequest) -> dict:
    try:
        release = ExternalReleaseImporter(registry).publish(request.import_id, request.name, request.request_id)
    except ImportFailure as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail) from exc
    except (OSError, ValueError, TypeError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    try:
        overview_manager.submit(str(release["release_id"]))
    except Exception:
        logging.getLogger(__name__).exception("外部数据已发布，训练数据汇总排队失败")
    return {"release": release}


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


@router.get("/dataset-upload-capabilities")
def dataset_upload_capabilities() -> dict[str, object]:
    return upload_manager.internal_capabilities()


@router.post("/dataset-releases/{release_id}/upload", status_code=202)
def upload_dataset_release(release_id: str, request: Optional[UploadReleaseRequest] = None) -> dict[str, object]:
    try:
        return {"job": upload_manager.submit(release_id, target=request.target if request else None)}
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except InternalUploadConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (OSError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/dataset-upload-jobs/{job_id}")
def dataset_upload_job(job_id: str) -> dict[str, object]:
    job = upload_manager.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="数据集上传作业不存在")
    return {"job": job}
