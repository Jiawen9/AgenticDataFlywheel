"""Durable write leases for supervised quality workers.

The batch lock protects short file writes. Revocation never waits for child file
I/O; the execution CAS entry commits in the same SQLite transaction as artifacts.
Standalone scripts have no context and retain their existing behavior.
"""
from __future__ import annotations

import os
import secrets
import socket
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

from .batch_operations import _process_identity
from .data_store import ArtifactStore, RecordStore
from .data_store.registry import utc_now

NAMESPACE = "quality_executions"
_context: ContextVar[tuple[Path, str, str] | None] = ContextVar("quality_execution", default=None)


class QualityExecutionRevoked(RuntimeError):
    body = {"error": {"code": "quality_execution_revoked"}}


class QualityExecutionBlocked(RuntimeError):
    body = {"error": {"code": "quality_worker_cleanup_failed"}}


def get_execution(root: Path, job_id: str) -> dict | None:
    return RecordStore(root).get(NAMESPACE, job_id)


def start_execution(root: Path, job_id: str, batch_id: str) -> dict:
    records = RecordStore(root)
    with ArtifactStore(root).batch_lock(batch_id):
        for item in records.list(NAMESPACE):
            process = item.get("process") or {}
            if item.get("batch_id") == batch_id and (
                item.get("state") == "active" or process.get("cleanup_failed") or
                (process.get("pid") and process.get("exited") is not True)
            ):
                raise QualityExecutionBlocked("旧质检执行尚未确认退出，不能再次派发")
        previous = records.get(NAMESPACE, job_id)
        return records.put(NAMESPACE, job_id, {
            "job_id": job_id, "batch_id": batch_id, "token": secrets.token_hex(32),
            "state": "active", "started_at": utc_now(), "process": {
                "supervisor_pid": os.getpid(), "supervisor_identity": _process_identity(os.getpid()),
                "hostname": socket.gethostname(),
                "launch_gate": True, "launch_released": False,
            },
        }, expected_revision=(previous or {}).get("storage_revision", 0))


def _matching(root: Path, job_id: str, token: str, *, active: bool = True) -> dict:
    record = get_execution(root, job_id)
    if not record or not secrets.compare_digest(str(record.get("token", "")), str(token)):
        raise QualityExecutionRevoked("质检执行凭证已失效")
    if active and record.get("state") != "active":
        raise QualityExecutionRevoked("质检执行已撤销或已提交")
    return record


def revoke_execution(root: Path, job_id: str, token: str, reason: str) -> dict:
    def revoke(record):
        if not secrets.compare_digest(str(record.get("token", "")), str(token)):
            raise QualityExecutionRevoked("质检执行凭证已失效")
        if record.get("state") != "active":
            return record
        return {
            **record, "state": "revoked", "revoked_at": utc_now(),
            # Only a bounded machine reason is persisted, never an exception body.
            "reason": str(reason)[:120],
        }
    return RecordStore(root).update(NAMESPACE, job_id, revoke)


def update_process(root: Path, job_id: str, token: str, metadata: dict) -> dict:
    def update(record):
        if not secrets.compare_digest(str(record.get("token", "")), str(token)):
            raise QualityExecutionRevoked("质检执行凭证已失效")
        fields = {key: value for key, value in metadata.items() if key in {
            "pid", "process_identity", "hostname", "exited", "cleanup_failed",
            "supervisor_pid", "supervisor_identity", "executable", "exited_at",
            "launch_gate", "launch_released",
        }}
        return {
            **record, "process": {**record.get("process", {}), **fields},
        }
    return RecordStore(root).update(NAMESPACE, job_id, update)


def update_job_progress(root: Path, job_id: str, token: str, changes: dict) -> dict | None:
    """Fence progress in SQLite without blocking the watchdog on a file lock."""
    records = RecordStore(root)
    with records._connection(write=True) as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            lease = records._read(connection, NAMESPACE, job_id)
            if (not lease or lease.get("state") != "active" or
                    not secrets.compare_digest(str(lease.get("token", "")), str(token))):
                connection.rollback()
                return None
            job = records._read(connection, "quality_jobs", job_id)
            if job is None or job.get("status") != "running":
                connection.rollback()
                return None
            saved = records._save(connection, "quality_jobs", job_id, {**job, **changes},
                                  job["storage_revision"] + 1)
            connection.commit()
            return saved
        except BaseException:
            connection.rollback()
            raise


def check_execution(root: Path, job_id: str, token: str, *, allow_committed: bool = False) -> dict:
    record = _matching(root, job_id, token, active=False)
    if record.get("state") not in ({"active", "committed"} if allow_committed else {"active"}):
        raise QualityExecutionRevoked("质检执行凭证已撤销")
    job = RecordStore(root).get("quality_jobs", job_id)
    if (job and job.get("status") != "running" and
            not (allow_committed and record.get("state") == "committed")):
        raise QualityExecutionRevoked("质检作业已结束，拒绝迟到回写")
    process = record.get("process") or {}
    parent_pid = process.get("supervisor_pid")
    if parent_pid:
        identity = _process_identity(int(parent_pid))
        expected = process.get("supervisor_identity")
        if identity is False or (identity is not None and expected is not None and identity != expected):
            raise QualityExecutionRevoked("质检监管进程已退出")
    return record


@contextmanager
def execution_context(root: Path, job_id: str, token: str | None):
    existing = get_execution(root, job_id)
    if existing and not token:
        raise QualityExecutionRevoked("受管质检执行缺少凭证")
    if token:
        check_execution(root, job_id, token)
    reset = _context.set((Path(root), job_id, token) if token else None)
    try:
        yield
    finally:
        _context.reset(reset)


@contextmanager
def execution_write_guard(root: Path | None = None, job_id: str | None = None, token: str | None = None):
    context = _context.get()
    if root is None and context:
        root, job_id, token = context
    if root is None or job_id is None:
        yield None
        return
    record = get_execution(root, job_id)
    if not record and token is None:
        yield None
        return
    token = token or (context[2] if context and context[:2] == (Path(root), job_id) else None)
    if not token:
        raise QualityExecutionRevoked("受管质检执行缺少凭证")
    record = _matching(root, job_id, token)
    with ArtifactStore(root).batch_lock(record["batch_id"]):
        yield check_execution(root, job_id, token)


def commit_record_entry(root: Path, job_id: str, batch_id: str, result: dict) -> dict | None:
    """Call while holding execution_write_guard; include with artifact writes."""
    with execution_write_guard(root, job_id) as record:
        if record is None:
            return None
        if record["batch_id"] != batch_id:
            raise QualityExecutionRevoked("质检执行批次不匹配")
        return {"namespace": NAMESPACE, "key": job_id,
                "expected_revision": record["storage_revision"],
                "payload": {**record, "state": "committed", "committed_at": utc_now(), "result": result}}


def committed_result(root: Path, job_id: str, token: str | None = None) -> dict | None:
    record = get_execution(root, job_id)
    if not record or record.get("state") != "committed":
        return None
    if token is not None and not secrets.compare_digest(str(record.get("token", "")), str(token)):
        return None
    result = dict(record["result"])
    store = ArtifactStore(root)
    with store.batch_lock(record["batch_id"]):
        # Resolve any interrupted artifact replacement before exposing recovery.
        store._recover(record["batch_id"])
        artifact = store.get(record["batch_id"], "05_quality")
        if artifact:
            payload = store.read_payload(artifact)
            if payload.get("job_id") == job_id:
                result["artifact"] = artifact
    return result
