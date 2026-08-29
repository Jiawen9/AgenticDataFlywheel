"""FastAPI routes for the isolated task-generation workbench."""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse

from . import service, store
from .jobs import TaskGenerationJobManager
from .schemas import KnowledgeJobRequest, SceneMatchJobRequest, VariantJobRequest


router = APIRouter(prefix="/api/task-generation", tags=["task-generation"])
job_manager = TaskGenerationJobManager()


@router.get("/source")
def task_generation_source() -> dict[str, Any]:
    return service.source_status()


@router.get("/jobs")
def task_generation_jobs() -> dict[str, Any]:
    return {"jobs": job_manager.list_jobs()}


@router.get("/jobs/{job_id}")
def task_generation_job(job_id: str) -> dict[str, Any]:
    try:
        job = job_manager.get(job_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if job is None:
        raise HTTPException(status_code=404, detail="任务生成作业不存在")
    return job


@router.get("/jobs/{job_id}/preview")
def task_generation_preview(job_id: str) -> dict[str, Any]:
    try:
        job = job_manager.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="任务生成作业不存在")
        return service.preview(job)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except (OSError, TypeError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=422, detail=f"结果预览失败：{exc}") from exc


@router.get("/jobs/{job_id}/download")
def task_generation_download(
    job_id: str,
    format: str = Query(default="json", pattern="^(json|xlsx)$"),
) -> FileResponse:
    try:
        job = job_manager.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="任务生成作业不存在")
        filename = "scene_matches.json" if job.get("kind") == "scene_match" else "result.json"
        if format == "xlsx":
            filename = "scene_matches.xlsx" if job.get("kind") == "scene_match" else "result.xlsx"
        path = store.artifact_path(job_id, filename)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not path.is_file():
        raise HTTPException(status_code=404, detail="结果文件尚未生成")
    media_type = (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        if format == "xlsx"
        else "application/json"
    )
    return FileResponse(path, media_type=media_type, filename=f"task_generation_{job_id}.{format}")


@router.post("/knowledge/jobs", status_code=202)
def create_knowledge_job(request: KnowledgeJobRequest) -> dict[str, Any]:
    params = request.model_dump(exclude_none=True)
    try:
        return job_manager.submit(
            "knowledge",
            {"params": params},
            lambda job_id: service.knowledge_runner(job_id, params),
        )
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/flywheel/inputs", status_code=201)
async def upload_flywheel_input(file: UploadFile = File(...)) -> dict[str, Any]:
    filename = file.filename or ""
    content = await file.read(service.MAX_UPLOAD_BYTES + 1)
    try:
        metadata = service.create_input(filename, content)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if metadata.get("status") != "ready":
        raise HTTPException(status_code=422, detail=metadata.get("error", "上传文件不可用"))
    return metadata


@router.post("/flywheel/scene-match/jobs", status_code=202)
def create_scene_match_job(request: SceneMatchJobRequest) -> dict[str, Any]:
    try:
        metadata = store.load_input(request.input_id)
        if not metadata or metadata.get("status") != "ready":
            raise HTTPException(status_code=404, detail="上传文件不存在或不可用")
        return job_manager.submit(
            "scene_match",
            {"input_id": request.input_id},
            lambda job_id: service.scene_match_runner(job_id, request.input_id),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/flywheel/variant/jobs", status_code=202)
def create_variant_job(request: VariantJobRequest) -> dict[str, Any]:
    try:
        source_job = job_manager.get(request.scene_match_job_id)
        if source_job is None or source_job.get("kind") != "scene_match":
            raise HTTPException(status_code=404, detail="场景匹配作业不存在")
        if source_job.get("status") != "succeeded":
            raise HTTPException(status_code=409, detail="请先完成场景匹配")
        return job_manager.submit(
            "variant",
            {
                "input_id": source_job.get("input_id"),
                "scene_match_job_id": request.scene_match_job_id,
                "params": {"generate_n": request.generate_n},
            },
            lambda job_id: service.variant_runner(job_id, request.scene_match_job_id, request.generate_n),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
