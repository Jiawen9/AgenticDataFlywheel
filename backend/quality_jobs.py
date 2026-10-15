"""Persistent background jobs for AdaRubric trajectory quality evaluation."""

from __future__ import annotations

from .pipeline_access import submit_with_context, execution_pipeline_id
from . import pipeline_diagnostics as diagnostics

import json
import os
import math
import queue
import re
import subprocess
import sys
import threading
import time
from concurrent.futures import Executor, ThreadPoolExecutor
from contextvars import copy_context
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from .batch_operations import active_batch_lock, batch_operation, _process_identity
from .trajectory_data import BACKEND_DIR, QUALITY_JOBS_DIR
from .data_store import RecordStore, DATA_ROOT, ArtifactStore
from .batch_results import (current_tree_payload, current_quality_payload, quality_task_fingerprints, StaleTaskInput)
from .stage_artifacts import store_root
from .pipeline_retry_errors import failure_from_exception, failure_from_payload
from .identifiers import allocate_id
from .file_io import io_path
from . import quality_execution
from .quality_process import process_metadata, stop_owned_process, stop_recorded_process


Progress = Callable[[dict[str, Any]], None]
QualityRunner = Callable[..., dict[str, Any]]


_PROGRESS_STAGES = {"preparing", "generating_rubric", "evaluating", "publishing"}
_PROGRESS_IDENT = re.compile(r"^[A-Za-z0-9_.:-]{1,96}$")
_HEARTBEAT_TIMEOUT = 60.0
_REQUEST_CLEANUP_GRACE = 15.0
_SUPERVISION_POLL = 0.25


def _safe_request_frame(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    ident = value.get("local_request_id")
    event = value.get("event")
    if not isinstance(ident, str) or not _PROGRESS_IDENT.fullmatch(ident) or event not in {
            "started", "finished", "failed", "timeout", "settled"}:
        return {}
    safe = {"local_request_id": ident, "event": event}
    for key in ("task_id", "trajectory_id", "model"):
        item = value.get(key)
        if isinstance(item, str) and _PROGRESS_IDENT.fullmatch(item):
            safe[key] = item
    if value.get("operation") in {"rubric_generate", "rubric_repair", "trajectory_score", "model_request", "client_cleanup"}:
        safe["operation"] = value["operation"]
    for key in ("started_at", "deadline_at"):
        item = value.get(key)
        if isinstance(item, str) and len(item) <= 40:
            try:
                datetime.fromisoformat(item.replace("Z", "+00:00"))
                safe[key] = item
            except ValueError:
                pass
    for key in ("timeout_seconds", "elapsed_ms", "input_characters", "max_tokens"):
        item = value.get(key)
        if type(item) in {int, float} and math.isfinite(item) and 0 <= item <= 100000000:
            safe[key] = item
    for key in ("http_proxy_enabled", "http_trust_env"):
        if type(value.get(key)) is bool:
            safe[key] = value[key]
    # The worker emits a sanitized address; remove credentials/query again at this boundary.
    address = value.get("base_url")
    if isinstance(address, str) and len(address) <= 256:
        from urllib.parse import urlsplit, urlunsplit
        try:
            parsed = urlsplit(address)
            if parsed.scheme in {"http", "https"} and parsed.hostname:
                host = parsed.hostname
                if ":" in host:
                    host = f"[{host}]"
                if parsed.port:
                    host += f":{parsed.port}"
                path = "/".join(part if part in {"", "v1", "v2", "v3", "api", "openai", "chat", "completions"}
                                else "_" for part in parsed.path.split("/"))
                safe["base_url"] = urlunsplit((parsed.scheme, host, path, "", ""))
        except ValueError:
            pass
    return safe


def _safe_progress_frame(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    safe: dict[str, Any] = {}
    if value.get("stage") in _PROGRESS_STAGES:
        safe["stage"] = value["stage"]
    for key in ("current_task", "current_trajectory"):
        item = value.get(key)
        if isinstance(item, str) and _PROGRESS_IDENT.fullmatch(item):
            safe[key] = item
    for key in ("task_index", "total_tasks", "completed_trajectories",
                "total_trajectories", "percent"):
        item = value.get(key)
        if type(item) is int and 0 <= item <= 10000000:
            safe[key] = item
    return safe


class QualitySubprocessError(RuntimeError):
    """A sanitized failure sent by the isolated quality worker."""

    def __init__(self, failure: dict[str, Any], exit_code: int) -> None:
        self.failure = failure_from_payload(failure)
        self.diagnostic_log = [
            f"quality worker exit={exit_code}; category={self.failure['category']}; "
            f"http_status={self.failure['http_status']}; "
            f"request_id={self.failure['request_id'] or '-'}; code={self.failure.get('code') or '-'}"
        ]
        super().__init__(self.failure["message"])


def _now() -> str:
    return datetime.now(ZoneInfo("Asia/Shanghai")).isoformat()


def _env_values(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if io_path(path).is_file():
        for raw in io_path(path).read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _execution_batch(root: Path, job_id: str, run_id: str) -> str:
    records = RecordStore(root)
    job = records.get("quality_jobs", job_id) or {}
    if job.get("batch_id"):
        return str(job["batch_id"])
    alias = records.get("batch_run_aliases", run_id)
    if alias:
        return str(alias["batch_id"])
    from .trajectory_data import resolve_tree_run_dir
    manifest = resolve_tree_run_dir(run_id, root / "system" / "trajectory_tree_runs") / "manifest.json"
    if io_path(manifest).is_file():
        payload = json.loads(io_path(manifest).read_text(encoding="utf-8"))
        return str(payload.get("batch_id") or run_id)
    return run_id


def run_quality_subprocess(run_id: str, task_ids: list[str], *, job_id: str, progress: Progress,
                           data_root: Path | None = None, execution_token: str | None = None,
                           stop_event: threading.Event | None = None) -> dict[str, Any]:
    root = data_root or DATA_ROOT
    try:
        job = RecordStore(root).get("quality_jobs", job_id) or {}
    except Exception:
        job = {}  # Diagnostic lookup must not prevent the existing execution path.
    context = diagnostics.current_context() or {}
    owner = job.get("pipeline_id") or (context.get("pipeline_id") if context.get("job_id") == job_id else None)
    with diagnostics.trace_context(_reset=True, root=root, pipeline_id=owner,
            batch_id=job.get("batch_id") or run_id, step_id="quality", job_id=job_id,
            execution_id=context.get("execution_id") or diagnostics.diagnostic_id("x", root=root)):
        return _run_quality_subprocess(run_id, task_ids, job_id=job_id, progress=progress,
            data_root=data_root, execution_token=execution_token, stop_event=stop_event)


def _run_quality_subprocess(run_id: str, task_ids: list[str], *, job_id: str, progress: Progress,
                            data_root: Path | None = None, execution_token: str | None = None,
                            stop_event: threading.Event | None = None) -> dict[str, Any]:
    root = data_root or DATA_ROOT
    env_file = BACKEND_DIR / ".env"
    values = _env_values(env_file)
    python = values.get("ADARUBRIC_PYTHON") or os.environ.get("ADARUBRIC_PYTHON")
    if not python:
        candidate = Path(r"D:\anaconda3\envs\guigent\python.exe")
        python = str(candidate) if candidate.is_file() else sys.executable
    runner = BACKEND_DIR / "DevelopRubrics" / "quality_job_runner.py"
    command = [python, "-u", str(runner), "--run-id", run_id, "--job-id", job_id]
    trace = diagnostics.current_context()
    if trace:
        command.extend(["--diagnostic-execution-id", trace["execution_id"]])
    for task_id in task_ids:
        command.extend(["--task-id", task_id])
    token = execution_token or quality_execution.start_execution(root, job_id, _execution_batch(root, job_id, run_id))["token"]
    diagnostics.register_secrets(token, *[value for key, value in {**os.environ, **values}.items()
                                          if any(part in key.upper() for part in ("API_KEY", "TOKEN", "PASSWORD", "SECRET"))])
    try:
        process = subprocess.Popen(
            command, cwd=str(BACKEND_DIR.parent), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", bufsize=1,
            env={**os.environ, "ADF_DATA_ROOT": str(root), "PIPELINE_MODEL_SINGLE_ATTEMPT": "0",
                 "ADF_QUALITY_EXECUTION_TOKEN": token, "ADF_QUALITY_PARENT_PID": str(os.getpid()),
                 "ADF_QUALITY_PARENT_IDENTITY": str(_process_identity(os.getpid()) or "")},
        )
    except BaseException:
        quality_execution.revoke_execution(root, job_id, token, "spawn_failed")
        quality_execution.update_process(root, job_id, token, {"exited": True})
        raise
    events = queue.Queue()
    reader_done = threading.Event()
    def read_output():
        try:
            assert process.stdout is not None
            for line in process.stdout:
                if trace:
                    diagnostics.append_text(f"jobs/{job_id}/{trace['execution_id']}/worker-output.log", line)
                # The diagnostic tee is independent of the bounded progress protocol.
                if line.startswith("RESULT ") or (line.startswith(("PROGRESS ", "ERROR ", "REQUEST ", "HEARTBEAT "))
                                                   and len(line) <= 65536):
                    events.put(line.rstrip())
        finally:
            reader_done.set()
            if process.stdout is not None:
                process.stdout.close()
    reader_context = copy_context()
    reader = threading.Thread(target=reader_context.run, args=(read_output,), name=f"quality-output-{job_id}", daemon=True)
    final: dict[str, Any] | None = None
    failure: dict[str, Any] | None = None
    active: dict[str, tuple[float, dict]] = {}
    requests: list[dict] = []
    last_heartbeat = time.monotonic()
    cleanup_required = False
    exited = False
    code = -1
    try:
        quality_execution.update_process(root, job_id, token,
                                         {**process_metadata(process), "launch_released": True})
        if stop_event is not None and stop_event.is_set():
            raise QualitySubprocessError({"category": "service_restart"}, -1)
        # The worker must not start until its identity is durable. If the parent
        # dies before this write, stdin closes and the worker refuses to run.
        assert process.stdin is not None
        process.stdin.write("QUALITY_GO\n")
        process.stdin.flush()
        process.stdin.close()
        reader.start()
        while True:
            now = time.monotonic()
            expired = next((frame for deadline, frame in active.values()
                            if now >= deadline + _REQUEST_CLEANUP_GRACE), None)
            if stop_event is not None and stop_event.is_set():
                failure = {"category": "service_restart"}
                cleanup_required = True
                break
            if expired is not None:
                cleanup_timeout = expired.get("operation") == "client_cleanup"
                failure = {"category": "unknown" if cleanup_timeout else "timeout",
                           "code": "quality_client_cleanup_timeout" if cleanup_timeout else "quality_request_deadline",
                           "trajectory_id": expired.get("trajectory_id")}
                cleanup_required = True
                break
            if now - last_heartbeat >= _HEARTBEAT_TIMEOUT:
                failure = {"category": "unknown", "code": "quality_worker_unresponsive"}
                cleanup_required = True
                break
            try:
                line = events.get(timeout=_SUPERVISION_POLL)
            except queue.Empty:
                if reader_done.is_set():
                    try:
                        code = process.wait(timeout=_SUPERVISION_POLL)
                        exited = True
                        break
                    except subprocess.TimeoutExpired:
                        continue
                continue
            try:
                if line.startswith("PROGRESS "):
                    progress(_safe_progress_frame(json.loads(line[9:])))
                elif line.startswith("RESULT "):
                    value = json.loads(line[7:])
                    if isinstance(value, dict):
                        final = value
                elif line.startswith("ERROR "):
                    failure = failure_from_payload(json.loads(line[6:]))
                elif line.startswith("HEARTBEAT "):
                    value = json.loads(line[10:])
                    if isinstance(value, dict):
                        last_heartbeat = time.monotonic()
                        progress({"heartbeat_at": _now()})
                elif line.startswith("REQUEST "):
                    frame = _safe_request_frame(json.loads(line[8:]))
                    if not frame:
                        continue
                    ident = frame["local_request_id"]
                    if frame["event"] == "started":
                        # A duplicate frame cannot reset the original deadline.
                        active.setdefault(ident, (time.monotonic() + frame.get("timeout_seconds", 300), frame))
                    elif frame["event"] == "settled":
                        # Legacy client retry: the worker explicitly confirmed cancellation
                        # and is beginning its existing next attempt in the same context.
                        active.pop(ident, None)
                    elif frame["event"] in {"finished", "failed"}:
                        if active.get(ident, (0, {}))[1].get("event") != "timeout":
                            active.pop(ident, None)
                    elif frame["event"] == "timeout":
                        if ident in active:
                            deadline, started = active[ident]
                            active[ident] = (min(deadline, time.monotonic()), {**started, "event": "timeout"})
                    requests.append(frame)
                    requests = requests[-20:]
                    progress({"model_requests": list(requests),
                              "active_model_requests": [item[1] for item in active.values()]})
            except (ValueError, TypeError, json.JSONDecodeError):
                continue
    finally:
        if not exited:
            # Revocation precedes process termination, including unexpected parent errors.
            try:
                quality_execution.revoke_execution(root, job_id, token,
                                                   (failure or {}).get("code") or "worker_cleanup")
            finally:
                exited = stop_owned_process(process)
        quality_execution.update_process(root, job_id, token, {"exited": exited, "cleanup_failed": not exited})
        if process.stdin is not None:
            process.stdin.close()
        if exited:
            reader.join(timeout=1) if reader.ident else None
            if process.stdout is not None and (reader_done.is_set() or not reader.ident):
                process.stdout.close()
        else:
            # Cleanup failure takes precedence over a retryable parent exception.
            raise QualitySubprocessError({"category": "unknown", "code": "quality_worker_cleanup_failed"}, -1)
    committed = quality_execution.committed_result(root, job_id, token)
    if committed is not None:
        return committed
    quality_execution.revoke_execution(root, job_id, token, "worker_finished")
    if cleanup_required:
        raise QualitySubprocessError(failure or {"category": "unknown"}, code)
    if failure and failure.get("category") == "stale":
        exc = StaleTaskInput(failure["message"])
        exc.failure = failure
        raise exc
    if code != 0 or final is None:
        raise QualitySubprocessError(failure or {"category": "unknown"}, code)
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
        self._closed = False
        self._futures = {}
        self._stop_events = {}
        io_path(self.jobs_dir).mkdir(parents=True, exist_ok=True)
        self.mark_interrupted_jobs()

    def _write(self, payload: dict[str, Any]) -> None:
        if execution_pipeline_id():
            payload.setdefault("pipeline_id", execution_pipeline_id())
        payload.update(self.records.put("quality_jobs", payload["job_id"], payload))
        diagnostics.record_job_snapshot(payload, "quality", self.data_root)

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
        cleanup = {}
        for execution in self.records.list("quality_executions"):
            job_id, token = execution["job_id"], execution["token"]
            metadata = execution.get("process") or {}
            if execution.get("state") == "active" or (metadata and metadata.get("exited") is not True):
                quality_execution.revoke_execution(self.data_root, job_id, token, "service_restart")
                exited = stop_recorded_process(metadata)
                quality_execution.update_process(self.data_root, job_id, token,
                                                 {"exited": exited, "cleanup_failed": not exited})
                cleanup[job_id] = exited
        for payload in self.list_jobs():
            try:
                payload = dict(payload)
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            if payload.get("status") in {"queued", "running"}:
                committed = (quality_execution.committed_result(self.data_root, payload["job_id"])
                             if cleanup.get(payload["job_id"], True) else None)
                if committed is not None and cleanup.get(payload["job_id"], True):
                    payload.update(status="succeeded", stage="succeeded", percent=100, completed_at=_now(),
                                   error=None, failure=None, warnings=committed.get("warnings", []))
                    self._write(payload)
                    continue
                failure = failure_from_payload({"category": "service_restart"} if cleanup.get(payload["job_id"], True)
                                               else {"category": "unknown", "code": "quality_worker_cleanup_failed"})
                payload.update(status="interrupted", stage="interrupted", completed_at=_now(),
                               error=failure["message"], failure=failure,
                               diagnostic_log=["quality worker interrupted by service restart"])
                self._write(payload)

    def submit(self, run_id: str | None = None, task_ids: list[str] | None = None,
               *, batch_id: str | None = None) -> dict[str, Any]:
        if self._closed:
            raise RuntimeError("质检服务正在关闭")
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
        if self._closed:
            raise RuntimeError("质检服务正在关闭")
        payload = {
            "job_id": allocate_id("j", root=self.data_root), "run_id": run_id, "task_ids": task_ids,
            "status": "queued", "stage": "queued", "created_at": _now(),
            "started_at": None, "completed_at": None, "current_task": None,
            "current_trajectory": None, "task_index": 0, "total_tasks": len(task_ids),
            "completed_trajectories": 0, "total_trajectories": 0, "percent": 0,
            "error": None, "failure": None, "diagnostic_log": [],
        }
        payload.update(extra or {})
        if not task_ids:
            payload.update(status="succeeded", stage="succeeded", completed_at=_now(), percent=100, reused=True)
        with self._lock:
            self._write(payload)
        if task_ids:
            with self._lock:
                if self._closed:
                    failure = failure_from_payload({"category": "service_restart"})
                    self._progress(payload["job_id"], {"status": "interrupted", "stage": "interrupted",
                        "completed_at": _now(), "error": failure["message"], "failure": failure})
                else:
                    future = submit_with_context(self._executor, self._run, payload["job_id"], run_id, task_ids)
                    self._futures[payload["job_id"]] = future
        return payload

    def _progress(self, job_id: str, changes: dict[str, Any], *, token: str | None = None) -> None:
        with self._lock:
            if token:
                quality_execution.update_job_progress(self.data_root, job_id, token, changes)
                try:
                    diagnostics.record_job_snapshot(self.get(job_id) or {}, "quality", self.data_root)
                except Exception:
                    diagnostics.log_event("diagnostic_lookup_failed", operation="quality_progress_snapshot")
            else:
                payload = self.get(job_id)
                if payload is None or payload.get("status") not in {"queued", "running"}:
                    return
                payload.update(changes)
                self._write(payload)

    def _run(self, job_id: str, run_id: str, task_ids: list[str]) -> None:
        try:
            job = self.get(job_id) or {}
        except Exception:
            job = {}
        with diagnostics.job_context(job, "quality", self.data_root):
            return self._run_with_diagnostics(job_id, run_id, task_ids)

    def _run_with_diagnostics(self, job_id: str, run_id: str, task_ids: list[str]) -> None:
        stop_event = threading.Event()
        with self._lock:
            if self._closed or (self.get(job_id) or {}).get("status") != "queued":
                return
            self._stop_events[job_id] = stop_event
            self._progress(job_id, {"status": "running", "stage": "preparing", "started_at": _now()})
        token = None
        try:
            with batch_operation(run_id, "quality_worker", self.data_root):
                job = self.get(job_id) or {}
                if job.get("source_tree_hashes"):
                    trees = current_tree_payload(run_id, self.data_root)
                    if any(trees.get("tree_hashes", {}).get(task) != value
                           for task, value in job["source_tree_hashes"].items()):
                        raise StaleTaskInput("所选任务的轨迹树已变化，请重新质检")
                if self.runner is run_quality_subprocess:
                    execution = quality_execution.start_execution(self.data_root, job_id, _execution_batch(self.data_root, job_id, run_id))
                    token = execution["token"]
                    options = {"data_root": self.data_root, "execution_token": token, "stop_event": stop_event}
                else:
                    options = {}
                report = self.runner(run_id, task_ids, job_id=job_id,
                                     progress=lambda value: self._progress(job_id, value, token=token), **options)
        except Exception as exc:
            diagnostics.log_event("job_exception", error=diagnostics.format_exception(exc))
            execution = quality_execution.get_execution(self.data_root, job_id) if token else None
            if execution and not execution.get("process", {}).get("pid") and not execution.get("process", {}).get("launch_released"):
                # The synchronous launcher returned before granting the gate (for
                # example an unreadable env file); no worker can perform writes.
                quality_execution.revoke_execution(self.data_root, job_id, token, "launch_failed")
                execution = quality_execution.update_process(self.data_root, job_id, token, {"exited": True})
            committed = (quality_execution.committed_result(self.data_root, job_id, token)
                         if token and (execution or {}).get("process", {}).get("exited") is True else None)
            if committed is not None:
                self._progress(job_id, {"status": "succeeded", "stage": "succeeded", "completed_at": _now(),
                                       "percent": 100, "error": None, "failure": None,
                                       "warnings": committed.get("warnings", [])})
                return
            status = "stale" if isinstance(exc, StaleTaskInput) else "failed"
            failure = failure_from_payload(getattr(exc, "failure", None)) if getattr(exc, "failure", None) else failure_from_exception(exc)
            diagnostic = exc.diagnostic_log if isinstance(exc, QualitySubprocessError) else [
                f"quality worker category={failure['category']}; "
                f"http_status={failure['http_status']}; request_id={failure['request_id'] or '-'}"
            ]
            self._progress(job_id, {"status": status, "stage": status, "completed_at": _now(),
                                    "error": failure["message"], "failure": failure,
                                    "diagnostic_log": diagnostic[:8]})
            return
        finally:
            with self._lock:
                self._stop_events.pop(job_id, None)
        self._progress(job_id, {"status": "succeeded", "stage": "succeeded", "completed_at": _now(), "percent": 100,
                                "error": None, "failure": None, "diagnostic_log": [],
                                "warnings": report.get("warnings", []) if isinstance(report, dict) else []})

    def shutdown(self) -> None:
        with self._lock:
            self._closed = True
            for event in self._stop_events.values():
                event.set()
            for job_id, future in list(self._futures.items()):
                if future is None or future.cancel():
                    failure = failure_from_payload({"category": "service_restart"})
                    self._progress(job_id, {"status": "interrupted", "stage": "interrupted", "completed_at": _now(),
                                           "error": failure["message"], "failure": failure})
            futures = [future for future in self._futures.values() if future is not None and not future.done()]
        # Shared model executors stay available to other modules. Only await our own workers.
        from concurrent.futures import wait
        if futures:
            wait(futures, timeout=12)
        if self._owns_executor:
            self._executor.shutdown(wait=False, cancel_futures=True)
