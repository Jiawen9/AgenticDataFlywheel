"""Persistent background jobs for regenerating COT after action correction."""

from __future__ import annotations

from ..pipeline_access import submit_with_context, execution_pipeline_id

import hashlib
import json
import re
import threading
import uuid
from concurrent.futures import Executor, ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from .constants import CORRECTION_BBOX_CACHE_DIR, CORRECTION_COT_JOBS_DIR, PROJECT_ROOT
from ..data_store import RecordStore, DATA_ROOT, rebase_data_path
from .cot_generator import QwenCotGenerator, read_env, SYSTEM_PROMPT
from ..bounding_box.build_annotations import resolve_action_box
from ..bounding_box.qwen_reviewer import QwenBoxReviewer
from ..trajectory_data import _format_manual_actions_box
from .draft_store import load_session, update_session, storage_root, session_batch_id
from .service import _snapshot, session_asset, publish_stage_snapshot, publish_cot_snapshot, _check_revision, active_session_lock
from .session_state import identified, row_index, fingerprint
from ..batch_lifecycle import BatchPublishedError, is_batch_active


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
    return QwenBoxReviewer(model=bbox_model, cache_path=CORRECTION_BBOX_CACHE_DIR / "qwen_review_cache.json")


def _stable_image(image: Path) -> Path:
    name = image.name.replace("_vla_input.jpg", "_vla_input_stability.jpg")
    candidate = image.with_name(name)
    return candidate if candidate.is_file() else image


def _xml_text(image: Path, row: dict[str, Any]) -> str:
    value = str(row.get("xml", "") or "")
    if value and not value.startswith("embedded:") and not value.startswith("missing"):
        try:
            path = rebase_data_path(value, storage_root())
            if path.is_file():
                return path.read_text(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            pass
    path = image.with_name(re.sub(r"_vla_input(?:_stability)?\.jpg$", "_vla_input_ui.xml", image.name, flags=re.IGNORECASE))
    try:
        return path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""
    except OSError:
        return ""


class CotJobManager:
    def __init__(self, jobs_dir: Path = CORRECTION_COT_JOBS_DIR, generator_factory: Callable[[], QwenCotGenerator] = QwenCotGenerator, executor: Executor | None = None) -> None:
        self.jobs_dir = jobs_dir
        self._records = RecordStore(DATA_ROOT if jobs_dir == CORRECTION_COT_JOBS_DIR else jobs_dir.parent)
        self.generator_factory = generator_factory
        self._lock = threading.RLock()
        self._owns_executor = executor is None
        self._executor = executor or ThreadPoolExecutor(max_workers=1, thread_name_prefix="cot-generation")
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self.mark_interrupted_jobs()

    def _path(self, job_id: str) -> Path:
        if not re.fullmatch(r"[a-f0-9]{32}", job_id):
            return self.jobs_dir / ".invalid-job-id"
        return self.jobs_dir / f"{job_id}.json"

    def _write(self, payload: dict[str, Any]) -> None:
        if execution_pipeline_id():
            payload.setdefault("pipeline_id", execution_pipeline_id())
        saved = self._records.put("correction_cot_jobs", str(payload["job_id"]), payload)
        payload["storage_revision"] = saved["storage_revision"]

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
                payload.update(status="interrupted", completed_at=_now(), error="服务重启导致 COT 生成中断；重新提交可复用已完成结果。")
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

    def submit(self, session_id: str, group_ids: list[str] | None = None, row_ids: list[int] | None = None, *, generate_bbox: bool = False, force_overwrite: bool = False, expected_revision: int | None = None) -> dict[str, Any]:
        with active_session_lock(session_id) as session:
            from ..pipeline_access import ensure_pipeline_write
            from .draft_store import storage_root
            ensure_pipeline_write(session_batch_id(session), storage_root())
            return self._submit(session_id, group_ids, row_ids, generate_bbox=generate_bbox,
                                force_overwrite=force_overwrite, expected_revision=expected_revision)

    def _submit(self, session_id: str, group_ids: list[str] | None = None, row_ids: list[int] | None = None, *, generate_bbox: bool = False, force_overwrite: bool = False, expected_revision: int | None = None) -> dict[str, Any]:
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
        request_fingerprint = fingerprint({"configuration": configuration_fingerprint, "session_id": session_id, "targets": targets,
            "generate_bbox": generate_bbox, "force_overwrite": force_overwrite})
        for previous in self.list_jobs():
            if previous.get("request_fingerprint") == request_fingerprint and previous.get("status") in {"queued", "running", "succeeded"}:
                return previous
        publish_stage_snapshot(session, _snapshot(session), "06_correction")
        payload = {
            "job_id": uuid.uuid4().hex,
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
        payload = self.get(job_id)
        if payload is None:
            return
        self._progress(job_id, {"status": "running", "stage": "generating_bbox" if payload.get("generate_bbox") else "generating_cot", "started_at": _now()})
        try:
            session = load_session(str(payload["session_id"]))
            if session is None:
                raise FileNotFoundError("纠偏会话不存在")
            with active_session_lock(str(payload["session_id"])):
                pass
            if payload.get("configuration_fingerprint") != self._configuration_fingerprint(bool(payload.get("generate_bbox"))):
                raise RuntimeError("模型配置已更新，请重新提交 COT 任务")
            generator = self.generator_factory()
            reviewer = _bbox_reviewer() if payload.get("generate_bbox") else None
            completed = int(payload.get("completed_steps") or 0)
            completed_bbox = int(payload.get("completed_bbox") or 0)
            completed_cot = int(payload.get("completed_cot") or 0)
            for target in payload["targets"]:
                with active_session_lock(str(payload["session_id"])):
                    pass
                self._progress(job_id, {"current_task": target["task"], "current_trajectory": target["trajectory_id"], "current_step": target["step"]})
                image = session_asset(str(payload["session_id"]), str(target["image"]))
                action = dict(target["action"])
                if reviewer is not None and str(action.get("action", "")).lower() in {"click", "long_press", "swipe"}:
                    stable = _stable_image(image)
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
                        edit.update(actions_box=box, bbox_source="generated", bbox_generated_at=_now(), bbox_model=reviewer.model)
                        current.setdefault("cot", {}).pop(str(target["excel_row"]), None)
                    update_session(str(payload["session_id"]), save_bbox)
                    target["reference_answer"] = box
                    target["bbox_hash"] = _bbox_hash(box)
                    completed_bbox += 1
                    self._progress(job_id, {"stage": "generating_bbox", "completed_bbox": completed_bbox})
                with active_session_lock(str(payload["session_id"])):
                    pass
                result = generator.generate(task=target["task"], trajectory_id=target["trajectory_id"], step=int(target["step"]), history=target["history"], action=target["action"], image=image, reference_answer=str(target.get("reference_answer") or ""))
                def save_cot(current):
                    self._validate_target(current, target)
                    edit = current.setdefault("row_edits", {}).setdefault(str(target["excel_row"]), {})
                    current_bbox = str(edit.get("actions_box", target.get("reference_answer", "")))
                    current.setdefault("cot", {})[str(target["excel_row"])] = {
                        "thought": str(result["thought"]), "summary": str(result["summary"]),
                        "model": generator.model, "content_tag": "thought_summary",
                        "step_key": target.get("step_key"), "source_fingerprint": target.get("source_fingerprint"),
                        "task_fingerprint": target.get("task_fingerprint"),
                        "action_hash": _action_hash(target["action"]), "bbox_hash": _bbox_hash(current_bbox),
                        "actions_box": current_bbox, "generated_at": _now(),
                    }
                    # Automated generation fills generated content without
                    # removing handwritten text. Only explicit force-overwrite
                    # may replace the baseline, never a later human edit.
                    baseline = target.get("edit_baseline", {})
                    if payload.get("force_overwrite"):
                        for field in ("summary", "thought"):
                            if field in baseline and edit.get(field) == baseline[field]:
                                edit.pop(field, None)
                update_session(str(payload["session_id"]), save_cot)
                completed += 1
                completed_cot += 1
                self._progress(job_id, {"stage": "generating_cot", "completed_steps": completed, "completed_cot": completed_cot, "percent": round(completed / len(payload["targets"]) * 100), "error": None})
        except BatchPublishedError as exc:
            self._progress(job_id, {"status": "failed", "stage": "batch_published", "completed_at": _now(),
                                   "error": str(exc), "error_code": "batch_published", "detail": exc.detail})
            return
        except Exception as exc:
            self._progress(job_id, {"status": "failed", "stage": "failed", "completed_at": _now(), "error": str(exc)})
            return
        try:
            artifact = publish_cot_snapshot(str(payload["session_id"]))
            self._progress(job_id, {"artifact": artifact})
        except BatchPublishedError as exc:
            self._progress(job_id, {"status": "failed", "stage": "batch_published", "completed_at": _now(),
                                   "error": str(exc), "error_code": "batch_published", "detail": exc.detail})
            return
        except Exception as exc:
            self._progress(job_id, {"status": "failed", "stage": "export_failed", "completed_at": _now(), "error": f"COT 已保存，过程表导出失败；可重新导出，无需重新生成：{exc}"})
            return
        self._progress(job_id, {"status": "succeeded", "stage": "succeeded", "completed_at": _now(), "percent": 100, "error": None})

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
