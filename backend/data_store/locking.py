"""Reentrant thread and process locks for a business batch."""
from __future__ import annotations

import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from .paths import contained_path

_guard = threading.Lock()
_locks: dict[str, threading.RLock] = {}
_local = threading.local()


@contextmanager
def batch_lock(root: Path, batch_id: str):
    from .artifacts import _identifier
    _identifier(batch_id, "batch ID")
    path = contained_path(root, "system", "batch_locks", batch_id + ".lock")
    key = str(path).casefold() if os.name == "nt" else str(path)
    with _guard:
        mutex = _locks.setdefault(key, threading.RLock())
    with mutex:
        held = getattr(_local, "held", None)
        if held is None:
            _local.held = held = set()
        if key in held:
            yield
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a+b") as stream:
            if stream.seek(0, 2) == 0:
                stream.write(b"0")
                stream.flush()
            stream.seek(0)
            if os.name == "nt":
                import msvcrt
                deadline = time.monotonic() + 60
                while True:
                    try:
                        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                        break
                    except OSError:
                        if time.monotonic() >= deadline:
                            raise TimeoutError("该批次正在保存，请稍后重试")
                        time.sleep(0.05)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            held.add(key)
            try:
                yield
            finally:
                held.remove(key)
                stream.seek(0)
                if os.name == "nt":
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
