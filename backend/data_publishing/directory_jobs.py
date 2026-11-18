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
from .internal_uploader import UploadError, UploadResult, normalize_prefix


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")).encode("utf-8")).hexdigest()


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
            keys += ("training_jsonl", "training", "training_only", "training_destination_prefix")
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
                          include_trajectories=True, training_jsonl=False, training_only=False,
                          training_destination_prefix=None):
        with self._lock:
            release = self.registry.get(release_id)
            if release is None:
                raise FileNotFoundError("数据集发布记录不存在")
            if training_only and not release.get("training_files"):
                raise InternalUploadConflict("此历史发布尚无配套 JSONL，请先生成配套训练文件；原表格与图片上传仍可使用")
            capability = self.internal_capabilities()["internal"]
            if not capability.get("configured") or capability.get("directory_upload") is not True:
                raise InternalUploadConflict(capability.get("reason") or "当前上传适配器不支持完整目录上传")
            destination = {"region": capability["region"], "bucket": capability["bucket"],
                           "prefix": normalize_prefix(destination_prefix if destination_prefix is not None
                                                      else capability.get("default_prefix") or "")}
            training_prefix = (normalize_prefix(training_destination_prefix)
                               if training_destination_prefix is not None else None)
            if retry_job_id:
                retry_job = self.get(retry_job_id)
                if (not retry_job or retry_job.get("release_id") != release_id
                        or not retry_job.get("folder_upload")
                        or retry_job.get("include_trajectories", True) != include_trajectories
                        or retry_job.get("training_jsonl", False) != training_jsonl
                        or retry_job.get("training_only", False) != training_only):
                    raise InternalUploadConflict("重试任务不属于该发布的目录上传")
                previous_target = retry_job["destination"]
                if destination_prefix is None:
                    destination["prefix"] = previous_target["prefix"]
                if destination != previous_target:
                    raise InternalUploadConflict("原上传目标与当前云道配置不同，请恢复原配置后重试；更换目录可创建新任务")
                if training_destination_prefix is None:
                    training_prefix = retry_job.get("training_destination_prefix")
                if training_prefix != retry_job.get("training_destination_prefix"):
                    raise InternalUploadConflict("训练文件上传目录已改变，请创建新任务")
            elif training_jsonl and training_prefix is None:
                # Only new jobs adopt the configured default. For an explicit
                # retry, None still denotes the old implicit destination.
                if not capability.get("training_default_prefix"):
                    raise InternalUploadConflict(capability.get("training_default_prefix_error")
                                                 or "云道 JSONL 默认目录不可用，请填写上传目录")
                training_prefix = f"{capability['training_default_prefix']}/{release_id}"
            # Only immutable release identity participates; current UI selections never do.
            excel_paths = [{key: deepcopy(value) for key, value in item.items() if key != "available"}
                           for item in release.get("excel_paths", [])]
            source_key = _digest({"release_id": release_id, "excel_paths": excel_paths,
                                  "source_refs": release.get("source_refs"), "include_trajectories": include_trajectories})
            if training_jsonl:
                source_key = _digest({"source": source_key, "training_jsonl": "training_jsonl_v1"})
            if release.get("training_files") and training_jsonl:
                source_key = _digest({"source": source_key, "training_files": release["training_files"]})
            if training_only or training_prefix is not None:
                source_key = _digest({"source": source_key, "training_only": training_only,
                                      "training_destination_prefix": training_prefix})
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
                "training_jsonl": bool(training_jsonl),
                "training_only": bool(training_only), "training_destination_prefix": training_prefix,
                "training": deepcopy((previous or {}).get("training")),
                "attempt": int((previous or {}).get("attempt", 0)) + 1,
                "status": "queued", "stage": "queued", "created_at": utc_now(),
                "started_at": None, "completed_at": None, "updated_at": utc_now(), "error": None,
                "destination": destination, "source_key": source_key, "request_key": request_key,
                "release_snapshot": {key: deepcopy(value) for key, value in release.items()
                                     if key in {"release_id", "name", "excel_paths", "batch_ids", "source_refs", "source_kind", "provenance", "training_files"}},
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
                sources = ([] if payload.get("training_only") else
                           resolve_upload_sources(self.registry, payload["release_snapshot"],
                                                  include_trajectories=payload.get("include_trajectories", True)))
                items = []
                prefix = payload["destination"]["prefix"]
                for source in sources:
                    relative = str(source["destination_relative"]).strip("/")
                    if source.get("frozen_bucket") and source["frozen_bucket"] != payload["destination"]["bucket"]:
                        raise ValueError("图片目标 bucket 与导出 JSONL 不一致，请恢复原图片 bucket；仅上传 JSONL 可使用独立目标")
                    target = "/".join(part for part in (source.get("frozen_prefix", prefix), relative) if part) + "/"
                    key = _digest({"release": payload["source_key"], "destination": payload["destination"],
                                   "source": source["source_identity"], "target": target})
                    items.append({**source, "bucket_path": target, "idempotency_key": key,
                                  "status": "pending", "error": None})
                if not items and not payload.get("training_only"):
                    raise ValueError("发布记录没有可上传内容")
                payload.update(items_resolved=True, item_results=items, source_summary={
                    "excel_count": sum(item["kind"] == "excel" for item in items),
                    "trajectory_count": sum(item["kind"] == "trajectory" for item in items),
                    "case_count": len({(item["batch_id"], item["case_id"]) for item in items
                                       if item["kind"] == "trajectory"}),
                })
                self._save_directory(payload)
            def upload_item(item):
                nonlocal current
                if item["status"] == "succeeded":
                    return
                current = item
                source_dir = Path(item["source_dir"])
                if not io_path(source_dir).is_dir():
                    raise FileNotFoundError(f"上传目录不存在或无法读取：{source_dir}")
                if item["kind"] == "excel":
                    self._validate_excel({"path": self.registry.project_path(Path(item["file_path"])),
                                          "filename": item["filename"], "sha256": item["sha256"]})
                elif item["kind"] == "training":
                    from .training_jsonl import file_digest
                    if file_digest(Path(item["file_path"])) != item["sha256"]:
                        raise ValueError("训练 JSONL 与冻结生成结果摘要不一致")
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
            for item in payload["item_results"]:
                if item["kind"] != "training":
                    upload_item(item)
            if payload.get("training_jsonl"):
                from .training_jsonl import write_training_bundle
                paired = payload["release_snapshot"].get("training_files")
                if paired:
                    self._append_paired_training(payload)
                    self._save_directory(payload)
                elif not (payload.get("training") or {}).get("file_path"):
                    payload.update(stage="generating_training", current_item="生成训练 JSONL")
                    self._save_directory(payload)
                    payload["training"] = write_training_bundle(self.registry, payload["release_snapshot"], payload["item_results"], payload["destination"], payload["request_key"])
                    self._save_directory(payload)
                training = payload["training"]
                if not paired and not any(item["kind"] == "training" for item in payload["item_results"]):
                    if payload.get("training_destination_prefix") is not None:
                        prefix = payload["training_destination_prefix"]
                        training["bucket_path"] = prefix + "/" if prefix else ""
                    payload["item_results"].append({"kind": "training", "source_dir": training["source_dir"],
                        "file_path": str(self.registry.resolve_project_path(training["file_path"])),
                        "filename": "train.jsonl", "label": "训练 JSONL", "sha256": training["sha256"],
                        "bucket_path": training["bucket_path"], "source_identity": payload["request_key"],
                        "idempotency_key": _digest([payload["request_key"], training["sha256"]]),
                        "status": "pending", "error": None})
                    self._save_directory(payload)
                if not training.get("included_count", 0):
                    raise ValueError("无可用训练样本；表格与原始轨迹上传结果已保留，请查看排除报告")
                for item in payload["item_results"]:
                    if item["kind"] == "training":
                        training["status"] = "uploading"
                        training["error"] = None
                        upload_item(item)
                        training["status"] = "succeeded"
                        training["s3_uri"] = f"s3://{payload['destination']['bucket']}/{item['bucket_path']}train.jsonl"
                if training.get("excluded_files"):
                    raise ValueError("部分配套 JSONL 未上传；其余文件已保留成功回执：" + "; ".join(item["error"] for item in training["excluded_files"][:3]))
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
            if payload.get("training_jsonl") and all(item["status"] == "succeeded" for item in payload.get("item_results", []) if item["kind"] != "training"):
                payload["training"] = {**(payload.get("training") or {}), "status": "failed", "error": message}
            payload.update(status="failed", stage="failed", error=message, completed_at=utc_now())
            try:
                save_progress(latest_progress, force=True)
            except Exception:
                pass  # Preserve the original transport/storage failure.
            self._save_directory(payload)

    def _append_paired_training(self, payload):
        """Upload already generated bytes; never inspect images or regenerate samples."""
        from .training_jsonl import file_digest
        release = payload["release_snapshot"]
        paired = release["training_files"]
        prefix = payload.get("training_destination_prefix")
        batches = list(dict.fromkeys(release.get("batch_ids", [])))
        if prefix is None:
            cloud_root = batches[0] if len(batches) == 1 else release["release_id"]
            prefix = "/".join(part for part in (payload["destination"]["prefix"], cloud_root, "训练数据") if part)
        training = {"version": 2, "status": "ready", "included_count": 0, "excluded_count": 0,
                    "image_prefixes": [], "files": [], "excluded_files": []}
        additions = []
        old_items = {item.get("idempotency_key"): item for item in payload["item_results"] if item["kind"] == "training"}
        for entry in paired:
            try:
                if entry.get("status") != "ready" or not entry.get("path") or not entry.get("included_count"):
                    raise ValueError(entry.get("error") or "配套 JSONL 尚无可用样本，请在纠偏导出中生成后重新发布")
                target = prefix.strip("/")
                if len(paired) > 1:
                    target = "/".join(part for part in (target, f"{entry['index'] + 1:03d}") if part)
                target = target + "/" if target else ""
                item_key = _digest([payload["request_key"], entry["index"], entry["sha256"], target])
                prior = old_items.get(item_key, {})
                if prior.get("status") == "succeeded":
                    # Already acknowledged bytes do not depend on a local file
                    # remaining available during another item's retry.
                    additions.append(deepcopy(prior))
                else:
                    path = self.registry.resolve_project_path(entry["path"])
                    expected = self.registry.data_root / "releases" / release["release_id"] / "training"
                    if not path.resolve().is_relative_to(expected.resolve()) or path.name != "train.jsonl":
                        raise ValueError("配套训练文件路径不属于本次发布")
                    if file_digest(path) != entry["sha256"]:
                        raise ValueError("配套训练文件与发布摘要不一致")
                    # The private script uploads a directory; never send a
                    # report or an unregistered file alongside the JSONL.
                    if {item.name for item in io_path(path.parent).iterdir()} != {path.name}:
                        raise ValueError("训练上传目录包含未登记文件，请检查发布目录")
                    additions.append({"kind": "training", "source_dir": str(path.parent),
                        "file_path": str(path), "filename": path.name, "label": f"训练 JSONL {entry['index'] + 1}",
                        "index": entry["index"], "sha256": entry["sha256"], "size_bytes": io_path(path).stat().st_size,
                        "bucket_path": target, "source_identity": entry["sha256"],
                        "idempotency_key": item_key, "status": "pending", "error": None})
                training["included_count"] += int(entry.get("included_count", 0))
                training["excluded_count"] += int(entry.get("excluded_count", 0))
                training["files"].append(deepcopy(entry))
                if entry.get("image_prefix") not in training["image_prefixes"]:
                    training["image_prefixes"].append(entry.get("image_prefix"))
            except (OSError, ValueError, KeyError) as exc:
                training["excluded_files"].append({"index": entry.get("index"), "error": str(exc)})
        if not training["files"]:
            payload["training"] = training
            raise ValueError("无可上传的配套 JSONL：" + "; ".join(item["error"] for item in training["excluded_files"][:3]))
        first = training["files"][0]
        training.update(file_path=first["path"], report_path=first.get("report_path"), sha256=first["sha256"],
                        report_sha256=first.get("report_sha256"))
        payload["item_results"] = [item for item in payload["item_results"] if item["kind"] != "training"] + additions
        payload["training"] = training

    def training_file(self, job_id, kind):
        payload = self.get(job_id)
        training = (payload or {}).get("training") or {}
        field, digest_field = ("file_path", "sha256") if kind == "jsonl" else ("report_path", "report_sha256")
        if kind not in {"jsonl", "report"} or not training.get(field):
            raise FileNotFoundError("训练导出文件尚未生成")
        path = self.registry.resolve_project_path(training[field])
        expected = (self.registry.data_root / "releases" / payload["release_id"] / "training").resolve()
        if not path.resolve().is_relative_to(expected):
            raise ValueError("训练导出路径不属于本次发布")
        from .training_jsonl import file_digest
        if file_digest(path) != training[digest_field]:
            raise ValueError("训练导出文件摘要校验失败")
        return path
