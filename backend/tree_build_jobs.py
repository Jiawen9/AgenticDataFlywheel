"""Persistent, single-worker background queue for immutable trajectory-tree runs."""

from __future__ import annotations

import json
import threading
import uuid
from concurrent.futures import Executor, ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from .trajectory_data import TREE_JOBS_DIR
from .tree_build_service import build_tree_run


BuildRunner = Callable[..., tuple[str, dict[str, Any]]]


def _now() -> str:
    return datetime.now(ZoneInfo("Asia/Shanghai")).isoformat()


class TreeBuildJobManager:
    def __init__(
        self,
        jobs_dir: Path = TREE_JOBS_DIR,
        runner: BuildRunner = build_tree_run,
        executor: Executor | None = None,
    ) -> None:
        self.jobs_dir = jobs_dir
        self.runner = runner
        self._lock = threading.RLock()
        self._owns_executor = executor is None
        self._executor = executor or ThreadPoolExecutor(max_workers=1, thread_name_prefix="tree-build")
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self.mark_interrupted_jobs()

    def _path(self, job_id: str) -> Path:
        return self.jobs_dir / f"{job_id}.json"

    def _write(self, payload: dict[str, Any]) -> None:
        path = self._path(payload["job_id"])
        temporary = path.with_name(f".{path.name}.tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temporary.replace(path)

    def get(self, job_id: str) -> dict[str, Any] | None:
        path = self._path(job_id)
        if not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None

    def mark_interrupted_jobs(self) -> None:
        for path in self.jobs_dir.glob("*.json"):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            if payload.get("status") in {"queued", "running"}:
                payload.update(
                    {
                        "status": "interrupted",
                        "stage": "interrupted",
                        "completed_at": _now(),
                        "error": "服务重启导致作业中断；可重新提交并复用已有模型缓存。",
                    }
                )
                self._write(payload)

    def submit(self, task_ids: list[str]) -> dict[str, Any]:
        job_id = uuid.uuid4().hex
        payload: dict[str, Any] = {
            "job_id": job_id,
            "status": "queued",
            "stage": "queued",
            "task_ids": task_ids,
            "created_at": _now(),
            "started_at": None,
            "completed_at": None,
            "current_task": None,
            "task_index": 0,
            "total_tasks": len(task_ids),
            "classified_steps": 0,
            "total_steps": 0,
            "summarized_trajectories": 0,
            "total_trajectories": 0,
            "percent": 0,
            "error": None,
            "run_id": None,
        }
        with self._lock:
            self._write(payload)
        self._executor.submit(self._run, job_id, task_ids)
        return payload

    def _progress(self, job_id: str, changes: dict[str, Any]) -> None:
        with self._lock:
            payload = self.get(job_id)
            if payload is None:
                return
            payload.update(changes)
            total = int(payload.get("total_steps") or 0)
            completed = int(payload.get("classified_steps") or 0)
            if payload.get("stage") == "summarizing_trajectories":
                summary_total = int(payload.get("total_trajectories") or 0)
                summarized = int(payload.get("summarized_trajectories") or 0)
                percent = 80 + round(10 * summarized / summary_total) if summary_total else 80
            elif payload.get("stage") == "building" and total:
                percent = 92
            elif payload.get("stage") == "publishing":
                percent = 97
            else:
                percent = round(80 * completed / total) if total else 0
            payload["percent"] = min(99, max(0, percent))
            self._write(payload)

    def _run(self, job_id: str, task_ids: list[str]) -> None:
        with self._lock:
            payload = self.get(job_id)
            if payload is None:
                return
            payload.update({"status": "running", "stage": "classifying_and_observing", "started_at": _now()})
            self._write(payload)
        try:
            run_id, _ = self.runner(
                task_ids,
                job_id=job_id,
                progress=lambda changes: self._progress(job_id, changes),
            )
        except Exception as exc:
            with self._lock:
                payload = self.get(job_id) or {"job_id": job_id}
                payload.update(
                    {
                        "status": "failed",
                        "stage": "failed",
                        "completed_at": _now(),
                        "error": str(exc),
                    }
                )
                self._write(payload)
            return
        with self._lock:
            payload = self.get(job_id) or {"job_id": job_id}
            payload.update(
                {
                    "status": "succeeded",
                    "stage": "succeeded",
                    "completed_at": _now(),
                    "percent": 100,
                    "run_id": run_id,
                    "error": None,
                }
            )
            self._write(payload)

    def shutdown(self) -> None:
        if self._owns_executor:
            self._executor.shutdown(wait=False, cancel_futures=True)
