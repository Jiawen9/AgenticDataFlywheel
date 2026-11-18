from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, File, Form, HTTPException, Query, Response, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

from .collection_batches import CollectionBatchDetail, CollectionBatchList
from .collection_input import CollectionInput, CollectionInputError
from .constants import ALLOWED_WORKBOOK_SUFFIXES, KNOWLEDGE_BASE_FILES
from .jobs import AugmentationStateError, TaskGenerationJobManager
from .knowledge_base import list_knowledge_bases, replace_knowledge_base, tree_payload
from .tree_store import VersionConflict, current_root, read_tree, save_tree, write_scene_workbook
from .read_models import result_page
from .resource_views import resource_view, invalidate_resource_view
from ..pipelines import PipelineError


class TaskSelection(BaseModel):
    node_id: str
    apps: list[str] = Field(min_length=1)


class TaskGenerationRequest(BaseModel):
    version: str
    selections: list[TaskSelection] = Field(min_length=1)
    generate_n: int = Field(default=5, ge=1, le=20)
    request_id: str | None = Field(default=None, min_length=1, max_length=128)
    pipeline_id: str | None = None


class BadCaseGenerationRequest(BaseModel):
    feedback_id: str
    generate_n: int = Field(default=10, ge=1, le=20)
    request_id: str = Field(min_length=1, max_length=128)
    pipeline_id: str | None = None
    auto_start: bool = False


class TreeUpdateRequest(BaseModel):
    base_version: str
    scenes: list[dict[str, Any]]


class ResultPatchRequest(BaseModel):
    task: Optional[str] = Field(default=None, max_length=4000)
    deleted: Optional[bool] = None


router = APIRouter(prefix="/api/task-generation", tags=["task-generation"])
manager: TaskGenerationJobManager | None = None
pipeline_manager = None


def configure_pipeline_manager(value):
    global pipeline_manager
    pipeline_manager = value


def _binding(pipeline_id, kind, request_id, feedback_id=None):
    if not pipeline_id:
        return None
    if pipeline_manager is None:
        raise PipelineError("Pipeline 服务尚未启动", 503)
    pipeline = pipeline_manager.validate_generation_binding(pipeline_id, kind, request_id=request_id)
    if pipeline.get("feedback_id") != feedback_id:
        raise PipelineError("扩增输入与 Pipeline 冻结的回流选择不一致")
    return lambda job: pipeline_manager.bind_generation(pipeline_id, job)


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
def upload_knowledge_base(kind: str, file: UploadFile = File(...), base_version: Optional[str] = Form(default=None)) -> dict[str, Any]:
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
def scene_tree(view: str | None = None, refresh: bool = False) -> dict[str, Any]:
    if view == "summary":
        if refresh:
            invalidate_resource_view(_manager().knowledge_base_dir)
        return resource_view(_manager().knowledge_base_dir)
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
    temporary_path: Path | None = None
    try:
        source = current_root(_manager().knowledge_base_dir)
        path = source / KNOWLEDGE_BASE_FILES["scene_tree"]
        if not path.is_file():
            raise FileNotFoundError("当前版本没有场景树 Excel")
        # Render the current contract without rewriting an immutable version
        # whose workbook may still contain retired editor-only columns.
        with tempfile.NamedTemporaryFile(prefix="scene-tree-export-", suffix=".xlsx", delete=False) as temporary:
            temporary_path = Path(temporary.name)
        shutil.copy2(path, temporary_path)
        write_scene_workbook(temporary_path, read_tree(source)["scenes"])
    except (OSError, ValueError) as exc:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return FileResponse(
        temporary_path,
        filename=path.name,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        background=BackgroundTask(temporary_path.unlink, missing_ok=True),
    )


@router.post("/jobs", status_code=202)
def create_task_generation_job(request: TaskGenerationRequest) -> dict[str, Any]:
    try:
        binding = _binding(request.pipeline_id, "initial", request.request_id)
        return _manager().submit_initial([item.model_dump() for item in request.selections], request.generate_n, version=request.version,
            request_id=request.request_id, pipeline_id=request.pipeline_id, on_created=binding)
    except PipelineError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
    except AugmentationStateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except VersionConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/augmentation-jobs", status_code=202)
def create_augmentation_job(
    file: UploadFile = File(...),
    generate_n: int = Form(default=10, ge=1, le=20),
    auto_start: bool = Form(default=True),
    request_id: str | None = Form(default=None),
    pipeline_id: str | None = Form(default=None),
) -> dict[str, Any]:
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in ALLOWED_WORKBOOK_SUFFIXES:
        raise HTTPException(status_code=422, detail="种子文件只支持 .xlsx 或 .xlsm 文件")
    temporary_path: Path | None = None
    try:
        binding = _binding(pipeline_id, "augmentation", request_id)
        with tempfile.NamedTemporaryFile(prefix="task-augmentation-input-", suffix=suffix, delete=False) as temporary:
            temporary_path = Path(temporary.name)
            shutil.copyfileobj(file.file, temporary)
        return _manager().submit_augmentation(temporary_path, file.filename or "input.xlsx", generate_n, auto_start=auto_start,
            request_id=request_id, pipeline_id=pipeline_id, on_created=binding)
    except PipelineError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
    except AugmentationStateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    finally:
        if temporary_path:
            temporary_path.unlink(missing_ok=True)


@router.post("/badcase-jobs", status_code=202)
def create_badcase_job(request: BadCaseGenerationRequest):
    from ..badcases import BadCaseError
    try:
        binding = _binding(request.pipeline_id, "augmentation", request.request_id, request.feedback_id)
        return _manager().submit_badcase_seeds(request.feedback_id, request.generate_n, auto_start=request.auto_start,
            request_id=request.request_id, pipeline_id=request.pipeline_id, on_created=binding)
    except (BadCaseError, PipelineError) as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
    except AugmentationStateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/jobs")
def task_generation_jobs(view: str | None = None, offset: int = Query(0, ge=0), limit: int = Query(10, ge=1, le=50),
                         search: str = "", status: str = "", kind: str = "") -> dict[str, Any]:
    if view == "summary":
        return _manager().store.summary_page("task_generation.jobs", offset=offset, limit=limit, q=search, status=status, kind=kind)
    return {"jobs": _manager().list_jobs()}


@router.get("/jobs/{job_id}")
def task_generation_job(job_id: str) -> dict[str, Any]:
    value = _manager().get(job_id)
    if value is None:
        raise HTTPException(status_code=404, detail="任务生成作业不存在")
    return value


@router.get("/jobs/{job_id}/results")
def task_generation_results(job_id: str, view: str | None = None, offset: int = Query(0, ge=0),
                            limit: int = Query(10, ge=1, le=50), app: str = "", task_type: str = "", dependency: str = "",
                            show_deleted: bool = False, node_id: str = "", seed_id: str = "", search: str = "") -> dict[str, Any]:
    if view == "groups":
        value = result_page(_manager().store, job_id, offset=offset, limit=limit, app=app, task_type=task_type,
                            dependency=dependency, show_deleted=show_deleted, node_id=node_id, seed_id=seed_id, search=search)
        if value is None:
            raise HTTPException(status_code=404, detail="任务生成作业不存在")
        return value
    job = _manager().get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="任务生成作业不存在")
    return {"results": _manager().results(job_id), "errors": job.get("errors", [])}


@router.post("/jobs/{job_id}/snapshot", status_code=201)
def save_task_snapshot(job_id: str) -> dict[str, Any]:
    """Retry JSON/Excel serialization from saved data without calling a model."""
    try:
        return _manager().snapshot(job_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (ValueError, OSError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get(
    "/jobs/{job_id}/collection-input",
    response_model=CollectionInput,
    summary="读取生成侧当前保存的统一任务视图",
    description="返回 succeeded/partial 作业全部未删除任务，包含最新人工修改。仅提供数据，不推断审核或执行许可；采集端负责保存执行快照。",
    responses={
        200: {"description": "当前任务视图", "headers": {"Cache-Control": {"schema": {"type": "string", "const": "no-store"}}}},
        404: {"description": "作业不存在"},
        409: {"description": "作业尚不可读取，或保存结果的字段/依赖引用无效"},
    },
)
def task_generation_collection_input(job_id: str, response: Response) -> dict[str, Any]:
    headers = {"Cache-Control": "no-store"}
    response.headers.update(headers)
    try:
        return _manager().collection_input(job_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc), headers=headers) from exc
    except CollectionInputError as exc:
        raise HTTPException(status_code=409, detail=str(exc), headers=headers) from exc


@router.post("/jobs/{job_id}/collection-batch", response_model=CollectionBatchDetail, status_code=201,
             summary="冻结并提交当前生成作业的采集批次",
             responses={200: {"description": "返回已提交的同一批次"}, 404: {"description": "作业不存在"},
                        409: {"description": "作业结果无法提交，或批次文件不可用"}})
def submit_collection_batch(job_id: str, response: Response) -> dict[str, Any]:
    headers = {"Cache-Control": "no-store"}
    response.headers.update(headers)
    try:
        batch, created = _manager().submit_collection_batch(job_id)
        response.status_code = 201 if created else 200
        return batch
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc), headers=headers) from exc
    except CollectionInputError as exc:
        raise HTTPException(status_code=409, detail=str(exc), headers=headers) from exc


@router.get("/collection-batches", response_model=CollectionBatchList, summary="列出已提交采集批次")
def collection_batches(response: Response, job_id: str | None = None) -> dict[str, Any]:
    headers = {"Cache-Control": "no-store"}
    response.headers.update(headers)
    try:
        return {"batches": _manager().collection_batches(job_id)}
    except CollectionInputError as exc:
        raise HTTPException(status_code=409, detail=str(exc), headers=headers) from exc


@router.get("/collection-batches/{batch_id}", response_model=CollectionBatchDetail,
            summary="读取采集批次及提交时冻结的完整任务")
def collection_batch(batch_id: str, response: Response) -> dict[str, Any]:
    headers = {"Cache-Control": "no-store"}
    response.headers.update(headers)
    try:
        return _manager().collection_batch(batch_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc), headers=headers) from exc
    except CollectionInputError as exc:
        raise HTTPException(status_code=409, detail=str(exc), headers=headers) from exc


@router.get("/collection-batches/{batch_id}/workbook", summary="下载冻结的 17 列采集表")
def collection_batch_workbook(batch_id: str) -> FileResponse:
    headers = {"Cache-Control": "no-store"}
    try:
        path = _manager().collection_batch_workbook(batch_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc), headers=headers) from exc
    except CollectionInputError as exc:
        raise HTTPException(status_code=409, detail=str(exc), headers=headers) from exc
    return FileResponse(path, filename=path.name, headers=headers,
                        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@router.get("/jobs/{job_id}/augmentation-preview")
def augmentation_preview(job_id: str, include_tree: bool = True) -> dict[str, Any]:
    try:
        return _manager().augmentation_preview(job_id, include_tree=include_tree)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (AugmentationStateError, ValueError, OSError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/jobs/{job_id}/start-augmentation", status_code=202)
def start_augmentation(job_id: str) -> dict[str, Any]:
    try:
        return _manager().start_augmentation(job_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (AugmentationStateError, ValueError, OSError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


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
    except AugmentationStateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
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
