"""FastAPI routes for the isolated trajectory-correction workbench."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse

from .schemas import CreateCotJobRequest, CreateSessionRequest, ExportStateRequest, RowPatchRequest
from .quality_selection import (
    QualitySelectionError,
    correction_batches,
    top1_recommendation,
)
from .service import (
    create_session,
    download_export,
    export_dataset_session,
    export_session,
    get_group,
    get_groups,
    get_session,
    patch_group_export,
    patch_row,
    published_tree_run_ids,
    session_asset,
    sessions,
    get_cot,
)
from .cot_jobs import CotJobManager


router = APIRouter(prefix="/api/correction", tags=["trajectory-correction"])
_cot_job_manager: CotJobManager | None = None


def configure_cot_job_manager(manager: CotJobManager) -> None:
    global _cot_job_manager
    _cot_job_manager = manager


@router.get("/recommendation")
def correction_recommendation(
    tree_run_id: Optional[str] = Query(default=None),
) -> dict[str, object]:
    if tree_run_id and tree_run_id in published_tree_run_ids():
        raise HTTPException(status_code=409, detail="该质检批次的纠偏会话已经发布")
    try:
        return top1_recommendation(tree_run_id)
    except QualitySelectionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/batches")
def correction_quality_batches() -> dict[str, object]:
    payload = correction_batches()
    published = published_tree_run_ids()
    batches = [item for item in payload.get("batches", []) if item.get("tree_run_id") not in published]
    default_run_id = str(batches[0].get("tree_run_id", "")) if batches else ""
    for item in batches:
        item["is_default"] = item.get("tree_run_id") == default_run_id
    return {"default_tree_run_id": default_run_id or None, "batches": batches}


@router.get("/sessions")
def correction_sessions() -> dict[str, object]:
    return {"sessions": sessions()}


@router.get("/cot-jobs")
def correction_cot_jobs() -> dict[str, object]:
    if _cot_job_manager is None:
        return {"jobs": []}
    return {"jobs": _cot_job_manager.list_jobs()}


@router.post("/cot-jobs", status_code=202)
def create_correction_cot_job(request: CreateCotJobRequest) -> dict[str, object]:
    if _cot_job_manager is None:
        raise HTTPException(status_code=503, detail="COT 作业服务尚未启动")
    try:
        return _cot_job_manager.submit(
            request.session_id,
            request.group_ids,
            request.row_ids,
            generate_bbox=request.generate_bbox,
            force_overwrite=request.force_overwrite,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (OSError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/cot-jobs/{job_id}")
def get_correction_cot_job(job_id: str) -> dict[str, object]:
    if _cot_job_manager is None:
        raise HTTPException(status_code=503, detail="COT 作业服务尚未启动")
    payload = _cot_job_manager.get(job_id)
    if payload is None:
        raise HTTPException(status_code=404, detail="COT 作业不存在")
    return payload


@router.post("/sessions", status_code=201)
def create_correction_session(request: CreateSessionRequest) -> dict[str, object]:
    try:
        return {"session": create_session(request.tree_run_id)}
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except QualitySelectionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (OSError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/sessions/{session_id}")
def correction_session(session_id: str) -> dict[str, object]:
    try:
        return {"session": get_session(session_id)}
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (OSError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/sessions/{session_id}/cot")
def correction_session_cot(session_id: str) -> dict[str, object]:
    try:
        return get_cot(session_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (OSError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/sessions/{session_id}/tasks")
def correction_tasks(session_id: str) -> dict[str, object]:
    try:
        return {"groups": get_groups(session_id)}
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (OSError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/sessions/{session_id}/tasks/{group_id}")
def correction_task(session_id: str, group_id: str) -> dict[str, object]:
    try:
        return {"group": get_group(session_id, group_id)}
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="修正任务不存在") from exc
    except (OSError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.patch("/sessions/{session_id}/tasks/{group_id}/export")
def correction_task_export(
    session_id: str,
    group_id: str,
    request: ExportStateRequest,
) -> dict[str, object]:
    try:
        return {"group": patch_group_export(session_id, group_id, request.export)}
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="修正任务不存在") from exc
    except (OSError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.patch("/sessions/{session_id}/rows/{excel_row}")
def correction_row_patch(
    session_id: str,
    excel_row: int,
    request: RowPatchRequest,
) -> dict[str, object]:
    try:
        return patch_row(session_id, excel_row, request.model_dump(exclude_none=True))
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Excel 行不存在") from exc
    except (OSError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/sessions/{session_id}/assets/{image_path:path}")
def correction_asset(session_id: str, image_path: str) -> FileResponse:
    try:
        path = session_asset(session_id, image_path)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="图片不存在") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return FileResponse(path, headers={"Cache-Control": "public, max-age=3600"})


@router.post("/sessions/{session_id}/export")
def correction_export(session_id: str) -> dict[str, object]:
    try:
        return export_session(session_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (OSError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/sessions/{session_id}/dataset-export")
def correction_dataset_export(session_id: str) -> dict[str, object]:
    try:
        return export_dataset_session(session_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (OSError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/sessions/{session_id}/exports/{filename:path}")
def correction_export_download(session_id: str, filename: str) -> FileResponse:
    try:
        path = download_export(session_id, filename)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return FileResponse(
        path,
        filename=path.name,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
