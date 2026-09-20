"""FastAPI application for trajectory collection, tree building, and inspection."""

from __future__ import annotations

import logging

import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .trajectory_data import (
    ANNOTATED_XLSX,
    PROJECT_ROOT,
    discover_tasks,
    find_tree_run,
    list_tree_runs,
    load_annotated_trajectory,
    load_annotated_trajectories,
    resolve_image_asset,
    resolve_tree_run_dir,
    task_summaries,
    trajectory_summaries,
    update_action_bbox,
)
from .quality_data import quality_manifest, quality_task, rubric_ready
from .quality_jobs import QualityJobManager
from .tree_build_jobs import TreeBuildJobManager
from .trajectory_correction.router import configure_cot_job_manager, router as correction_router
from .trajectory_correction.cot_jobs import CotJobManager
from .task_generation.jobs import TaskGenerationJobManager
from .task_generation.router import configure_job_manager, router as task_generation_router
from .data_publishing.router import router as data_publishing_router
from .data_registry_api import router as data_registry_router
from .batch_api import router as batch_results_router, task_status_summaries
from .data_store import ArtifactStore, DATA_ROOT, RecordStore
from .batch_results import current_tree_payload, current_tree_batch, recover_pending_batch_results
from .batch_lifecycle import install_lifecycle_handlers, ensure_batch_active, is_batch_active
from .phone_factory import router as phone_factory_router
from .stage_artifacts import read_workbook_payload
from .preprocessing_jobs import PreprocessingJobManager
from .preprocessing_router import router as preprocessing_router, configure_preprocessing_manager
from .trajectory_context import AnnotationVersionConflict
from .training_data_overview.router import router as training_overview_router, get_manager as get_overview_manager


FRONTEND_DIST = PROJECT_ROOT / "frontend" / "dist"


def _observation_index(workbook_path: Path, task_id: str) -> dict[tuple[str, int], str]:
    if not workbook_path.is_file():
        raise FileNotFoundError("质检输入 JSON 不存在")
    result = {}
    for row in read_workbook_payload(workbook_path)["sheets"].get("Steps", []):
        if not {"trajectory_id", "task_id", "step_id", "observation"}.issubset(row):
            continue
        if str(row.get("task_id")) != task_id:
            continue
        observation = str(row.get("observation") or "").strip()
        if observation:
            try:
                step_id = int(row["step_id"])
            except (ValueError, TypeError):
                continue
            result[(str(row["trajectory_id"]), step_id)] = observation
    return result


def _attach_tree_observations(tree: dict[str, Any], observations: dict[tuple[str, int], str]) -> None:
    stack = [tree]
    while stack:
        node = stack.pop()
        for occurrence in node.get("occurrences", []):
            key = (str(occurrence.get("trajectory", "")), int(occurrence.get("step", 0)))
            if key in observations:
                occurrence["observation"] = observations[key]
        stack.extend(node.get("children", []))
    for trajectory in tree.get("source_trajectories", []):
        trajectory_id = str(trajectory.get("trajectory", ""))
        for step in trajectory.get("steps", []):
            key = (trajectory_id, int(step.get("step", 0)))
            if key in observations:
                step["observation"] = observations[key]


class TreeBuildRequest(BaseModel):
    task_ids: list[str]
    batch_id: str | None = None
    annotation_version: str | None = None


class QualityJobRequest(BaseModel):
    task_ids: list[str]
    batch_id: str | None = None
    run_id: str | None = None


class BBoxUpdateRequest(BaseModel):
    excel_row: int
    bbox: list[int]
    action: Optional[dict[str, Any]] = None
    batch_id: str | None = None
    annotation_version: str | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    from .batch_operations import recover_operations
    recover_operations(DATA_ROOT)
    recover_pending_batch_results(DATA_ROOT)
    import asyncio
    from contextlib import suppress
    from .data_publishing.router import recover_dataset_imports
    await asyncio.to_thread(recover_dataset_imports)
    manager = get_overview_manager()
    manager.start()

    async def clean_expired_imports():
        while True:
            await asyncio.sleep(3600)
            try:
                await asyncio.to_thread(recover_dataset_imports)
            except Exception:
                logging.getLogger(__name__).exception("外部表格暂存清理失败")

    cleanup = asyncio.create_task(clean_expired_imports())
    try:
        yield
    finally:
        cleanup.cancel()
        with suppress(asyncio.CancelledError):
            await cleanup
        manager.close()


app = FastAPI(title="Agentic Data Flywheel", version="1.0.0", lifespan=lifespan)
install_lifecycle_handlers(app)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)
app.include_router(correction_router)
app.include_router(task_generation_router)
app.include_router(data_publishing_router)
app.include_router(data_registry_router)
app.include_router(batch_results_router)
app.include_router(phone_factory_router)
app.include_router(preprocessing_router)
app.include_router(training_overview_router)
model_job_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="model-job")
job_manager = TreeBuildJobManager(executor=model_job_executor)
quality_job_manager = QualityJobManager(executor=model_job_executor)
cot_job_manager = CotJobManager(executor=model_job_executor)
task_generation_job_manager = TaskGenerationJobManager(executor=model_job_executor)
configure_job_manager(task_generation_job_manager)
configure_cot_job_manager(cot_job_manager)
preprocessing_job_manager = PreprocessingJobManager(executor=model_job_executor)
configure_preprocessing_manager(preprocessing_job_manager)


def _batch_options(batch_id: str | None, annotation_version: str | None) -> dict:
    if batch_id:
        ensure_batch_active(batch_id, DATA_ROOT)
    if annotation_version and not batch_id:
        raise HTTPException(status_code=422, detail="指定标框版本时必须提供批次号")
    return {"batch_id": batch_id, "annotation_version": annotation_version} if batch_id else {}


def _batch_read(operation):
    try:
        return operation()
    except FileNotFoundError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (ValueError, OSError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/tasks")
def get_tasks(batch_id: str | None = None, annotation_version: str | None = None) -> dict[str, Any]:
    options = _batch_options(batch_id, annotation_version)
    return {"tasks": _batch_read(lambda: task_status_summaries(batch_id, annotation_version)
                              if batch_id else task_summaries(**options))}


@app.get("/api/tasks/{task_id}/trajectories")
def get_task_trajectories(task_id: str, batch_id: str | None = None,
                          annotation_version: str | None = None) -> dict[str, Any]:
    options = _batch_options(batch_id, annotation_version)
    tasks = {item["task_id"]: item for item in _batch_read(lambda: task_summaries(**options))}
    if task_id not in tasks:
        raise HTTPException(status_code=404, detail="任务不存在")
    trajectories = _batch_read(lambda: trajectory_summaries(task_id, **options))
    return {"task": tasks[task_id], "trajectories": trajectories}


@app.get("/api/tasks/{task_id}/trajectories/{trajectory_id}")
def get_task_trajectory(task_id: str, trajectory_id: str, batch_id: str | None = None,
                       annotation_version: str | None = None) -> dict[str, Any]:
    options = _batch_options(batch_id, annotation_version)
    tasks = {item["task_id"]: item for item in _batch_read(lambda: task_summaries(**options))}
    if task_id not in tasks:
        raise HTTPException(status_code=404, detail="任务不存在")
    trajectory = _batch_read(lambda: load_annotated_trajectory(task_id, trajectory_id, **options))
    if trajectory is None:
        raise HTTPException(status_code=404, detail="轨迹不存在")
    return {"trajectory": trajectory}


@app.patch("/api/tasks/{task_id}/trajectories/{trajectory_id}/steps/{step}/bbox")
def patch_step_bbox(
    task_id: str,
    trajectory_id: str,
    step: int,
    request: BBoxUpdateRequest,
) -> dict[str, Any]:
    if len(request.bbox) != 4:
        raise HTTPException(status_code=422, detail="bbox 必须包含四个整数")
    _batch_options(request.batch_id, request.annotation_version)
    if request.batch_id and not request.annotation_version:
        raise HTTPException(status_code=422, detail="编辑标框必须提供当前版本")
    try:
        actions_box = update_action_bbox(
            task_id,
            trajectory_id,
            step,
            request.excel_row,
            tuple(request.bbox),
            action_override=request.action,
            **({"batch_id": request.batch_id, "expected_annotation_version": request.annotation_version}
               if request.batch_id else {}),
        )
    except AnnotationVersionConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (OSError, PermissionError) as exc:
        raise HTTPException(status_code=409, detail=f"无法更新 Excel，请确认文件未被占用：{exc}") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return actions_box if isinstance(actions_box, dict) else {"actions_box": actions_box}


@app.post("/api/tree-builds", status_code=202)
def create_tree_build(request: TreeBuildRequest) -> dict[str, Any]:
    unique = list(dict.fromkeys(value.strip() for value in request.task_ids if value.strip()))
    if not unique:
        raise HTTPException(status_code=422, detail="至少选择一个任务")
    options = _batch_options(request.batch_id, request.annotation_version)
    tasks = {item["task_id"]: item for item in _batch_read(lambda: task_summaries(**options))}
    unknown = [task_id for task_id in unique if task_id not in tasks]
    if unknown:
        raise HTTPException(status_code=404, detail=f"任务不存在：{', '.join(unknown)}")
    unprocessed = [task_id for task_id in unique if not tasks[task_id]["annotated"]]
    if unprocessed:
        raise HTTPException(
            status_code=409,
            detail=f"任务尚未完成轨迹预处理：{', '.join(unprocessed)}",
        )
    return _batch_read(lambda: job_manager.submit(unique, **options))


@app.get("/api/tree-builds")
def get_tree_builds(batch_id: str | None = None) -> dict[str, Any]:
    return {"jobs": job_manager.list_jobs(batch_id=batch_id)}


@app.get("/api/tree-builds/{job_id}")
def get_tree_build(job_id: str) -> dict[str, Any]:
    payload = job_manager.get(job_id)
    if payload is None:
        raise HTTPException(status_code=404, detail="建树作业不存在")
    return payload


@app.post("/api/quality-jobs", status_code=202)
def create_quality_job(request: QualityJobRequest) -> dict[str, Any]:
    unique = list(dict.fromkeys(value.strip() for value in request.task_ids if value.strip()))
    if not unique:
        raise HTTPException(status_code=422, detail="至少选择一个任务")
    if request.batch_id:
        return _batch_read(lambda: quality_job_manager.submit(batch_id=request.batch_id, task_ids=unique))
    if not request.run_id:
        raise HTTPException(status_code=422, detail="请选择业务批次")
    manifest = find_tree_run(request.run_id)
    if manifest and manifest.get("batch_id"):
        ensure_batch_active(manifest["batch_id"], DATA_ROOT)
    if manifest is None:
        expired = RecordStore(DATA_ROOT).get("batch_run_aliases", request.run_id)
        if expired:
            ensure_batch_active(expired["batch_id"], DATA_ROOT)
        ensure_batch_active(request.run_id, DATA_ROOT)
        raise HTTPException(status_code=410 if expired else 404, detail="该运行已失效，请查看批次当前结果" if expired else "任务集不存在")
    available = {str(item.get("task_id")) for item in manifest.get("tasks", [])}
    if any(task_id not in available for task_id in unique):
        raise HTTPException(status_code=404, detail="任务不在该批次当前结果中")
    if manifest.get("batch_id") and current_tree_batch(manifest["batch_id"]):
        return _batch_read(lambda: quality_job_manager.submit(batch_id=manifest["batch_id"], task_ids=unique))
    return quality_job_manager.submit(request.run_id, unique)


@app.get("/api/quality-jobs")
def get_quality_jobs() -> dict[str, Any]:
    return {"jobs": quality_job_manager.list_jobs()}


@app.get("/api/quality-jobs/{job_id}")
def get_quality_job(job_id: str) -> dict[str, Any]:
    payload = quality_job_manager.get(job_id)
    if payload is None:
        raise HTTPException(status_code=404, detail="质检作业不存在")
    return payload


@app.get("/api/tree-runs")
def get_tree_runs() -> dict[str, Any]:
    return {"runs": list_tree_runs()}


@app.get("/api/tree-runs/{run_id}")
def get_tree_run(run_id: str) -> dict[str, Any]:
    manifest = find_tree_run(run_id)
    if manifest and manifest.get("batch_id"):
        ensure_batch_active(manifest["batch_id"], DATA_ROOT)
    if manifest is None:
        expired = RecordStore(DATA_ROOT).get("batch_run_aliases", run_id)
        if expired:
            ensure_batch_active(expired["batch_id"], DATA_ROOT)
        ensure_batch_active(run_id, DATA_ROOT)
        raise HTTPException(status_code=410 if expired else 404, detail="该运行已失效，请查看批次当前结果" if expired else "任务集不存在")
    return manifest


@app.get("/api/tree-runs/{run_id}/quality")
def get_run_quality(run_id: str) -> dict[str, Any]:
    tree_manifest = find_tree_run(run_id)
    if tree_manifest and tree_manifest.get("batch_id"):
        ensure_batch_active(tree_manifest["batch_id"], DATA_ROOT)
    if tree_manifest is None:
        expired = RecordStore(DATA_ROOT).get("batch_run_aliases", run_id)
        if expired:
            ensure_batch_active(expired["batch_id"], DATA_ROOT)
        ensure_batch_active(run_id, DATA_ROOT)
        raise HTTPException(status_code=410 if expired else 404, detail="该运行已失效，请查看批次当前结果" if expired else "任务集不存在")
    published = quality_manifest(run_id)
    completed = {str(item.get("task_id")): item for item in published.get("tasks", [])}
    tasks = []
    for task in tree_manifest.get("tasks", []):
        task_id = str(task.get("task_id", ""))
        tasks.append({
            "task_id": task_id,
            "status": "succeeded" if task_id in completed else "unreviewed",
            "rubric_ready": rubric_ready(task_id),
            **completed.get(task_id, {}),
        })
    return {"run_id": run_id, "updated_at": published.get("updated_at"), "tasks": tasks}


@app.get("/api/tree-runs/{run_id}/tasks/{task_id}/quality")
def get_task_quality(run_id: str, task_id: str) -> dict[str, Any]:
    manifest = find_tree_run(run_id)
    if manifest and manifest.get("batch_id"):
        ensure_batch_active(manifest["batch_id"], DATA_ROOT)
    if manifest is None:
        expired = RecordStore(DATA_ROOT).get("batch_run_aliases", run_id)
        if expired:
            ensure_batch_active(expired["batch_id"], DATA_ROOT)
        ensure_batch_active(run_id, DATA_ROOT)
        raise HTTPException(status_code=410 if expired else 404, detail="该运行已失效，请查看批次当前结果" if expired else "任务集不存在")
    available = {str(item.get("task_id")) for item in manifest.get("tasks", [])}
    if task_id not in available:
        raise HTTPException(status_code=404, detail="任务不在该任务集中")
    result = quality_task(run_id, task_id)
    if result is None:
        raise HTTPException(status_code=404, detail="该任务还没有质检结果")
    return result


@app.get("/api/tree-runs/{run_id}/tasks/{task_id}/tree")
def get_task_tree(run_id: str, task_id: str) -> JSONResponse:
    manifest = find_tree_run(run_id)
    if manifest and manifest.get("batch_id"):
        ensure_batch_active(manifest["batch_id"], DATA_ROOT)
    if manifest is None:
        expired = RecordStore(DATA_ROOT).get("batch_run_aliases", run_id)
        if expired:
            ensure_batch_active(expired["batch_id"], DATA_ROOT)
        ensure_batch_active(run_id, DATA_ROOT)
        raise HTTPException(status_code=410 if expired else 404, detail="该运行已失效，请查看批次当前结果" if expired else "任务集不存在")
    if manifest.get("batch_id"):
        value = current_tree_payload(manifest["batch_id"])
        if value.get("trees"):
            tree = value["trees"].get(task_id)
            if tree is None:
                raise HTTPException(status_code=409, detail="任务尚无有效建树结果")
            return JSONResponse(tree)
    task = next((item for item in manifest.get("tasks", []) if item.get("task_id") == task_id), None)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不在该任务集中")
    run_dir = resolve_tree_run_dir(run_id).resolve()
    tree_path = (run_dir / str(task.get("tree_file", ""))).resolve()
    try:
        tree_path.relative_to(run_dir)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="无效的树文件路径") from exc
    if not tree_path.is_file():
        raise HTTPException(status_code=404, detail="轨迹树文件缺失")
    tree = json.loads(tree_path.read_text(encoding="utf-8"))
    try:
        name = manifest.get("quality_input_json")
        if not isinstance(name, str) or not name:
            raise ValueError("建树批次没有登记质检输入 JSON")
        structured = (run_dir / name).resolve()
        structured.relative_to(run_dir)
        if structured.suffix.lower() != ".json":
            raise ValueError("质检输入必须为 JSON")
        _attach_tree_observations(tree, _observation_index(structured, task_id))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise HTTPException(status_code=409, detail=f"质检输入 JSON 缺失或损坏：{exc}") from exc
    return JSONResponse(tree)


@app.get("/api/assets/{relative_path:path}")
def get_asset(relative_path: str, batch_id: str | None = None,
              annotation_version: str | None = None) -> FileResponse:
    try:
        path = resolve_image_asset(relative_path, **_batch_options(batch_id, annotation_version))
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="图片不存在") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return FileResponse(path, headers={"Cache-Control": "public, max-age=3600"})


@app.api_route(
    "/api/{unmatched_path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    include_in_schema=False,
)
def unmatched_api(unmatched_path: str) -> None:
    raise HTTPException(status_code=404, detail=f"API 不存在：/api/{unmatched_path}")


if FRONTEND_DIST.is_dir():
    assets = FRONTEND_DIST / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=assets), name="frontend-assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    def frontend(full_path: str) -> FileResponse:
        candidate = (FRONTEND_DIST / full_path).resolve()
        try:
            candidate.relative_to(FRONTEND_DIST.resolve())
        except ValueError:
            candidate = FRONTEND_DIST / "index.html"
        if candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(FRONTEND_DIST / "index.html")
else:
    @app.get("/", include_in_schema=False)
    def api_root() -> dict[str, str]:
        return {"message": "Frontend has not been built. Run npm run build --prefix frontend."}
