"""Durable retry policy for remote results, separate from execution state."""
from __future__ import annotations

import hashlib
import logging
import os
import threading
import time
import traceback
from contextlib import contextmanager
from datetime import datetime, timezone

from .file_io import io_path
from .data_store.locking import batch_lock

LOG = logging.getLogger(__name__)
TERMINAL = {"completed", "succeeded", "partial", "failed", "interrupted", "cancelled"}
_FLIGHTS = {}
_FLIGHT_GUARD = threading.Lock()


def _epoch(value):
    if isinstance(value, (int, float)):
        return float(value)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.replace(tzinfo=parsed.tzinfo or timezone.utc).timestamp()
    except (ValueError, TypeError):
        return 0.0


def _iso(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat().replace("+00:00", "Z")


class SyncDeferred(ValueError):
    def __init__(self, message, info, status=503):
        super().__init__(message)
        self.info, self.status = info, status


class _Flight:
    def __init__(self):
        self.done = threading.Event()
        self.result, self.error = None, None


@contextmanager
def _process_lock(records, key):
    """Nonblocking OS lock; released by the OS if a worker crashes."""
    path = records.root / "system" / "remote_sync_locks" / (hashlib.sha256(key.encode()).hexdigest() + ".lock")
    io_path(path.parent).mkdir(parents=True, exist_ok=True)
    with io_path(path).open("a+b") as handle:
        handle.seek(0, 2)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        locked = False
        try:
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
            except (OSError, BlockingIOError):
                pass
            yield locked
        finally:
            if locked:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class RemoteSyncPolicy:
    def __init__(self, records, clock=None):
        self.records, self.clock = records, clock or time.time

    @staticmethod
    def run_id(run):
        return run.get("collection_run_id") or run["run_id"]

    @staticmethod
    def _key(kind, run_id):
        if kind not in {"evaluation", "collection"}:
            raise ValueError("Unknown remote synchronization kind")
        return kind + ":" + run_id

    def view(self, kind, run_id):
        value = self.records.get("remote_sync", self._key(kind, run_id)) or {}
        return {"state": "active", "failure_count": 0, "not_found_count": 0,
                "final_failure_count": 0, "pause_reason": None, "last_error": None,
                "last_success_at": None, "last_attempt_at": None, "next_retry_at": None,
                "remote_status": None, **{k: v for k, v in value.items() if k != "storage_revision"}}

    def _update(self, kind, run_id, **changes):
        return self.records.update("remote_sync", self._key(kind, run_id),
                                   lambda value: value.update(**changes), default={})

    def due(self, kind, run):
        if run.get("dispatch_attempted") is False or run.get("dispatch_rejected") is True:
            return False
        state = self.view(kind, self.run_id(run))
        if state["state"] in {"paused", "complete"}:
            return False
        if kind == "collection" and run.get("transfer_status") == "completed":
            return False
        return _epoch(state.get("next_retry_at")) <= self.clock()

    def priority(self, kind, run):
        state = self.view(kind, self.run_id(run))
        retry = bool(state.get("failure_count"))
        active = run.get("status") not in TERMINAL
        # Give active runs the first slots, but age overdue retries into that
        # tier so continuous active work cannot starve historical retries.
        aged = retry and self.clock() - _epoch(state.get("next_retry_at")) >= 60
        return ((0 if aged else 2) if retry else (0 if active else 1),
                _epoch(state.get("last_attempt_at")),
                -_epoch(run.get("submitted_at") or run.get("created_at")), self.run_id(run))

    def reset(self, kind, run_id):
        self._update(kind, run_id, state="active", failure_count=0, not_found_count=0,
                     final_failure_count=0, pause_reason=None, last_error=None,
                     last_error_category=None, next_retry_at=None)

    def reject_dispatch(self, kind, run_id, message):
        self._update(kind, run_id, state="paused", pause_reason="dispatch_rejected",
                     last_error=message, last_error_category="dispatch_rejected",
                     next_retry_at=None, failure_count=0, not_found_count=0, final_failure_count=0)

    def _check_dispatch(self, kind, run):
        namespace = "collection_runs" if kind == "collection" else "model_iter_runs"
        current = self.records.get(namespace, self.run_id(run)) or run
        if current.get("dispatch_rejected") is True:
            raise SyncDeferred("采集请求未被接受，请修复下发错误后使用原请求编号重试；当前没有可回传的运行",
                               self.view(kind, self.run_id(run)), 409)
        if current.get("dispatch_attempted") is False:
            raise SyncDeferred("采集请求尚未下发，请先完成下发后再同步", self.view(kind, self.run_id(run)), 409)
        return current

    def complete(self, kind, run_id):
        if self.view(kind, run_id)["state"] != "complete":
            self._update(kind, run_id, state="complete", next_retry_at=None, pause_reason=None,
                         failure_count=0, not_found_count=0, final_failure_count=0, last_error=None)

    def observe_remote(self, kind, run_id, status):
        def update(value):
            # Cancellation may be acknowledged while an older status query is
            # still in flight. A late running response cannot undo that fact.
            if value.get("remote_status") not in TERMINAL or status in TERMINAL:
                value.update(remote_status=status, remote_observed_at=_iso(self.clock()))
            value["not_found_count"] = 0
        self.records.update("remote_sync", self._key(kind, run_id), update, default={})

    @staticmethod
    def _classification(exc):
        category = getattr(exc, "remote_kind", None)
        status = getattr(exc, "remote_status", None)
        command = getattr(exc, "remote_command", None)
        if isinstance(command, (tuple, list)):
            command = command[0] if command else None
        stage = command if command in {"run", "run-evaluations", "run-archive", "run-manifest", "run-report",
            "results", "reports", "report", "archive", "manifest", "download"} else "unknown"
        if category == "http" and type(status) is int:
            return f"http:{status}:{stage}", f"采集服务返回 HTTP {status}（{stage}）", status == 404 and stage == "run", True
        if category in {"timeout", "connection", "network", "client"}:
            label = {"timeout": "采集服务请求超时", "connection": "无法连接采集服务",
                     "network": "采集服务网络异常", "client": "采集客户端执行失败"}[category]
            return category + ":" + stage, label, False, True
        if category in {"protocol", "identity"}:
            label = "采集服务响应格式或结果不完整" if category == "protocol" else "采集服务返回的运行身份不匹配"
            return category + ":" + stage, label, False, True
        return "unknown:" + type(exc).__name__, "同步异常（" + type(exc).__name__ + "）", False, False

    def _failed(self, kind, run, exc):
        run_id, now = self.run_id(run), self.clock()
        before = self.view(kind, run_id)
        category, message, missing, expected = self._classification(exc)
        count = before.get("failure_count", 0) + 1
        missing_count = before.get("not_found_count", 0) + 1 if missing else 0
        final_count = before.get("final_failure_count", 0) + 1 if before.get("remote_status") in TERMINAL else 0
        submitted = _epoch(run.get("submitted_at") or run.get("created_at"))
        # Missing timestamps in legacy data start their grace period at first attempt.
        first = _epoch(before.get("first_attempt_at")) or now
        old_enough = now - (submitted or first) >= 300
        reason = "remote_missing" if missing_count >= 3 and old_enough else (
            "final_sync_exhausted" if final_count >= 8 else None)
        paused = reason is not None
        should_log = (count == 1 or before.get("last_error_category") != category or paused
                      or (not expected and now - _epoch(before.get("last_exception_log_at")) >= 300))
        changes = dict(state="paused" if paused else "backoff", failure_count=count,
                       not_found_count=missing_count, final_failure_count=final_count,
                       pause_reason=reason, last_error=message, last_error_category=category,
                       next_retry_at=None if paused else _iso(now + (30, 60, 120, 300)[min(count - 1, 3)]))
        if should_log and not expected:
            changes["last_exception_log_at"] = _iso(now)
        self._update(kind, run_id, **changes)
        if should_log:
            LOG.warning("Remote sync %s/%s %s: %s", kind, run_id, "paused" if paused else "waiting", message)
            if not expected:
                # Frames locate unexpected bugs without exposing arbitrary exception
                # text (which can contain command arguments or credentials).
                LOG.warning("Remote sync diagnostic %s/%s:\n%s", kind, run_id,
                            "\n".join(f'  File "{frame.filename}", line {frame.lineno}, in {frame.name}'
                                      for frame in traceback.extract_tb(exc.__traceback__)))

    def execute(self, kind, run, callback, force=False, coalesce=True):
        run_id = self.run_id(run)
        key = (str(self.records.database_path.resolve()), self._key(kind, run_id))
        while True:
            with _FLIGHT_GUARD:
                flight = _FLIGHTS.get(key)
                owned = flight is None
                if owned:
                    flight = _FLIGHTS[key] = _Flight()
            if owned:
                break
            if not force or not flight.done.wait(35):
                raise SyncDeferred("该运行正在同步，请稍后查看结果", self.view(kind, run_id), 409)
            if coalesce:
                with batch_lock(self.records.root, "dispatch_" + run_id):
                    self._check_dispatch(kind, run)
                if flight.error is not None:
                    raise flight.error
                return flight.result
            # A caller that has changed the remote state (such as cancellation)
            # must query again after the old attempt releases its lock. Its old
            # result/error cannot acknowledge that subsequent remote change.
        try:
            with _process_lock(self.records, self._key(kind, run_id)) as locked:
                if not locked:
                    raise SyncDeferred("该运行正在其他进程同步", self.view(kind, run_id), 409)
                # Serialize only state transitions with submission. Downloads and
                # evaluation calls remain independent and never hold this lock.
                with batch_lock(self.records.root, "dispatch_" + run_id):
                    run = self._check_dispatch(kind, run)
                    if not force and not self.due(kind, run):
                        info = self.view(kind, run_id)
                        message = "自动同步已暂停，请手动同步重试" if info["state"] == "paused" else "等待下一次同步"
                        raise SyncDeferred(message, info)
                    previous = self.view(kind, run_id)
                    if force:
                        self.reset(kind, run_id)
                    now = _iso(self.clock())
                    self._update(kind, run_id, last_attempt_at=now,
                                 first_attempt_at=previous.get("first_attempt_at") or now)
                try:
                    result = callback()
                except SyncDeferred:
                    raise
                except Exception as exc:
                    with batch_lock(self.records.root, "dispatch_" + run_id):
                        self._check_dispatch(kind, run)
                        self._failed(kind, run, exc)
                    raise
                with batch_lock(self.records.root, "dispatch_" + run_id):
                    self._check_dispatch(kind, run)
                    after = self.view(kind, run_id)
                    self._update(kind, run_id, state="complete" if after["state"] == "complete" else "active",
                                 failure_count=0, not_found_count=0, final_failure_count=0, pause_reason=None,
                                 last_error=None, last_error_category=None, last_success_at=_iso(self.clock()),
                                 next_retry_at=None if after["state"] == "complete" else _iso(self.clock() + 10))
                if previous.get("failure_count") or previous["state"] == "paused":
                    LOG.info("Remote sync %s/%s recovered", kind, run_id)
                flight.result = result
                return result
        except BaseException as exc:
            flight.error = exc
            raise
        finally:
            with _FLIGHT_GUARD:
                _FLIGHTS.pop(key, None)
                flight.done.set()


class BoundedSyncPool:
    """At most N daemon workers; submit never queues work behind busy workers."""
    def __init__(self, name, max_workers=2):
        self.name, self.max_workers = name, max_workers
        self._guard, self._workers = threading.Lock(), {}
        self._closed = False

    @property
    def available(self):
        with self._guard:
            return 0 if self._closed else self.max_workers - len(self._workers)

    @property
    def inflight(self):
        with self._guard:
            return tuple(self._workers)

    def submit(self, key, callback):
        def work():
            try:
                callback()
            finally:
                with self._guard:
                    self._workers.pop(key, None)
        with self._guard:
            if self._closed or key in self._workers or len(self._workers) >= self.max_workers:
                return False
            thread = threading.Thread(target=work, name=self.name, daemon=True)
            self._workers[key] = thread
            thread.start()
            return True

    def close(self):
        with self._guard:
            self._closed = True
            workers = list(self._workers.values())
        deadline = time.monotonic() + 5
        for thread in workers:
            if thread is not threading.current_thread():
                thread.join(max(0, deadline - time.monotonic()))
