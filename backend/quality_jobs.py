"""Persistent background jobs for AdaRubric trajectory quality evaluation."""

from __future__ import annotations

import json
import os
import subprocess
import threading
import uuid
from concurrent.futures import Executor, ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from .trajectory_data import BACKEND_DIR, QUALITY_JOBS_DIR


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


def run_quality_subprocess(run_id: str, task_ids: list[str], *, job_id: str, progress: Progress) -> dict[str, Any]:
    env_file = BACKEND_DIR / ".env"
    values = _env_values(env_file)
    python = values.get("ADARUBRIC_PYTHON") or os.environ.get("ADARUBRIC_PYTHON")
    if not python:
        candidate = Path(r"D:\anaconda3\envs\guigent\python.exe")
        python = str(candidate) if candidate.is_file() else "python"
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
    )
    final: dict[str, Any] | None = None
    assert process.stdout is not None
    for line in process.stdout:
        line = line.rstrip()
        if line.startswith("PROGRESS "):
            progress(json.loads(line[9:]))
        elif line.startswith("RESULT "):
            final = json.loads(line[7:])
    code = process.wait()
    if code != 0 or final is None:
        raise RuntimeError(f"质检子进程失败，退出码 {code}")
    return final


class QualityJobManager:
    def __init__(self, jobs_dir: Path = QUALITY_JOBS_DIR, runner: QualityRunner = run_quality_subprocess, executor: Executor | None = None) -> None:
        self.jobs_dir = jobs_dir
        self.runner = runner
        self._lock = threading.RLock()
        self._owns_executor = executor is None
        self._executor = executor or ThreadPoolExecutor(max_workers=1, thread_name_prefix="quality")
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self.mark_interrupted_jobs()

    def _path(self, job_id: str) -> Path:
        return self.jobs_dir / f"{job_id}.json"

    def _write(self, payload: dict[str, Any]) -> None:
        path = self._path(payload["job_id"])
        temporary = path.with_name(f".{path.name}.tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)

    def get(self, job_id: str) -> dict[str, Any] | None:
        # Share the writer lock with polling reads.  Without this, Windows
        # may keep the JSON read handle open while _write() replaces it.
        with self._lock:
            path = self._path(job_id)
            if not path.is_file():
                return None
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError, json.JSONDecodeError):
                return None
            return value if isinstance(value, dict) else None

    def list_jobs(self) -> list[dict[str, Any]]:
        """Return persisted quality jobs so clients can restore the queue."""
        with self._lock:
            jobs: list[dict[str, Any]] = []
            for path in self.jobs_dir.glob("*.json"):
                try:
                    value = json.loads(path.read_text(encoding="utf-8"))
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
        for path in self.jobs_dir.glob("*.json"):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            if payload.get("status") in {"queued", "running"}:
                payload.update(status="interrupted", stage="interrupted", completed_at=_now(), error="服务重启导致质检中断；重新提交可复用 checkpoint。")
                self._write(payload)

    def submit(self, run_id: str, task_ids: list[str]) -> dict[str, Any]:
        payload = {
            "job_id": uuid.uuid4().hex, "run_id": run_id, "task_ids": task_ids,
            "status": "queued", "stage": "queued", "created_at": _now(),
            "started_at": None, "completed_at": None, "current_task": None,
            "current_trajectory": None, "task_index": 0, "total_tasks": len(task_ids),
            "completed_trajectories": 0, "total_trajectories": 0, "percent": 0,
            "error": None,
        }
        with self._lock:
            self._write(payload)
        self._executor.submit(self._run, payload["job_id"], run_id, task_ids)
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
            self.runner(run_id, task_ids, job_id=job_id, progress=lambda value: self._progress(job_id, value))
        except Exception as exc:
            self._progress(job_id, {"status": "failed", "stage": "failed", "completed_at": _now(), "error": str(exc)})
            return
        self._progress(job_id, {"status": "succeeded", "stage": "succeeded", "completed_at": _now(), "percent": 100, "error": None})

    def shutdown(self) -> None:
        if self._owns_executor:
            self._executor.shutdown(wait=False, cancel_futures=True)
