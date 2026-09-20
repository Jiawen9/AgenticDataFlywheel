"""Read-only overview queries and explicit conversion retries."""
from fastapi import APIRouter, HTTPException, Response
from fastapi.responses import FileResponse

router = APIRouter(prefix="/api/training-data-overview", tags=["training-data-overview"])


def get_manager():
    from ..data_publishing.router import overview_manager
    return overview_manager


@router.get("")
def overview(response: Response, source: str = "", level1: str = "", level2: str = "", app: str = "",
             start_date: str = "", end_date: str = ""):
    """DEV statistics; scene/date facets use all supplied filters, apps ignore only app."""
    response.headers["Cache-Control"] = "no-store"
    try:
        return get_manager().query(source=source, level1=level1, level2=level2, app=app,
                                   start_date=start_date, end_date=end_date)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/workbook")
def workbook():
    try:
        path = get_manager().workbook()
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return FileResponse(path, filename="all_data.xlsx", headers={"Cache-Control": "no-store"},
                        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@router.post("/releases/{release_id}/retry", status_code=202)
def retry(release_id: str):
    try:
        return {"conversion": get_manager().submit(release_id)}
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
