"""Persistent serialized job manager for task-generation model work."""

from __future__ import annotations

from concurrent.futures import Executor, ThreadPoolExecutor
from datetime import datetime, timezone
import threading
from typing import Any, Callable

from . import store


ProgressCallback = Callable[..., None]
JobRunner = Callable[[ProgressCallback], dict[str, Any]]
JobFactory = Callable[[str], JobRunner]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class TaskGenerationJobManager:
    """Queue one generation job at a time and persist every visible update."""

    def __init__(self, executor: Executor | None = None) -> None:
        store.ensure_dirs()
        store.mark_unfinished_jobs_interrupted()
        self._executor = executor or ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="task-generation-job",
        )
        self._lock = threading.RLock()

    def submit(self, kind: str, metadata: dict[str, Any], runner_factory: JobFactory) -> dict[str, Any]:
        job_id = store.new_id()
        now = _now()
        job: dict[str, Any] = {
            "job_id": job_id,
            "kind": kind,
            "status": "queued",
            "stage": "queued",
            "created_at": now,
            "started_at": None,
            "completed_at": None,
            "updated_at": now,
            "percent": 0,
            "current": None,
            "completed": 0,
            "total": 0,
            "result_count": 0,
            "error": None,
            **metadata,
        }
        store.save_job(job)
        self._executor.submit(self._run, job_id, runner_factory(job_id))
        return job

    def get(self, job_id: str) -> dict[str, Any] | None:
        return store.load_job(job_id)

    def list_jobs(self) -> list[dict[str, Any]]:
        return store.list_jobs()

    def _update(self, job_id: str, **changes: Any) -> None:
        with self._lock:
            job = store.load_job(job_id)
            if job is None:
                return
            job.update(changes)
            store.save_job(job)

    def _progress(self, job_id: str, **changes: Any) -> None:
        if "completed" in changes and "total" in changes:
            completed = max(0, int(changes["completed"]))
            total = max(0, int(changes["total"]))
            changes["percent"] = 0 if total == 0 else min(99, int(completed * 100 / total))
        self._update(job_id, **changes)

    def _run(self, job_id: str, runner: JobRunner) -> None:
        self._update(
            job_id,
            status="running",
            stage="preparing",
            started_at=_now(),
            error=None,
        )

        def progress(**changes: Any) -> None:
            self._progress(job_id, **changes)

        try:
            result = runner(progress) or {}
            self._update(
                job_id,
                status="succeeded",
                stage="succeeded",
                percent=100,
                completed_at=_now(),
                result_count=int(result.get("result_count", 0)),
                **{key: value for key, value in result.items() if key != "result_count"},
            )
        except Exception as exc:  # noqa: BLE001 - persisted job boundary
            self._update(
                job_id,
                status="failed",
                stage="failed",
                completed_at=_now(),
                error=str(exc),
            )
