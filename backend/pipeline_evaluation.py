"""Platform-owned evaluation entry and handoff to the existing augmentation stage."""
from .badcases import BadCaseError, BadCaseService, digest
from .data_store import RevisionConflict
from .data_store.artifacts import _identifier
from .data_store.locking import batch_lock
from .data_store.registry import utc_now
from .pipeline_access import TERMINAL


WAIT_EVALUATION = "waiting_for_evaluation"
EVAL_TERMINAL = {"completed", "succeeded", "partial", "failed", "cancelled", "interrupted"}


def _error(message, status=409):
    from .pipelines import PipelineError
    return PipelineError(message, status)


def _step(pipeline):
    return next(step for step in pipeline["steps"] if step["id"] == "evaluation")


def binding_for(pipeline_id, run, **context):
    request = run.get("request", {})
    return {"pipeline_id": pipeline_id, "run_id": run["run_id"],
            "execution_mode": request.get("execution_mode", "collect_and_evaluate"),
            "source": request.get("source"), "batch_id": run.get("batch_id"),
            "evaluation_profile_id": request.get("evaluation_profile_id"),
            "evaluation_profile_digest": request.get("evaluation_profile_digest"), **context}


def check_start(runtime, context, run_id):
    """Call under the Pipeline lock when registering a new evaluation run."""
    from .phone_factory import PhoneFactoryError
    pipeline_id = context["pipeline_id"]
    pipeline = runtime.records.get("pipelines", pipeline_id)
    if not pipeline:
        raise PhoneFactoryError("Pipeline 不存在", 404)
    if pipeline.get("start_mode") != "evaluation" or pipeline.get("current_step") != "evaluation":
        raise PhoneFactoryError("本轮 Pipeline 当前不接受新的评测", 409)
    if pipeline["status"] != WAIT_EVALUATION:
        raise PhoneFactoryError("请先继续或重试本轮评测阶段", 409)
    binding = runtime.records.get("pipeline_evaluations", pipeline_id)
    if binding and binding["run_id"] != run_id:
        raise PhoneFactoryError("本轮已有评测运行，请使用该运行提取 BadCase", 409)


def start_context(runtime, data, execution_mode):
    """Remove platform context before request normalization and collector dispatch."""
    if "pipeline_id" not in data:
        return data, None, None
    from .phone_factory import PhoneFactoryError
    from .task_generation.collection_batches import payload_digest
    original = dict(data)
    try:
        pipeline_id = _identifier(original.pop("pipeline_id"), "Pipeline ID")
        request_id = _identifier(original.get("request_id"), "评测请求编号")
    except ValueError as exc:
        raise PhoneFactoryError(str(exc), 422) from exc
    context = {"pipeline_id": pipeline_id, "pipeline_request_digest": digest([execution_mode, original])}
    # The ordinary transport continues using the same run-ID contract. Scope
    # user request IDs to their round so standalone evaluations cannot collide.
    original["request_id"] = digest(["pipeline-evaluation", pipeline_id, request_id])
    run_id = "eval_" + payload_digest(original["request_id"])
    existing = runtime.records.get("model_iter_runs", run_id)
    if existing:
        if any(existing.get(key) != value for key, value in context.items()):
            raise PhoneFactoryError("相同评测请求编号对应的运行参数不同", 409)
    else:
        check_start(runtime, context, run_id)
    return original, context, existing


def create_run(runtime, run_id, payload, context=None):
    from .display_identifiers import DisplayIdentifierStore
    store = DisplayIdentifierStore(runtime.root)
    if context is None:
        return store.create_run("model_iter_runs", run_id, payload)
    with batch_lock(runtime.root, "pipeline_" + context["pipeline_id"]):
        if runtime.records.get("model_iter_runs", run_id):
            raise RevisionConflict("运行记录已存在")
        check_start(runtime, context, run_id)
        return store.create_run("model_iter_runs", run_id, {**payload, **context},
                                pipeline_binding=binding_for(context["pipeline_id"], payload, created_from_pipeline=True))


def may_dispatch(records, run, *, allow_stop=False):
    """A replay of a retired or paused round may read its run, but may not launch it."""
    pipeline_id = run.get("pipeline_id")
    if pipeline_id is None:
        return True
    binding = records.get("pipeline_evaluations", pipeline_id)
    if not binding or binding["run_id"] != run["run_id"]:
        return False
    if allow_stop and run.get("stop_requested_at"):
        return True
    pipeline = records.get("pipelines", pipeline_id) or {}
    return pipeline.get("current_step") == "evaluation" and pipeline.get("status") == WAIT_EVALUATION


def dispatch_guard(runtime, run):
    """Only held for local dispatch intent registration. The POST runs unlocked."""
    from contextlib import nullcontext
    return (batch_lock(runtime.root, "pipeline_" + run["pipeline_id"])
            if run.get("pipeline_id") else nullcontext())


def confirm_badcases(manager, pipeline_id, data):
    _identifier(pipeline_id, "Pipeline ID")
    request_id = _identifier(data.get("request_id"), "回流请求编号")
    run_id = _identifier(data.get("run_id"), "评测运行编号")
    ids = sorted(set(data["candidate_ids"]))
    fingerprint = digest([run_id, ids])
    with manager.artifacts.batch_lock("pipeline_" + pipeline_id):
        pipeline = manager.get(pipeline_id, public=False)
        saved = pipeline.get("badcase_confirmation")
        if saved and saved["request_id"] == request_id:
            if saved["request_digest"] != fingerprint:
                raise _error("相同回流编号对应的评测或候选不同")
            return manager.get(pipeline_id)
        if pipeline.get("start_mode") != "evaluation" or pipeline["status"] != WAIT_EVALUATION or pipeline["current_step"] != "evaluation":
            raise _error("当前 Pipeline 不接受新的回流选择")
        if data["expected_revision"] != pipeline["storage_revision"]:
            raise RevisionConflict("流程已更新，请刷新后确认，当前选择不会丢失")
        binding = manager.records.get("pipeline_evaluations", pipeline_id)
        if binding and binding["run_id"] != run_id:
            raise _error("本轮已有评测运行，不能替换已登记的来源")
        run = manager.records.get("model_iter_runs", run_id)
        if not run:
            raise _error("评测运行不存在", 404)
        service = BadCaseService(manager.root)
        with batch_lock(manager.root, "badcase_" + digest(run_id)[:24]):
            head = manager.records.get("badcase_heads", run_id) or {}
            if head.get("status") != "ready" or head.get("final") is not True:
                raise _error("请等待评测结束和 BadCase 整理完成后确认回流")
            if ids:
                try:
                    feedback = service.freeze(run_id, ids, digest([pipeline_id, request_id]))
                except BadCaseError as exc:
                    raise _error(str(exc), exc.status) from exc
                pipeline.update(status="waiting_for_generation_config", current_step="generation",
                                feedback_id=feedback["feedback_id"], error=None)
                next(step for step in pipeline["steps"] if step["id"] == "generation").update(
                    status="waiting", message="已确认 BadCase，请配置任务扩增")
                message = f"已冻结 {len(ids)} 项 BadCase，等待扩增"
            else:
                counts = head.get("summary", {})
                if counts.get("candidate", 0) or counts.get("needs_completion", 0):
                    raise _error("仍有业务 BadCase，请选择回流项或补全任务信息")
                pipeline.update(status="no_publishable_data", error=None)
                message = "无可回流项"
                for step in pipeline["steps"]:
                    if step["id"] != "evaluation":
                        step.update(status="skipped", message=message)
            _step(pipeline).update(status="succeeded", job_ids=[run_id], percent=100, message=message,
                                   completed_at=utc_now(), error=None)
            pipeline.update(source_evaluation_run_id=run_id,
                            badcase_confirmation={"request_id": request_id, "request_digest": fingerprint},
                            updated_at=utc_now())
            manager._log(pipeline, message)
            entries = [{"namespace": "pipelines", "key": pipeline_id, "payload": pipeline,
                        "expected_revision": pipeline["storage_revision"]}]
            if not binding:
                entries.append({"namespace": "pipeline_evaluations", "key": pipeline_id,
                                "payload": binding_for(pipeline_id, run), "expected_revision": 0})
            manager.records.put_many(entries)
        return manager.get(pipeline_id)


def tick(manager, pipeline):
    if pipeline["status"] in TERMINAL | {"paused", "failed"}:
        return
    binding = manager.records.get("pipeline_evaluations", pipeline["pipeline_id"])
    run = manager.records.get("model_iter_runs", binding["run_id"]) if binding else None
    step = _step(pipeline)
    if pipeline["status"] == "terminating":
        if not run or run["status"] in EVAL_TERMINAL or run.get("dispatch_attempted") is False:
            pipeline.update(status="terminated", error=None)
            step.update(status="skipped", message="已终止后续推进，保留评测结果", completed_at=utc_now())
        return
    if not run:
        return
    head = manager.records.get("badcase_heads", run["run_id"]) or {}
    if head.get("final") and head.get("status") == "ready":
        counts = head.get("summary", {})
        message = "请选择回流 BadCase" if counts.get("candidate", 0) or counts.get("needs_completion", 0) else "无可回流项"
        step.update(status="waiting", percent=100, message=message)
    elif run["status"] in {"failed", "cancelled", "interrupted"} and (
            run.get("dispatch_attempted") is False or run.get("dispatch_rejected") and not run.get("dispatch_uncertain")
            or run.get("evaluation_control_terminal") or run.get("dispatch_confirmed") and run.get("preparation", {}).get("status") == "failed"):
        step.update(status="failed", error=run.get("dispatch_error") or "评测未产生结果，可重试本轮评测")
        pipeline.update(status="failed", error=step["error"])
    elif run["status"] in EVAL_TERMINAL:
        step.update(status="waiting", message="等待评测结果同步与 BadCase 整理")
    else:
        step.update(status="running", message="评测运行中")
    step.update(job_ids=[run["run_id"]], started_at=run.get("created_at"))


def control(manager, pipeline, action):
    status = pipeline["status"]
    if action == "pause" and status == WAIT_EVALUATION:
        pipeline.update(status="paused", paused_from=WAIT_EVALUATION)
    elif action == "resume" and status == "paused":
        pipeline.update(status=pipeline.pop("paused_from", WAIT_EVALUATION))
    elif action == "terminate" and status not in TERMINAL:
        pipeline.update(status="terminating")
        tick(manager, pipeline)
    elif action == "retry" and status == "failed":
        identity = pipeline["pipeline_id"]
        binding = manager.records.get("pipeline_evaluations", identity)
        if binding:
            previous = list(pipeline.get("previous_evaluation_run_ids", []))
            pipeline["previous_evaluation_run_ids"] = [*previous, binding["run_id"]]
        pipeline.update(status=WAIT_EVALUATION, error=None, updated_at=utc_now(),
                        evaluation_attempt=pipeline.get("evaluation_attempt", 0) + 1)
        _step(pipeline).update(status="waiting", error=None, job_ids=[], started_at=None,
                               percent=0, message="请重新开始评测或选择已有评测")
        manager.records.put_many([{"namespace": "pipelines", "key": identity, "payload": pipeline,
                                  "expected_revision": pipeline["storage_revision"]}],
                                 deletes=[{"namespace": "pipeline_evaluations", "key": identity,
                                           "expected_revision": binding["storage_revision"]}] if binding else None)
        return True
    else:
        raise _error("当前状态不能执行该操作")
    return False
