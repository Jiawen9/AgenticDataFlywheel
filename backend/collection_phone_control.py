"""Scoped device control. Reads use cache; durable intents precede remote actions."""
from __future__ import annotations

import copy
import json
import os
import sqlite3
import threading
import time

from .collection_runs import _identifier
from .data_store import RevisionConflict
from .data_store.registry import utc_now
from .phone_factory import PhoneFactoryError
from .remote_sync import BoundedSyncPool
from .task_generation.collection_batches import payload_digest

TERMINAL = {"completed", "conflict", "failed", "rejected"}


class PhoneControlService:
    def __init__(self, resume):
        self.resume, self.runtime, self.records = resume, resume.runtime, resume.records
        self.pool = BoundedSyncPool("phone-control", max_workers=2)
        self.cache, self.due = {}, {}
        self.guard = threading.RLock()

    def close(self):
        self.pool.close()

    def _run_phone(self, run_id, phone, mode, *, read=False):
        run = self.resume._run(run_id, mode, read=read)
        metadata = run.get("request", {}).get("metadata", {})
        if phone not in metadata.get("phone_ids", []):
            raise PhoneFactoryError("手机不属于本次冻结采集计划", 404)
        return run

    @staticmethod
    def _server(run):
        return str(run.get("request", {}).get("metadata", {}).get("collector_server")
                   or os.environ.get("PHONE_FACTORY_SERVER", "http://localhost:9011")).rstrip("/")

    def _remote(self, command, args, server):
        try:
            return self.runtime.store._remote_result([command, *args, "--server", server])
        except PhoneFactoryError as exc:
            if exc.remote_status in {400, 404, 405, 409, 422}:
                raise PhoneFactoryError(str(exc), exc.remote_status, remote_status=exc.remote_status,
                                        remote_kind=exc.remote_kind, remote_command=command) from exc
            raise

    def _fetch(self, run, phone, server):
        capability = self._remote("phone-control-capabilities", [], server)
        if capability.get("phone_control", {}).get("version") != 1:
            return {"phone_id": phone, "supported": False, "availability": "unknown",
                    "stale": False, "reason_code": "phone_control_unsupported", "owners": [],
                    "supported_actions": [], "message": "请升级采集端的手机级控制能力"}
        value = self._remote("phone-ownership", [phone], server)
        if (not isinstance(value, dict) or value.get("phone_id", phone) != phone
                or value.get("availability") not in {"free", "busy", "unknown"}
                or not isinstance(value.get("owners", []), list)):
            raise PhoneFactoryError("手机占用响应身份或格式无效", 502)
        return {**value, "phone_id": phone, "supported": True, "received_at": utc_now()}

    def _refresh(self, run, phone, server):
        key = (run["collection_run_id"], phone)
        try:
            value = self._fetch(run, phone, server)
        except Exception as exc:
            value = {**self.cache.get(key, {}), "phone_id": phone, "stale": True,
                     "availability": "unknown", "supported_actions": [], "sync_error": str(exc)[:500]}
        with self.guard:
            self.cache[key] = value
            self.due[key] = time.monotonic() + 5

    def ownership(self, run_id, phone, mode):
        run = self._run_phone(run_id, phone, mode, read=True)
        key = (run_id, phone)
        if time.monotonic() >= self.due.get(key, 0):
            self.pool.submit("ownership:" + run_id + ":" + phone,
                             lambda: self._refresh(run, phone, self._server(run)))
        value = copy.deepcopy(self.cache.get(key))
        return value or {"phone_id": phone, "availability": "unknown", "stale": True,
                         "reason_code": "loading", "supported_actions": [], "owners": []}

    def submit(self, run_id, phone, action, data, mode):
        run = self._run_phone(run_id, phone, mode)
        if run.get("request", {}).get("metadata", {}).get("collection_artifacts_version") != 2:
            raise PhoneFactoryError("旧运行暂不支持手机级控制；不会自动停止整批", 409)
        if action not in {"stop", "reconcile"} or not isinstance(data, dict) or set(data) != {
                "request_id", "execution_id", "expected_state_revision", "ownership_token"}:
            raise PhoneFactoryError("手机控制参数无效")
        request_id = _identifier(data["request_id"], "请求编号")
        execution = _identifier(data["execution_id"], "执行编号")
        if (type(data["expected_state_revision"]) is not int or data["expected_state_revision"] < 0
                or not isinstance(data["ownership_token"], str) or not 1 <= len(data["ownership_token"]) <= 512):
            raise PhoneFactoryError("手机控制凭据无效")
        fingerprint = payload_digest({"run_id": run_id, "phone_id": phone, "action": action, "request": data})
        value = {"operation_id": request_id, "request_id": request_id, "run_id": run_id,
                 "execution_id": execution, "phone_id": phone, "action": action,
                 "fingerprint": fingerprint, "request": copy.deepcopy(data), "server": self._server(run),
                 "mode": mode, "status": "accepted", "remote_submitted": False, "created_at": utc_now()}
        try:
            saved = self.records.put("collection_phone_controls", request_id, value, expected_revision=0)
        except RevisionConflict:
            saved = self.records.get("collection_phone_controls", request_id)
            if not saved or saved.get("fingerprint") != fingerprint:
                raise PhoneFactoryError("相同手机控制请求编号对应不同操作", 409)
        self.pool.submit("operation:" + request_id, lambda: self._work(request_id))
        return self._public(saved)

    @staticmethod
    def _public(row):
        return {k: copy.deepcopy(v) for k, v in row.items() if k in {
            "operation_id", "request_id", "run_id", "execution_id", "phone_id", "action", "status",
            "error", "ownership", "created_at", "updated_at"}}

    def operation(self, run_id, request_id, mode):
        self.resume._run(run_id, mode, read=True)
        row = self.records.get("collection_phone_controls", _identifier(request_id, "请求编号"))
        if not row or row["run_id"] != run_id or row["mode"] != mode:
            raise PhoneFactoryError("手机控制操作不存在", 404)
        if row["status"] not in TERMINAL:
            self.pool.submit("operation:" + request_id, lambda: self._work(request_id))
        return self._public(row)

    def _work(self, request_id):
        row = self.records.get("collection_phone_controls", request_id)
        if time.monotonic() < self.due.get("operation:" + request_id, 0):
            return
        if not row or row["status"] in TERMINAL:
            return
        try:
            # A lost submission acknowledgement is resolved by querying the same
            # operation ID before considering retransmission of its frozen body.
            response = None
            if row.get("remote_submitted"):
                try:
                    response = self._remote("phone-control-operation", [request_id], row["server"])
                except PhoneFactoryError as exc:
                    if exc.remote_status != 404:
                        raise
            if response is None:
                run = self._run_phone(row["run_id"], row["phone_id"], row["mode"])
                if not row.get("remote_submitted"):
                    value = self._fetch(run, row["phone_id"], row["server"])
                    match = next((owner for owner in value.get("owners", []) if
                        owner.get("execution_id") == row["execution_id"] and
                        owner.get("state_revision") == row["request"]["expected_state_revision"]), None)
                    if (value.get("supported") is not True or value.get("stale") or not match
                            or row["action"] not in match.get("supported_actions", [])):
                        raise PhoneFactoryError("占用归属已变化或尚无法核验，请刷新诊断", 409)
                self.records.update("collection_phone_controls", request_id,
                    lambda item: item.update(remote_submitted=True, status="running", updated_at=utc_now()))
                payload = {k: row["request"][k] for k in ("request_id", "expected_state_revision", "ownership_token")}
                # Snapshot tokens rotate even when ownership is unchanged. The
                # collector validates the original proof's TTL and process identity.
                response = self._remote("phone-" + row["action"],
                    [row["execution_id"], row["phone_id"], "--payload", json.dumps(payload, ensure_ascii=False)], row["server"])
            if (not isinstance(response, dict) or response.get("operation_id") != request_id
                    or response.get("execution_id") != row["execution_id"]
                    or response.get("phone_id") != row["phone_id"] or response.get("action") != row["action"]
                    or response.get("status") not in {"accepted", "running", "completed", "needs_attention", "conflict", "failed"}):
                raise PhoneFactoryError("手机控制响应身份或格式无效", 502)
            self.records.update("collection_phone_controls", request_id, lambda item: item.update(
                **{key: response.get(key) for key in ("status", "error", "ownership")}, updated_at=utc_now()))
            self.due[(row["run_id"], row["phone_id"])] = 0
        except Exception as exc:
            rejected = isinstance(exc, PhoneFactoryError) and exc.status in {400, 404, 405, 409, 422}
            try:
                self.records.update("collection_phone_controls", request_id, lambda item: item.update(
                    status="conflict" if rejected else "uncertain", error=str(exc)[:500], updated_at=utc_now()))
            except (OSError, sqlite3.Error):
                pass  # Persisted intent remains recoverable; never create a new ID.

        finally:
            self.due["operation:" + request_id] = time.monotonic() + 5

    def schedule(self):
        from .collector_storage import unavailable
        if not self.pool.available:
            return
        try:
            rows = self.records.list("collection_phone_controls")
        except (OSError, sqlite3.Error):
            return
        for row in rows:
            if unavailable(self.records, row["run_id"]):
                continue
            if row.get("status") not in TERMINAL:
                self.pool.submit("operation:" + row["request_id"], lambda rid=row["request_id"]: self._work(rid))
