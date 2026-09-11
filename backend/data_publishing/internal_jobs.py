"""Excel-only uploads sharing the existing durable job infrastructure."""
from copy import deepcopy
import hashlib
from pathlib import Path
import re
from typing import Any
from urllib.parse import urlsplit
import uuid

from ..trajectory_correction.draft_store import utc_now
from .internal_uploader import UploadContext, UploadError, UploadResult


class InternalUploadConflict(ValueError):
    pass


class InternalUploadJobsMixin:
    def internal_capabilities(self) -> dict[str, Any]:
        try:
            configured = self.internal_adapter.is_configured() is True
            reason = None if configured else "云道S3上传尚未配置"
        except Exception:
            configured, reason = False, "云道S3上传配置检查失败，请联系维护人员"
        return {"internal": {"configured": configured, "reason": reason}}

    def _validate_excel(self, item: dict[str, Any]) -> Path:
        try:
            path = self.registry.resolve_project_path(str(item.get("path", "")))
            if path.suffix.lower() not in {".xlsx", ".xlsm"} or not path.is_file():
                raise ValueError("文件不存在或不是 Excel")
            expected = str(item.get("sha256", ""))
            if not re.fullmatch(r"[0-9a-fA-F]{64}", expected):
                raise ValueError("缺少有效的发布校验值")
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            if digest.hexdigest() != expected.lower():
                raise ValueError("文件内容与发布时的 SHA256 不一致")
            return path
        except (OSError, ValueError) as exc:
            raise InternalUploadConflict(f"{item.get('filename') or '发布表格'}：{exc}") from exc

    def _save_internal(self, payload: dict[str, Any]) -> None:
        # Persist receipts first, then the release's display summary.
        with self._lock:
            successful = [item for item in payload["file_results"] if item["status"] == "succeeded"]
            payload["completed_files"] = len(successful)
            payload["completed_bytes"] = sum(item["size_bytes"] for item in successful)
            payload["percent"] = round(100 * len(successful) / payload["total_files"]) if payload["total_files"] else 0
            payload["updated_at"] = utc_now()
            self._write(payload)
            keys = ("job_id", "attempt", "status", "created_at", "updated_at", "completed_at", "error",
                    "current_file", "completed_files", "total_files", "completed_bytes",
                    "total_bytes", "percent", "file_results")
            self.registry.update(payload["release_id"], {
                "internal_upload": deepcopy({key: payload.get(key) for key in keys}),
            })

    def _submit_internal(self, release_id: str) -> dict[str, Any]:
        with self._lock:
            release = self.registry.get(release_id)
            if release is None:
                raise FileNotFoundError("数据集发布记录不存在")
            summary = release.get("internal_upload") or {}
            previous = self.get(summary["job_id"]) if summary.get("job_id") else None
            if previous and (previous.get("mode") != "internal" or previous.get("release_id") != release_id):
                raise InternalUploadConflict("云道S3上传记录与数据集不匹配")
            if previous and previous["status"] in {"queued", "uploading", "succeeded"}:
                return previous
            capability = self.internal_capabilities()["internal"]
            if not capability["configured"]:
                raise InternalUploadConflict(capability["reason"])
            if not release.get("excel_paths"):
                raise InternalUploadConflict("发布记录没有可上传的 Excel")
            prior_results = (previous or summary).get("file_results", [])
            files = []
            for index, source in enumerate(release["excel_paths"]):
                path = self._validate_excel(source)
                try:
                    size = path.stat().st_size
                except OSError as exc:
                    raise InternalUploadConflict(f"{source['filename']}：无法读取发布文件") from exc
                digest = source["sha256"].lower()
                key = f"{release_id}:{index}:{digest}"
                prior = next((item for item in prior_results
                              if item.get("idempotency_key") == key and item.get("status") == "succeeded"), None)
                files.append({
                    "index": index, "path": source["path"], "filename": source["filename"],
                    "sha256": digest, "size_bytes": size, "idempotency_key": key,
                    "status": "succeeded" if prior else "pending",
                    "remote_id": prior.get("remote_id") if prior else None,
                    "url": prior.get("url") if prior else None, "error": None,
                })
            payload = {
                "job_id": uuid.uuid4().hex, "release_id": release_id, "dataset_name": release["name"],
                "attempt": int((previous or summary).get("attempt", 0)) + 1,
                "mode": "internal", "status": "queued", "stage": "queued",
                "created_at": utc_now(), "started_at": None, "completed_at": None,
                "current_file": None, "completed_files": 0, "completed_bytes": 0,
                "total_files": len(files), "total_bytes": sum(item["size_bytes"] for item in files),
                "percent": 0, "file_results": files, "s3_uri": None, "error": None,
            }
            if all(item["status"] == "succeeded" for item in files):
                payload.update(status="succeeded", stage="succeeded", completed_at=utc_now())
            self._save_internal(payload)
            submitted = deepcopy(payload)
            if payload["status"] != "succeeded":
                try:
                    self._executor.submit(self._run_internal, payload["job_id"])
                except Exception as exc:
                    payload.update(status="failed", stage="failed", completed_at=utc_now(),
                                   error="上传任务无法启动，请稍后重试")
                    self._save_internal(payload)
                    raise InternalUploadConflict(payload["error"]) from exc
            return submitted

    def _run_internal(self, job_id: str) -> None:
        payload = self.get(job_id)
        if payload is None:
            return
        current = None
        try:
            payload.update(status="uploading", stage="uploading", started_at=utc_now())
            self._save_internal(payload)
            for item in payload["file_results"]:
                if item["status"] == "succeeded":
                    continue
                current = item
                item["status"] = "uploading"
                payload["current_file"] = item["filename"]
                self._save_internal(payload)
                path = self._validate_excel(item)
                result = self.internal_adapter.upload_excel(UploadContext(
                    file_path=path, filename=item["filename"], dataset_name=payload["dataset_name"],
                    release_id=payload["release_id"], file_index=item["index"], sha256=item["sha256"],
                    idempotency_key=item["idempotency_key"],
                ))
                if not isinstance(result, UploadResult) or result.success is not True:
                    raise UploadError("上传接口未明确确认文件接收成功")
                if result.remote_id is not None and not isinstance(result.remote_id, str):
                    raise UploadError("上传接口返回的记录编号无效")
                if result.url is not None:
                    if not isinstance(result.url, str):
                        raise UploadError("上传接口返回的链接无效")
                    parsed = urlsplit(result.url)
                    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
                        raise UploadError("上传接口返回的链接必须是无凭据的 HTTP(S) 地址")
                item.update(status="succeeded", remote_id=result.remote_id, url=result.url, error=None)
                self._save_internal(payload)
                current = None
            payload.update(status="succeeded", stage="succeeded", current_file=None, completed_at=utc_now())
            self._save_internal(payload)
        except Exception as exc:
            message = str(exc) if isinstance(exc, (UploadError, InternalUploadConflict)) else "云道S3上传异常，请联系维护人员后重试"
            # Preserve confirmed receipts even if updating the summary failed.
            if current is not None and current["status"] != "succeeded":
                current.update(status="failed", error=message)
            payload.update(status="failed", stage="failed", error=message, completed_at=utc_now())
            self._save_internal(payload)
