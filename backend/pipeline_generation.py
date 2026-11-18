"""Generation entry for Pipelines; no result batch exists until review commits."""
from __future__ import annotations

import math
import json
import time
from copy import deepcopy

from .batch_lifecycle import ensure_batch_active
from .data_store import RevisionConflict
from .data_store.artifacts import _identifier
from .data_store.registry import utc_now
from .identifiers import ID_SCHEME, allocate_id
from .pipeline_access import TERMINAL, active_pipeline

WAIT_CONFIG = "waiting_for_generation_config"
WAIT_REVIEW = "waiting_for_generation_review"
ACTIVE_GENERATION = {"queued", "running"}


def _error(message, status=409):
    from .pipelines import PipelineError
    return PipelineError(message, status)


def _digest(value):
    from .pipelines import digest
    return digest(value)


def _step(p):
    return next(s for s in p["steps"] if s["id"] == "generation")


def create(manager, data):
    from .pipelines import RETRY_POLICY, STEPS
    request_id = str(data.get("request_id") or "")
    _identifier(request_id, "请求编号")
    name = str(data.get("name") or "").strip()
    mode, kind, threshold = data.get("mode"), data.get("generation_kind"), data.get("threshold")
    if not name or len(name) > 200:
        raise _error("流程名称须为 1～200 个字符", 422)
    if mode not in {"manual", "automatic"} or kind not in {"initial", "augmentation"}:
        raise _error("请选择有效的处理模式和任务生成方式", 422)
    if data.get("batch_id") or data.get("collection_config"):
        raise _error("生成起点在审核确认时绑定批次和采集配置", 422)
    if mode == "automatic" and (isinstance(threshold, bool) or not isinstance(threshold, (int, float))
            or not math.isfinite(threshold) or not 0 <= threshold <= 5):
        raise _error("自动发布总分阈值必须在 0～5 之间", 422)
    profile, cot_policy = data.get("export_profile"), data.get("cot_policy")
    if profile not in {None, "corrected_selection_v1"} or cot_policy not in {None, "generate", "skip"}:
        raise _error("不支持的导出或 COT 策略", 422)
    if cot_policy is not None and profile is None:
        raise _error("COT 策略需要明确的导出配置", 422)
    fingerprint, key = _digest(data), _digest(request_id)
    with manager.artifacts.batch_lock("pipeline_create"):
        old = manager.records.get("pipeline_requests", key)
        if old:
            if old["request_digest"] != fingerprint:
                raise _error("相同请求编号对应的 Pipeline 配置不同")
            return manager.get(old["pipeline_id"])
        feedback_id, parent_id = data.get("feedback_id"), data.get("parent_pipeline_id")
        if parent_id:
            manager.get(parent_id, public=False)
        if feedback_id:
            if kind != "augmentation":
                raise _error("BadCase 回流必须使用任务扩增", 422)
            from .badcases import BadCaseService, BadCaseError
            try:
                feedback = BadCaseService(manager.root).get_feedback(feedback_id)
            except BadCaseError as exc:
                raise _error(str(exc), exc.status) from exc
            if parent_id and feedback.get("parent_pipeline_id") not in {None, parent_id}:
                raise _error("回流来源与父流程不一致")
        identifier = allocate_id("p", manager.root, key=["pipeline", request_id], request_fingerprint=fingerprint)
        now = utc_now()
        p = {"pipeline_id": identifier, "id_scheme": ID_SCHEME, "name": name, "batch_id": None,
             "mode": mode, "start_mode": "generate", "generation_kind": kind, "generation_job_id": None,
             "generation_attempt": 0, "feedback_id": feedback_id, "parent_pipeline_id": parent_id,
             "threshold": threshold if mode == "automatic" else None, "task_ids": [], "batch_snapshot": None,
             "collection_config": None, "config_fingerprints": {}, "retry_policy": dict(RETRY_POLICY),
             "status": WAIT_CONFIG, "current_step": "generation", "created_at": now, "updated_at": now,
             "error": None, "session_id": None, "release_id": None, "selection": None, "confirmation": None,
             "collection_run_ids": [], "logs": [],
             "steps": [{"id": k, "label": label, "status": "waiting" if k == "generation" else "pending",
                        "job_ids": [], "percent": 0, "message": "请配置任务生成" if k == "generation" else "等待前置步骤",
                        "error": None, "started_at": None, "completed_at": None,
                        "retry_info": {"attempts": 0, "max_attempts": RETRY_POLICY["max_attempts"],
                                       "last_error": None, "next_retry_at": None}}
                       for k, label in [("generation", "任务生成与审核"), *STEPS]]}
        if profile:
            p.update(export_profile=profile, cot_policy=cot_policy or "generate")
        manager.records.put_many([
            {"namespace": "pipelines", "key": identifier, "payload": p, "expected_revision": 0},
            {"namespace": "pipeline_requests", "key": key, "payload": {
                "pipeline_id": identifier, "request_digest": fingerprint}, "expected_revision": 0}])
        return manager.get(identifier)


def validate_binding(manager, pipeline_id, kind, request_id=None):
    p = manager.get(pipeline_id, public=False)
    actual_kind = "initial" if kind == "task_generation" else kind
    if p.get("start_mode") != "generate" or p.get("generation_kind") != actual_kind:
        raise _error("生成方式与 Pipeline 配置不一致")
    if p.get("batch_id") or p["status"] in TERMINAL | {"paused", "terminating"}:
        raise _error("该 Pipeline 当前不接受新的生成作业")
    if p.get("generation_job_id"):
        job = manager.records.get("task_generation.jobs", p["generation_job_id"])
        if not request_id or not job or job.get("request_id") != request_id:
            raise _error("Pipeline 已关联生成作业，请查看原作业或重试失败阶段")
    elif p.get("generation_retry"):
        if request_id != p["generation_retry"]["request_id"]:
            raise _error("正在重试冻结的生成输入，不能提交不同配置")
    elif p["status"] != WAIT_CONFIG:
        raise _error("请先重试失败的生成阶段")
    return p


def bind(manager, pipeline_id, job):
    with manager.artifacts.batch_lock("pipeline_" + pipeline_id):
        p = manager.get(pipeline_id, public=False)
        if p.get("generation_job_id") == job["job_id"]:
            return manager.get(pipeline_id)
        validate_binding(manager, pipeline_id, job.get("kind"), job.get("request_id"))
        if job.get("pipeline_id") != pipeline_id or (p.get("feedback_id") or None) != (job.get("feedback_id") or None):
            raise _error("生成作业的 Pipeline 或回流来源身份不匹配")
        step = _step(p)
        if job["job_id"] in step.get("previous_job_ids", []):
            raise _error("此请求属于旧生成尝试，请使用新的请求编号")
        p.update(generation_job_id=job["job_id"], generation_request_id=job.get("request_id"), status="running", error=None)
        p.pop("generation_retry", None)
        step.update(status="running", job_ids=[job["job_id"]], started_at=utc_now(), message="等待任务生成完成", error=None)
        manager._log(p, "已关联生成作业，页面关闭后后台继续执行")
        manager._save(p)
        return manager.get(pipeline_id)


def tick(manager, p):
    if p["status"] in TERMINAL | {"paused", "failed"}:
        return
    step = _step(p)
    pending = p.get("generation_confirmation") or {}
    if pending.get("status") == "preparing" and p["status"] != "terminating":
        # Accepted review is durable work, not another form submission. In
        # particular, a browser refresh must not read newer collection defaults.
        if pending.get("next_retry_epoch", 0) > time.time():
            return
        try:
            key = pending["intent_key"]
            intent = manager.records.get("pipeline_generation_confirmations", key)
            if not intent:
                raise _error("已确认的采集配置记录缺失，请恢复存储后重试")
            current = _complete_confirmation(manager, p, key, intent)
            p.clear()
            p.update(current)
            return True  # The pipeline, owner and intent committed together.
        except Exception as exc:
            from .pipeline_diagnostics import sanitize
            attempts = pending.get("attempts", 0) + 1
            pending.update(attempts=attempts, next_retry_epoch=time.time() + min(60, 5 * 2 ** min(attempts - 1, 4)))
            p.update(status="running", error=str(sanitize(str(exc))))
            step.update(status="waiting", error=p["error"], message="采集配置已确认，准备暂时失败；后台按原配置重试")
            return
    if p.get("generation_retry") and p["status"] == "running" and not p.get("generation_job_id"):
        retry = p["generation_retry"]
        try:
            generation = getattr(manager.runtime, "generation", None)
            if generation is None:
                raise _error("任务生成服务尚未接入", 503)
            generation.retry_generation(retry["source_job_id"], request_id=retry["request_id"],
                pipeline_id=p["pipeline_id"], on_created=lambda job: manager.bind_generation(p["pipeline_id"], job))
            current = manager.get(p["pipeline_id"], public=False)
            p.clear()
            p.update(current)
            step = _step(p)
        except Exception as exc:
            current = manager.get(p["pipeline_id"], public=False)
            p.clear()
            p.update(current)
            from .pipeline_diagnostics import sanitize
            p.update(status="failed", error=str(sanitize(str(exc))))
            _step(p).update(status="failed", error=p["error"], message="生成重试准备失败，原输入保留")
            return
    if not p.get("generation_job_id") and p["status"] == WAIT_CONFIG:
        # The job is written before the binding callback. Reconcile that crash
        # window by its registered pipeline identity, never by a display name.
        with manager.records._connection() as connection:
            candidates = connection.execute("SELECT payload FROM records WHERE namespace='task_generation.jobs' "
                "AND json_extract(payload,'$.pipeline_id')=?", (p["pipeline_id"],)).fetchall()
        previous = set(step.get("previous_job_ids", []))
        jobs = [json.loads(row[0]) for row in candidates]
        jobs = [job for job in jobs if job.get("job_id") not in previous]
        if len(jobs) > 1:
            p.update(status="failed", error="发现多个未绑定的生成作业，请核对作业身份")
            step.update(status="failed", error=p["error"])
            return
        if jobs:
            job = jobs[0]
            kind = "initial" if job.get("kind") == "task_generation" else job.get("kind")
            if kind != p["generation_kind"] or (p.get("feedback_id") or None) != (job.get("feedback_id") or None):
                p.update(status="failed", error="生成作业来源与流程不一致")
                step.update(status="failed", error=p["error"])
                return
            p.update(generation_job_id=job["job_id"], generation_request_id=job.get("request_id"), status="running")
            step.update(job_ids=[job["job_id"]], status="running", started_at=job.get("created_at"))
    job = manager.records.get("task_generation.jobs", p["generation_job_id"]) if p.get("generation_job_id") else None
    if p["status"] == "terminating":
        if not job or job.get("status") not in ACTIVE_GENERATION:
            p.update(status="terminated", error=None)
            step.update(status="skipped", message="已终止后续推进，保留生成结果", completed_at=utc_now())
        return
    if not job:
        if p.get("generation_job_id"):
            p.update(status="failed", error="关联的生成作业不存在，请检查存储后重试")
            step.update(status="failed", error=p["error"])
        return
    status = job.get("status")
    step["percent"] = job.get("percent", 0)
    if status in {"succeeded", "partial"}:
        p.update(status=WAIT_REVIEW, error=None)
        step.update(status="waiting", message="生成完成，请审核任务并确认采集配置", percent=100)
    elif status == "awaiting_confirmation":
        step.update(status="waiting", message="分类完成，请在任务扩增页面确认开始扩增")
    elif status in {"failed", "interrupted", "cancelled"}:
        p.update(status="failed", error=job.get("error") or "任务生成失败，请查看生成作业")
        step.update(status="failed", error=p["error"], message=p["error"])
    else:
        step.update(status="running", message="任务生成中")


def control(manager, p, action):
    if p["status"] in TERMINAL:
        raise _error("该 Pipeline 已结束")
    if action == "pause" and p["status"] in {"running", WAIT_CONFIG, WAIT_REVIEW}:
        p.update(paused_from=p["status"], status="paused")
    elif action == "resume" and p["status"] == "paused":
        p["status"] = p.pop("paused_from", WAIT_CONFIG)
    elif action == "terminate":
        p["status"] = "terminating"
        tick(manager, p)
    elif action == "retry" and p["status"] == "failed":
        step = _step(p)
        source_job_id = p.get("generation_job_id") or (p.get("generation_retry") or {}).get("source_job_id")
        step.setdefault("previous_job_ids", []).extend(step["job_ids"])
        step.update(job_ids=[], status="pending", error=None, percent=0, started_at=None, completed_at=None,
                    message="等待使用原冻结输入重试生成")
        p.update(status="running" if source_job_id else WAIT_CONFIG, error=None, generation_job_id=None,
                 generation_request_id=None, generation_attempt=p.get("generation_attempt", 0) + 1)
        if source_job_id:
            p["generation_retry"] = {"source_job_id": source_job_id,
                "request_id": f"{p['pipeline_id']}-generation-retry-{p['generation_attempt']}"}
    else:
        raise _error("当前状态不能执行该操作")
    manager._log(p, {"pause": "暂停后续派发，已开始的生成继续完成", "resume": "继续生成流程",
                     "terminate": "终止后续采集，保留生成结果", "retry": "按原冻结输入重试生成阶段"}[action])


def confirm(manager, pipeline_id, data):
    request_id = str(data.get("request_id") or "")
    _identifier(request_id, "请求编号")
    fingerprint = _digest({"pipeline_id": pipeline_id, "collection_config": data.get("collection_config") or {}})
    key = _digest([pipeline_id, request_id])
    with manager.artifacts.batch_lock("pipeline_" + pipeline_id):
        p = manager.get(pipeline_id, public=False)
        intent = manager.records.get("pipeline_generation_confirmations", key)
        if intent:
            if intent["request_digest"] != fingerprint:
                raise _error("相同确认编号对应的采集配置不同")
            if intent.get("status") == "completed":
                return manager.get(pipeline_id)
        else:
            pending = p.get("generation_confirmation") or {}
            if pending.get("status") == "preparing":
                raise _error("已有确认请求正在按冻结配置准备采集，无需重新配置；请查看流程进度")
            if data.get("expected_revision") != p["storage_revision"]:
                raise RevisionConflict("流程已更新，请刷新后确认，当前配置不会丢失")
        preparing = (p.get("generation_confirmation") or {}).get("status") == "preparing"
        if (p.get("start_mode") != "generate" or p.get("batch_id")
                or (p["status"] != WAIT_REVIEW and not (preparing and p["status"] == "running"))):
            raise _error("请先完成生成并审核任务后确认采集")
        generation = getattr(manager.runtime, "generation", None)
        if generation is None:
            raise _error("任务生成服务尚未接入", 503)
        job = manager.records.get("task_generation.jobs", p["generation_job_id"])
        if not job or job.get("status") not in {"succeeded", "partial"}:
            raise _error("生成作业尚未产生可采集的结果")
        if intent is None:
            intent = {
                "pipeline_id": pipeline_id, "request_id": request_id, "request_digest": fingerprint, "status": "preparing",
                "generation_job_id": p["generation_job_id"],
                "collection_config": manager.runtime.collection_options(data.get("collection_config") or {}),
                "config_fingerprints": manager.runtime.configurations(), "created_at": utc_now()}
            p.update(status="running", collection_config=intent["collection_config"], error=None,
                     generation_confirmation={"intent_key": key, "request_id": request_id, "status": "preparing"})
            _step(p).update(status="running", error=None, message="审核已确认，正在准备采集批次")
            p, intent = manager.records.put_many([
                {"namespace": "pipelines", "key": pipeline_id, "payload": p, "expected_revision": p["storage_revision"]},
                {"namespace": "pipeline_generation_confirmations", "key": key, "payload": intent, "expected_revision": 0}])
        _complete_confirmation(manager, p, key, intent)
        return manager.get(pipeline_id)


def _complete_confirmation(manager, p, key, intent):
    """Finish the accepted review using only its persisted input, idempotently."""
    pipeline_id = p["pipeline_id"]
    if intent["generation_job_id"] != p.get("generation_job_id") or intent["pipeline_id"] != pipeline_id:
        raise _error("此确认请求属于其他生成作业，不能替换原冻结配置")
    generation = getattr(manager.runtime, "generation", None)
    if generation is None:
        raise _error("任务生成服务尚未接入", 503)
    # Source publication and result preparation each have durable idempotency.
    source, _ = generation.submit_collection_batch(intent["generation_job_id"])
    options = intent["collection_config"]
    result = manager.runtime.prepare_collection(source["batch_id"], pipeline_id, options)
    batch = result["batch_id"]
    if (result.get("batch_role") != "collection_result" or result.get("task_source_batch_id") != source["batch_id"]
            or batch == source["batch_id"]):
        raise _error("采集结果批次未与生成任务来源正确隔离")
    with manager.artifacts.batch_lock(batch):
        ensure_batch_active(batch, manager.root)
        owner = active_pipeline(batch, manager.root)
        if owner and owner["pipeline_id"] != pipeline_id:
            raise _error("结果批次已由另一流程管理")
        snapshot = manager.runtime.snapshot(batch)
        updated = deepcopy(p)
        updated.update(batch_id=batch, batch_role="collection_result", task_source_batch_id=source["batch_id"],
                 batch_snapshot=snapshot, task_ids=snapshot["task_ids"], collection_config=options,
                 collection_dispatch_key=pipeline_id, config_fingerprints=intent["config_fingerprints"],
                 status="running", current_step="collection", error=None, generation_confirm_request_id=intent.get("request_id"),
                 generation_confirmation={"intent_key": key, "request_id": intent.get("request_id"), "status": "completed"},
                 updated_at=utc_now())
        _step(updated).update(status="succeeded", error=None, message="任务已审核，来源及采集配置已冻结", percent=100, completed_at=utc_now())
        manager._log(updated, "审核确认完成，开始采集；生成来源与结果批次分别保存")
        previous_owner = manager.records.get("pipeline_owners", batch)
        saved = manager.records.put_many([
            {"namespace": "pipelines", "key": pipeline_id, "payload": updated, "expected_revision": p["storage_revision"]},
            {"namespace": "pipeline_owners", "key": batch, "payload": {"pipeline_id": pipeline_id, "batch_id": batch},
             "expected_revision": (previous_owner or {}).get("storage_revision", 0)},
            {"namespace": "pipeline_generation_confirmations", "key": key, "payload": {**intent, "status": "completed", "batch_id": batch},
             "expected_revision": intent["storage_revision"]}])
        return saved[0]
