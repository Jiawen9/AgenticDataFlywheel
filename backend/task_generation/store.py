"""Persistent storage for task-generation inputs, jobs, and outputs."""

from __future__ import annotations

import json
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[2]
TASK_GENERATION_ROOT = PROJECT_ROOT / "backend_workspace" / "task_generation"
INPUTS_DIR = TASK_GENERATION_ROOT / "inputs"
JOBS_DIR = TASK_GENERATION_ROOT / "jobs"
OUTPUTS_DIR = TASK_GENERATION_ROOT / "outputs"

ID_RE = re.compile(r"^[a-f0-9]{12,64}$")
STORE_LOCK = threading.RLock()


def ensure_dirs() -> None:
    for directory in (INPUTS_DIR, JOBS_DIR, OUTPUTS_DIR):
        directory.mkdir(parents=True, exist_ok=True)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_id(length: int = 24) -> str:
    return uuid.uuid4().hex[:length]


def _validate_id(value: str, label: str) -> str:
    if not ID_RE.fullmatch(value):
        raise ValueError(f"无效的{label} ID")
    return value


def _atomic_write(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with STORE_LOCK:
        temporary.write_text(payload, encoding="utf-8")
        for attempt in range(4):
            try:
                temporary.replace(path)
                break
            except PermissionError:
                if attempt == 3:
                    raise
                time.sleep(0.05 * (attempt + 1))
        else:  # pragma: no cover - the loop either replaces or raises
            raise PermissionError(f"无法写入 {path}")


def save_job(job: dict[str, Any]) -> dict[str, Any]:
    ensure_dirs()
    job["updated_at"] = utc_now()
    job_id = _validate_id(str(job["job_id"]), "作业")
    path = JOBS_DIR / f"{job_id}.json"
    _atomic_write(path, json.dumps(job, ensure_ascii=False, indent=2))
    return job


def load_job(job_id: str) -> dict[str, Any] | None:
    job_id = _validate_id(job_id, "作业")
    path = JOBS_DIR / f"{job_id}.json"
    if not path.is_file():
        return None
    with STORE_LOCK:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            return None


def list_jobs() -> list[dict[str, Any]]:
    ensure_dirs()
    values: list[dict[str, Any]] = []
    with STORE_LOCK:
        for path in JOBS_DIR.glob("*.json"):
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            if isinstance(value, dict):
                values.append(value)
    return sorted(values, key=lambda item: str(item.get("created_at", "")), reverse=True)


def mark_unfinished_jobs_interrupted() -> None:
    for job in list_jobs():
        if job.get("status") in {"queued", "running"}:
            job["status"] = "interrupted"
            job["stage"] = "interrupted"
            job["error"] = "服务重启，作业未继续执行"
            save_job(job)


def save_input(input_id: str, original_filename: str, content: bytes) -> dict[str, Any]:
    ensure_dirs()
    input_id = _validate_id(input_id, "输入")
    directory = (INPUTS_DIR / input_id).resolve()
    try:
        directory.relative_to(INPUTS_DIR.resolve())
    except ValueError as exc:
        raise ValueError("输入目录无效") from exc
    directory.mkdir(parents=True, exist_ok=True)
    source_path = directory / "source.xlsx"
    source_path.write_bytes(content)
    metadata = {
        "input_id": input_id,
        "original_filename": original_filename,
        "size_bytes": len(content),
        "created_at": utc_now(),
        "status": "ready",
    }
    save_input_metadata(metadata)
    return metadata


def save_input_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    input_id = _validate_id(str(metadata["input_id"]), "输入")
    directory = (INPUTS_DIR / input_id).resolve()
    try:
        directory.relative_to(INPUTS_DIR.resolve())
    except ValueError as exc:
        raise ValueError("输入目录无效") from exc
    _atomic_write(directory / "metadata.json", json.dumps(metadata, ensure_ascii=False, indent=2))
    return metadata


def load_input(input_id: str) -> dict[str, Any] | None:
    input_id = _validate_id(input_id, "输入")
    path = (INPUTS_DIR / input_id / "metadata.json").resolve()
    try:
        path.relative_to(INPUTS_DIR.resolve())
    except ValueError as exc:
        raise ValueError("输入路径无效") from exc
    if not path.is_file():
        return None
    with STORE_LOCK:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            return None


def input_path(input_id: str) -> Path:
    input_id = _validate_id(input_id, "输入")
    path = (INPUTS_DIR / input_id / "source.xlsx").resolve()
    try:
        path.relative_to(INPUTS_DIR.resolve())
    except ValueError as exc:
        raise ValueError("输入路径无效") from exc
    return path


def artifact_path(job_id: str, filename: str) -> Path:
    job_id = _validate_id(job_id, "作业")
    if filename not in {"result.json", "result.xlsx", "scene_matches.json", "scene_matches.xlsx"}:
        raise ValueError("不支持的结果文件")
    directory = (OUTPUTS_DIR / job_id).resolve()
    try:
        directory.relative_to(OUTPUTS_DIR.resolve())
    except ValueError as exc:
        raise ValueError("结果目录无效") from exc
    return directory / filename


def save_json_artifact(job_id: str, filename: str, value: Any) -> Path:
    path = artifact_path(job_id, filename)
    _atomic_write(path, json.dumps(value, ensure_ascii=False, indent=2, default=str))
    return path
