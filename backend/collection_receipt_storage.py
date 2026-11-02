"""Run-scoped storage backpressure; transport failures never pause phones."""
from __future__ import annotations

import errno
import sqlite3
import threading

from .data_store.registry import utc_now


def storage_failure(exc):
    # CLI binary downloads explicitly separate local filesystem writes from
    # remote HTTP/read failures before forwarding this machine-readable kind.
    kind = getattr(exc, "remote_kind", None)
    if kind is not None:
        return kind == "storage"
    if isinstance(exc, OSError):
        return (exc.errno in {errno.ENOSPC, errno.EACCES, errno.EROFS, getattr(errno, "EDQUOT", -1)}
                or getattr(exc, "winerror", None) in {5, 19, 39, 112, 1816})
    if isinstance(exc, sqlite3.OperationalError):
        return any(text in str(exc).lower() for text in
                   ("disk is full", "database or disk is full", "readonly", "read-only", "disk i/o", "unable to open database"))
    return False


class ReceiptStorage:
    def __init__(self, owner, run):
        self.owner, self.run_id = owner, run["collection_run_id"]
        self.enabled = run.get("request", {}).get("metadata", {}).get("collection_artifacts_version") == 2
        self.failed = threading.Event()
        with owner._receipt_storage_guard:
            self.guard = owner._receipt_storage_locks.setdefault(self.run_id, threading.RLock())

    def _state(self):
        state = self.owner._receipt_storage_states.get(self.run_id)
        if state is None:
            state = self.owner.records.get("collection_receipt_storage", self.run_id) or {}
            self.owner._receipt_storage_states[self.run_id] = state
        return state

    def _persist(self, state):
        self.owner.records.put("collection_receipt_storage", self.run_id, state)

    def _send(self, state):
        state.update(pending=True, updated_at=utc_now())
        try:
            self._persist(state)
        except (OSError, sqlite3.Error):
            # A full local disk must not prevent the best-effort pause signal.
            pass
        try:
            result = self.owner.remote.set_receipt_storage(self.run_id, state["paused"])
            if not isinstance(result, dict) or result.get("ok") is not True:
                raise ValueError("采集端未确认回传存储状态")
            remote = result.get("receipt_storage")
            if isinstance(remote, dict) and remote.get("paused") is not state["paused"]:
                raise ValueError("采集端回传存储状态不匹配")
            state.update(pending=False, error=None, acknowledged_at=utc_now())
        except Exception as exc:
            state.update(pending=True, error=str(exc)[:500])
        try:
            self._persist(state)
        except (OSError, sqlite3.Error):
            pass

    def on_error(self, exc):
        if not self.enabled or not storage_failure(exc):
            return False
        self.failed.set()
        with self.guard:
            try:
                state = self._state()
            except (OSError, sqlite3.Error):
                state = self.owner._receipt_storage_states.setdefault(self.run_id, {})
            notify = not state.get("paused") or state.get("pending")
            state.update(paused=True, reason="platform_storage_unavailable", updated_at=utc_now())
            if notify:
                self._send(state)
        return True

    def observe(self, remote):
        """Recover a sent pause even when the local disk could not persist it."""
        observed = remote.get("receipt_storage") if isinstance(remote, dict) else None
        if not self.enabled or not isinstance(observed, dict) or observed.get("paused") is not True:
            return
        with self.guard:
            state = self._state()
            if not state:
                state.update(paused=True, pending=False, reason="platform_storage_unavailable",
                             recovered_from_remote=True, updated_at=utc_now())
                self._persist(state)

    def recover(self, ledger):
        if not self.enabled:
            return
        with self.guard:
            state = self._state()
            if not state or not state.get("paused") and not state.get("pending"):
                return
            if self.failed.is_set() or ledger.storage_blocked_count():
                if state.get("pending"):
                    self._send(state)
                return
            # Persist a successful local write before clearing collector pause.
            # A restart can replay this intent; remote revisions protect order.
            state.update(paused=False, reason="", pending=True, updated_at=utc_now())
            try:
                self._persist(state)
            except (OSError, sqlite3.Error) as exc:
                state.update(paused=True, reason="platform_storage_unavailable")
                self.failed.set()
                self._send(state)
                raise exc
            self._send(state)
