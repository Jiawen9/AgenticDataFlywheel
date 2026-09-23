"""Persistent background jobs for AdaRubric trajectory quality evaluation."""

from __future__ import annotations

from .pipeline_access import submit_with_context, execution_pipeline_id

import json
import os
import subprocess
import sys
import threading
import uuid
from concurrent.futures import Executor, ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from .batch_operations import active_batch_lock, batch_operation
from .trajectory_data import BACKEND_DIR, QUALITY_JOBS_DIR
from .data_store import RecordStore, DATA_ROOT, ArtifactStore
from .batch_results import (current_tree_payload, current_quality_payload, quality_task_fingerprints, StaleTaskInput)
from .stage_artifacts import store_root


Progress = Callable[[dict[str, Any]], None]
QualityRunner = Callable[..., dict[str, Any]]


def _now() -> str:
    return datetime.now(ZoneInfo("Asia/Shanghai")).isoformat()


def _env_values(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if path.is_file():
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def run_quality_subprocess(run_id: str, task_ids: list[str], *, job_id: str, progress: Progress,
                           data_root: Path | None = None) -> dict[str, Any]:
    env_file = BACKEND_DIR / ".env"
    values = _env_values(env_file)
    python = values.get("ADARUBRIC_PYTHON") or os.environ.get("ADARUBRIC_PYTHON")
    if not python:
        candidate = Path(r"D:\anaconda3\envs\guigent\python.exe")
        python = str(candidate) if candidate.is_file() else sys.executable
    runner = BACKEND_DIR / "DevelopRubrics" / "quality_job_runner.py"
    command = [python, str(runner), "--run-id", run_id, "--job-id", job_id]
    for task_id in task_ids:
        command.extend(["--task-id", task_id])
    process = subprocess.Popen(
        command,
        cwd=str(BACKEND_DIR.parent),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        env={**os.environ, "ADF_DATA_ROOT": str(data_root or DATA_ROOT)},
    )
    final: dict[str, Any] | None = None
    failure: dict[str, Any] | None = None
    assert process.stdout is not None
    for line in process.stdout:
        line = line.rstrip()
        if line.startswith("PROGRESS "):
            progress(json.loads(line[9:]))
        elif line.startswith("RESULT "):
            final = json.loads(line[7:])
        elif line.startswith("ERROR "):
            failure = json.loads(line[6:])
    code = process.wait()
    if failure and failure.get("kind") == "stale":
        raise StaleTaskInput(failure.get("message") or "质检输入已失效")
    if code != 0 or final is None:
        raise RuntimeError((failure or {}).get("message") or f"质检子进程失败，退出码 {code}")
    return final


class QualityJobManager:
    def __init__(self, jobs_dir: Path = QUALITY_JOBS_DIR, runner: QualityRunner = run_quality_subprocess,
                 executor: Executor | None = None, data_root: Path | None = None) -> None:
        self.jobs_dir = jobs_dir
        self.data_root = store_root(jobs_dir, data_root)
        self.records = RecordStore(self.data_root)
        self.runner = runner
        self._lock = threading.RLock()
        self._owns_executor = executor is None
        self._executor = executor or ThreadPoolExecutor(max_workers=1, thread_name_prefix="quality")
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self.mark_interrupted_jobs()

    def _write(self, payload: dict[str, Any]) -> None:
        if execution_pipeline_id():
            payload.setdefault("pipeline_id", execution_pipeline_id())
        payload.update(self.records.put("quality_jobs", payload["job_id"], payload))

    def get(self, job_id: str) -> dict[str, Any] | None:
        # Polling and worker changes share the manager lock; SQLite owns state.
        with self._lock:
            return self.records.get("quality_jobs", job_id)

    def list_jobs(self) -> list[dict[str, Any]]:
        """Return persisted quality jobs so clients can restore the queue."""
        with self._lock:
            jobs: list[dict[str, Any]] = []
            ids = {item["job_id"] for item in self.records.list("quality_jobs")}
            for job_id in ids:
                try:
                    value = self.get(job_id)
                except (OSError, ValueError, json.JSONDecodeError):
                    continue
                if isinstance(value, dict):
                    jobs.append(value)
            return sorted(
                jobs,
                key=lambda item: str(item.get("created_at", "")),
                reverse=True,
            )

    def mark_interrupted_jobs(self) -> None:
        for payload in self.list_jobs():
            try:
                payload = dict(payload)
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            if payload.get("status") in {"queued", "running"}:
                payload.update(status="interrupted", stage="interrupted", completed_at=_now(), error="服务重启导致质检中断；重新提交可复用 checkpoint。")
                self._write(payload)

    def submit(self, run_id: str | None = None, task_ids: list[str] | None = None,
               *, batch_id: str | None = None) -> dict[str, Any]:
        run_id = batch_id or run_id
        task_ids = list(dict.fromkeys(task_ids or []))
        if not run_id or not task_ids:
            raise ValueError("请选择批次和质检任务")
        alias = self.records.get("batch_run_aliases", run_id)
        if alias:
            from .batch_lifecycle import ensure_batch_active
            ensure_batch_active(alias["batch_id"], self.data_root)
            from .batch_results import resolve_current_batch_id
            if resolve_current_batch_id(run_id, self.data_root) is None:
                raise ValueError("旧建树运行已失效")
            run_id = alias["batch_id"]
        store = ArtifactStore(self.data_root)
        with active_batch_lock(run_id, self.data_root), self._lock:
            from .pipeline_access import ensure_pipeline_write
            ensure_pipeline_write(run_id, self.data_root)
            trees = current_tree_payload(run_id, self.data_root)
            if trees.get("trees"):
                return self._submit_batch(run_id, task_ids, trees)
            if batch_id is not None:
                raise ValueError("所选批次没有当前轨迹树")
            return self._submit_job(run_id, task_ids)

    def _submit_batch(self, batch_id, task_ids, trees):
        if any(task not in trees["trees"] for task in task_ids):
            raise ValueError("所选任务没有当前轨迹树")
        fingerprints = quality_task_fingerprints(trees)
        expected = {task: fingerprints[task] for task in task_ids}
        current = current_quality_payload(batch_id, self.data_root).get("task_fingerprints", {})
        jobs = [job for job in self.list_jobs() if (job.get("batch_id") or job.get("run_id")) == batch_id]
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
        return self._submit_job(batch_id, pending, {"batch_id": batch_id, "task_fingerprints": {task: expected[task] for task in pending} if pending else expected,
            "source_tree_hashes": {task: trees["tree_hashes"][task] for task in pending},
            "requested_task_ids": task_ids, "reused_task_ids": [task for task in task_ids if task not in pending]})

    def _submit_job(self, run_id, task_ids, extra=None):
        payload = {
            "job_id": uuid.uuid4().hex, "run_id": run_id, "task_ids": task_ids,
            "status": "queued", "stage": "queued", "created_at": _now(),
            "started_at": None, "completed_at": None, "current_task": None,
            "current_trajectory": None, "task_index": 0, "total_tasks": len(task_ids),
            "completed_trajectories": 0, "total_trajectories": 0, "percent": 0,
            "error": None,
        }
        payload.update(extra or {})
        if not task_ids:
            payload.update(status="succeeded", stage="succeeded", completed_at=_now(), percent=100, reused=True)
        with self._lock:
            self._write(payload)
        if task_ids:
            submit_with_context(self._executor, self._run, payload["job_id"], run_id, task_ids)
        return payload

    def _progress(self, job_id: str, changes: dict[str, Any]) -> None:
        with self._lock:
            payload = self.get(job_id)
            if payload is None:
                return
            payload.update(changes)
            self._write(payload)

    def _run(self, job_id: str, run_id: str, task_ids: list[str]) -> None:
        self._progress(job_id, {"status": "running", "stage": "preparing", "started_at": _now()})
        try:
            with batch_operation(run_id, "quality_worker", self.data_root):
                job = self.get(job_id) or {}
                if job.get("source_tree_hashes"):
                    trees = current_tree_payload(run_id, self.data_root)
                    if any(trees.get("tree_hashes", {}).get(task) != value
                           for task, value in job["source_tree_hashes"].items()):
                        raise StaleTaskInput("所选任务的轨迹树已变化，请重新质检")
                options = {"data_root": self.data_root} if self.runner is run_quality_subprocess else {}
                report = self.runner(run_id, task_ids, job_id=job_id,
                                     progress=lambda value: self._progress(job_id, value), **options)
        except Exception as exc:
            status = "stale" if isinstance(exc, StaleTaskInput) else "failed"
            self._progress(job_id, {"status": status, "stage": status, "completed_at": _now(), "error": str(exc)})
            return
        self._progress(job_id, {"status": "succeeded", "stage": "succeeded", "completed_at": _now(), "percent": 100,
                                "error": None, "warnings": report.get("warnings", []) if isinstance(report, dict) else []})

    def shutdown(self) -> None:
        if self._owns_executor:
            self._executor.shutdown(wait=False, cancel_futures=True)
