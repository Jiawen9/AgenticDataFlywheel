"""Admission and durable execution reservations for batch workers and CLI calls."""
from __future__ import annotations

import os
import socket
import uuid
from contextlib import contextmanager
from pathlib import Path

from .batch_lifecycle import ensure_batch_active
from .data_store import ArtifactStore, DATA_ROOT, RecordStore
from .data_store.registry import utc_now


def _process_identity(pid: int):
    """Creation identity, False for an exited process, None when unverifiable."""
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return False if ctypes.get_last_error() == 87 else None
        try:
            code = wintypes.DWORD()
            if not kernel.GetExitCodeProcess(handle, ctypes.byref(code)):
                return None
            if code.value != 259:
                return False
            times = [wintypes.FILETIME() for _ in range(4)]
            if not kernel.GetProcessTimes(handle, *(ctypes.byref(value) for value in times)):
                return None
            return str((times[0].dwHighDateTime << 32) | times[0].dwLowDateTime)
        finally:
            kernel.CloseHandle(handle)
    try:
        # Linux start time distinguishes a surviving process from a reused PID.
        text = Path(f"/proc/{pid}/stat").read_text()
        return text.rsplit(")", 1)[1].split()[19]
    except FileNotFoundError:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            pass
        return None
    except (OSError, IndexError):
        return None


def batch_from_path(path: Path, root: Path | None = None) -> str | None:
    """Identify managed source/output paths without interpreting arbitrary directory names."""
    path, root = Path(path).resolve(), Path(root or DATA_ROOT).resolve()
    for parent in (root / "batches", root / "raw" / "collection_batches"):
        if path.is_relative_to(parent):
            parts = path.relative_to(parent).parts
            if parts:
                return parts[0]
    return None


@contextmanager
def active_batch_lock(batch_id: str | None, root: Path | None = None):
    """Serialize admission/result mutation with publication; unmanaged inputs have no batch."""
    if batch_id is None:
        yield
        return
    with ArtifactStore(root or DATA_ROOT).batch_lock(batch_id):
        ensure_batch_active(batch_id, root)
        yield


@contextmanager
def batch_operation(batch_id: str | None, kind: str, root: Path | None = None):
    """Reserve under the publication lock, then execute without holding it."""
    if batch_id is None:
        yield None
        return
    records = RecordStore(root or DATA_ROOT)
    operation_id = uuid.uuid4().hex
    with active_batch_lock(batch_id, root):
        records.put("batch_operations", operation_id, {
            "job_id": operation_id, "batch_id": batch_id, "kind": kind,
            "status": "running", "started_at": utc_now(),
            "pid": os.getpid(), "process_identity": _process_identity(os.getpid()),
            "hostname": socket.gethostname(),
        }, expected_revision=0)
    try:
        yield operation_id
        with active_batch_lock(batch_id, root):
            pass
    except BaseException as exc:
        records.update("batch_operations", operation_id, lambda item: item.update(
            status="failed", completed_at=utc_now(), error=str(exc)))
        raise
    else:
        records.update("batch_operations", operation_id, lambda item: item.update(
            status="succeeded", completed_at=utc_now(), error=None))


def recover_operations(root: Path | None = None) -> int:
    """Mark only proven dead processes interrupted, retaining independently running CLIs."""
    records = RecordStore(root or DATA_ROOT)
    count = 0
    for item in records.list("batch_operations"):
        if item.get("status") != "running" or item.get("hostname") != socket.gethostname():
            continue
        identity = _process_identity(int(item.get("pid") or 0))
        if identity is False or (identity is not None and item.get("process_identity") is not None
                                 and identity != item["process_identity"]):
            with ArtifactStore(root or DATA_ROOT).batch_lock(item["batch_id"]):
                current = records.get("batch_operations", item["job_id"])
                if not current or current.get("status") != "running":
                    continue
                records.put("batch_operations", item["job_id"], {
                    **current, "status": "interrupted", "completed_at": utc_now(),
                    "error": "执行进程已退出；保留已有结果。",
                }, expected_revision=current["storage_revision"])
                count += 1
    return count
