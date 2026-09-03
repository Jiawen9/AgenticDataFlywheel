"""Persistent mock uploader for published datasets."""

from __future__ import annotations

from concurrent.futures import Executor, ThreadPoolExecutor
import json
import os
from pathlib import Path
import threading
import time
import uuid
from typing import Any, Callable, Optional

from ..trajectory_correction.draft_store import utc_now
from .constants import BACKEND_DIR, UPLOAD_JOBS_DIR
from .service import DatasetReleaseRegistry


Progress = Callable[[dict[str, Any]], None]


def _load_env() -> dict[str, str]:
    values: dict[str, str] = {}
    path = BACKEND_DIR / ".env"
    if path.is_file():
        for raw in path.read_text(encoding="utf-8-sig").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _setting(name: str, default: str) -> str:
    return os.getenv(name) or _load_env().get(name) or default


class DatasetUploadJobManager:
    def __init__(
        self,
        registry: DatasetReleaseRegistry,
        *,
        jobs_dir: Path = UPLOAD_JOBS_DIR,
        executor: Optional[Executor] = None,
        step_delay: float = 0.01,
    ) -> None:
        self.registry = registry
        self.jobs_dir = jobs_dir
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._owns_executor = executor is None
        self._executor = executor or ThreadPoolExecutor(max_workers=1, thread_name_prefix="dataset-upload")
        self.step_delay = step_delay
        self.mode = _setting("DATASET_UPLOAD_MODE", "mock")
        self.bucket = _setting("DATASET_S3_BUCKET", "training-data")
        self.prefix = _setting("DATASET_S3_PREFIX", "gui-agent-datasets").strip("/")
        self.mark_interrupted_jobs()

    def _path(self, job_id: str) -> Path:
        return self.jobs_dir / f"{job_id}.json"

    def _write(self, payload: dict[str, Any]) -> None:
        target = self._path(str(payload["job_id"]))
        temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(target)

    def get(self, job_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
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
            if not isinstance(payload, dict) or payload.get("status") not in {"queued", "running", "uploading"}:
                continue
            payload.update(
                {
                    "status": "interrupted",
                    "stage": "interrupted",
                    "completed_at": utc_now(),
                    "error": "服务重启导致模拟上传中断，可重新提交上传",
                }
            )
            self._write(payload)
            try:
                self.registry.update(
                    str(payload.get("release_id", "")),
                    {
                        "upload_status": "interrupted",
                        "upload_error": payload["error"],
                    },
                )
            except (FileNotFoundError, ValueError, OSError):
                pass

    def _files(self, release: dict[str, Any]) -> list[tuple[str, int]]:
        files: list[tuple[str, int]] = []
        for item in release.get("excel_paths", []):
            value = str(item.get("path", ""))
            path = self.registry.resolve_project_path(value)
            if not path.is_file():
                raise FileNotFoundError(f"发布文件不存在：{value}")
            files.append((value, path.stat().st_size))
        for root_value in release.get("trajectory_paths", []):
            root = self.registry.resolve_project_path(str(root_value))
            if not root.is_dir():
                raise FileNotFoundError(f"轨迹目录不存在：{root_value}")
            for path in sorted((item for item in root.rglob("*") if item.is_file()), key=lambda item: item.as_posix()):
                files.append((self.registry.project_path(path), path.stat().st_size))
        if not files:
            raise ValueError("发布记录没有可上传文件")
        return files

    def _progress(self, job_id: str, changes: dict[str, Any]) -> None:
        with self._lock:
            payload = self.get(job_id)
            if payload is None:
                return
            payload.update(changes)
            total = int(payload.get("total_files") or 0)
            completed = int(payload.get("completed_files") or 0)
            if payload.get("status") == "succeeded":
                payload["percent"] = 100
            else:
                payload["percent"] = min(99, round(100 * completed / total)) if total else 0
            self._write(payload)

    def submit(self, release_id: str) -> dict[str, Any]:
        if self.mode != "mock":
            raise ValueError("当前版本仅支持 DATASET_UPLOAD_MODE=mock")
        release = self.registry.get(release_id)
        if release is None:
            raise FileNotFoundError("数据集发布记录不存在")
        if release.get("upload_status") in {"queued", "uploading"}:
            raise ValueError("该数据集正在上传")
        job_id = uuid.uuid4().hex
        payload: dict[str, Any] = {
            "job_id": job_id,
            "release_id": release_id,
            "mode": self.mode,
            "status": "queued",
            "stage": "queued",
            "created_at": utc_now(),
            "started_at": None,
            "completed_at": None,
            "current_file": None,
            "completed_files": 0,
            "total_files": 0,
            "completed_bytes": 0,
            "total_bytes": 0,
            "percent": 0,
            "s3_uri": None,
            "error": None,
        }
        with self._lock:
            self._write(payload)
            self.registry.update(
                release_id,
                {
                    "upload_status": "queued",
                    "upload_job_id": job_id,
                    "upload_error": None,
                },
            )
        self._executor.submit(self._run, job_id)
        return payload

    def _run(self, job_id: str) -> None:
        payload = self.get(job_id)
        if payload is None:
            return
        release_id = str(payload["release_id"])
        try:
            self._progress(job_id, {"status": "uploading", "stage": "scanning", "started_at": utc_now()})
            release = self.registry.get(release_id)
            if release is None:
                raise FileNotFoundError("数据集发布记录不存在")
            files = self._files(release)
            total_bytes = sum(size for _, size in files)
            self._progress(
                job_id,
                {"stage": "uploading", "total_files": len(files), "total_bytes": total_bytes},
            )
            self.registry.update(release_id, {"upload_status": "uploading"})
            completed_bytes = 0
            for index, (name, size) in enumerate(files, 1):
                if self.step_delay:
                    time.sleep(self.step_delay)
                completed_bytes += size
                self._progress(
                    job_id,
                    {
                        "current_file": name,
                        "completed_files": index,
                        "completed_bytes": completed_bytes,
                    },
                )
            s3_uri = f"s3://{self.bucket}/{self.prefix}/{release_id}/"
            completed_at = utc_now()
            final = {
                "status": "succeeded",
                "stage": "succeeded",
                "completed_at": completed_at,
                "current_file": None,
                "percent": 100,
                "s3_uri": s3_uri,
                "error": None,
            }
            self._progress(job_id, final)
            self.registry.update(
                release_id,
                {
                    "upload_status": "succeeded",
                    "upload_error": None,
                    "s3_uri": s3_uri,
                    "uploaded_at": completed_at,
                    "uploaded_files": len(files),
                    "uploaded_bytes": completed_bytes,
                },
            )
        except Exception as exc:
            message = str(exc)
            self._progress(
                job_id,
                {
                    "status": "failed",
                    "stage": "failed",
                    "completed_at": utc_now(),
                    "error": message,
                },
            )
            try:
                self.registry.update(
                    release_id,
                    {"upload_status": "failed", "upload_error": message},
                )
            except (FileNotFoundError, ValueError, OSError):
                pass

    def shutdown(self) -> None:
        if self._owns_executor:
            self._executor.shutdown(wait=True)
