"""Background transport of frozen release workbooks and complete source folders."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import time

from ..identifiers import allocate_id
from ..file_io import io_path
from ..trajectory_correction.draft_store import utc_now
from .internal_jobs import InternalUploadConflict
from .internal_uploader import UploadError, UploadResult


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")).encode("utf-8")).hexdigest()


def normalize_prefix(value):
    value = str(value).strip().replace("\\", "/")
    if "://" in value or any(ord(char) < 32 for char in value):
        raise ValueError("云道目标请填写 bucket 内的目录前缀，不填写 URL 或控制字符")
    parts = [part for part in value.split("/") if part]
    if any(part in {".", ".."} for part in parts):
        raise ValueError("云道目录前缀不能包含 . 或 .. 路径段")
    return "/".join(parts)


class DirectoryUploadJobsMixin:
    def _save_directory(self, payload):
        """Receipts change per directory, not once per speed output line."""
        with self._lock:
            items = payload.get("item_results", [])
            payload["completed_items"] = sum(item["status"] == "succeeded" for item in items)
            payload["total_items"] = len(items)
            payload["percent"] = (round(100 * payload["completed_items"] / len(items)) if items else 0)
            if not payload.get("include_trajectories", True):
                # Old Excel-only clients still receive their familiar receipt/count shape.
                payload["file_results"] = [{**item, "path": self.registry.project_path(Path(item["file_path"])),
                                            "size_bytes": item.get("size_bytes", 0), "remote_id": None, "url": None}
                                           for item in items]
                payload["total_files"] = len(items)
                payload["completed_files"] = payload["completed_items"]
                payload["total_bytes"] = sum(item.get("size_bytes", 0) for item in items)
                payload["completed_bytes"] = sum(item.get("size_bytes", 0) for item in items
                                                 if item["status"] == "succeeded")
            payload["updated_at"] = utc_now()
            self._write(payload)
            keys = ("job_id", "attempt", "status", "stage", "created_at", "updated_at", "completed_at",
                    "error", "folder_upload", "destination", "source_summary", "item_results",
                    "current_item", "completed_items", "total_items", "percent", "include_trajectories",
                    "file_results", "completed_files", "total_files", "completed_bytes", "total_bytes")
            self.registry.update(payload["release_id"], {
                "internal_upload": deepcopy({key: payload.get(key) for key in keys})})

    def _probe_directory_process(self, payload):
        record = payload.get("process")
        if not record:
            return "dead"
        try:
            return self.internal_adapter.probe_process(record)
        except Exception:
            return "unknown"

    def _recover_directory_job(self, payload):
        if payload.get("status") not in {"queued", "running", "uploading"}:
            return
        if self._probe_directory_process(payload) != "dead":
            payload.update(status="uploading", stage="recovering", completed_at=None,
                           error="后端已重启，正在核对原上传进程；不会重复启动上传")
        else:
            self._interrupt_directory(payload)
        self._save_directory(payload)

    @staticmethod
    def _interrupt_directory(payload):
        for item in payload.get("item_results", []):
            if item["status"] == "uploading":
                item.update(status="interrupted", error="原上传进程已退出，未收到完成确认")
        payload.update(status="interrupted", stage="interrupted", completed_at=utc_now(), process=None,
                       error="上传已中断，重试只传未确认完成的目录")

    def _read_directory_job(self, payload):
        if payload.get("stage") == "recovering" and self._probe_directory_process(payload) == "dead":
            self._interrupt_directory(payload)
            self._save_directory(payload)
        progress = self._records.get("dataset_upload_progress", payload["job_id"])
        if progress:
            payload["progress"] = progress.get("progress", {})
            payload["recent_output"] = progress.get("recent_output", [])
        return payload

    def _submit_directory(self, release_id, *, destination_prefix=None, retry_job_id=None,
                          include_trajectories=True):
        with self._lock:
            release = self.registry.get(release_id)
            if release is None:
                raise FileNotFoundError("数据集发布记录不存在")
            capability = self.internal_capabilities()["internal"]
            if not capability.get("configured") or not capability.get("directory_upload"):
                raise InternalUploadConflict(capability.get("reason") or "当前上传适配器不支持完整目录上传")
            destination = {"region": capability["region"], "bucket": capability["bucket"],
                           "prefix": normalize_prefix(destination_prefix if destination_prefix is not None
                                                      else capability.get("default_prefix") or "")}
            if retry_job_id:
                retry_job = self.get(retry_job_id)
                if (not retry_job or retry_job.get("release_id") != release_id
                        or not retry_job.get("folder_upload")
                        or retry_job.get("include_trajectories", True) != include_trajectories):
                    raise InternalUploadConflict("重试任务不属于该发布的目录上传")
                previous_target = retry_job["destination"]
                if destination_prefix is None:
                    destination["prefix"] = previous_target["prefix"]
                if destination != previous_target:
                    raise InternalUploadConflict("原上传目标与当前云道配置不同，请恢复原配置后重试；更换目录可创建新任务")
            # Only immutable release identity participates; current UI selections never do.
            excel_paths = [{key: deepcopy(value) for key, value in item.items() if key != "available"}
                           for item in release.get("excel_paths", [])]
            source_key = _digest({"release_id": release_id, "excel_paths": excel_paths,
                                  "source_refs": release.get("source_refs"), "include_trajectories": include_trajectories})
            request_key = _digest({"source": source_key, "destination": destination})
            index = self._records.get("dataset_upload_requests", request_key)
            previous = self.get(index["job_id"]) if index else None
            if previous:
                if previous["status"] in {"queued", "uploading", "succeeded"}:
                    return previous
                if self._probe_directory_process(previous) != "dead":
                    previous.update(status="uploading", stage="recovering", completed_at=None,
                                    error="原上传进程尚未确认退出，正在对账，不重复启动")
                    self._save_directory(previous)
                    return previous
            items = deepcopy((previous or {}).get("item_results", []))
            for item in items:
                if item["status"] != "succeeded":
                    item.update(status="pending", error=None)
            payload = {
                "job_id": allocate_id("j", self.registry.data_root), "release_id": release_id,
                "dataset_name": release["name"], "mode": "internal", "folder_upload": True,
                "include_trajectories": include_trajectories,
                "attempt": int((previous or {}).get("attempt", 0)) + 1,
                "status": "queued", "stage": "queued", "created_at": utc_now(),
                "started_at": None, "completed_at": None, "updated_at": utc_now(), "error": None,
                "destination": destination, "source_key": source_key, "request_key": request_key,
                "release_snapshot": {key: deepcopy(value) for key, value in release.items()
                                     if key in {"release_id", "name", "excel_paths", "batch_ids", "source_refs", "source_kind", "provenance"}},
                "item_results": items, "items_resolved": bool((previous or {}).get("items_resolved")),
                "source_summary": deepcopy((previous or {}).get("source_summary", {})),
                "current_item": None, "completed_items": 0, "total_items": len(items),
                "percent": 0, "process": None,
                # Legacy consumers must not mistake directories for byte-counted Excel files.
                "file_results": [], "total_files": 0, "completed_files": 0,
                "total_bytes": 0, "completed_bytes": 0,
            }
            payload["release_snapshot"]["excel_paths"] = excel_paths
            # Job and dedup index are registered together before any child can start.
            try:
                self._records.put_many([
                    {"namespace": "dataset_upload_jobs", "key": payload["job_id"], "payload": payload,
                     "expected_revision": 0},
                    {"namespace": "dataset_upload_requests", "key": request_key,
                     "payload": {"job_id": payload["job_id"]},
                     "expected_revision": (index or {}).get("storage_revision", 0)},
                ])
            except Exception:
                winner = self._records.get("dataset_upload_requests", request_key)
                if winner and winner.get("job_id") != (index or {}).get("job_id"):
                    return self.get(winner["job_id"])
                raise
            self._save_directory(payload)
            submitted = deepcopy(payload)
            try:
                self._executor.submit(self._run_directory, payload["job_id"])
            except Exception as exc:
                payload.update(status="failed", stage="failed", completed_at=utc_now(),
                               error="上传任务无法启动，请重试")
                self._save_directory(payload)
                raise InternalUploadConflict(payload["error"]) from exc
            return submitted

    def _run_directory(self, job_id):
        from .internal_uploader import DirectoryUploadContext
        from .upload_sources import resolve_upload_sources
        payload = self.get(job_id)
        if not payload or payload["status"] != "queued":
            return
        current = None
        recent = []
        latest_progress = {}
        last_progress_write = 0.0

        def save_progress(value, *, force=False):
            nonlocal last_progress_write, latest_progress
            # Adapter messages are already redacted; it never supplies argv or secrets.
            latest_progress = {key: value[key] for key in
                               ("message", "speed_text", "transferred_bytes", "total_bytes", "percent")
                               if key in value}
            message = latest_progress.get("message")
            if message and (not recent or recent[-1] != message):
                recent.append(str(message)[-1200:])
                del recent[:-20]
            if force or time.monotonic() - last_progress_write >= 1:
                self._records.put("dataset_upload_progress", job_id,
                                  {"progress": latest_progress, "recent_output": recent, "updated_at": utc_now()})
                last_progress_write = time.monotonic()

        def save_process(record):
            payload["process"] = record
            # Adapter waits for this durable identity before allowing the script to execute.
            self._write(payload)

        try:
            payload.update(status="uploading", stage="resolving", started_at=utc_now())
            self._save_directory(payload)
            if not payload["items_resolved"]:
                sources = resolve_upload_sources(self.registry, payload["release_snapshot"],
                                                 include_trajectories=payload.get("include_trajectories", True))
                items = []
                prefix = payload["destination"]["prefix"]
                for source in sources:
                    relative = str(source["destination_relative"]).strip("/")
                    target = "/".join(part for part in (prefix, relative) if part) + "/"
                    key = _digest({"release": payload["source_key"], "destination": payload["destination"],
                                   "source": source["source_identity"], "target": target})
                    items.append({**source, "bucket_path": target, "idempotency_key": key,
                                  "status": "pending", "error": None})
                if not items:
                    raise ValueError("发布记录没有可上传内容")
                payload.update(items_resolved=True, item_results=items, source_summary={
                    "excel_count": sum(item["kind"] == "excel" for item in items),
                    "trajectory_count": sum(item["kind"] == "trajectory" for item in items),
                    "case_count": len({(item["batch_id"], item["case_id"]) for item in items
                                       if item["kind"] == "trajectory"}),
                })
                self._save_directory(payload)
            for item in payload["item_results"]:
                if item["status"] == "succeeded":
                    continue
                current = item
                source_dir = Path(item["source_dir"])
                if not io_path(source_dir).is_dir():
                    raise FileNotFoundError(f"上传目录不存在或无法读取：{source_dir}")
                if item["kind"] == "excel":
                    self._validate_excel({"path": self.registry.project_path(Path(item["file_path"])),
                                          "filename": item["filename"], "sha256": item["sha256"]})
                item.update(status="uploading", error=None)
                payload.update(stage="uploading", current_item=item.get("label") or item["filename"], process=None)
                self._save_directory(payload)
                save_progress({}, force=True)
                result = self.internal_adapter.upload_directory(DirectoryUploadContext(
                    source_dir=source_dir, bucket_path=item["bucket_path"],
                    region=payload["destination"]["region"], bucket=payload["destination"]["bucket"],
                    release_id=payload["release_id"], idempotency_key=item["idempotency_key"]),
                    on_progress=save_progress, on_process=save_process)
                if not isinstance(result, UploadResult) or result.success is not True:
                    raise UploadError("上传脚本未明确确认目录传输完成")
                payload["process"] = None
                item.update(status="succeeded", error=None)
                save_progress(latest_progress, force=True)
                self._save_directory(payload)
                current = None
            payload.update(status="succeeded", stage="succeeded", current_item=None, completed_at=utc_now())
            save_progress({}, force=True)
            self._save_directory(payload)
        except Exception as exc:
            if isinstance(exc, (UploadError, InternalUploadConflict, FileNotFoundError, ValueError)):
                message = str(exc)
            else:
                message = "上传失败，请检查平台存储及上传脚本后重试"
            sanitizer = getattr(self.internal_adapter, "redact", None)
            if callable(sanitizer):
                message = sanitizer(message)
            if current is not None and current["status"] != "succeeded":
                current.update(status="failed", error=message)
            payload.update(status="failed", stage="failed", error=message, completed_at=utc_now())
            try:
                save_progress(latest_progress, force=True)
            except Exception:
                pass  # Preserve the original transport/storage failure.
            self._save_directory(payload)
