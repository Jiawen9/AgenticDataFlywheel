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
from .data_store import RecordStore
from .stage_artifacts import store_root
from .trajectory_context import resolve_batch_context, row_task_id, validate_batch_sources


BuildRunner = Callable[..., tuple[str, dict[str, Any]]]


def _now() -> str:
    return datetime.now(ZoneInfo("Asia/Shanghai")).isoformat()


class TreeBuildJobManager:
    def __init__(
        self,
        jobs_dir: Path = TREE_JOBS_DIR,
        runner: BuildRunner = build_tree_run,
        executor: Executor | None = None,
        data_root: Path | None = None,
    ) -> None:
        self.jobs_dir = jobs_dir
        self.data_root = store_root(jobs_dir, data_root)
        self.records = RecordStore(self.data_root)
        self.runner = runner
        self._lock = threading.RLock()
        self._owns_executor = executor is None
        self._executor = executor or ThreadPoolExecutor(max_workers=1, thread_name_prefix="tree-build")
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self.mark_interrupted_jobs()

    def _write(self, payload: dict[str, Any]) -> None:
        payload.update(self.records.put("tree_jobs", payload["job_id"], payload))

    def get(self, job_id: str) -> dict[str, Any] | None:
        # Polling and worker changes share the manager lock; SQLite owns state.
        with self._lock:
            return self.records.get("tree_jobs", job_id)

    def list_jobs(self, batch_id: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            jobs = self.records.list("tree_jobs")
            if batch_id is not None:
                jobs = [item for item in jobs if item.get("batch_id") == batch_id]
            return sorted(jobs, key=lambda item: (str(item.get("created_at", "")), str(item.get("job_id", ""))),
                          reverse=True)

    def mark_interrupted_jobs(self) -> None:
        ids = {item["job_id"] for item in self.records.list("tree_jobs")}
        for job_id in ids:
            try:
                payload = self.get(job_id)
                if payload is None:
                    continue
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

    def submit(self, task_ids: list[str], *, batch_id: str | None = None,
               annotation_version: str | None = None) -> dict[str, Any]:
        context = resolve_batch_context(batch_id, annotation_version, self.data_root) if batch_id is not None else None
        if context is not None:
            validate_batch_sources(context, self.data_root)
            available = {row_task_id(row) for rows in context.payload["sheets"].values() for row in rows}
            if not task_ids or any(task_id not in available for task_id in task_ids):
                raise ValueError("所选任务不在该批次的标框版本中")
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
        if context is not None:
            payload.update({"batch_id": batch_id, "annotation_version": context.annotation_version,
                            "annotation_ref": context.annotation_ref, "raw_root": str(context.raw_root)})
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
            context_options = {}
            if payload.get("batch_id"):
                context_options = {"batch_id": payload["batch_id"], "annotation_version": payload["annotation_version"],
                                   "data_root": self.data_root}
            run_id, _ = self.runner(
                task_ids,
                job_id=job_id,
                progress=lambda changes: self._progress(job_id, changes),
                **context_options,
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
