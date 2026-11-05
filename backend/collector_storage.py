"""Durable collector-only storage operations. Never delete platform evidence."""
from __future__ import annotations

import hashlib
import json
import time
import os
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask

from .collection_runs import _identifier
from .data_store import RevisionConflict
from .data_store.locking import batch_lock
from .data_store.registry import utc_now
from .evaluation_job_previews import endpoint, remote
from .phone_factory import PhoneFactoryError
from .remote_sync import BoundedSyncPool

OPERATIONS = "collector_storage_operations"
STATES = "collector_storage_runs"
UNAVAILABLE = {"retiring", "retired", "cleanup_failed"}
TERMINAL = {"completed", "partial", "failed"}


def state(records, run_id):
    value = records.get(STATES, run_id)
    if value:
        return value
    execution = records.get("collection_executions", run_id)
    if execution and execution.get("root_run_id"):
        return records.get(STATES, execution["root_run_id"]) or {}
    return {}


def unavailable(records, run_id):
    return state(records, run_id).get("collector_data_state") in UNAVAILABLE


def require_available(records, run_id):
    if unavailable(records, run_id):
        raise PhoneFactoryError("采集端数据正在清理或已清理，不能继续读取、续采或回传", 410)


def public_state(records, run_id):
    row = state(records, run_id)
    return {key: row[key] for key in ("collector_data_state", "cleanup_operation_id", "cleaned_at", "cleanup_error") if key in row}


class StorageService:
    def __init__(self, runtime):
        self.runtime, self.records = runtime, runtime.records
        self.pool = BoundedSyncPool("collector-storage", max_workers=2)

    def close(self):
        self.pool.close()

    def _run(self, run_id):
        run_id = _identifier(run_id, "运行编号")
        for namespace in ("collection_runs", "model_iter_runs", "collection_executions"):
            value = self.records.get(namespace, run_id)
            if value:
                if namespace == "collection_executions":
                    return self._run(value["root_run_id"])
                return value
        raise PhoneFactoryError("平台运行记录不存在", 404)

    def _scope(self, run_id):
        return [run_id, *[row["collection_run_id"] for row in self.records.list("collection_executions")
                         if row.get("root_run_id") == run_id]]

    def _mark(self, operation, value, affected=(), error=None):
        ids = set(self._scope(operation["run_id"])) | set(affected)
        for run_id in ids:
            _identifier(run_id, "运行编号")
            existing = self.records.get(STATES, run_id) or {}
            if (existing.get("collector_data_state") == value and existing.get("cleanup_operation_id") == operation["operation_id"]
                    and existing.get("cleanup_error") == error):
                continue
            def transition(old):
                if old.get("collector_data_state") == "retired" and value != "retired":
                    return
                if old.get("cleanup_created_at", "") > operation["created_at"]:
                    return
                old.update(collector_data_state=value, cleanup_operation_id=operation["operation_id"],
                    cleanup_created_at=operation["created_at"], updated_at=utc_now(), cleanup_error=error,
                    **({"cleaned_at": utc_now()} if value == "retired" else {}))
            committed = self.records.update(STATES, run_id, transition, default={"run_id": run_id})
            if committed.get("cleanup_operation_id") != operation["operation_id"] or committed.get("collector_data_state") != value:
                continue
            for namespace in ("collection_runs", "model_iter_runs", "collection_executions"):
                if self.records.get(namespace, run_id):
                    def mark_run(old):
                        old.update(collector_data_state=value, cleanup_operation_id=operation["operation_id"])
                        if value == "retired" and old.get("status") in {"running", "starting", "preparing", "queued", "pending", "dispatching", "stopping", "needs_attention"}:
                            old.update(cleanup_previous_status=old["status"], status="cancelled", control_terminal=True,
                                       evaluation_control_terminal=True, stop_reason="collector_data_cleanup", updated_at=utc_now())
                    self.records.update(namespace, run_id, mark_run)

    def public(self, row):
        hidden = {"collector_server", "body", "signature", "remote_operation_id", "next_retry", "submitted", "attempted"}
        value = {k: v for k, v in row.items() if k not in hidden}
        value["collector_data_state"] = state(self.records, row["run_id"]).get("collector_data_state", "available")
        return value

    def accept(self, run_id, kind, body):
        if not isinstance(body, dict) or set(body) - {"request_id", "mode"}:
            raise PhoneFactoryError("存储操作仅接受请求编号及清理模式")
        if kind not in {"scan", "cleanup", "archive"}:
            raise PhoneFactoryError("未知存储操作", 404)
        run = self._run(run_id)
        run_id = run.get("root_run_id") or run.get("collection_run_id") or run["run_id"]
        mode = body.get("mode", "retire") if kind == "cleanup" else None
        if mode is not None and mode not in {"retire", "cache"}:
            raise PhoneFactoryError("清理模式必须为 retire 或 cache")
        request_id = _identifier(body.get("request_id") or (uuid.uuid4().hex if kind == "scan" else ""), "请求编号")
        operation_id = "sto_" + hashlib.sha256(request_id.encode()).hexdigest()[:40]
        signature = [run_id, kind, mode]
        with batch_lock(self.runtime.root, "storage_" + operation_id):
            old = self.records.get(OPERATIONS, operation_id)
            if old and old["signature"] != signature:
                raise PhoneFactoryError("同一清理请求编号对应不同的运行或操作", 409)
            if old:
                if old["status"] in {"failed", "partial"}:
                    old = self.records.update(OPERATIONS, operation_id, lambda row: row.update(
                        status="queued", phase="queued", error=None, next_retry=0))
            else:
                if kind == "archive":
                    require_available(self.records, run_id)
                request = run.get("request", {})
                server = request.get("collector_server") or request.get("metadata", {}).get("collector_server") or endpoint()
                payload = {"request_id": request_id, **({"mode": mode} if mode else {})}
                old = self.records.put(OPERATIONS, operation_id, {
                    "operation_id": operation_id, "request_id": request_id, "run_id": run_id,
                    "kind": kind, "mode": mode, "signature": signature, "body": payload,
                    "collector_server": server, "status": "queued", "phase": "queued",
                    "created_at": utc_now(), "updated_at": utc_now(), "next_retry": 0,
                    "submitted": False, "attempted": False, "errors": [], "summary": {},
                }, expected_revision=0)
        if old["status"] not in TERMINAL:
            self.pool.submit(operation_id, lambda: self.work(operation_id))
        return self.public(old)

    def schedule(self):
        for row in self.records.list(OPERATIONS):
            if row["status"] not in TERMINAL and row.get("next_retry", 0) <= time.time():
                oid = row["operation_id"]
                self.pool.submit(oid, lambda oid=oid: self.work(oid))

    def work(self, operation_id):
        row = self.records.get(OPERATIONS, operation_id)
        if not row or row["status"] in TERMINAL:
            return
        retiring = row["kind"] == "cleanup" and row["mode"] == "retire"
        try:
            if not row.get("submitted"):
                capability = remote(self.runtime.store, ["capabilities"], row["collector_server"])
                feature = "archive_on_demand" if row["kind"] == "archive" else "storage_management"
                if not isinstance(capability, dict) or capability.get(feature, {}).get("version") != 1:
                    raise PhoneFactoryError("采集端尚不支持此存储操作，请升级采集端", 409)
                if retiring:
                    self._mark(row, "retiring")
                self.records.update(OPERATIONS, operation_id, lambda current: current.update(attempted=True))
                value = remote(self.runtime.store, ["storage-" + row["kind"], row["run_id"], "--payload",
                    json.dumps(row["body"], ensure_ascii=False)], row["collector_server"])
            else:
                value = remote(self.runtime.store, ["storage-operation", row["remote_operation_id"]], row["collector_server"])
            if (not isinstance(value, dict) or value.get("status") not in TERMINAL | {"queued", "running"}
                    or value.get("run_id") != row["run_id"] or value.get("kind") != row["kind"]
                    or value.get("request_id") != row["request_id"]):
                raise PhoneFactoryError("采集端存储操作身份或状态不匹配", 502, remote_kind="protocol")
            remote_id = _identifier(value.get("operation_id"), "远端操作编号")
            allowed = {k: value[k] for k in ("status", "phase", "execution_ids", "dependent_run_ids", "summary",
                       "items", "errors", "error", "download_path") if k in value}
            # Retry of a partial operation must POST the same id, not just keep reading it.
            submitted = value["status"] not in {"failed", "partial"}
            def merge(current):
                if current["status"] == "completed":
                    return
                current.update(**allowed, remote_operation_id=remote_id, submitted=submitted,
                               next_retry=time.time() + 2, updated_at=utc_now())
            saved = self.records.update(OPERATIONS, operation_id, merge)
            if retiring:
                target = "retired" if saved["status"] == "completed" else "cleanup_failed" if saved["status"] in {"partial", "failed"} else "retiring"
                # Dependents are stopped but their data must not be marked deleted.
                self._mark(row, target, value.get("execution_ids", []), value.get("error"))
                for dependent in value.get("dependent_run_ids", []):
                    if self.records.get("model_iter_runs", dependent):
                        def stop_dependent(current):
                            if current.get("status") not in {"completed", "succeeded", "failed", "cancelled", "partial", "interrupted"}:
                                current.update(status="cancelled", evaluation_control_terminal=True,
                                               dispatch_error="评测来源已清理", updated_at=utc_now())
                        self.records.update("model_iter_runs", dependent, stop_dependent)
            elif row["kind"] == "archive":
                for namespace in ("collection_runs", "model_iter_runs"):
                    if self.records.get(namespace, row["run_id"]):
                        self.records.update(namespace, row["run_id"], lambda current: current.update(
                            archive_status="ready" if value["status"] == "completed" else "failed" if value["status"] in {"partial", "failed"} else "building"))
            elif row["kind"] == "cleanup" and row["mode"] == "cache" and value["status"] == "completed" and value.get("summary", {}).get("deleted_files", 0):
                self.records.update(OPERATIONS, operation_id, lambda current: current.update(archive_status="not_requested"))
                for namespace in ("collection_runs", "model_iter_runs"):
                    if self.records.get(namespace, row["run_id"]):
                        self.records.update(namespace, row["run_id"], lambda current: current.update(archive_status="not_requested"))
        except Exception as exc:
            fresh = self.records.get(OPERATIONS, operation_id) or row
            definitive = isinstance(exc, PhoneFactoryError) and ((exc.remote_status or exc.status) in {400, 404, 405, 409, 410, 422}
                                                               or exc.remote_kind == "protocol")
            self.records.update(OPERATIONS, operation_id, lambda current: current.update(
                status="failed" if definitive else "queued", phase="failed" if definitive else "waiting_connection",
                error=str(exc)[:1000], next_retry=time.time() + 10, updated_at=utc_now()))
            if retiring and fresh.get("attempted"):
                self._mark(row, "cleanup_failed" if definitive else "retiring", error=str(exc)[:1000])

    def get(self, operation_id):
        row = self.records.get(OPERATIONS, _identifier(operation_id, "存储操作编号"))
        if not row:
            raise PhoneFactoryError("存储操作不存在", 404)
        return self.public(row)

    def download(self, run_id):
        run = self._run(run_id)
        run_id = run.get("collection_run_id") or run["run_id"]
        require_available(self.records, run_id)
        matches = [row for row in self.records.list(OPERATIONS) if row["run_id"] == run_id
                   and row["kind"] == "archive" and row["status"] == "completed"]
        if not matches and run.get("archive_status") != "ready":
            raise PhoneFactoryError("请先生成完整归档，完成后再下载", 409)
        request = run.get("request", {})
        protocol = request.get("metadata", {}).get("collection_artifacts_version", request.get("collection_artifacts_version"))
        op = max(matches, key=lambda value: value["updated_at"]) if matches else {
            "collector_server": request.get("collector_server") or request.get("metadata", {}).get("collector_server") or endpoint(),
            "download_path": "/runs/" + run_id + ("/collection-archive" if protocol == 2 else "/archive")}
        suffix = op.get("download_path", "").rsplit("/", 1)[-1]
        kind = "collection" if suffix == "collection-archive" else "run"
        server = op["collector_server"].rstrip("/")
        parsed = urllib.parse.urlsplit(server)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise PhoneFactoryError("冻结的采集服务地址无效", 409)
        suffix = "collection-archive" if kind == "collection" else "archive"
        headers = {}
        token = os.environ.get("PHONE_FACTORY_TOKEN")
        if token:
            headers["Authorization"] = "Bearer " + token
        request = urllib.request.Request(server + "/runs/" + urllib.parse.quote(run_id, safe="") + "/" + suffix, headers=headers)
        try:
            response = urllib.request.urlopen(request, timeout=float(os.environ.get("PHONE_FACTORY_TIMEOUT", "30")))
        except urllib.error.HTTPError as exc:
            code = exc.code
            exc.close()
            raise PhoneFactoryError("完整归档暂不可下载，请刷新归档状态", code if code in {404,409,410} else 502) from exc
        except OSError as exc:
            raise PhoneFactoryError("采集服务归档连接失败，请重试下载", 502) from exc
        def chunks():
            try:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    yield chunk
            finally:
                response.close()
        headers = {"Content-Disposition": 'attachment; filename="' + run_id + '.zip"',
                   "Cache-Control": "no-store", "X-Accel-Buffering": "no"}
        length = response.headers.get("Content-Length", "")
        if length.isdigit():
            headers["Content-Length"] = length
        return StreamingResponse(chunks(), media_type="application/zip", headers=headers,
                                 background=BackgroundTask(response.close))

    def dispatch(self, parts, method, body):
        if len(parts) == 4 and parts[:2] == ["storage", "runs"]:
            if method == "POST" and parts[3] in {"scan", "cleanup", "archive"}:
                return self.accept(parts[2], parts[3], body)
            if method == "GET" and parts[3] == "archive-download":
                return self.download(parts[2])
        if len(parts) == 3 and parts[:2] == ["storage", "operations"] and method == "GET":
            return self.get(parts[2])
        raise PhoneFactoryError("存储接口不存在", 404)
