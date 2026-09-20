"""Read the single current artifact of each business batch and stage."""
from __future__ import annotations

from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Response

from .data_store.artifacts import ArtifactStore
from .data_store.paths import DATA_ROOT
from .batch_lifecycle import is_batch_active


def no_cache(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


router = APIRouter(prefix="/api", tags=["data-storage"], dependencies=[Depends(no_cache)])
store = ArtifactStore(DATA_ROOT)


@router.get("/data-storage")
def storage_information() -> dict[str, Any]:
    return {"schema_version": 2, "data_root": str(store.root), "storage_mode": "new_only",
            "database": "system/app.sqlite", "stage_files": "batches/<batch_id>/<stage>",
            "excel_role": "read_only_snapshot", "stage_policy": "single_current"}


@router.get("/data-batches")
def data_batches() -> dict[str, Any]:
    values: dict[str, dict] = {}
    for manifest in store.list():
        if not is_batch_active(manifest["batch_id"], store.root):
            continue
        batch = values.setdefault(manifest["batch_id"], {"batch_id": manifest["batch_id"], "stages": [],
                                                         "stage_count": 0, "updated_at": ""})
        if manifest["stage"] not in batch["stages"]:
            batch["stages"].append(manifest["stage"])
        batch["updated_at"] = max(batch["updated_at"], manifest["created_at"])
    for value in values.values():
        value["stages"].sort()
        value["stage_count"] = len(value["stages"])
        value["version_count"] = value["stage_count"]  # legacy clients, not history
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


@router.get("/data-batches/{batch_id}/artifacts/{stage}")
def current_artifact_detail(batch_id: str, stage: str) -> dict[str, Any]:
    return artifact_detail(batch_id, stage)


@router.get("/data-batches/{batch_id}/artifacts/{stage}/{version}")
def artifact_detail(batch_id: str, stage: str, version: str | None = None) -> dict[str, Any]:
    try:
        manifest = store.get(batch_id, stage, version)
    except (ValueError, OSError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if manifest is None:
        if version is not None and store.get(batch_id, stage) is not None:
            raise HTTPException(status_code=410, detail="该历史产物已失效，请查看批次当前结果")
        raise HTTPException(status_code=404, detail="阶段产物不存在")
    return manifest


def _download(batch_id: str, stage: str, filename: str, version: str | None = None) -> Response:
    # Keep the lock through resolving, hashing, and reading: fixed paths may be replaced.
    with store.batch_lock(batch_id):
        manifest = artifact_detail(batch_id, stage, version)
        try:
            content = store.read_file(manifest, filename)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="产物文件不存在") from exc
        except (ValueError, OSError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
    media = "application/json" if filename.endswith(".json") else "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    return Response(content, media_type=media, headers={
        "Cache-Control": "no-store", "Content-Disposition": "attachment; filename*=UTF-8''" + quote(filename)})


@router.get("/data-batches/{batch_id}/artifacts/{stage}/files/{filename}")
def current_artifact_file(batch_id: str, stage: str, filename: str) -> Response:
    return _download(batch_id, stage, filename)


@router.get("/data-batches/{batch_id}/artifacts/{stage}/{version}/files/{filename}")
def artifact_file(batch_id: str, stage: str, version: str, filename: str) -> Response:
    return _download(batch_id, stage, filename, version)
