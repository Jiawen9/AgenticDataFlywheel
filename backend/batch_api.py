"""Current business-batch views; execution identifiers are not selection keys."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Response

from .batch_results import current_tree_batch, current_tree_payload, current_quality_payload
from .data_store import ArtifactStore, DATA_ROOT, RecordStore
from .trajectory_data import task_summaries
from .batch_lifecycle import lifecycle, ensure_batch_active
from .coordinate_info import tree_coordinate_info
from .trajectory_context import AnnotationVersionConflict

router = APIRouter(prefix="/api/data-batches", tags=["batch-results"])
store = ArtifactStore(DATA_ROOT)


def _read(operation):
    try:
        return operation()
    except (ValueError, OSError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


def _quality_status(result: dict | None, previous: str = "pending") -> str:
    if result is None:
        return "stale" if previous == "succeeded" else previous
    if result.get("result_state") == "partial" or result.get("fusion_complete") is False:
        status = result.get("status")
        return status if status in {"queued", "running", "failed", "interrupted"} else "failed"
    return "succeeded"


def task_status_summaries(batch_id: str, annotation_version: str | None = None) -> list[dict]:
    """Share task status with the collection page without exposing run choices."""
    with store.batch_lock(batch_id):
        ensure_batch_active(batch_id, store.root)
        summaries = task_summaries(batch_id=batch_id, annotation_version=annotation_version, data_root=store.root)
        trees = current_tree_payload(batch_id, store.root).get("trees", {})
        quality = {item["task_id"]: item for item in current_quality_payload(batch_id, store.root).get("tasks", [])}
        for item in summaries:
            state = store.records.get("batch_task_states", f"{batch_id}:{item['task_id']}") or {}
            for stage, valid in (("tree", item["task_id"] in trees), ("quality", item["task_id"] in quality)):
                prior = state.get(f"{stage}_status", "pending")
                item[f"{stage}_status"] = "succeeded" if valid else ("stale" if prior == "succeeded" else prior)
                if stage == "quality":
                    item["quality_status"] = _quality_status(quality.get(item["task_id"]), prior)
        return summaries


def _tree(batch_id: str) -> dict:
    with store.batch_lock(batch_id):
        ensure_batch_active(batch_id, store.root)
        if not store.list(batch_id):
            raise HTTPException(status_code=404, detail="批次不存在")
        value = current_tree_batch(batch_id, store.root) or {}
        built = {item["task_id"]: item for item in value.get("tasks", [])}
        summaries = task_summaries(batch_id=batch_id, data_root=store.root) if store.get(batch_id, "02_annotation") else []
        quality = current_quality_payload(batch_id, store.root)
        reviewed = {item["task_id"]: item for item in quality.get("tasks", [])}
        records = RecordStore(store.root)
        tasks = []
        for summary in summaries:
            task_id = summary["task_id"]
            state = records.get("batch_task_states", f"{batch_id}:{task_id}") or {}
            tree_status = "succeeded" if task_id in built else state.get("tree_status", "pending")
            quality_status = _quality_status(reviewed.get(task_id), state.get("quality_status", "pending"))
            tasks.append({"task_id": task_id, "goal": summary.get("goal", ""),
                          "tree_file": "", "trajectory_count": summary.get("trajectory_count", 0),
                          "original_step_count": summary.get("step_count", 0), "tree_step_count": 0,
                          "ignored_step_count": 0, "action_node_count": 0,
                          **built.get(task_id, {}), "status": tree_status, "tree_status": tree_status,
                          "quality_status": quality_status})
        return {**value, "run_id": batch_id, "batch_id": batch_id, "tasks": tasks,
                "task_ids": [item["task_id"] for item in tasks], "task_count": len(tasks),
                "completed_at": value.get("completed_at", ""), "model_name": value.get("model_name", ""),
                "total_original_steps": sum(item["original_step_count"] for item in tasks),
                "total_tree_steps": sum(item["tree_step_count"] for item in tasks),
                "revision": (store.get(batch_id, "04_tree") or {}).get("revision", 0)}


@router.get("/{batch_id}/tree")
def batch_tree(batch_id: str, response: Response):
    response.headers["Cache-Control"] = "no-store"
    return _read(lambda: _tree(batch_id))


@router.get("/{batch_id}/tasks/{task_id}/tree")
def task_tree(batch_id: str, task_id: str, response: Response, view: str | None = None,
              annotation_version: str | None = None, tree_version: str | None = None,
              trajectory_id: str | None = None, image: str | None = None):
    response.headers["Cache-Control"] = "no-store"
    ensure_batch_active(batch_id, store.root)
    if view is not None:
        if view != "coordinate" or not all((annotation_version, tree_version, trajectory_id, image)):
            raise HTTPException(status_code=422, detail="坐标查询需要 view=coordinate、标框版本、树版本、轨迹编号和图片")
        try:
            return tree_coordinate_info(batch_id, task_id, annotation_version, tree_version, trajectory_id, image, root=store.root)
        except AnnotationVersionConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (OSError, TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    with store.batch_lock(batch_id):
        value = _read(lambda: current_tree_payload(batch_id, store.root))
        tree = value.get("trees", {}).get(task_id)
        if tree is None:
            raise HTTPException(status_code=409, detail="该任务尚无有效轨迹树，请先完成建树")
        annotation = store.get(batch_id, "02_annotation") or {}
        reference = store.get(batch_id, "04_tree") or {}
        return {**tree, "coordinate_lookup_version": 1, "annotation_version": annotation.get("version"),
                "tree_version": reference.get("version")}


@router.get("/{batch_id}/quality")
def batch_quality(batch_id: str, response: Response):
    response.headers["Cache-Control"] = "no-store"
    def read():
        with store.batch_lock(batch_id):
            tree = _tree(batch_id)
            quality = current_quality_payload(batch_id, store.root)
            results = {item["task_id"]: item for item in quality.get("tasks", [])}
            tasks = []
            for task in tree["tasks"]:
                result = results.get(task["task_id"], {})
                summary = {key: value for key, value in result.items() if key not in {"evaluations", "rubric", "artifact"}}
                tasks.append({**summary, "task_id": task["task_id"], "status": task["quality_status"],
                              "results_available": bool(result.get("evaluations")),
                              "partial_results_available": bool(result.get("evaluations")) and task["quality_status"] != "succeeded",
                              "rubric_ready": bool(result.get("rubric")), "tree_status": task["tree_status"]})
            return {"batch_id": batch_id, "run_id": batch_id, "tasks": tasks,
                    "updated_at": quality.get("updated_at")}
    return _read(read)


@router.get("/{batch_id}/tasks/{task_id}/quality")
def task_quality(batch_id: str, task_id: str, response: Response):
    response.headers["Cache-Control"] = "no-store"
    ensure_batch_active(batch_id, store.root)
    value = _read(lambda: current_quality_payload(batch_id, store.root))
    result = next((item for item in value.get("tasks", []) if item["task_id"] == task_id), None)
    if result is None:
        raise HTTPException(status_code=409, detail="该任务尚无有效质检结果，请先完成质检")
    return result


@router.get("/{batch_id}/lifecycle")
def batch_lifecycle(batch_id: str, response: Response):
    response.headers["Cache-Control"] = "no-store"
    def read():
        state = lifecycle(batch_id, store.root)
        if state["status"] == "active" and not store.list(batch_id):
            raise HTTPException(status_code=404, detail="批次不存在")
        return state
    return _read(read)
