"""Durable preview acceptance and small, immutable page records.

HTTP requests never fetch a whole remote preview. File lists remain at the
collector; the platform stores only bounded public pages and source identities.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from collections.abc import Mapping

from .collection_runs import _identifier
from .data_store import RevisionConflict
from .data_store.locking import batch_lock
from .data_store.registry import utc_now
from .phone_factory import PhoneFactoryError

PAGE_SIZE = 100


def preview_id(request_id):
    return "ep_" + hashlib.sha256(request_id.encode("utf-8")).hexdigest()[:40]


def endpoint():
    return os.environ.get("PHONE_FACTORY_SERVER", "http://localhost:9011").rstrip("/")


def remote(store, args, server):
    try:
        return store._remote_result([*args, "--server", server])
    except PhoneFactoryError as exc:
        if exc.remote_status in {400, 404, 405, 409, 410, 422, 429}:
            raise PhoneFactoryError(str(exc), exc.remote_status, remote_kind=exc.remote_kind,
                                    remote_status=exc.remote_status, remote_command=args[0]) from exc
        raise


def _summary(value):
    return {k: v for k, v in value.items() if k not in {"cases", "case_offset", "has_more", "files"}}


def _pages_namespace(pid, revision):
    return f"evaluation_preview_pages:{pid}:{revision}"


def _cases_namespace(pid, revision):
    return f"evaluation_preview_cases:{pid}:{revision}"


def accept_request(store, source, request_id, *, original_source=None):
    from .collector_storage import require_available
    if source.get("source_run_id"):
        require_available(store.records, source["source_run_id"])
    from .evaluation_previews import schedule
    request_id = _identifier(request_id, "预览请求编号")
    pid = preview_id(request_id)
    original = original_source or source
    with batch_lock(store.root, "evaluation_preview_" + pid):
        old = store.records.get("model_iter_previews", pid)
        if old:
            if old.get("original_request", old["request"]) != original:
                raise PhoneFactoryError("相同预览 request_id 对应不同的轨迹来源", 409)
        else:
            value = {"preview_id": pid, "job_version": 1, "status": "preparing", "phase": "queued",
                     "revision": 0, "valid": False, "trajectory_count": 0, "errors": [],
                     "progress": {"discovered": 0, "checked": 0, "eligible": 0, "excluded": 0},
                     "source": {"kind": "directory", "path": source["source_path"]} if "source_path" in source
                               else {"kind": "run", "run_id": source["source_run_id"]}}
            store.records.put("model_iter_previews", pid, {"preview_id": pid, "request_id": request_id,
                "original_request": original, "request": source, "collector_server": endpoint(),
                "preview": value, "paged": True, "submitted": False, "cached_count": 0,
                "control_epoch": 0, "input_mode": "reference", "created_at": utc_now()}, expected_revision=0)
    schedule(store, pid)
    return state(store, pid)


def accept_remote(store, source, value, *, server=None):
    """Compatibility entry for callers without client-generated request IDs."""
    from .evaluation_previews import validate, schedule
    validate(value)
    pid = value["preview_id"]
    old = store.records.get("model_iter_previews", pid)
    if old and old["request"] != source:
        raise PhoneFactoryError("远端复用了其他来源的预览编号", 502)
    if not old:
        store.records.put("model_iter_previews", pid, {"preview_id": pid, "request": source,
            "collector_server": server or endpoint(), "preview": _summary(value), "paged": True,
            "submitted": True, "cached_count": 0, "control_epoch": 0, "created_at": utc_now()}, expected_revision=0)
    save_page(store, pid, value, 0, (old or {}).get("control_epoch", 0))
    schedule(store, pid)
    return state(store, pid)


def save_page(store, pid, value, offset, epoch):
    from .evaluation_previews import validate
    validate(value)
    if value.get("preview_id") != pid or value.get("case_offset", 0) != offset:
        raise PhoneFactoryError("远端预览页身份或页码不匹配", 502)
    revision = value.get("revision", 0)
    if type(revision) is not int or revision < 0:
        raise PhoneFactoryError("远端预览版本无效", 502)
    cases = value["cases"]
    if len(cases) > PAGE_SIZE or offset + len(cases) > value["trajectory_count"]:
        raise PhoneFactoryError("远端预览页超过声明数量", 502)
    if value.get("has_more") and not cases and value.get("status") != "preparing":
        raise PhoneFactoryError("远端预览分页未推进", 502)
    with batch_lock(store.root, "evaluation_preview_" + pid):
        old = store.records.get("model_iter_previews", pid)
        if not old or old.get("control_epoch", 0) != epoch:
            return
        previous = old["preview"]
        if revision < previous.get("revision", 0):
            return
        same = revision == previous.get("revision", 0)
        if same and previous.get("valid") and (previous.get("source_digest") != value.get("source_digest")
                                              or previous.get("source") != value.get("source")
                                              or previous.get("trajectory_count") != value.get("trajectory_count")):
            raise PhoneFactoryError("已完成预览来源、数量或摘要变化", 502)
        if offset and (not same or previous.get("source_digest") != value.get("source_digest")
                       or previous.get("source") != value.get("source")):
            raise PhoneFactoryError("远端预览分页版本发生变化", 502)
        cached = old.get("cached_count", 0) if same else 0
        entries = []
        # Submission responses may use a smaller default page. Cache only our
        # aligned pages, otherwise a subsequent offset=100 lookup could miss rows.
        cache_page = (offset % PAGE_SIZE == 0 and
                      (len(cases) == PAGE_SIZE or offset + len(cases) == value["trajectory_count"]))
        if value.get("valid") and cache_page:
            for number, row in enumerate(cases, offset):
                identity = store.records.get(_cases_namespace(pid, revision), row["source_trajectory_id"])
                if identity and identity != {**row, "ordinal": number, "storage_revision": identity["storage_revision"]}:
                    raise PhoneFactoryError("远端预览跨页重复或改变轨迹身份", 502)
                if not identity:
                    entries.append({"namespace": _cases_namespace(pid, revision), "key": row["source_trajectory_id"],
                                    "payload": {**row, "ordinal": number}, "expected_revision": 0})
            entries.append({"namespace": _pages_namespace(pid, revision), "key": str(offset),
                            "payload": {"offset": offset, "cases": cases}})
            if offset <= cached:
                cached = max(cached, offset + len(cases))
        updated = {**old, "preview": _summary(value), "submitted": True, "cached_count": cached,
                   "sync_error": None, "force_refresh": False, "checked_epoch": time.time(), "updated_at": utc_now()}
        entries.append({"namespace": "model_iter_previews", "key": pid, "payload": updated,
                        "expected_revision": old["storage_revision"]})
        store.records.put_many(entries)


def refresh(store, pid):
    old = store.records.get("model_iter_previews", pid)
    if not old or not old.get("paged"):
        return
    if old["preview"].get("status") in {"failed", "cancelled"} and not old.get("pending_control") and not old.get("force_refresh"):
        return
    epoch = old.get("control_epoch", 0)
    try:
        if not old.get("submitted"):
            capability = remote(store, ["capabilities"], old["collector_server"])
            if capability.get("offline_evaluation_jobs", {}).get("version") != 1:
                raise PhoneFactoryError("采集服务尚未升级目录后台预览与输入准备能力，请先升级 9011", 409)
            if old.get("input_mode") == "reference" and capability.get("offline_evaluation_jobs", {}).get("readonly_input_version") != 1:
                raise PhoneFactoryError("采集服务尚不支持直接读取评测原件，请升级采集端", 409)
            # Durable marker makes cancellation after a lost acknowledgement
            # reconcile the request instead of declaring a remote job absent.
            def begin(row):
                if row.get("control_epoch", 0) == epoch and row["preview"].get("status") != "cancelled":
                    row["submit_started"] = True
            started = store.records.update("model_iter_previews", pid, begin)
            if started.get("control_epoch", 0) != epoch or started["preview"].get("status") == "cancelled":
                return
            value = remote(store, ["evaluation-preview", "--payload", json.dumps(
                {**old["request"], "request_id": old["request_id"]}, ensure_ascii=False)], old["collector_server"])
            save_page(store, pid, value, 0, epoch)
            old = store.records.get("model_iter_previews", pid)
            if old.get("control_epoch", 0) != epoch:
                return
        if old.get("pending_control"):
            operation = old["pending_control"]
            body = dict(operation["body"])
            if operation.get("unacknowledged"):
                body["expected_revision"] = old["preview"].get("revision", 0)
            value = remote(store, ["evaluation-preview-" + operation["action"], pid,
                "--payload", json.dumps(body)], old["collector_server"])
            save_page(store, pid, value, 0, epoch)
            def finish(row):
                if row.get("control_epoch", 0) == epoch:
                    row.pop("pending_control", None)
            store.records.update("model_iter_previews", pid, finish)
            store.records.update("evaluation_preview_controls", operation["key"], lambda row: row.update(complete=True))
            return
        value = old["preview"]
        offset = old.get("cached_count", 0) if value.get("valid") and not old.get("force_refresh") else 0
        if not old.get("force_refresh") and (value.get("status") in {"failed", "cancelled"}
                or value.get("valid") and offset >= value["trajectory_count"]):
            return
        # One page per worker invocation keeps other requests fair.
        page = remote(store, ["evaluation-preview-status", pid, "--offset", str(offset), "--limit", str(PAGE_SIZE)],
                      old["collector_server"])
        save_page(store, pid, page, offset, epoch)
    except Exception as exc:
        rejected_control = bool(old.get("pending_control") and isinstance(exc, PhoneFactoryError)
                                and exc.status in {400, 404, 405, 409, 422, 429})
        def failed(row):
            if row.get("control_epoch", 0) != epoch:
                return
            row.update(sync_error=str(exc)[:1000], checked_epoch=time.time())
            if rejected_control:
                # Progress may advance the collector revision between display
                # and click. A definitive stale-control response ends this
                # operation; never retry an obsolete expected_revision forever.
                row.pop("pending_control", None)
                row["control_error"] = str(exc)[:1000]
                row["force_refresh"] = True
                return
            if isinstance(exc, PhoneFactoryError) and exc.status in {400, 404, 405, 409, 410, 422, 429}:
                row["preview"] = {**row["preview"], "status": "failed", "valid": False,
                    "phase": "failed", "retryable": True, "errors": [{"error": str(exc)[:1000]}]}
        store.records.update("model_iter_previews", pid, failed)
        if rejected_control:
            store.records.update("evaluation_preview_controls", old["pending_control"]["key"],
                                 lambda row: row.update(complete=True, error=str(exc)[:1000]))


def state(store, pid, offset=0, limit=50):
    from .evaluation_previews import schedule
    from .collection_resume_snapshots import evaluation_snapshot_status
    offset, limit = int(offset), int(limit)
    if offset < 0 or not 1 <= limit <= 100:
        raise PhoneFactoryError("预览分页无效")
    row = store.records.get("model_iter_previews", pid)
    if not row:
        raise PhoneFactoryError("预览不存在", 404)
    value = dict(row["preview"])
    complete = value.get("valid") and row.get("cached_count", 0) == value["trajectory_count"]
    if row.get("force_refresh") or row.get("pending_control") or value.get("status") == "preparing" or value.get("valid") and not complete:
        if time.time() >= row.get("checked_epoch", 0) + 1:
            schedule(store, pid)
    cases = []
    if complete:
        for begin in range(offset // PAGE_SIZE * PAGE_SIZE, offset + limit, PAGE_SIZE):
            page = store.records.get(_pages_namespace(pid, value.get("revision", 0)), str(begin)) or {}
            cases.extend(page.get("cases", []))
        start = offset % PAGE_SIZE
        cases = cases[start:start + limit]
    elif value.get("valid"):
        value.update(status="preparing", valid=False, phase="syncing")
    if row.get("pending_control"):
        value.update(phase="cancelling" if row["pending_control"]["action"] == "cancel" else "retrying")
    return {**value, "cases": cases, "case_offset": offset,
            "has_more": bool(complete and offset + len(cases) < value["trajectory_count"]),
            **evaluation_snapshot_status(store.records, row.get("request", {})),
            **({"control_error": row["control_error"]} if row.get("control_error") else {}),
            **({"sync_error": row["sync_error"]} if row.get("sync_error") else {})}


def control(store, pid, action, body):
    from .evaluation_previews import schedule
    if action not in {"cancel", "retry"} or set(body) - {"request_id", "expected_revision"}:
        raise PhoneFactoryError("预览控制参数无效")
    request_id = _identifier(body.get("request_id"), "操作请求编号")
    if "expected_revision" in body and (type(body["expected_revision"]) is not int or body["expected_revision"] < 0):
        raise PhoneFactoryError("预览版本无效")
    key = hashlib.sha256(json.dumps([pid, action, request_id]).encode()).hexdigest()
    with batch_lock(store.root, "evaluation_preview_" + pid):
        row = store.records.get("model_iter_previews", pid)
        if not row:
            raise PhoneFactoryError("预览不存在", 404)
        if not row.get("paged"):
            raise PhoneFactoryError("旧预览不支持任务控制，请重新检查", 409)
        previous = store.records.get("evaluation_preview_controls", key)
        if previous and previous["request"] != body:
            raise PhoneFactoryError("相同操作编号对应不同的预览控制", 409)
        if previous and previous.get("complete"):
            return state(store, pid)
        if not previous:
            if "expected_revision" in body and body["expected_revision"] != row["preview"].get("revision", 0):
                raise PhoneFactoryError("预览版本已变化，请刷新后重试", 409)
            if row.get("pending_control"):
                raise PhoneFactoryError("已有预览控制正在处理中", 409)
            row.update(control_epoch=row.get("control_epoch", 0) + 1)
            row.pop("control_error", None)
            row["sync_error"] = None
            row["pending_control"] = {"action": action, "body": body, "key": key,
                                      "unacknowledged": not row.get("submitted")}
            if action == "retry" and not row.get("submitted"):
                row["preview"].update(status="preparing", phase="queued", valid=False, errors=[])
                row.pop("pending_control", None)
            if action == "cancel" and not row.get("submitted") and not row.get("submit_started"):
                row["preview"].update(status="cancelled", phase="cancelled", valid=False,
                                      revision=row["preview"].get("revision", 0) + 1)
                row.pop("pending_control", None)
                row["force_refresh"] = False
            store.records.put_many([
                {"namespace": "evaluation_preview_controls", "key": key,
                 "payload": {"request": body, "epoch": row["control_epoch"], "complete": not row.get("pending_control")}, "expected_revision": 0},
                {"namespace": "model_iter_previews", "key": pid, "payload": row,
                 "expected_revision": row["storage_revision"]}])
    schedule(store, pid)
    return state(store, pid)


class SourceCases(Mapping):
    """Identity lookup without embedding all preview rows in each run update."""
    def __init__(self, records, pid, digest, *, revision=None, total=None):
        row = records.get("model_iter_previews", pid)
        if not row or revision is None and row["preview"].get("source_digest") != digest:
            raise PhoneFactoryError("冻结预览摘要不一致", 502)
        self.records, self.pid = records, pid
        self.revision = row["preview"].get("revision", 0) if revision is None else revision
        self.total = row["preview"]["trajectory_count"] if total is None else total

    def __len__(self):
        return self.total

    def __getitem__(self, key):
        row = self.records.get(_cases_namespace(self.pid, self.revision), key)
        if row is None:
            raise KeyError(key)
        return row

    def __iter__(self):
        for offset in range(0, self.total, PAGE_SIZE):
            page = self.records.get(_pages_namespace(self.pid, self.revision), str(offset)) or {}
            yield from (r["source_trajectory_id"] for r in page.get("cases", []))
