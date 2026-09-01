from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from .constants import ALLOWED_WORKBOOK_SUFFIXES, KNOWLEDGE_BASE_FILES
from .jobs import TaskGenerationJobManager
from .knowledge_base import list_knowledge_bases, replace_knowledge_base, tree_payload
from .tree_store import VersionConflict, current_root, save_tree


class TaskSelection(BaseModel):
    node_id: str
    apps: list[str] = Field(min_length=1)


class TaskGenerationRequest(BaseModel):
    version: str
    selections: list[TaskSelection] = Field(min_length=1)
    generate_n: int = Field(default=5, ge=1, le=20)


class TreeUpdateRequest(BaseModel):
    base_version: str
    scenes: list[dict[str, Any]]


class ResultPatchRequest(BaseModel):
    task: str | None = Field(default=None, max_length=4000)
    deleted: bool | None = None


router = APIRouter(prefix="/api/task-generation", tags=["task-generation"])
manager: TaskGenerationJobManager | None = None


def configure_job_manager(value: TaskGenerationJobManager) -> None:
    global manager
    manager = value


def _manager() -> TaskGenerationJobManager:
    if manager is None:
        raise RuntimeError("任务生成作业管理器尚未初始化")
    return manager


@router.get("/knowledge-bases")
def knowledge_bases() -> dict[str, Any]:
    return {"knowledge_bases": list_knowledge_bases(_manager().knowledge_base_dir)}


@router.put("/knowledge-bases/{kind}")
def upload_knowledge_base(kind: str, file: UploadFile = File(...), base_version: str | None = Form(default=None)) -> dict[str, Any]:
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in ALLOWED_WORKBOOK_SUFFIXES:
        raise HTTPException(status_code=422, detail="知识库只支持 .xlsx 或 .xlsm 文件")
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(prefix="task-generation-kb-", suffix=suffix, delete=False) as temporary:
            temporary_path = Path(temporary.name)
            shutil.copyfileobj(file.file, temporary)
        return {"knowledge_base": replace_knowledge_base(kind, temporary_path, root=_manager().knowledge_base_dir, base_version=base_version)}
    except VersionConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    finally:
        if temporary_path:
            temporary_path.unlink(missing_ok=True)


@router.get("/tree")
def scene_tree() -> dict[str, Any]:
    try:
        return tree_payload(_manager().knowledge_base_dir)
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.put("/tree")
def update_scene_tree(request: TreeUpdateRequest) -> dict[str, Any]:
    try:
        return save_tree(request.scenes, request.base_version, root=_manager().knowledge_base_dir)
    except VersionConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/tree/export")
def download_scene_tree() -> FileResponse:
    try:
        path = current_root(_manager().knowledge_base_dir) / KNOWLEDGE_BASE_FILES["scene_tree"]
        if not path.is_file():
            raise FileNotFoundError("当前版本没有场景树 Excel")
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return FileResponse(path, filename=path.name, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@router.post("/jobs", status_code=202)
def create_task_generation_job(request: TaskGenerationRequest) -> dict[str, Any]:
    try:
        return _manager().submit_initial([item.model_dump() for item in request.selections], request.generate_n, version=request.version)
    except VersionConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/augmentation-jobs", status_code=202)
def create_augmentation_job(
    file: UploadFile = File(...),
    generate_n: int = Form(default=10, ge=1, le=20),
) -> dict[str, Any]:
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in ALLOWED_WORKBOOK_SUFFIXES:
        raise HTTPException(status_code=422, detail="种子文件只支持 .xlsx 或 .xlsm 文件")
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(prefix="task-augmentation-input-", suffix=suffix, delete=False) as temporary:
            temporary_path = Path(temporary.name)
            shutil.copyfileobj(file.file, temporary)
        return _manager().submit_augmentation(temporary_path, file.filename or "input.xlsx", generate_n)
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    finally:
        if temporary_path:
            temporary_path.unlink(missing_ok=True)


@router.get("/jobs")
def task_generation_jobs() -> dict[str, Any]:
    return {"jobs": _manager().list_jobs()}


@router.get("/jobs/{job_id}")
def task_generation_job(job_id: str) -> dict[str, Any]:
    value = _manager().get(job_id)
    if value is None:
        raise HTTPException(status_code=404, detail="任务生成作业不存在")
    return value


@router.get("/jobs/{job_id}/results")
def task_generation_results(job_id: str) -> dict[str, Any]:
    job = _manager().get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="任务生成作业不存在")
    return {"results": _manager().results(job_id), "errors": job.get("errors", [])}


@router.patch("/jobs/{job_id}/results/{result_id}")
def patch_task_generation_result(job_id: str, result_id: str, request: ResultPatchRequest) -> dict[str, Any]:
    patch = request.model_dump(exclude_none=True)
    if not patch:
        raise HTTPException(status_code=422, detail="没有可更新的字段")
    try:
        return {"result": _manager().patch_result(job_id, result_id, patch)}
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="结果行不存在") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/jobs/{job_id}/export")
def export_task_generation(job_id: str) -> dict[str, Any]:
    try:
        return _manager().export(job_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/jobs/{job_id}/exports/{filename:path}")
def download_task_generation_export(job_id: str, filename: str) -> FileResponse:
    try:
        path = _manager().download(job_id, filename)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return FileResponse(path, filename=path.name, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
