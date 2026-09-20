"""Current business-batch views; execution identifiers are not selection keys."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Response

from .batch_results import current_tree_batch, current_tree_payload, current_quality_payload
from .data_store import ArtifactStore, DATA_ROOT, RecordStore
from .trajectory_data import task_summaries
from .batch_lifecycle import lifecycle, ensure_batch_active

router = APIRouter(prefix="/api/data-batches", tags=["batch-results"])
store = ArtifactStore(DATA_ROOT)


def _read(operation):
    try:
        return operation()
    except (ValueError, OSError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


def task_status_summaries(batch_id: str, annotation_version: str | None = None) -> list[dict]:
    """Share task status with the collection page without exposing run choices."""
    with store.batch_lock(batch_id):
        ensure_batch_active(batch_id, store.root)
        summaries = task_summaries(batch_id=batch_id, annotation_version=annotation_version, data_root=store.root)
        trees = current_tree_payload(batch_id, store.root).get("trees", {})
        quality = {item["task_id"] for item in current_quality_payload(batch_id, store.root).get("tasks", [])}
        for item in summaries:
            state = store.records.get("batch_task_states", f"{batch_id}:{item['task_id']}") or {}
            for stage, valid in (("tree", item["task_id"] in trees), ("quality", item["task_id"] in quality)):
                prior = state.get(f"{stage}_status", "pending")
                item[f"{stage}_status"] = "succeeded" if valid else ("stale" if prior == "succeeded" else prior)
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
        reviewed = {item["task_id"] for item in quality.get("tasks", [])}
        records = RecordStore(store.root)
        tasks = []
        for summary in summaries:
            task_id = summary["task_id"]
            state = records.get("batch_task_states", f"{batch_id}:{task_id}") or {}
            tree_status = "succeeded" if task_id in built else state.get("tree_status", "pending")
            quality_status = "succeeded" if task_id in reviewed else state.get("quality_status", "pending")
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
def task_tree(batch_id: str, task_id: str, response: Response):
    response.headers["Cache-Control"] = "no-store"
    ensure_batch_active(batch_id, store.root)
    value = _read(lambda: current_tree_payload(batch_id, store.root))
    tree = value.get("trees", {}).get(task_id)
    if tree is None:
        raise HTTPException(status_code=409, detail="该任务尚无有效轨迹树，请先完成建树")
    return tree


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
