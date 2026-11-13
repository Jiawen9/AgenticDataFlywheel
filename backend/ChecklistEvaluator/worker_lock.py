"""Process-lifetime case lock, released by the OS even after a killed worker."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
try:
    from .file_access import io_path, logical_path
except ImportError:
    from file_access import io_path, logical_path


class WorkerBusy(RuntimeError):
    pass


@contextmanager
def case_lock(output: Path):
    output.mkdir(parents=True, exist_ok=True)
    handle = (output / "_worker.lock").open("a+b")
    locked = False
    marker = output / "_worker_pid.json"
    try:
        if os.name == "nt":
            import msvcrt
            if handle.seek(0, 2) == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise WorkerBusy("evaluation worker already owns this output directory") from exc
        else:
            import fcntl
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise WorkerBusy("evaluation worker already owns this output directory") from exc
        locked = True
        temporary = marker.with_suffix(".json.tmp")
        temporary.write_text(json.dumps({"pid": os.getpid()}), encoding="utf-8")
        os.replace(temporary, marker)
        yield
    finally:
        if locked:
            try:
                if json.loads(marker.read_text(encoding="utf-8")).get("pid") == os.getpid():
                    marker.unlink()
            except (OSError, ValueError, AttributeError):
                pass
            if os.name == "nt":
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()
