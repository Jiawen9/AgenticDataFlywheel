"""Persistent, single-worker background queue for immutable trajectory-tree runs."""

from __future__ import annotations

from .pipeline_access import submit_with_context, execution_pipeline_id

import json
import threading
import uuid
from concurrent.futures import Executor, ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from .batch_operations import active_batch_lock, batch_operation
from .trajectory_data import TREE_JOBS_DIR
from .tree_build_service import build_tree_run, tree_build_config
from .data_store import RecordStore, ArtifactStore
from .batch_results import (annotation_task_fingerprints, tree_task_fingerprints,
                            current_tree_payload, assert_task_inputs, StaleTaskInput)
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
        if execution_pipeline_id():
            payload.setdefault("pipeline_id", execution_pipeline_id())
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
        if batch_id is not None:
            with active_batch_lock(batch_id, self.data_root), self._lock:
                from .pipeline_access import ensure_pipeline_write
                ensure_pipeline_write(batch_id, self.data_root)
                context = resolve_batch_context(batch_id, annotation_version, self.data_root)
                return self._submit_batch(task_ids, context)
        return self._submit_job(task_ids)

    def _submit_batch(self, task_ids, context):
        batch_id = context.batch_id
        validate_batch_sources(context, self.data_root)
        available = {row_task_id(row) for rows in context.payload["sheets"].values() for row in rows}
        if not task_ids or any(task_id not in available for task_id in task_ids):
            raise ValueError("所选任务不在该批次的标框版本中")
        config = tree_build_config()
        inputs = annotation_task_fingerprints(context.payload)
        fingerprints = tree_task_fingerprints(context.payload, config)
        expected = {task: fingerprints[task] for task in task_ids}
        current = current_tree_payload(batch_id, self.data_root).get("task_fingerprints", {})
        jobs = self.list_jobs(batch_id)
        for job in jobs:
            if job.get("status") in {"queued", "running"} and all(
                    job.get("task_fingerprints", {}).get(task) == value for task, value in expected.items()):
                return job
            if job.get("status") == "succeeded" and all(current.get(task) == value and
                    job.get("task_fingerprints", {}).get(task) == value for task, value in expected.items()):
                return job
        active = {task: value for job in jobs if job.get("status") in {"queued", "running"}
                  for task, value in job.get("task_fingerprints", {}).items()}
        pending = [task for task, value in expected.items() if current.get(task) != value and active.get(task) != value]
        if not pending:
            waiting = next((job for job in jobs if job.get("status") in {"queued", "running"} and any(
                current.get(task) != value and job.get("task_fingerprints", {}).get(task) == value
                for task, value in expected.items())), None)
            if waiting is not None:
                return waiting
        extra = {"batch_id": batch_id, "annotation_version": context.annotation_version,
                 "annotation_ref": context.annotation_ref, "raw_root": str(context.raw_root),
                 "task_fingerprints": {task: expected[task] for task in pending} if pending else expected, "build_config": config,
                 "source_task_fingerprints": {task: inputs[task] for task in pending},
                 "requested_task_ids": task_ids, "reused_task_ids": [task for task in task_ids if task not in pending]}
        return self._submit_job(pending, extra)

    def _submit_job(self, task_ids, extra=None):
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
        payload.update(extra or {})
        if not task_ids:
            payload.update(status="succeeded", stage="succeeded", completed_at=_now(),
                           percent=100, run_id=payload.get("batch_id"), reused=True)
        with self._lock:
            self._write(payload)
        if task_ids:
            submit_with_context(self._executor, self._run, job_id, task_ids)
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
            with batch_operation(payload.get("batch_id"), "tree_build_worker", self.data_root):
                context_options = {}
                if payload.get("batch_id"):
                    assert_task_inputs(payload["batch_id"], payload["source_task_fingerprints"], self.data_root)
                    context = resolve_batch_context(payload["batch_id"], root=self.data_root)
                    context_options = {"batch_id": payload["batch_id"], "annotation_version": context.annotation_version,
                                       "data_root": self.data_root}
                    if self.runner is build_tree_run:
                        context_options.update(expected_task_inputs=payload["source_task_fingerprints"],
                                               expected_build_config=payload["build_config"])
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
                        "status": "stale" if isinstance(exc, StaleTaskInput) else "failed",
                        "stage": "stale" if isinstance(exc, StaleTaskInput) else "failed",
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
