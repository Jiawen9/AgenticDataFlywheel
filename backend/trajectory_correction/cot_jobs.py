"""Persistent background jobs for regenerating COT after action correction."""

from __future__ import annotations

from ..pipeline_access import submit_with_context, execution_pipeline_id, retry_enabled_for_pipeline
from .. import pipeline_diagnostics as diagnostics
from ..pipeline_diagnostic_http import install_box_reviewer_logging

import hashlib
import json
import re
import threading
from concurrent.futures import Executor, ThreadPoolExecutor
from contextlib import suppress
from datetime import datetime
from pathlib import Path
from backend.file_io import io_path

from typing import Any, Callable
from zoneinfo import ZoneInfo

from .constants import CORRECTION_BBOX_CACHE_DIR, CORRECTION_COT_JOBS_DIR, PROJECT_ROOT
from ..data_store import RecordStore, DATA_ROOT, rebase_data_path
from ..stage_artifacts import fingerprint as file_digest
from ..identifiers import allocate_id
from .cot_generator import QwenCotGenerator, read_env, SYSTEM_PROMPT
from .assets import resolve_asset
from ..bounding_box.build_annotations import resolve_action_box
from ..bounding_box.qwen_reviewer import QwenBoxReviewer
from ..trajectory_data import _format_manual_actions_box
from .draft_store import load_session, update_session, storage_root, session_batch_id
from .service import _snapshot, _source_for_session, session_asset, publish_stage_snapshot, publish_cot_snapshot, _check_revision, active_session_lock
from ..pipeline_job_stop import PipelineJobCancelled, check_job_stop, job_commit_guard
from .session_state import identified, row_index, fingerprint
from ..batch_lifecycle import BatchPublishedError, is_batch_active
from ..pipeline_retry_errors import failure_from_exception, failure_from_payload


Progress = Callable[[dict[str, Any]], None]


def _now() -> str:
    return datetime.now(ZoneInfo("Asia/Shanghai")).isoformat()


def _action_hash(action: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(action, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _bbox_hash(value: str) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def _bbox_reviewer() -> QwenBoxReviewer:
    """Build the existing reviewer from the project's canonical .env file."""
    values = read_env(PROJECT_ROOT / "backend" / ".env")
    bbox_model = values["MODEL_NAME"]
    # qwen_reviewer predates the correction service and reads these aliases.
    # Populate them only for this process; no secret is logged or persisted.
    import os
    os.environ["TRAJECTORY_API_KEY"] = values["YUNAI_API_KEY"]
    os.environ["TRAJECTORY_API_BASE_URL"] = values["MODEL_URL"]
    os.environ["TRAJECTORY_MODEL"] = bbox_model
    return QwenBoxReviewer(model=bbox_model, cache_path=CORRECTION_BBOX_CACHE_DIR / "qwen_review_cache.json",
                           use_response_cache=False)


def _stable_image(image: Path) -> Path:
    name = image.name.replace("_vla_input.jpg", "_vla_input_stability.jpg")
    candidate = image.with_name(name)
    return candidate if io_path(candidate).is_file() else image


def _xml_text(image: Path, row: dict[str, Any]) -> str:
    value = str(row.get("xml", "") or "")
    if value and not value.startswith("embedded:") and not value.startswith("missing"):
        try:
            path = rebase_data_path(value, storage_root())
            if io_path(path).is_file():
                return io_path(path).read_text(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            pass
    path = image.with_name(re.sub(r"_vla_input(?:_stability)?\.jpg$", "_vla_input_ui.xml", image.name, flags=re.IGNORECASE))
    try:
        return io_path(path).read_text(encoding="utf-8", errors="replace") if io_path(path).is_file() else ""
    except OSError:
        return ""


def _target_identity(target):
    return {key: target.get(key) for key in ("step_key", "source_fingerprint", "task_id",
        "task_fingerprint", "task", "trajectory_id", "action", "history_hash", "step")}


def _checkpoint_inputs(payload, target, image):
    """Bind a draft receipt to the exact inputs consumed by each model."""
    base = [1, payload["configuration_fingerprint"], _target_identity(target)]
    try:
        image_digest = file_digest(image)
    except FileNotFoundError:
        # No proof of the image. Never restore a result without it.
        return None, None
    bbox_key = None
    if payload["generate_bbox"] and target["action"].get("action", "").lower() in {"click", "long_press", "swipe"}:
        stable = _stable_image(image)
        stable_digest = image_digest if stable == image else file_digest(stable)
        bbox_key = fingerprint([base, stable_digest, _xml_text(stable, target), target.get("summary", "")])
    return fingerprint([base, image_digest]), bbox_key


def _restored_parts(session, payload, target, cot_key, bbox_key):
    row = str(target["excel_row"])
    edit = session.get("row_edits", {}).get(row, {})
    receipt = edit.get("bbox_checkpoint") or {}
    box = target["bbox_hash"]
    same_op = receipt.get("operation_id") == payload["operation_id"]
    has_box = bool(bbox_key and same_op and receipt.get("input") == bbox_key
                   and receipt.get("box") == box and edit.get("bbox_source") == "generated")
    cot = session.get("cot", {}).get(row) or {}
    has_cot = bool(cot_key and cot.get("input_digest") == cot_key and cot.get("bbox_hash") == box
                   and cot.get("content_tag") == "thought_summary"
                   and all(isinstance(cot.get(key), str) and cot[key].strip() for key in ("thought", "summary"))
                   and (not payload["force_overwrite"] or cot.get("operation_id") == payload["operation_id"])
                   and (not bbox_key or has_box))
    return has_cot, has_box


def cot_checkpoint_progress(records, pipeline, jobs):
    """Count saved results for this Pipeline operation, including writes before a crash."""
    session_id = pipeline.get("session_id")
    if not session_id:
        return 0
    session = records.get("correction_sessions", session_id) or {}
    receipts = session.get("cot", {})
    completed = set()
    for job in jobs:
        operation = job.get("operation_id")
        if (not operation or job.get("session_id") != session_id
                or job.get("pipeline_id") != pipeline["pipeline_id"]
                or job.get("batch_id") != pipeline["batch_id"]):
            continue
        for target in job.get("targets", []):
            receipt = receipts.get(str(target["excel_row"])) or {}
            if (receipt.get("operation_id") != operation or receipt.get("step_key") != target["step_key"]
                    or receipt.get("source_fingerprint") != target["source_fingerprint"]
                    or receipt.get("task_fingerprint") != target.get("task_fingerprint")
                    or receipt.get("action_hash") != _action_hash(target["action"])):
                continue
            # Only the worker verifies the actual image before restoring a row.
            # Counting a committed receipt here does not authorize its reuse.
            if _restored_parts(session, job, target, receipt.get("input_digest"), None)[0]:
                completed.add(target["step_key"])
    return len(completed)


class CotJobManager:
    def __init__(self, jobs_dir: Path = CORRECTION_COT_JOBS_DIR, generator_factory: Callable[[], QwenCotGenerator] = QwenCotGenerator, executor: Executor | None = None) -> None:
        self.jobs_dir = jobs_dir
        self.data_root = DATA_ROOT if jobs_dir == CORRECTION_COT_JOBS_DIR else jobs_dir.parent
        self._records = RecordStore(self.data_root)
        self.generator_factory = generator_factory
        self._lock = threading.RLock()
        self._owns_executor = executor is None
        self._executor = executor or ThreadPoolExecutor(max_workers=1, thread_name_prefix="cot-generation")
        io_path(self.jobs_dir).mkdir(parents=True, exist_ok=True)
        self.mark_interrupted_jobs()

    def _path(self, job_id: str) -> Path:
        if not re.fullmatch(r"(?:[a-f0-9]{32}|j[0-9]{6}-[0-9]{3,})", job_id):
            return self.jobs_dir / ".invalid-job-id"
        return self.jobs_dir / f"{job_id}.json"

    def _write(self, payload: dict[str, Any]) -> None:
        if execution_pipeline_id():
            payload.setdefault("pipeline_id", execution_pipeline_id())
        def update(current):
            stop_requested_at = current.get("stop_requested_at")
            if stop_requested_at and payload.get("status") in {"queued", "running"}:
                return current
            return {**payload, **({"stop_requested_at": stop_requested_at} if stop_requested_at else {})}
        saved = self._records.update("correction_cot_jobs", str(payload["job_id"]), update, default={})
        payload["storage_revision"] = saved["storage_revision"]
        diagnostics.record_job_snapshot(payload, "cot", storage_root())

    def get(self, job_id: str) -> dict[str, Any] | None:
        if self._path(job_id).name == ".invalid-job-id":
            return None
        with self._lock:
            return self._records.get("correction_cot_jobs", job_id)

    def list_jobs(self, *, active_only: bool = False) -> list[dict[str, Any]]:
        with self._lock:
            jobs = sorted(self._records.list("correction_cot_jobs"), key=lambda item: str(item.get("created_at", "")), reverse=True)
        if active_only:
            jobs = [job for job in jobs if is_batch_active(str(job.get("batch_id") or
                session_batch_id(load_session(str(job["session_id"])) or {"session_id": job["session_id"]})), storage_root())]
        return jobs

    def mark_interrupted_jobs(self) -> None:
        for payload in self.list_jobs():
            if payload.get("status") in {"queued", "running"}:
                try:
                    check_job_stop(self._records, "correction_cot_jobs", payload["job_id"])
                except PipelineJobCancelled:
                    self._cancelled(payload["job_id"])
                    continue
                payload.update(status="interrupted", completed_at=_now(), error="服务重启导致 COT 生成中断；重新提交可复用已完成结果。",
                               failure=failure_from_payload({"category": "service_restart"}))
                self._write(payload)

    @staticmethod
    def _targets(session_id: str, group_ids: list[str] | None, row_ids: list[int] | None = None) -> list[dict[str, Any]]:
        session = load_session(session_id)
        if session is None:
            raise FileNotFoundError("纠偏会话不存在")
        snapshot = identified(_snapshot(session))
        allowed = set(group_ids or [str(group["group_id"]) for group in snapshot["groups"]])
        requested_rows = {int(value) for value in (row_ids or [])}
        targets: list[dict[str, Any]] = []
        invalid_tasks = set(session.get("stale_tasks", [])) | {item["task_id"] for item in session.get("pending_review", {}).values()}
        for group in snapshot["groups"]:
            if str(group["group_id"]) not in allowed and str(group.get("legacy_group_id")) not in allowed:
                continue
            if group.get("task_id") in invalid_tasks:
                raise ValueError("待复核任务不能生成 COT")
            rows = sorted(group["rows"], key=lambda row: int(row["step"]))
            for index, row in enumerate(rows):
                edit = session.get("row_edits", {}).get(str(row["excel_row"]), {})
                bbox_requested = int(row["excel_row"]) in requested_rows
                current_bbox = str(edit.get("actions_box", row.get("actions_box", "")))
                bbox_changed = current_bbox != str(session.get("bbox_baselines", {}).get(str(row["excel_row"]), row.get("actions_box", "")))
                if (not edit.get("actions") and not bbox_requested and not bbox_changed) or edit.get("deleted"):
                    continue
                action = row.get("action") or {}
                if edit.get("actions"):
                    try:
                        action = json.loads(edit["actions"])
                    except (TypeError, ValueError):
                        continue
                elif not isinstance(action, dict):
                    continue
                try:
                    action = dict(action)
                except (TypeError, ValueError):
                    continue
                history = "\n".join(
                    f"Step {int(previous['step'])}: {str(previous.get('original_summary') or previous.get('summary') or '').strip()}"
                    for previous in rows[:index]
                    if str(previous.get("summary") or "").strip() and not session.get("row_edits", {}).get(str(previous["excel_row"]), {}).get("deleted")
                )
                targets.append({
                    "base_action": row.get("action") or {},
                    "step_key": row["step_key"],
                    "source_fingerprint": row["source_fingerprint"],
                    "task_id": group["task_id"],
                    "task_fingerprint": session.get("task_fingerprints", {}).get(group["task_id"]),
                    "edit_baseline": dict(edit),
                    "group_id": group["group_id"],
                    "task": group["task"],
                    "trajectory_id": group["meta_task"],
                    "excel_row": int(row["excel_row"]),
                    "step": int(row["step"]),
                    "image": row["image"],
                    "action": action,
                    "history": history,
                    "history_hash": fingerprint(history),
                    "reference_answer": current_bbox,
                    "bbox_hash": _bbox_hash(current_bbox),
                    "summary": str(row.get("original_summary") or row.get("summary") or ""),
                    "xml": str(row.get("xml") or ""),
                    "bbox_source": str(edit.get("bbox_source", "manual" if bbox_changed else "original")),
                    "summary_source": "manual" if "summary" in edit else "original",
                    "thought_source": "manual" if "thought" in edit else "original",
                })
        if not targets:
            raise ValueError("当前没有动作已修改且未删除的步骤可生成 COT")
        return targets

    def _configuration_fingerprint(self, generate_bbox: bool) -> str:
        if self.generator_factory is QwenCotGenerator:
            values = read_env(PROJECT_ROOT / "backend" / ".env")
            return fingerprint({"model": values.get("COT_MODEL_NAME") or "qwen3-vl-32b-instruct",
                "endpoint": values.get("MODEL_URL"), "prompt": SYSTEM_PROMPT,
                "bbox_model": values.get("MODEL_NAME") if generate_bbox else None})
        return fingerprint({"factory": f"{self.generator_factory.__module__}.{self.generator_factory.__qualname__}"})

    def _session_jobs(self, session_id):
        keys = self._records.matching_keys("correction_cot_jobs", session_id=session_id)
        return sorted(self._records.get_many("correction_cot_jobs", keys), key=lambda job: job["created_at"], reverse=True)

    def submit(self, session_id: str, group_ids: list[str] | None = None, row_ids: list[int] | None = None, *, generate_bbox: bool = False, force_overwrite: bool = False, expected_revision: int | None = None, origin_job_id: str | None = None) -> dict[str, Any]:
        reusable = None
        if not force_overwrite:
            # A final job fingerprint alone cannot detect a changed external
            # image. Validate its committed inputs outside the batch write lock.
            old = next((job for job in self._session_jobs(session_id)
                        if job.get("status") == "succeeded" and job.get("resume_scope")
                        and job["group_ids"] == (group_ids or []) and job["row_ids"] == (row_ids or [])
                        and job["generate_bbox"] == generate_bbox and not job["force_overwrite"]), None)
            if old:
                with active_session_lock(session_id) as session:
                    _check_revision(session, expected_revision)
                    _, asset_root = _source_for_session(session)
                for target in old["targets"]:
                    try:
                        image = resolve_asset(asset_root, target["image"])
                    except FileNotFoundError:
                        break
                    cot_key, bbox_key = _checkpoint_inputs(old, target, image)
                    if not _restored_parts(session, old, target, cot_key, bbox_key)[0]:
                        break
                else:
                    reusable = (old["job_id"], session.get("storage_revision"))
        with active_session_lock(session_id) as session:
            from ..pipeline_access import ensure_pipeline_write
            from .draft_store import storage_root
            ensure_pipeline_write(session_batch_id(session), storage_root())
            return self._submit(session_id, group_ids, row_ids, reusable=reusable, generate_bbox=generate_bbox,
                                force_overwrite=force_overwrite, expected_revision=expected_revision,
                                origin_job_id=origin_job_id)

    def _submit(self, session_id: str, group_ids: list[str] | None = None, row_ids: list[int] | None = None, *, generate_bbox: bool = False, force_overwrite: bool = False, expected_revision: int | None = None, reusable=None, origin_job_id: str | None = None) -> dict[str, Any]:
        _check_revision(load_session(session_id) or {}, expected_revision)
        targets = self._targets(session_id, group_ids, row_ids)
        if generate_bbox and not force_overwrite:
            conflicts = [
                f"{target['trajectory_id']} Step {target['step']}"
                for target in targets
                if target.get("bbox_source") == "manual" or target.get("summary_source") == "manual" or target.get("thought_source") == "manual"
            ]
            if conflicts:
                raise ValueError("批量生成将覆盖人工修改，请确认后重试：" + "、".join(conflicts))
        session = load_session(session_id)
        configuration_fingerprint = self._configuration_fingerprint(generate_bbox)
        # Generated boxes and cleared draft fields can change a session while a
        # force operation runs. Keep its identity independent of those writes.
        scope = fingerprint([session_id, execution_pipeline_id(), configuration_fingerprint, generate_bbox,
                             force_overwrite, [_target_identity(t) for t in targets]])
        request_fingerprint = fingerprint({"configuration": configuration_fingerprint, "session_id": session_id, "targets": targets,
            "generate_bbox": generate_bbox, "force_overwrite": force_overwrite})
        matching = [job for job in self._session_jobs(session_id) if job.get("resume_scope") == scope]
        previous = matching[0] if matching else {}
        if origin_job_id:
            origin = next((job for job in matching if job["job_id"] == origin_job_id), None)
            if (not execution_pipeline_id() or not origin or origin.get("status") not in {"interrupted", "failed"}
                    or origin["request_fingerprint"] != request_fingerprint):
                raise ValueError("COT 重试来源、模型配置或已确认的输入不匹配")
            # An accepted retry may already have finished before the response arrives.
            child = next((job for job in matching if job.get("origin_job_id") == origin_job_id), None)
            if child:
                return child
            if previous.get("status") in {"queued", "running"}:
                return previous
            previous = origin
        if previous.get("status") in {"queued", "running"} and (
                previous.get("request_fingerprint") == request_fingerprint or generate_bbox):
            return previous
        if (reusable and reusable == (previous.get("job_id"), session.get("storage_revision"))
                and previous.get("request_fingerprint") == request_fingerprint):
            return previous
        resume = previous.get("status") in {"interrupted", "failed"}
        job_id = allocate_id("j", self.data_root)
        if resume and force_overwrite:
            # The original confirmation only authorized clearing the manual
            # values that existed then, never edits made since the failure.
            originals = {t["step_key"]: t.get("force_baseline", t["edit_baseline"]) for t in previous["targets"]}
            resume = all(t["bbox_source"] != "manual" or
                         t["edit_baseline"].get("actions_box") == originals[t["step_key"]].get("actions_box")
                         for t in targets)
            if resume:
                for target in targets:
                    target["force_baseline"] = originals[target["step_key"]]
        publish_stage_snapshot(session, _snapshot(session), "06_correction")
        payload = {
            "job_id": job_id, "resume_scope": scope,
            **({"origin_job_id": origin_job_id} if origin_job_id else {}),
            "operation_id": previous["operation_id"] if resume else job_id,
            "request_fingerprint": request_fingerprint,
            "configuration_fingerprint": configuration_fingerprint,
            "session_id": session_id,
            "batch_id": session_batch_id(session),
            "group_ids": group_ids or [],
            "row_ids": row_ids or [],
            "generate_bbox": bool(generate_bbox),
            "force_overwrite": bool(force_overwrite),
            "targets": targets,
            "status": "queued",
            "stage": "queued",
            "created_at": _now(),
            "started_at": None,
            "completed_at": None,
            "current_task": None,
            "current_trajectory": None,
            "current_step": None,
            "completed_steps": 0,
            "total_steps": len(targets),
            "percent": 0,
            "completed_bbox": 0,
            "completed_cot": 0,
            "error": None,
        }
        with self._lock:
            self._write(payload)
        submit_with_context(self._executor, self._run, payload["job_id"])
        return payload

    def _progress(self, job_id: str, changes: dict[str, Any]) -> None:
        with self._lock:
            payload = self.get(job_id)
            if payload is None:
                return
            payload.update(changes)
            self._write(payload)

    def _run(self, job_id: str) -> None:
        try:
            job = self.get(job_id) or {}
        except Exception:
            job = {}
        with diagnostics.job_context(job, "cot", storage_root()):
            if diagnostics.current_context() and job.get("generate_bbox"):
                install_box_reviewer_logging()
            return self._run_with_diagnostics(job_id)

    def _run_with_diagnostics(self, job_id: str) -> None:
        payload = self.get(job_id)
        if payload is None:
            return
        def check_stop():
            check_job_stop(self._records, "correction_cot_jobs", job_id)

        def commit_guard():
            return job_commit_guard(self._records, "correction_cot_jobs", job_id)

        generator = reviewer = None
        session_id = str(payload["session_id"])
        try:
            with self._lock, commit_guard():
                self._progress(job_id, {"status": "running", "stage": "generating_bbox" if payload.get("generate_bbox") else "generating_cot", "started_at": _now()})
            session = load_session(session_id)
            if session is None:
                raise FileNotFoundError("纠偏会话不存在")
            with active_session_lock(str(payload["session_id"])):
                pass
            if payload.get("configuration_fingerprint") != self._configuration_fingerprint(bool(payload.get("generate_bbox"))):
                raise RuntimeError("模型配置已更新，请重新提交 COT 任务")
            payload.setdefault("operation_id", job_id)
            managed = retry_enabled_for_pipeline(payload.get("pipeline_id"), storage_root())
            pending = []
            verified = {}
            completed = completed_bbox = 0
            for target in payload["targets"]:
                check_stop()
                row = str(target["excel_row"])
                if (session.get("cot", {}).get(row, {}).get("input_digest") or
                        session.get("row_edits", {}).get(row, {}).get("bbox_checkpoint")):
                    image = session_asset(session_id, target["image"])
                    cot_key, bbox_key = _checkpoint_inputs(payload, target, image)
                    has_cot, has_box = _restored_parts(session, payload, target, cot_key, bbox_key)
                    if has_cot:
                        completed += 1
                        completed_bbox += int(has_box)
                        verified[target["step_key"]] = (cot_key, target["bbox_hash"])
                        continue
                pending.append(target)
            self._progress(job_id, {"completed_steps": completed, "completed_cot": completed,
                                    "completed_bbox": completed_bbox,
                                    "percent": round(completed / len(payload["targets"]) * 100)})
            if pending:
                generator = self.generator_factory()
                if isinstance(generator, QwenCotGenerator):
                    generator.cache_dir = None
                    if managed:
                        generator.client = generator.client.with_options(max_retries=0)
            for target in pending:
                check_stop()
                with active_session_lock(session_id) as current:
                    self._validate_target(current, target)
                self._progress(job_id, {"current_task": target["task"], "current_trajectory": target["trajectory_id"], "current_step": target["step"]})
                image = session_asset(session_id, target["image"])
                cot_key, bbox_key = _checkpoint_inputs(payload, target, image)
                _, has_box = _restored_parts(current, payload, target, cot_key, bbox_key)
                completed_bbox += int(has_box)
                action = dict(target["action"])
                if payload.get("generate_bbox") and not has_box and str(action.get("action", "")).lower() in {"click", "long_press", "swipe"}:
                    if reviewer is None:
                        reviewer = _bbox_reviewer()
                        if managed:
                            reviewer.client = reviewer.client.with_options(max_retries=0)
                            reviewer.max_output_attempts = 1
                    stable = _stable_image(image)
                    with diagnostics.trace_context(task_id=target.get("task_id"), trajectory_id=target["trajectory_id"],
                                                   step=target["step"], step_key=target.get("step_key")):
                        resolution = resolve_action_box(
                            image_path=stable,
                            xml_text=_xml_text(stable, target),
                            action=action,
                            action_summary=str(target.get("summary") or ""),
                            reviewer=reviewer,
                            max_review_rounds=4,
                        )
                    box = _format_manual_actions_box(action, resolution.result.bbox)
                    def save_bbox(current):
                        self._validate_target(current, target)
                        edit = current.setdefault("row_edits", {}).setdefault(str(target["excel_row"]), {})
                        baseline = target.get("edit_baseline", {})
                        if edit.get("actions_box") != baseline.get("actions_box"):
                            raise RuntimeError("生成期间标框已被修改，请重新提交")
                        if ("force_baseline" in target and edit.get("bbox_source") == "manual"
                                and edit.get("actions_box") != target["force_baseline"].get("actions_box")):
                            raise RuntimeError("已确认的人工标框发生变化，请重新确认覆盖")
                        edit.update(actions_box=box, bbox_source="generated", bbox_generated_at=_now(), bbox_model=reviewer.model)
                        if bbox_key:
                            edit["bbox_checkpoint"] = {"operation_id": payload["operation_id"],
                                                      "input": bbox_key, "box": _bbox_hash(box)}
                        current.setdefault("cot", {}).pop(str(target["excel_row"]), None)
                    with active_session_lock(session_id), commit_guard():
                        update_session(session_id, save_bbox)
                    target["reference_answer"] = box
                    target["bbox_hash"] = _bbox_hash(box)
                    completed_bbox += 1
                    self._progress(job_id, {"stage": "generating_bbox", "completed_bbox": completed_bbox})
                with active_session_lock(str(payload["session_id"])):
                    pass
                check_stop()
                with diagnostics.trace_context(task_id=target.get("task_id"), trajectory_id=target["trajectory_id"],
                                               step=target["step"], step_key=target.get("step_key")):
                    result = generator.generate(task=target["task"], trajectory_id=target["trajectory_id"], step=int(target["step"]), history=target["history"], action=target["action"], image=image, reference_answer=str(target.get("reference_answer") or ""))
                if (cot_key, bbox_key) != _checkpoint_inputs(payload, target, image):
                    raise RuntimeError("生成期间图片或标框输入发生变化，请重新提交")
                def save_cot(current):
                    self._validate_target(current, target)
                    edit = current.setdefault("row_edits", {}).setdefault(str(target["excel_row"]), {})
                    current_bbox = str(edit.get("actions_box", target.get("reference_answer", "")))
                    current.setdefault("cot", {})[str(target["excel_row"])] = {
                        "thought": str(result["thought"]), "summary": str(result["summary"]),
                        "model": generator.model, "content_tag": "thought_summary",
                        "input_digest": cot_key, "operation_id": payload["operation_id"],
                        "step_key": target.get("step_key"), "source_fingerprint": target.get("source_fingerprint"),
                        "task_fingerprint": target.get("task_fingerprint"),
                        "action_hash": _action_hash(target["action"]), "bbox_hash": _bbox_hash(current_bbox),
                        "actions_box": current_bbox, "generated_at": _now(),
                    }
                    # Automated generation fills generated content without
                    # removing handwritten text. Only explicit force-overwrite
                    # may replace the baseline, never a later human edit.
                    baseline = target.get("force_baseline", target.get("edit_baseline", {}))
                    if payload.get("force_overwrite"):
                        for field in ("summary", "thought"):
                            if field in baseline and edit.get(field) == baseline[field]:
                                edit.pop(field, None)
                with active_session_lock(session_id), commit_guard():
                    update_session(session_id, save_cot)
                verified[target["step_key"]] = (cot_key, target["bbox_hash"])
                completed += 1
                self._progress(job_id, {"stage": "generating_cot", "completed_steps": completed, "completed_cot": completed, "percent": round(completed / len(payload["targets"]) * 100), "error": None})
        except PipelineJobCancelled:
            self._cancelled(job_id)
            return
        except BatchPublishedError as exc:
            diagnostics.log_event("job_exception", error=diagnostics.format_exception(exc))
            self._progress(job_id, {"status": "failed", "stage": "batch_published", "completed_at": _now(),
                                   "error": str(exc), "error_code": "batch_published", "detail": exc.detail})
            return
        except Exception as exc:
            diagnostics.log_event("job_exception", error=diagnostics.format_exception(exc))
            failure = failure_from_exception(exc)
            self._progress(job_id, {"status": "failed", "stage": "failed", "completed_at": _now(),
                                    "error": (failure["message"] if retry_enabled_for_pipeline(payload.get("pipeline_id"), storage_root())
                                              else str(exc)), "failure": failure})
            return
        finally:
            # with_options() shares the transport; close the final client once.
            if isinstance(generator, QwenCotGenerator):
                with suppress(Exception):
                    generator.close()
            if reviewer is not None:
                with suppress(Exception):
                    reviewer.client.close()
        try:
            check_stop()
            with active_session_lock(session_id) as current:
                # Recheck the entire selection once before publication, including
                # skipped rows which had no write transaction during this attempt.
                targets = self._targets(session_id, payload.get("group_ids"), payload.get("row_ids"))
                latest = {t["step_key"]: t for t in targets}
                for original in payload["targets"]:
                    now = latest.get(original["step_key"])
                    if now is None or _target_identity(now) != _target_identity(original):
                        raise RuntimeError("COT 生成后步骤来源或动作发生变化，请重新提交")
                    result = current.get("cot", {}).get(str(now["excel_row"])) or {}
                    if ((result.get("input_digest"), now["bbox_hash"]) != verified[original["step_key"]] or
                            result.get("bbox_hash") != now["bbox_hash"] or not result.get("thought")
                            or not result.get("summary")):
                        raise RuntimeError("COT 生成后已保存的步骤发生变化，请重新提交")
                artifact = (publish_cot_snapshot(session_id, commit_guard=commit_guard) if payload.get("pipeline_id")
                            else publish_cot_snapshot(session_id))
            self._progress(job_id, {"artifact": artifact})
        except PipelineJobCancelled:
            self._cancelled(job_id)
            return
        except BatchPublishedError as exc:
            diagnostics.log_event("job_exception", error=diagnostics.format_exception(exc))
            self._progress(job_id, {"status": "failed", "stage": "batch_published", "completed_at": _now(),
                                   "error": str(exc), "error_code": "batch_published", "detail": exc.detail})
            return
        except Exception as exc:
            diagnostics.log_event("job_exception", error=diagnostics.format_exception(exc))
            self._progress(job_id, {"status": "failed", "stage": "export_failed", "completed_at": _now(), "error": f"COT 已保存，过程表导出失败；可重新导出，无需重新生成：{exc}"})
            return
        self._progress(job_id, {"status": "succeeded", "stage": "succeeded", "completed_at": _now(), "percent": 100, "error": None})

    def _cancelled(self, job_id):
        self._progress(job_id, {"status": "cancelled", "stage": "cancelled", "completed_at": _now(),
                                "error": None, "failure": None})

    @staticmethod
    def _validate_target(session, target):
        if target.get("task_id") in session.get("stale_tasks", []):
            raise RuntimeError("生成期间任务来源已更新，请重新提交")
        if target.get("step_key"):
            snapshot = identified(_snapshot(session))
            current = row_index(snapshot).get(target["step_key"])
            current_group = next((group for group in snapshot["groups"] if any(row["step_key"] == target["step_key"] for row in group["rows"])), None)
            if current_group is not None:
                rows = sorted(current_group["rows"], key=lambda row: int(row["step"]))
                index = next(index for index, row in enumerate(rows) if row["step_key"] == target["step_key"])
                history = "\n".join(f"Step {int(previous['step'])}: {str(previous.get('original_summary') or previous.get('summary') or '').strip()}"
                    for previous in rows[:index] if str(previous.get("summary") or "").strip()
                    and not session.get("row_edits", {}).get(str(previous["excel_row"]), {}).get("deleted"))
                if target.get("history_hash") and fingerprint(history) != target["history_hash"]:
                    raise RuntimeError("生成期间步骤上下文已更新，请重新提交")
            if current is None or current["source_fingerprint"] != target.get("source_fingerprint"):
                raise RuntimeError("生成期间步骤来源已更新，请重新提交")
            if session.get("task_fingerprints", {}).get(target["task_id"]) != target.get("task_fingerprint"):
                raise RuntimeError("生成期间任务来源已更新，请重新提交")
            # Another task may have changed the Excel position without changing this step.
            target["excel_row"] = int(current["excel_row"])
        edit = session.get("row_edits", {}).get(str(target["excel_row"]), {})
        if "actions_box" in edit and _bbox_hash(str(edit["actions_box"])) != target.get("bbox_hash"):
            raise RuntimeError("生成期间标框已修改，请重新提交")
        if edit.get("deleted"):
            raise RuntimeError(f"第 {target['step']} 步在生成期间被删除，请重新提交")
        current_action = json.loads(edit["actions"]) if "actions" in edit else target.get("base_action", target["action"])
        if _action_hash(current_action) != _action_hash(target["action"]):
            raise RuntimeError(f"第 {target['step']} 步在生成期间动作发生变化，请重新提交")

    def shutdown(self) -> None:
        if self._owns_executor:
            self._executor.shutdown(wait=False, cancel_futures=True)
