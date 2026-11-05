"""Durable, non-blocking platform dispatch for collector preparation jobs."""
from __future__ import annotations

import json
import time
import uuid

from . import model_iteration
from .collection_runs import _identifier
from .data_store import RevisionConflict
from .data_store.locking import batch_lock
from .data_store.registry import utc_now
from .evaluation_job_previews import remote
from .phone_factory import PhoneFactoryError
from .task_generation.collection_batches import payload_digest

OPTIONS = {"evaluator_config", "evaluation_profile_id", "evaluation_profile_digest", "profile_id", "profile_digest",
           "checklist_source", "evaluation_mode"}
TERMINAL = {"completed", "succeeded", "partial", "failed", "interrupted", "cancelled"}


def accept(runtime, data):
    if set(data) - OPTIONS - {"request_id", "preview_id"}:
        raise PhoneFactoryError("独立评测不接受手机、App、VLA 或采集参数")
    request_id = _identifier(data.get("request_id") or uuid.uuid4().hex, "request_id")
    run_id = "eval_" + payload_digest(request_id)
    pid = _identifier(data.get("preview_id"), "预览编号")
    old = runtime.records.get("model_iter_runs", run_id)
    # Replay precedes current defaults, preview expiry and selectable profile
    # checks: an accepted request must remain the same request after recovery.
    if old:
        from .collector_storage import require_available
        require_available(runtime.records, run_id)
        frozen = old["request"]
        candidate = {k: frozen[k] for k in OPTIONS if k in frozen}
        candidate.update({k: v for k, v in data.items() if k in OPTIONS})
        if "profile_id" in data:
            candidate["evaluation_profile_id"] = data["profile_id"]
        if "profile_digest" in data:
            candidate["evaluation_profile_digest"] = data["profile_digest"]
        options = model_iteration.evaluation_options(runtime.store, candidate, offline=True)
        if frozen.get("preview_id") != pid or any(frozen.get(k) != v for k, v in options.items()):
            raise PhoneFactoryError("相同 request_id 对应的运行参数不同", 409)
        if old.get("stop_requested_at") or old.get("evaluation_control_terminal"):
            return {"ok": True, "reused": True, **runtime._public_run(old)}
        if old.get("dispatch_rejected"):
            old = runtime.records.update("model_iter_runs", run_id, lambda row: row.update(
                dispatch_rejected=False, status="preparing", dispatch_error=None, dispatch_checked_epoch=0))
        schedule(runtime, run_id)
        return {"ok": True, "reused": True, **runtime._public_run(old)}
    record = runtime.records.get("model_iter_previews", pid)
    if not record or not record.get("paged"):
        raise PhoneFactoryError("请重新检查轨迹，使用后台准备预览", 409)
    preview = record["preview"]
    from .collector_storage import require_available
    if record.get("request", {}).get("source_run_id"):
        require_available(runtime.records, record["request"]["source_run_id"])
    if (not preview.get("valid") or preview.get("status") != "completed" or record.get("pending_control")
            or record.get("cached_count", 0) != preview["trajectory_count"]):
        raise PhoneFactoryError("轨迹预览尚未完成或没有可评测轨迹", 409)
    options = model_iteration.evaluation_options(runtime.store, data, offline=True)
    request = {**options, "run_mode": "modeliter", "execution_mode": "evaluate_only", "result_transport": "http",
               "preview_id": pid, "source_preview_id": pid, "source_preview_revision": preview["revision"],
               "source_count": preview["trajectory_count"], "source": preview["source"],
               "source_digest": preview["source_digest"], "source_request": record["request"],
               "collector_server": record["collector_server"], "offline_evaluation_jobs_version": 1,
               "input_mode": "reference"}
    payload = {"run_id": run_id, "collection_run_id": run_id, "run_mode": "modeliter",
               "execution_mode": "evaluate_only", "source": preview["source"], "source_digest": preview["source_digest"],
               "evaluation_profile_id": options["evaluation_profile_id"],
               "evaluation_profile_digest": options["evaluation_profile_digest"],
               "status": "preparing", "filename": "已有轨迹评测", "vla": "", "created_at": utc_now(),
               "request": request, "request_digest": payload_digest(request), "errors": [], "dispatch_error": None,
               "dispatch_attempted": False, "dispatch_confirmed": False, "evaluation_control_epoch": 0,
               "preparation": {"status": "preparing", "phase": "queued", "revision": 0,
                               "progress": {"prepared": 0, "total": preview["trajectory_count"]}}}
    from .display_identifiers import DisplayIdentifierStore
    try:
        saved = DisplayIdentifierStore(runtime.root).create_run("model_iter_runs", run_id, payload)
    except RevisionConflict:
        return accept(runtime, data)
    schedule(runtime, run_id)
    return {"ok": True, **runtime._public_run(saved)}


def schedule(runtime, run_id):
    runtime._evaluation_pool.submit(run_id, lambda: dispatch(runtime, run_id))


def dispatch(runtime, run_id):
    from .collector_storage import unavailable
    if unavailable(runtime.records, run_id):
        return
    run = runtime.records.get("model_iter_runs", run_id)
    if not run or run.get("dispatch_confirmed") or run.get("dispatch_rejected"):
        return
    if run.get("evaluation_control_terminal"):
        return
    epoch = run.get("evaluation_control_epoch", 0)
    try:
        def mark(row):
            if not row.get("dispatch_attempted") and row.get("stop_requested_at"):
                row.update(status="cancelled", evaluation_control_terminal=True,
                           preparation={**row["preparation"], "status": "cancelled", "phase": "cancelled"})
                return
            row.update(dispatch_attempted=True, submitted_at=utc_now())
        run = runtime.records.update("model_iter_runs", run_id, mark)
        if run.get("evaluation_control_terminal"):
            return
        frozen = run["request"]
        if frozen.get("input_mode") == "reference":
            capability = remote(runtime.store, ["capabilities"], frozen["collector_server"])
            if capability.get("offline_evaluation_jobs", {}).get("readonly_input_version") != 1:
                raise PhoneFactoryError("采集服务尚不支持直接读取评测原件，请升级采集端", 409)
        payload = {k: frozen[k] for k in OPTIONS if k in frozen}
        payload.update(collection_run_id=run_id, preview_id=frozen["preview_id"])
        if "input_mode" in frozen:
            payload["input_mode"] = frozen["input_mode"]
        response = remote(runtime.store, ["start-evaluation", "--payload", json.dumps(payload, ensure_ascii=False)],
                          frozen["collector_server"])
        validate(run_id, response)
        def accepted(row):
            if unavailable(runtime.records, run_id):
                return
            # Acknowledgement establishes identity even if a concurrent stop
            # changed the local control epoch; it cannot clear the stop intent.
            row.update(dispatch_confirmed=True, dispatch_error=None, dispatch_checked_epoch=time.time())
            if row.get("evaluation_control_terminal") or row["status"] in {"cancelled", "interrupted", "succeeded", "partial", "completed"}:
                return
            row.update(status="stopping" if row.get("stop_requested_at") and response["status"] not in TERMINAL
                       else response["status"], remote_status=response["status"], updated_at=utc_now())
            if (row.get("evaluation_control_epoch", 0) == epoch and "preparation" in response
                    and response["preparation"].get("revision", 0) >= row.get("preparation", {}).get("revision", 0)):
                row["preparation"] = response["preparation"]
        saved = runtime.records.update("model_iter_runs", run_id, accepted)
        runtime.sync_policy.reset("evaluation", run_id)
        if saved.get("stop_requested_at"):
            runtime._control_pool.submit("evaluation:" + run_id, lambda: runtime._cancel_evaluation_remote(run_id))
    except Exception as exc:
        rejected = isinstance(exc, PhoneFactoryError) and exc.status in {400, 404, 405, 409, 410, 422, 429}
        def failed(row):
            if row.get("evaluation_control_terminal"):
                return
            row.update(dispatch_error=str(exc)[:1000], dispatch_checked_epoch=time.time(), dispatch_rejected=rejected)
            row["dispatch_uncertain"] = row.get("dispatch_uncertain", False) or not rejected
            if rejected and not row.get("stop_requested_at"):
                row.update(status="failed", preparation={**row["preparation"], "status": "failed", "phase": "failed",
                           "error": str(exc)[:1000], "retryable": True})
        runtime.records.update("model_iter_runs", run_id, failed)


def validate(run_id, response):
    if (not isinstance(response, dict) or response.get("ok") is False
            or response.get("run_id", response.get("collection_run_id")) != run_id
            or response.get("run_mode") != "modeliter" or response.get("execution_mode") != "evaluate_only"
            or response.get("status") not in TERMINAL | {"preparing", "queued", "starting", "running", "stopping", "needs_attention"}):
        raise PhoneFactoryError("远端独立评测运行身份或状态不匹配", 502)


def retry(runtime, run_id, body):
    from .collector_storage import require_available
    require_available(runtime.records, run_id)
    if set(body) - {"request_id", "expected_revision"}:
        raise PhoneFactoryError("输入准备重试参数无效")
    request_id = _identifier(body.get("request_id"), "重试请求编号")
    if "expected_revision" in body and (type(body["expected_revision"]) is not int or body["expected_revision"] < 0):
        raise PhoneFactoryError("准备版本无效")
    key = payload_digest([run_id, request_id])
    with batch_lock(runtime.root, "evaluation_preparation_" + run_id):
        run = runtime.records.get("model_iter_runs", run_id)
        if not run or run.get("request", {}).get("offline_evaluation_jobs_version") != 1:
            raise PhoneFactoryError("此运行不支持输入准备恢复", 409)
        previous = runtime.records.get("evaluation_preparation_controls", key)
        if previous:
            if previous["request"] != body:
                raise PhoneFactoryError("相同重试编号对应不同参数", 409)
        else:
            prep = run.get("preparation", {})
            if not prep.get("retryable") or prep.get("status") not in {"failed", "paused"}:
                raise PhoneFactoryError("当前输入准备不能重试", 409)
            if body.get("expected_revision", prep.get("revision", 0)) != prep.get("revision", 0):
                raise PhoneFactoryError("输入准备版本已变化，请刷新后重试", 409)
            if run.get("pending_preparation_control") or run.get("stop_requested_at"):
                raise PhoneFactoryError("运行已有控制请求，请等待完成", 409)
            run["evaluation_control_epoch"] = run.get("evaluation_control_epoch", 0) + 1
            run["pending_preparation_control"] = {"key": key, "body": body, "epoch": run["evaluation_control_epoch"]}
            runtime.records.put_many([
                {"namespace": "evaluation_preparation_controls", "key": key,
                 "payload": {"request": body, "complete": False}, "expected_revision": 0},
                {"namespace": "model_iter_runs", "key": run_id, "payload": run,
                 "expected_revision": run["storage_revision"]}])
    runtime._control_pool.submit("preparation:" + run_id, lambda: retry_remote(runtime, run_id))
    return {"ok": True, **runtime._public_run(run)}


def retry_remote(runtime, run_id):
    from .collector_storage import unavailable
    if unavailable(runtime.records, run_id):
        return
    run = runtime.records.get("model_iter_runs", run_id)
    operation = run.get("pending_preparation_control") if run else None
    if not operation or run.get("stop_requested_at"):
        return
    try:
        if run.get("dispatch_rejected") and not run.get("dispatch_confirmed"):
            runtime.records.update("model_iter_runs", run_id, lambda row: row.update(
                dispatch_rejected=False, dispatch_checked_epoch=0, status="preparing"))
            dispatch(runtime, run_id)
            current = runtime.records.get("model_iter_runs", run_id)
            if current.get("dispatch_confirmed") or current.get("dispatch_rejected"):
                def finish(row):
                    if row.get("evaluation_control_epoch", 0) == operation["epoch"]:
                        row.pop("pending_preparation_control", None)
                runtime.records.update("model_iter_runs", run_id, finish)
                runtime.records.update("evaluation_preparation_controls", operation["key"], lambda row: row.update(complete=True))
            return
        response = remote(runtime.store, ["evaluation-preparation-retry", run_id, "--payload", json.dumps(operation["body"])],
                          run["request"]["collector_server"])
        validate(run_id, response)
        def update(row):
            if row.get("evaluation_control_epoch", 0) != operation["epoch"] or row.get("stop_requested_at"):
                return
            incoming = response.get("preparation", {})
            if (incoming.get("revision", 0) >= row.get("preparation", {}).get("revision", 0)
                    and not (row["status"] in {"succeeded", "partial", "completed", "cancelled", "interrupted"}
                             and response["status"] not in TERMINAL)):
                row.update(status=response["status"], preparation=incoming, dispatch_error=None,
                           evaluation_control_terminal=False)
            row["preparation_control_error"] = None
            row.pop("pending_preparation_control", None)
        runtime.records.update("model_iter_runs", run_id, update)
        runtime.records.update("evaluation_preparation_controls", operation["key"], lambda row: row.update(complete=True))
        runtime.sync_policy.reset("evaluation", run_id)
    except Exception as exc:
        def failed(row):
            if row.get("evaluation_control_epoch", 0) == operation["epoch"]:
                row.update(preparation_control_error=str(exc)[:1000], preparation_control_checked_epoch=time.time())
                if isinstance(exc, PhoneFactoryError) and exc.status in {400, 404, 405, 409, 422}:
                    row.pop("pending_preparation_control", None)
        runtime.records.update("model_iter_runs", run_id, failed)


def schedule_previews(runtime):
    from .evaluation_previews import schedule as preview_schedule
    for row in runtime.records.list("model_iter_previews"):
        value = row.get("preview", {})
        if (row.get("paged") and time.time() >= row.get("checked_epoch", 0) + 2
                and (row.get("force_refresh") or row.get("pending_control") or value.get("status") == "preparing"
                     or value.get("valid") and row.get("cached_count", 0) < value.get("trajectory_count", 0))):
            preview_schedule(runtime.store, row["preview_id"])
    for run in runtime.records.list("model_iter_runs"):
        if run.get("pending_preparation_control") and time.time() >= run.get("preparation_control_checked_epoch", 0) + 5:
            rid = run["run_id"]
            runtime._control_pool.submit("preparation:" + rid, lambda rid=rid: retry_remote(runtime, rid))
