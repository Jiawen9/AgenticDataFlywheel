"""Persistent background jobs for regenerating COT after action correction."""

from __future__ import annotations

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
from .cot_generator import QwenCotGenerator, read_env
from ..bounding_box.build_annotations import resolve_action_box
from ..bounding_box.qwen_reviewer import QwenBoxReviewer
from ..trajectory_data import _format_manual_actions_box
from .draft_store import load_session, save_session
from .service import _snapshot, session_asset


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
            path = Path(value)
            if path.is_file():
                return path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            pass
    path = image.with_name(re.sub(r"_vla_input(?:_stability)?\.jpg$", "_vla_input_ui.xml", image.name, flags=re.IGNORECASE))
    try:
        return path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""
    except OSError:
        return ""


class CotJobManager:
    def __init__(self, jobs_dir: Path = CORRECTION_COT_JOBS_DIR, generator_factory: Callable[[], QwenCotGenerator] = QwenCotGenerator, executor: Executor | None = None) -> None:
        self.jobs_dir = jobs_dir
        self.generator_factory = generator_factory
        self._lock = threading.RLock()
        self._owns_executor = executor is None
        self._executor = executor or ThreadPoolExecutor(max_workers=1, thread_name_prefix="cot-generation")
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self.mark_interrupted_jobs()

    def _path(self, job_id: str) -> Path:
        return self.jobs_dir / f"{job_id}.json"

    def _write(self, payload: dict[str, Any]) -> None:
        target = self._path(str(payload["job_id"]))
        temporary = target.with_name(f".{target.name}.tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(target)

    def get(self, job_id: str) -> dict[str, Any] | None:
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
        with self._lock:
            values: list[dict[str, Any]] = []
            for path in self.jobs_dir.glob("*.json"):
                try:
                    value = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError, json.JSONDecodeError):
                    continue
                if isinstance(value, dict):
                    values.append(value)
            return sorted(values, key=lambda item: str(item.get("created_at", "")), reverse=True)

    def mark_interrupted_jobs(self) -> None:
        for path in self.jobs_dir.glob("*.json"):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            if payload.get("status") in {"queued", "running"}:
                payload.update(status="interrupted", completed_at=_now(), error="服务重启导致 COT 生成中断；重新提交可复用已完成结果。")
                self._write(payload)

    @staticmethod
    def _targets(session_id: str, group_ids: list[str] | None, row_ids: list[int] | None = None) -> list[dict[str, Any]]:
        session = load_session(session_id)
        if session is None:
            raise FileNotFoundError("纠偏会话不存在")
        snapshot = _snapshot(session)
        allowed = set(group_ids or [str(group["group_id"]) for group in snapshot["groups"]])
        requested_rows = {int(value) for value in (row_ids or [])}
        targets: list[dict[str, Any]] = []
        for group in snapshot["groups"]:
            if str(group["group_id"]) not in allowed:
                continue
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
                    "group_id": group["group_id"],
                    "task": group["task"],
                    "trajectory_id": group["meta_task"],
                    "excel_row": int(row["excel_row"]),
                    "step": int(row["step"]),
                    "image": row["image"],
                    "action": action,
                    "history": history,
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

    def submit(self, session_id: str, group_ids: list[str] | None = None, row_ids: list[int] | None = None, *, generate_bbox: bool = False, force_overwrite: bool = False) -> dict[str, Any]:
        targets = self._targets(session_id, group_ids, row_ids)
        if generate_bbox and not force_overwrite:
            conflicts = [
                f"{target['trajectory_id']} Step {target['step']}"
                for target in targets
                if target.get("bbox_source") == "manual" or target.get("summary_source") == "manual" or target.get("thought_source") == "manual"
            ]
            if conflicts:
                raise ValueError("批量生成将覆盖人工修改，请确认后重试：" + "、".join(conflicts))
        payload = {
            "job_id": uuid.uuid4().hex,
            "session_id": session_id,
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
        self._executor.submit(self._run, payload["job_id"])
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
            generator = self.generator_factory()
            reviewer = _bbox_reviewer() if payload.get("generate_bbox") else None
            cot = session.setdefault("cot", {})
            completed = int(payload.get("completed_steps") or 0)
            completed_bbox = int(payload.get("completed_bbox") or 0)
            completed_cot = int(payload.get("completed_cot") or 0)
            for target in payload["targets"]:
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
                    edit = session.setdefault("row_edits", {}).setdefault(str(target["excel_row"]), {})
                    edit["actions_box"] = box
                    edit["bbox_source"] = "generated"
                    edit["bbox_generated_at"] = _now()
                    edit["bbox_model"] = reviewer.model
                    cot.pop(str(target["excel_row"]), None)
                    target["reference_answer"] = box
                    target["bbox_hash"] = _bbox_hash(box)
                    save_session(session)
                    completed_bbox += 1
                    self._progress(job_id, {"stage": "generating_bbox", "completed_bbox": completed_bbox})
                result = generator.generate(task=target["task"], trajectory_id=target["trajectory_id"], step=int(target["step"]), history=target["history"], action=target["action"], image=image, reference_answer=str(target.get("reference_answer") or ""))
                current_edit = session.get("row_edits", {}).get(str(target["excel_row"]), {})
                current_action = json.loads(current_edit.get("actions", json.dumps(target["action"], ensure_ascii=False)))
                if _action_hash(current_action) != _action_hash(target["action"]):
                    raise RuntimeError(f"第 {target['step']} 步在生成期间动作发生变化，请重新提交")
                current_edit = session.get("row_edits", {}).get(str(target["excel_row"]), {})
                current_bbox = str(current_edit.get("actions_box", target.get("reference_answer", "")))
                cot[str(target["excel_row"])] = {
                    "thought": str(result["thought"]),
                    "summary": str(result["summary"]),
                    "model": generator.model,
                    "content_tag": "thought_summary",
                    "action_hash": _action_hash(target["action"]),
                    "bbox_hash": _bbox_hash(current_bbox),
                    "actions_box": current_bbox,
                    "generated_at": _now(),
                }
                current_edit = session.setdefault("row_edits", {}).setdefault(str(target["excel_row"]), {})
                current_edit.pop("summary", None)
                current_edit.pop("thought", None)
                save_session(session)
                completed += 1
                completed_cot += 1
                self._progress(job_id, {"stage": "generating_cot", "completed_steps": completed, "completed_cot": completed_cot, "percent": round(completed / len(payload["targets"]) * 100), "error": None})
        except Exception as exc:
            self._progress(job_id, {"status": "failed", "stage": "failed", "completed_at": _now(), "error": str(exc)})
            return
        self._progress(job_id, {"status": "succeeded", "stage": "succeeded", "completed_at": _now(), "percent": 100, "error": None})

    def shutdown(self) -> None:
        if self._owns_executor:
            self._executor.shutdown(wait=False, cancel_futures=True)
