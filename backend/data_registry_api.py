"""Read registered stage versions and download their verified artifacts."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.responses import FileResponse

from .data_store.artifacts import ArtifactStore
from .data_store.paths import DATA_ROOT

def no_cache(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


router = APIRouter(prefix="/api", tags=["data-storage"], dependencies=[Depends(no_cache)])
store = ArtifactStore(DATA_ROOT)


@router.get("/data-storage")
def storage_information() -> dict[str, Any]:
    return {"schema_version": 1, "data_root": str(store.root), "storage_mode": "new_only",
            "database": "system/app.sqlite", "stage_files": "batches/<batch_id>/<stage>/<version>",
            "excel_role": "read_only_snapshot"}


@router.get("/data-batches")
def data_batches() -> dict[str, Any]:
    values: dict[str, dict] = {}
    for manifest in store.list():
        batch = values.setdefault(manifest["batch_id"], {"batch_id": manifest["batch_id"], "stages": [], "version_count": 0, "updated_at": ""})
        if manifest["stage"] not in batch["stages"]:
            batch["stages"].append(manifest["stage"])
        batch["version_count"] += 1
        batch["updated_at"] = max(batch["updated_at"], manifest["created_at"])
    return {"batches": sorted(values.values(), key=lambda item: item["updated_at"], reverse=True)}


@router.get("/data-batches/{batch_id}/artifacts")
def batch_artifacts(batch_id: str) -> dict[str, Any]:
    try:
        manifests = store.list(batch_id=batch_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if not manifests:
        raise HTTPException(status_code=404, detail="没有登记的批次产物")
    return {"batch_id": batch_id, "artifacts": manifests}


@router.get("/data-batches/{batch_id}/artifacts/{stage}/{version}")
def artifact_detail(batch_id: str, stage: str, version: str) -> dict[str, Any]:
    try:
        manifest = store.get(batch_id, stage, version)
    except (ValueError, OSError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if manifest is None:
        raise HTTPException(status_code=404, detail="产物版本不存在")
    return manifest


@router.get("/data-batches/{batch_id}/artifacts/{stage}/{version}/files/{filename}")
def artifact_file(batch_id: str, stage: str, version: str, filename: str) -> FileResponse:
    manifest = artifact_detail(batch_id, stage, version)
    try:
        path = store.resolve_file(manifest, filename)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="产物文件不存在") from exc
    except (ValueError, OSError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return FileResponse(path, filename=filename, headers={"Cache-Control": "no-store"})
