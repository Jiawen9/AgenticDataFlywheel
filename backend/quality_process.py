"""Bounded, identity-checked cleanup of quality workers (never unrelated Python jobs)."""
from __future__ import annotations

import os
import signal
import socket
import subprocess
import time

from .batch_operations import _process_identity


def process_metadata(process) -> dict:
    pid = process.pid
    if type(pid) is not int or pid <= 0:
        return {}  # Test runners need not have an OS process.
    return {"pid": pid, "process_identity": _process_identity(pid),
            "hostname": socket.gethostname(), "exited": False,
            "supervisor_pid": os.getpid(), "supervisor_identity": _process_identity(os.getpid())}


def stop_owned_process(process, timeout: float = 5.0) -> bool:
    """Popen owns the process handle / unreaped child identity until wait returns."""
    try:
        if process.poll() is not None:
            process.wait(timeout=timeout)
            return True
        process.terminate()
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=timeout)
        return True
    except (OSError, subprocess.TimeoutExpired):
        return False


def stop_recorded_process(metadata: dict, timeout: float = 5.0) -> bool:
    """Stop a surviving worker after restart, only through a verified OS handle."""
    if metadata.get("exited") is True:
        return True
    if not metadata:
        return False
    pid, identity = metadata.get("pid"), metadata.get("process_identity")
    if not pid and metadata.get("launch_gate") is True and metadata.get("launch_released") is False:
        parent_pid, expected = metadata.get("supervisor_pid"), metadata.get("supervisor_identity")
        if type(parent_pid) is int and parent_pid > 0 and expected and metadata.get("hostname") == socket.gethostname():
            observed = _process_identity(parent_pid)
            # No worker could have passed the durable-PID-before-GO gate.
            return observed is False or (observed is not None and observed != expected)
        return False
    if type(pid) is not int or pid <= 0 or metadata.get("hostname") != socket.gethostname():
        return False
    observed = _process_identity(pid)
    if observed is False or (identity is not None and observed is not None and observed != identity):
        return True  # PID was reused: the original worker is gone; do not kill its replacement.
    if identity is None or observed is None:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
        kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        handle = kernel.OpenProcess(0x1000 | 0x100000 | 1, False, pid)
        if not handle:
            return ctypes.get_last_error() == 87
        try:
            times = [wintypes.FILETIME() for _ in range(4)]
            if not kernel.GetProcessTimes(handle, *(ctypes.byref(value) for value in times)):
                return False
            handle_identity = str((times[0].dwHighDateTime << 32) | times[0].dwLowDateTime)
            if handle_identity != identity:
                return True
            if kernel.WaitForSingleObject(handle, 0) == 0:
                return True
            if not kernel.TerminateProcess(handle, 1):
                return kernel.WaitForSingleObject(handle, 0) == 0
            return kernel.WaitForSingleObject(handle, int(timeout * 1000)) == 0
        finally:
            kernel.CloseHandle(handle)
    if not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal"):
        return False  # Cannot safely target a recycled PID on this platform.
    try:
        fd = os.pidfd_open(pid)
    except ProcessLookupError:
        return True
    except OSError:
        return False
    try:
        if _process_identity(pid) != identity:
            return True
        signal.pidfd_send_signal(fd, signal.SIGKILL)
        import select
        return bool(select.select([fd], [], [], timeout)[0])
    except ProcessLookupError:
        return True
    except OSError:
        return False
    finally:
        os.close(fd)
