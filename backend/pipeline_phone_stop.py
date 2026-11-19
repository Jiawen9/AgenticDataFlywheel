"""Reconcile device and evaluation work launched by a terminating Pipeline."""

from .collection_control import TERMINAL
from .data_store.registry import utc_now
from .pipeline_access import internal_pipeline
from .pipeline_diagnostics import sanitize


def collection_owner(records, run):
    """Source references and observed runs are not dispatch ownership."""
    batch_id = run.get("batch_id")
    request_id = run.get("dispatch_key")
    if not batch_id or not request_id:
        return None
    owner = records.get("pipeline_owners", batch_id) or {}
    for pipeline_id in dict.fromkeys((request_id, owner.get("pipeline_id"))):
        if not pipeline_id:
            continue
        pipeline = records.get("pipelines", pipeline_id)
        if (pipeline and pipeline.get("batch_id") == batch_id
                and pipeline.get("start_mode") in {"collect", "generate", "evaluation"}
                and (pipeline.get("collection_dispatch_key") or pipeline_id) == request_id):
            return pipeline
    return None


def dispatch_never_accepted(run):
    if (run.get("dispatch_response") or run.get("dispatched_at")
            or run.get("dispatch_confirmed") or run.get("remote_status")
            or run.get("phone_states")):
        return False
    return (run.get("dispatch_attempted") is False
            or run.get("dispatch_rejected") is True and not run.get("dispatch_uncertain"))


def phones_unconfirmed(states):
    return {phone_id: str(state.get("reason") or "等待采集端确认进程退出")
            for phone_id, state in (states or {}).items()
            if state.get("status") not in TERMINAL or state.get("claimed") is not False
            or state.get("stop_confirmed") is not True}


def devices_stopped(run):
    if (run.get("cancel_response") or {}).get("stop_intent_durable") is False:
        return False
    states = run.get("phone_states")
    if states:
        expected = (run.get("dispatch_response") or {}).get("phone_states") or {}
        return set(expected) <= set(states) and not phones_unconfirmed(states)
    metadata = run.get("request", {}).get("metadata", {})
    # Legacy HTTP collectors have no per-device state. Only use their
    # synchronous stop confirmation or an already observed completion.
    if metadata.get("collection_artifacts_version") != 2:
        return (run.get("control_terminal") and not run.get("can_cancel_collection")
                or run.get("status") == "completed" and not run.get("dispatch_error"))
    return False


def waiting(stage, run_id, message, states=None):
    pending = phones_unconfirmed(states)
    if pending:
        return [{"stage": stage, "job_id": run_id, "phone_id": phone_id,
                 "message": str(sanitize(reason))[:500]}
                for phone_id, reason in sorted(pending.items())]
    return [{"stage": stage, "job_id": run_id, "message": str(sanitize(message))[:500]}]


def _cancel_unlaunched_collection(runtime, run_id):
    def update(current):
        if dispatch_never_accepted(current):
            current.update(stop_requested_at=current.get("stop_requested_at") or utc_now(),
                           status="interrupted", collection_status="cancelled",
                           control_terminal=True, can_cancel_collection=False,
                           collection_dispatch_cancelled=True, updated_at=utc_now())
    return runtime.records.update("collection_runs", run_id, update).get("collection_dispatch_cancelled") is True


def _resume_devices_stopped(records, run, head, operations):
    if not devices_stopped(run) or any(op["status"] in {"pending", "uncertain", "accepted", "preparing"}
                                       for op in operations):
        return False
    executions = [child for child in records.get_many("collection_executions",
                  records.matching_keys("collection_executions", batch_id=run["batch_id"]))
                  if child.get("root_run_id") == run["collection_run_id"]]
    observed = {child["collection_run_id"] for child in executions}
    started = {op.get("execution_id") for op in operations if op["status"] == "started"}
    return (head.get("remote_revision", 0) == len(executions)
            and started <= observed and all(devices_stopped(child) for child in executions))


def _collection_stop(factory, pipeline, run, retry_id):
    run_id = run["collection_run_id"]
    head = factory.records.get("collection_resume_heads", run_id) or {}
    intent = head.get("cancel_intent") or {}
    if head.get("active"):
        operations = [op for op in factory.records.list("collection_resume_operations")
                      if op.get("run_id") == run_id and op.get("epoch", 0) <= head.get("revision", 0)]
        if any(op["status"] in {"pending", "uncertain"} for op in operations):
            return waiting("collection", run_id, "等待核对已提交的续采请求")
        if (intent.get("pipeline_id") == pipeline["pipeline_id"] and intent.get("confirmed")
                and intent.get("epoch") == head.get("revision")):
            return []
        if _resume_devices_stopped(factory.records, run, head, operations):
            return []
        revision_advanced = head.get("remote_revision", 0) > intent.get("expected_revision", 0)
        if (intent.get("pipeline_id") != pipeline["pipeline_id"] or
                intent.get("rejected") and (revision_advanced or retry_id and retry_id != intent.get("retry_id"))):
            factory.resume_service.cancel(run_id, pipeline_id=pipeline["pipeline_id"], retry_id=retry_id)
        diagnostics = factory.resume_service.diagnostics(run_id, "generate")
        phones = diagnostics.get("phones", []) if not diagnostics.get("stale") else []
        pending = [p for p in phones if p.get("process_exit_confirmed") is not True or p.get("status") not in TERMINAL]
        if pending:
            return [{"stage": "collection", "job_id": run_id, "phone_id": p["phone_id"],
                     "message": str(sanitize(p.get("reason") or p.get("message") or "等待采集端确认执行分段退出"))[:500]}
                    for p in pending]
        return waiting("collection", run_id, intent.get("error") or "等待续采分段确认退出")
    if run.get("collection_dispatch_cancelled") or devices_stopped(run):
        return []
    if dispatch_never_accepted(run) and _cancel_unlaunched_collection(factory, run_id):
        return []
    if (not run.get("stop_requested_at") or run.get("pipeline_stop_requested") is not True
            or retry_id and retry_id != run.get("pipeline_stop_retry_id")):
        with internal_pipeline(pipeline["pipeline_id"]):
            factory.cancel_collection(run_id, pipeline_stop=True, pipeline_retry_id=retry_id)
    return waiting("collection", run_id, run.get("control_error") or "等待采集端确认手机释放",
                   run.get("phone_states"))


def _evaluation_stop(factory, pipeline, retry_id):
    pipeline_id = pipeline["pipeline_id"]
    binding = factory.records.get("pipeline_evaluations", pipeline_id) or {}
    if binding.get("created_from_pipeline") is not True:
        return []
    run_id = binding["run_id"]
    run = factory.records.get("model_iter_runs", run_id)
    if not run or run.get("pipeline_id") != pipeline_id:
        return waiting("evaluation", run_id, "等待核对本轮评测运行登记")
    if run.get("pipeline_stop_confirmed"):
        return []
    collection = factory.records.get("collection_runs", run_id) or {}
    if (run.get("status") in TERMINAL and run.get("remote_status") in TERMINAL
            and not run.get("dispatch_error") and not run.get("stop_requested_at")
            and (binding.get("execution_mode") == "evaluate_only" or devices_stopped(collection))):
        return []
    if (not run.get("stop_requested_at") or run.get("pipeline_stop_requested") is not True
            or retry_id and retry_id != run.get("pipeline_stop_retry_id")):
        run = factory.cancel_evaluation(run_id, pipeline_stop=True, pipeline_retry_id=retry_id)
    if run.get("pipeline_stop_confirmed"):
        return []
    return waiting("evaluation", run_id, run.get("control_error") or "等待评测执行退出",
                   collection.get("phone_states"))


def request_phone_stops(runtime, pipeline):
    """Called by the termination worker outside the Pipeline lock."""
    factory = runtime.factory.runtime
    retry_id = (pipeline.get("termination") or {}).get("retry_requested_at")
    pending = []
    if pipeline.get("start_mode") in {"collect", "generate", "evaluation"} and pipeline.get("batch_id"):
        for run in runtime.collection_runs(pipeline["batch_id"]):
            owner = collection_owner(factory.records, run)
            if not owner or owner["pipeline_id"] != pipeline["pipeline_id"]:
                continue
            try:
                pending.extend(_collection_stop(factory, pipeline, run, retry_id))
            except Exception as exc:
                pending.extend(waiting("collection", run["collection_run_id"], exc))
    if pipeline.get("start_mode") == "evaluation":
        try:
            pending.extend(_evaluation_stop(factory, pipeline, retry_id))
        except Exception as exc:
            pending.extend(waiting("evaluation", pipeline["pipeline_id"], exc))
    return pending
