"""FastAPI routes for the isolated trajectory-correction workbench."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse

from .schemas import CreateSessionRequest, ExportStateRequest, RowPatchRequest
from .quality_selection import (
    QualitySelectionError,
    correction_batches,
    top1_recommendation,
)
from .service import (
    create_session,
    download_export,
    export_session,
    get_group,
    get_groups,
    get_session,
    patch_group_export,
    patch_row,
    session_asset,
    sessions,
)


router = APIRouter(prefix="/api/correction", tags=["trajectory-correction"])


@router.get("/recommendation")
def correction_recommendation(
    tree_run_id: str | None = Query(default=None),
) -> dict[str, object]:
    try:
        return top1_recommendation(tree_run_id)
    except QualitySelectionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/batches")
def correction_quality_batches() -> dict[str, object]:
    return correction_batches()


@router.get("/sessions")
def correction_sessions() -> dict[str, object]:
    return {"sessions": sessions()}


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
