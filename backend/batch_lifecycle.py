"""Durable completion of a business batch, independent of retained artifacts."""
from __future__ import annotations

from pathlib import Path

from .data_store import DATA_ROOT, RecordStore
from .data_store.artifacts import ArtifactStore, _identifier

NAMESPACE = "batch_lifecycle"
ACTIVE_JOB_STATUSES = {"queued", "running", "dispatching", "pending"}


class BatchPublishedError(RuntimeError):
    def __init__(self, state: dict):
        self.detail = {"code": "batch_published", "message": "该批次已发布，处理已结束",
                       **{key: state.get(key) for key in ("batch_id", "release_id", "published_at")}}
        super().__init__(self.detail["message"])


class BatchNotReadyError(RuntimeError):
    def __init__(self, batch_id: str, blockers: list[dict]):
        self.detail = {"code": "batch_not_ready", "message": "整批处理完成后才能发布",
                       "batch_id": batch_id, "blockers": blockers}
        super().__init__(self.detail["message"] + "：" + "；".join(item["message"] for item in blockers))


def lifecycle(batch_id: str, root: Path | None = None) -> dict:
    _identifier(batch_id, "batch ID")
    value = RecordStore(root or DATA_ROOT).get(NAMESPACE, batch_id)
    return {key: value.get(key) for key in ("batch_id", "status", "published_at", "release_id")} if value else {
        "batch_id": batch_id, "status": "active", "published_at": None, "release_id": None}


def is_batch_active(batch_id: str, root: Path | None = None) -> bool:
    return lifecycle(batch_id, root)["status"] != "published"


def ensure_batch_active(batch_id: str, root: Path | None = None) -> None:
    state = lifecycle(batch_id, root)
    if state["status"] == "published":
        raise BatchPublishedError(state)


def session_batch_id(session: dict, root: Path | None = None) -> str:
    value = str(session.get("storage_batch_id") or session.get("batch_id") or session.get("tree_run_id")
                or session.get("session_id") or "")
    alias = RecordStore(root or DATA_ROOT).get("batch_run_aliases", value) if value else None
    return str(alias["batch_id"]) if alias else value


def published_entry(batch_id: str, release_id: str, published_at: str, root: Path | None = None) -> dict:
    ensure_batch_active(batch_id, root)
    current = RecordStore(root or DATA_ROOT).get(NAMESPACE, batch_id)
    return {"namespace": NAMESPACE, "key": batch_id,
            "expected_revision": (current or {}).get("storage_revision", 0),
            "payload": {"batch_id": batch_id, "status": "published", "release_id": release_id,
                        "published_at": published_at}}


def active_jobs(batch_id: str, root: Path | None = None) -> list[dict]:
    records = RecordStore(root or DATA_ROOT)
    session_ids = {item["session_id"] for item in records.list("correction_sessions")
                   if session_batch_id(item, root) == batch_id}
    # Alias records do not contain their key, so resolve each run identifier as needed.
    def belongs(item):
        identifier = str(item.get("batch_id") or item.get("storage_batch_id") or item.get("run_id") or "")
        if identifier == batch_id or item.get("session_id") in session_ids:
            return True
        if identifier:
            alias = records.get("batch_run_aliases", identifier)
            if alias and alias["batch_id"] == batch_id:
                return True
        return item.get("job_id") == batch_id
    result = []
    for namespace in ("collection_runs", "preprocessing_jobs", "tree_jobs", "quality_jobs",
                      "correction_cot_jobs", "task_generation.jobs", "batch_operations"):
        for item in records.list(namespace):
            if item.get("status") in ACTIVE_JOB_STATUSES and belongs(item):
                result.append({"namespace": namespace, "job_id": item.get("job_id") or item.get("collection_run_id") or item.get("operation_id"),
                               "status": item["status"]})
    return result


def publication_blockers(batch_id: str, sessions: list[dict], root: Path | None = None) -> list[dict]:
    """Read authoritative current files. No reconcile, model call, or draft write."""
    from .batch_results import annotation_task_fingerprints, current_tree_payload, current_quality_payload
    store = ArtifactStore(root or DATA_ROOT)
    problems = []
    def add(code, message, task_id=None):
        problems.append({"code": code, "message": message, **({"task_id": task_id} if task_id else {})})
    with store.batch_lock(batch_id):
        values = {}
        for stage in ("01_conversion", "02_annotation", "03_observation", "04_tree", "05_quality"):
            ref = store.get(batch_id, stage)
            if ref is None:
                add("missing_stage", f"缺少 {stage} 过程件")
                continue
            try:
                values[stage] = store.read_payload(ref)
            except (OSError, ValueError) as exc:
                add("invalid_stage", f"{stage} 校验失败：{exc}")
        # A new completed collection can add trajectories for existing task IDs.
        # Matching task sets alone cannot prove that preprocessing consumed them.
        completed_runs = [item for item in store.records.list("collection_runs")
                          if item.get("batch_id") == batch_id and item.get("status") == "completed"]
        if completed_runs:
            from .collection_runs import CollectionRunStore
            try:
                ready = CollectionRunStore(store.root).ready_input(batch_id)
                conversion = store.get(batch_id, "01_conversion") or {}
                consumed = next((item.get("input_digest") for item in conversion.get("source_refs", [])
                                 if item.get("kind") == "raw_trajectories" and item.get("input_digest")), None)
                if consumed != ready["input_digest"]:
                    add("stale_collection_input", "存在尚未预处理的采集输入；请按当前采集清单重新预处理")
            except (OSError, ValueError, KeyError) as exc:
                add("invalid_collection_input", f"采集输入校验失败：{exc}")
        conversion = store.get(batch_id, "01_conversion")
        annotation = store.get(batch_id, "02_annotation")
        if conversion and annotation:
            queue = list(annotation.get("source_refs", []))
            seen = set()
            matched_conversion = False
            while queue:
                reference = queue.pop(0)
                key = (reference.get("batch_id"), reference.get("stage"), reference.get("version"))
                if key in seen:
                    continue
                seen.add(key)
                if reference.get("stage") == "01_conversion":
                    matched_conversion = (reference.get("batch_id") == batch_id
                        and reference.get("version") == conversion.get("version")
                        and reference.get("content_hash") == conversion.get("content_hash"))
                    break
                if reference.get("stage") == "02_annotation":
                    queue.extend(reference.get("source_refs", []))
            if not matched_conversion:
                add("stale_preprocessing", "标框结果尚未对应当前转换结果，请完成预处理")
        expected = set()
        collection = store.get(batch_id, "00_collection")
        if collection:
            try:
                payload = store.read_payload(collection)
                expected.update(str(item["task_id"]) for item in payload.get("snapshot", {}).get("tasks", [])
                                if item.get("task_id"))
            except (OSError, ValueError) as exc:
                add("invalid_collection", f"采集批次校验失败：{exc}")
        for stage in ("01_conversion", "02_annotation"):
            expected.update(annotation_task_fingerprints(values.get(stage, {})))
        if not expected:
            add("no_tasks", "该批次没有可发布任务")
        try:
            trees = current_tree_payload(batch_id, store.root)
            quality = current_quality_payload(batch_id, store.root)
        except (OSError, ValueError) as exc:
            trees, quality = {}, {}
            add("invalid_results", f"当前结果校验失败：{exc}")
        reviewed = {item["task_id"]: item for item in quality.get("tasks", [])}
        tree_tasks = {item["task_id"]: item for item in trees.get("tasks", [])}
        converted = set(annotation_task_fingerprints(values.get("01_conversion", {})))
        annotated = set(annotation_task_fingerprints(values.get("02_annotation", {})))
        for task in sorted(expected):
            if task not in converted or task not in annotated:
                add("pending_preprocessing", f"{task} 尚未完成预处理", task)
            if task not in trees.get("trees", {}):
                add("pending_tree", f"{task} 尚无有效建树结果", task)
            item = reviewed.get(task, {})
            expected_count = int(tree_tasks.get(task, {}).get("trajectory_count") or 0)
            if (not item or item.get("status") not in (None, "succeeded", "completed")
                    or not item.get("evaluations")
                    or (expected_count and len(item["evaluations"]) != expected_count)):
                add("pending_quality", f"{task} 尚无完整有效质检结果", task)
        for session in sessions:
            if session.get("pending_review"):
                add("pending_review", "存在尚未采用或放弃的人工修改")
            if session.get("stale_tasks"):
                add("stale_tasks", "修正会话包含待重新处理任务")
            source = session.get("task_fingerprints", {})
            if set(source) != expected or any(trees.get("task_fingerprints", {}).get(task) != signature for task, signature in source.items()):
                add("stale_session", "修正会话来源已变化，请刷新并完成复核")
        if store.records.get("batch_invalidations", batch_id):
            add("pending_reconciliation", "结果已更新但依赖状态尚未同步，恢复完成后才能发布")
        for job in active_jobs(batch_id, store.root):
            add("active_job", f"作业 {job['job_id']} 正在执行，完成后才能发布")
    return problems


def ensure_publishable(batch_id: str, sessions: list[dict], root: Path | None = None) -> None:
    ensure_batch_active(batch_id, root)
    blockers = publication_blockers(batch_id, sessions, root)
    if blockers:
        raise BatchNotReadyError(batch_id, blockers)


def install_lifecycle_handlers(app) -> None:
    from fastapi.responses import JSONResponse
    async def handle(_request, exc):
        return JSONResponse(status_code=409, content={"detail": exc.detail})
    app.add_exception_handler(BatchPublishedError, handle)
    app.add_exception_handler(BatchNotReadyError, handle)
