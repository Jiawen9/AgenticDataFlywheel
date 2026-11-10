"""Platform orchestration; device execution belongs to the independent collector."""
from __future__ import annotations
import hashlib
import io
import json
import logging
import math
import tempfile
import threading
import sqlite3
import time
import uuid
import mimetypes
import zipfile
from pathlib import Path
from urllib.parse import urlsplit
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask
from .batch_lifecycle import BatchPublishedError, ensure_batch_active, is_batch_active, lifecycle
from .batch_operations import active_batch_lock
from .collection_runs import _identifier, matches_legacy_logprobs_request
from .data_store import RevisionConflict
from .data_store.locking import batch_lock
from .data_store.registry import utc_now
from .manual_collection import ManualCollectionStore
from . import model_iteration
from .phone_factory import DEFAULT_CONFIG, PhoneFactoryError, _atomic_write, _filename, _text
from .remote_sync import BoundedSyncPool, RemoteSyncPolicy, SyncDeferred
from .task_generation.collection_batches import payload_digest
from .agent_types import normalize_agent_type, engine_capabilities, PROVENANCE_FIELDS
from .pipeline_model_config import MODEL_OVERRIDE_FIELDS, normalize_overrides
from .collection_protocol import repeat_times, COLLECTION_ARTIFACTS_VERSION, SUPPORTED_ARTIFACT_VERSIONS
from .collection_control import merge_control, can_cancel

TERMINAL = {"completed", "succeeded", "partial", "failed", "interrupted", "cancelled"}

class FactoryRuntime:
    def __init__(self, store):
        self.store, self.root, self.records = store, store.root, store.records
        self.manual = ManualCollectionStore(self.root)
        self._transfer = None
        self._stop = threading.Event()
        self._thread = None
        self._protocol_cache = None
        self._protocol_lock = threading.Lock()
        self.sync_policy = RemoteSyncPolicy(self.records)
        self._evaluation_pool = BoundedSyncPool("model-iteration-sync", max_workers=2)
        self._archive_pool = BoundedSyncPool("collection-archive-status", max_workers=2)
        self._preview_pool = BoundedSyncPool("evaluation-previews", max_workers=2)
        self._control_pool = BoundedSyncPool("collection-control", max_workers=4)
        self._control_memory = {}
        self._resume_service = None
        from .collector_storage import StorageService
        self.storage_service = StorageService(self)

    @property
    def resume_service(self):
        if self._resume_service is None:
            from .collection_resume import ResumeService
            self._resume_service = ResumeService(self)
        return self._resume_service

    @property
    def transfer(self):
        if self._transfer is None:
            from .collection_transfer import CollectionTransferManager
            self._transfer = CollectionTransferManager(self.root, self.store.collection_runs, self,
                                                       sync_policy=self.sync_policy)
        return self._transfer

    def get_run(self, run_id):
        from .collector_storage import require_available
        require_available(self.records, run_id)
        run = self.records.get("model_iter_runs", run_id)
        if run and run.get("request", {}).get("collector_server"):
            from .evaluation_job_previews import remote
            return remote(self.store, ["run", run_id], run["request"]["collector_server"])
        return self.store._remote_result(["run", run_id])

    def _can_reject_dispatch(self, run, mode):
        """Missing historical evidence is uncertainty, never proof of non-acceptance."""
        if run.get("dispatch_attempted") is not False and run.get("dispatch_rejected") is not True:
            return False
        run_id = run.get("collection_run_id") or run["run_id"]
        related = [run]
        if mode == "modeliter":
            related.append(self.store.collection_runs.get(run_id) or {})
        for item in related:
            if (item.get("dispatch_response") or item.get("dispatched_at") or item.get("remote_status")
                    or item.get("trajectories") or item.get("transfer_status") == "completed"):
                return False
        for kind in (("collection", "evaluation") if mode == "modeliter" else ("collection",)):
            sync = self.sync_policy.view(kind, run_id)
            if sync.get("remote_status") or sync.get("last_success_at"):
                return False
        return True

    def _begin_dispatch(self, run_id, mode, incremental):
        def begin(item):
            if item.get("dispatch_rejected") is True:
                item.update(transfer_error=None, transfer_status="waiting")
            item.update(dispatch_attempted=True, submitted_at=utc_now(), dispatch_rejected=False)
            item.pop("dispatch_rejection", None)
        namespace = "collection_runs" if mode == "generate" else "model_iter_runs"
        self.records.update(namespace, run_id, begin)
        if mode == "modeliter" and incremental:
            self.records.update("collection_runs", run_id, begin)
            self.sync_policy.reset("collection", run_id)
        self.sync_policy.reset("collection" if mode == "generate" else "evaluation", run_id)

    def download_archive(self, run_id, destination):
        self.store._remote_result(["run-archive", run_id, "--out", str(destination)])

    def get_collection_artifacts(self, run_id, cursor=0, limit=100):
        from .collector_storage import require_available
        require_available(self.records, run_id)
        return self.store._remote_result(["collection-artifacts", run_id, "--cursor", str(cursor), "--limit", str(limit)])

    def get_collection_artifact(self, run_id, artifact_id):
        from .collector_storage import require_available
        require_available(self.records, run_id)
        return self.store._remote_result(["collection-artifact", run_id, artifact_id])

    def download_collection_artifact(self, run_id, artifact_id, destination):
        from .collector_storage import require_available
        require_available(self.records, run_id)
        return self.store._remote_result(["collection-artifact-archive", run_id, artifact_id, "--out", str(destination)])

    def set_receipt_storage(self, run_id, paused):
        from .collector_storage import require_available
        require_available(self.records, run_id)
        return self.store._remote_result(["receipt-storage", run_id, "--payload", json.dumps({
            "paused": bool(paused), "reason": "platform_storage_unavailable" if paused else "",
            "revision": time.time_ns()})])

    def get_collection_manifest(self, run_id):
        from .collector_storage import require_available
        require_available(self.records, run_id)
        return self.store._remote_result(["collection-manifest", run_id])

    def download_collection_archive(self, run_id, destination):
        return self.store._remote_result(["collection-archive", run_id, "--out", str(destination)])

    def start(self):
        self.transfer.start()
        if self._thread is None or not self._thread.is_alive():
            self._stop.clear()
            self._thread = threading.Thread(target=self._poll_evaluations, daemon=True, name="model-iteration-sync")
            self._thread.start()

    def close(self):
        self._stop.set()
        self.storage_service.close()
        if self._resume_service is not None:
            self._resume_service.close()
        if self._transfer is not None:
            self._transfer.close()
        if self._thread:
            self._thread.join(timeout=2)
        self._evaluation_pool.close()
        self._archive_pool.close()
        self._preview_pool.close()
        self._control_pool.close()

    def _poll_evaluations(self):
        while not self._stop.wait(5):
            for schedule in (self.storage_service.schedule,self._schedule_controls,self._schedule_archives,self._schedule_evaluations,
                             self._schedule_previews, lambda:self.resume_service.schedule()):
                try:
                    schedule()
                except Exception:
                    logging.getLogger(__name__).exception("后台同步调度失败")

    def _schedule_previews(self):
        from .offline_evaluation_dispatch import schedule_previews
        schedule_previews(self)

    def _schedule_archives(self):
        from .collector_storage import unavailable
        for run in self.store.collection_runs.list_runs():
            if unavailable(self.records, run["collection_run_id"]):
                continue
            if (run.get("receipt_closed") and run.get("archive_status") in {"pending", "building"}
                    and is_batch_active(run["batch_id"], self.root)
                    and time.time() >= run.get("archive_checked_epoch", 0) + 30):
                run_id = run["collection_run_id"]
                self._archive_pool.submit(run_id, lambda run_id=run_id: self._refresh_archive(run_id))

    def _refresh_archive(self, run_id):
        run = self.store.collection_runs.get(run_id)
        metadata = run.get("request", {}).get("metadata", {})
        try:
            remote = self.get_run(run_id)
            if (remote.get("collection_run_id", remote.get("run_id")) != run_id
                    or remote.get("batch_id") != run["batch_id"]
                    or remote.get("agent_type", "newagent") != metadata.get("agent_type", "newagent")
                    or remote.get("archive_status") not in {"not_requested", "pending", "building", "ready", "failed"}):
                raise PhoneFactoryError("采集归档状态身份或格式不匹配", 502)
            values = {"archive_status": remote["archive_status"], "archive_error": None}
        except Exception as exc:
            values = {"archive_error": str(exc)[:500]}
        try:
            with active_batch_lock(run["batch_id"], self.root):
                self.records.update("collection_runs", run_id, lambda row: row.update(
                    **values, archive_checked_epoch=time.time()))
        except BatchPublishedError:
            pass  # Publishing freezes public batch metadata too.

    def _schedule_evaluations(self):
        from .collector_storage import unavailable
        candidates = []
        for run in self.records.list("model_iter_runs"):
            if unavailable(self.records, run["run_id"]):
                continue
            if run.get("request", {}).get("offline_evaluation_jobs_version") == 1:
                if not run.get("dispatch_confirmed"):
                    if (not run.get("dispatch_rejected") and not run.get("evaluation_control_terminal")
                            and time.time() >= run.get("dispatch_checked_epoch", 0) + 5):
                        from .offline_evaluation_dispatch import schedule
                        schedule(self, run["run_id"])
                    continue
            snapshot = self.records.get("model_iter_results", run["run_id"]) or {}
            if snapshot.get("final"):
                self._restore_final_evaluation(run, snapshot)
                continue
            needs_snapshot = run.get("request", {}).get("evaluator_config") and not snapshot.get("final")
            if run["status"] in TERMINAL and not needs_snapshot and not (run["status"] == "failed" and run.get("dispatch_error")):
                continue
            request = run.get("request", {})
            if (request.get("result_transport") or request.get("metadata", {}).get("result_transport")) == "legacy":
                continue
            if self.sync_policy.due("evaluation", run):
                candidates.append(run)
        candidates.sort(key=lambda run: self.sync_policy.priority("evaluation", run))
        for run in candidates:
            if self._stop.is_set():
                break
            run_id = run["run_id"]
            self._evaluation_pool.submit(run_id, lambda run_id=run_id: self._background_evaluation(run_id))

    def _background_evaluation(self, run_id):
        try:
            self.sync_evaluation(run_id, force=False)
        except Exception:
            # The common policy persists and rate-limits diagnostics for both chains.
            pass

    def _restore_final_evaluation(self, run, snapshot):
        """Recover a crash between saving the validated result and the run status."""
        run_id = run["run_id"]
        remote_status = self.sync_policy.view("evaluation", run_id).get("remote_status")
        if remote_status in TERMINAL and (run["status"] not in TERMINAL
                or run["status"] == "failed" and run.get("dispatch_error")):
            def restore(current):
                if current["status"] not in TERMINAL or current["status"] == "failed" and current.get("dispatch_error"):
                    current.update(status=remote_status, remote_status=remote_status, dispatch_error=None,
                                   evaluation_summary=snapshot["result"]["summary"], updated_at=utc_now())
            run = self.records.update("model_iter_runs", run_id, restore)
        self.sync_policy.complete("evaluation", run_id)
        return run

    def sync_evaluation(self, run_id, *, force=True, fresh=False):
        from .collector_storage import require_available
        require_available(self.records, run_id)
        run = self.records.get("model_iter_runs", _identifier(run_id, "评估运行编号"))
        if run is None:
            raise PhoneFactoryError("评估运行不存在", 404)
        snapshot = self.records.get("model_iter_results", run_id) or {}
        if snapshot.get("final"):
            return self._restore_final_evaluation(run, snapshot)
        try:
            result = self.sync_policy.execute("evaluation", run,
                lambda: self._sync_evaluation(run_id), force=force, coalesce=not fresh)
        except SyncDeferred as exc:
            raise PhoneFactoryError(str(exc), exc.status) from exc
        snapshot = self.records.get("model_iter_results", run_id) or {}
        if snapshot.get("final") or (not run.get("request", {}).get("evaluator_config")
                                      and result["status"] in TERMINAL and not result.get("dispatch_error")) or (
                run.get("request", {}).get("offline_evaluation_jobs_version") == 1
                and result["status"] in TERMINAL
                and result.get("preparation", {}).get("status") in {"failed", "cancelled"}):
            self.sync_policy.complete("evaluation", run_id)
        return result

    def _sync_evaluation(self, run_id):
        from .collector_storage import require_available
        require_available(self.records, run_id)
        run = self.records.get("model_iter_runs", run_id)
        remote = self.get_run(run_id)
        require_available(self.records, run_id)
        if not isinstance(remote, dict):
            raise PhoneFactoryError("远端评估运行响应格式无效", 502, remote_kind="protocol", remote_command="run")
        if remote.get("run_id", remote.get("collection_run_id")) != run_id or remote.get("run_mode") != "modeliter":
            raise PhoneFactoryError("远端评估运行身份不匹配", 502, remote_kind="identity", remote_command="run")
        if (run.get("request", {}).get("execution_mode") == "evaluate_only"
                and remote.get("execution_mode") != "evaluate_only"):
            raise PhoneFactoryError("远端运行不是独立评测任务", 502, remote_kind="identity", remote_command="run")
        if (run.get("request", {}).get("execution_mode") != "evaluate_only"
                and remote.get("agent_type", "newagent") != run.get("request", {}).get("agent_type", "newagent")):
            raise PhoneFactoryError("远端评估运行采集引擎不匹配", 502, remote_kind="identity", remote_command="run")
        if remote.get("status") not in TERMINAL | {"preparing", "queued", "starting", "running", "stopping", "needs_attention"}:
            raise PhoneFactoryError("远端评估运行状态无效", 502, remote_kind="protocol", remote_command="run")
        remote_revision = remote.get("state_revision")
        if type(remote_revision) is int and remote_revision < run.get("evaluation_state_revision", -1):
            return run
        if ("preparation" in remote and remote["preparation"].get("revision", 0)
                < run.get("preparation", {}).get("revision", 0)):
            return run
        self.sync_policy.observe_remote("evaluation", run_id, remote["status"])
        result = None
        preparation_pending = (run.get("request", {}).get("execution_mode") == "evaluate_only"
                               and ("preparation" in remote or run.get("request", {}).get("offline_evaluation_jobs_version") == 1)
                               and remote.get("preparation", {}).get("status") not in {"completed", "ready"})
        if run.get("request", {}).get("evaluator_config") and remote["status"] != "preparing" and not preparation_pending:
            try:
                result = model_iteration.capture_results(self, run, final=remote["status"] in TERMINAL,
                                                         remote_status=remote["status"])
            except PhoneFactoryError as exc:
                if not getattr(exc, "remote_kind", None):
                    exc.remote_kind, exc.remote_command = "protocol", "run-evaluations"
                raise
        def update(current):
            if current.get("evaluation_control_epoch", 0) != run.get("evaluation_control_epoch", 0):
                return
            if ("preparation" in remote and remote["preparation"].get("revision", 0)
                    < current.get("preparation", {}).get("revision", 0)):
                return
            if current.get("dispatch_rejected") is True:
                return  # A response from before the rejected submission is stale.
            if type(remote_revision) is int:
                if remote_revision < current.get("evaluation_state_revision", -1):
                    return
                current["evaluation_state_revision"] = remote_revision
            current.update({key: remote[key] for key in PROVENANCE_FIELDS if key in remote})
            if "preparation" in remote:
                current["preparation"] = remote["preparation"]
            if result is not None:
                current["evaluation_summary"] = result["summary"]
            if current["status"] not in TERMINAL or (current["status"] == "failed" and current.get("dispatch_error")):
                status = "stopping" if current.get("stop_requested_at") and remote["status"] not in TERMINAL else remote["status"]
                current.update(status=status, errors=remote.get("errors", []), remote_status=remote["status"], updated_at=utc_now(), dispatch_error=None)
                if current.get("stop_requested_at") and status in TERMINAL:
                    current["evaluation_control_terminal"] = True
        return self.records.update("model_iter_runs", run_id, update)

    def register_manual(self, data, filename, content):
        digest = hashlib.sha256(content).hexdigest()
        previous = next((item for item in self.store._current()["tasks"] if item.get("original_filename", item["filename"]) == filename
            and (not item.get("source_batch_id") or is_batch_active(item["source_batch_id"], self.root))), None)
        if previous and previous.get("content_sha256") not in {None, digest}:
            raise PhoneFactoryError("任务文件名已存在，请使用新文件名，已有任务不会被覆盖", 409)
        # Retry a lost upload response without creating a second business batch.
        request_id = data.get("request_id") or "upload_" + payload_digest([filename, digest])
        batch = self.manual.register(filename, content, _text(data.get("description")), request_id)
        return {**data, "filename": batch["filename"], "original_filename": filename, "source_batch_id": batch["batch_id"], "warnings": batch.get("warnings", [])}

    def _task(self, filename, mode, *, source_reference=False):
        tasks = self.store._current()["tasks"]
        task = next((item for item in tasks if item["filename"] == filename), None)
        if task is None:
            aliases = [item for item in tasks if item.get("original_filename") == filename]
            active = [item for item in aliases if not item.get("source_batch_id") or is_batch_active(item["source_batch_id"], self.root)]
            if len(active) > 1:
                raise PhoneFactoryError("原文件名对应多个采集批次，请选择业务批次", 409)
            task = next(iter(active or aliases), None)
        if task is None:
            raise PhoneFactoryError(f"任务 {filename} 不存在", 404)
        if task.get("source_batch_id") and not source_reference:
            ensure_batch_active(task["source_batch_id"], self.root)
        path = self.store.task_path(task["filename"])
        if source_reference and task.get("source_batch_id"):
            # The immutable batch workbook is authoritative. A prepared result
            # remains usable even if the optional upload copy was removed or
            # changed. First creation validates the canonical source checksum.
            return task, path, None
        if not path.is_file():
            raise PhoneFactoryError("已登记任务文件缺失，请重新上传原文件", 409)
        content = path.read_bytes()
        if task.get("content_sha256") and hashlib.sha256(content).hexdigest() != task["content_sha256"]:
            raise PhoneFactoryError("任务文件摘要不匹配", 409)
        if mode == "generate" and not task.get("source_batch_id"):
            registered = self.register_manual(task, task["filename"], content)
            _atomic_write(self.store.task_path(registered["filename"]), content)
            def replace(state):
                for row in state["tasks"]:
                    if row["filename"] == task["filename"]:
                        row.update(registered, content_sha256=hashlib.sha256(content).hexdigest())
            self.store._update(replace)
            task, path = registered, self.store.task_path(registered["filename"])
        return task, path, content

    def _evaluation_collection_source(self, task, content):
        """Preserve the upload byte-for-byte and freeze an internal 17-column workbook."""
        from openpyxl import load_workbook, Workbook
        from .task_generation.collection_batches import COLLECTION_COLUMNS
        source_id = payload_digest([task["filename"], hashlib.sha256(content).hexdigest()])
        directory = self.root / "system" / "model_iteration_sources" / source_id
        with batch_lock(self.root, "modeliter_source_" + source_id):
            directory.mkdir(parents=True, exist_ok=True)
            original, canonical = directory / "original.xlsx", directory / "canonical.xlsx"
            if original.exists() and original.read_bytes() != content:
                raise PhoneFactoryError("评测任务原件快照校验失败", 409)
            if not original.exists():
                _atomic_write(original, content)
            if not canonical.exists():
                try:
                    with zipfile.ZipFile(io.BytesIO(content)) as archive:
                        if sum(item.file_size for item in archive.infolist()) > 512 * 1024 * 1024:
                            raise PhoneFactoryError("评测任务 Excel 解压后超过 512 MiB", 413)
                except zipfile.BadZipFile as exc:
                    raise PhoneFactoryError("无法读取评测任务 Excel", 422) from exc
                book = load_workbook(io.BytesIO(content), read_only=True, data_only=False, keep_links=False)
                normalized = Workbook()
                try:
                    sheet = book.active
                    rows = sheet.iter_rows(values_only=True)
                    headers = list(next(rows, ()))
                    if any(headers.count(name) != 1 for name in COLLECTION_COLUMNS):
                        raise PhoneFactoryError("评测任务必须包含唯一的标准17列；原始列顺序和额外列可以保留", 422)
                    indices = [headers.index(name) for name in COLLECTION_COLUMNS]
                    normalized.active.title = sheet.title
                    normalized.active.append(COLLECTION_COLUMNS)
                    for row in rows:
                        if not any(value is not None and value != "" for value in row):
                            continue
                        normalized.active.append([row[index] if index < len(row) else None for index in indices])
                    output = io.BytesIO()
                    normalized.save(output)
                    _atomic_write(canonical, output.getvalue())
                finally:
                    book.close()
                    normalized.close()
            source = self.manual.register(task["filename"], canonical.read_bytes(), task.get("description", ""),
                                          "modeliter_source_" + source_id)
        return source, {"original_filename": task["filename"], "sha256": hashlib.sha256(content).hexdigest(),
                        "relative_path": original.relative_to(self.root).as_posix(),
                        "normalized_sha256": source["workbook_sha256"]}

    def _options(self, data, mode):
        if data.get("run_mode", mode) != mode:
            raise PhoneFactoryError("运行模式与接口不匹配", 409)
        state = self.store._current()
        try:
            agent_type = normalize_agent_type(data.get("agent_type", "newagent"))
            model_overrides = normalize_overrides(data, agent_type)
            repeats = repeat_times(data.get("repeat_times", 1), mode=mode)
            if "collection_artifacts_version" in data and (type(data["collection_artifacts_version"]) is not int
                    or data["collection_artifacts_version"] not in SUPPORTED_ARTIFACT_VERSIONS):
                raise ValueError("collection_artifacts_version 必须为 1 或 2")
        except ValueError as exc:
            raise PhoneFactoryError(str(exc)) from exc
        config = data.get("config", {})
        if not isinstance(config, dict) or set(config) - set(DEFAULT_CONFIG):
            raise PhoneFactoryError("本次采样配置无效")
        config = {**DEFAULT_CONFIG, **state.get("config", {}), **config}
        for key in ("sampling_enabled", "use_experience_lib", "capture_logprobs"):
            if not isinstance(config[key], bool):
                raise PhoneFactoryError(f"{key} 必须是布尔值")
        for key, maximum in (("temperature", 2), ("top_p", 1)):
            value = config[key]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= maximum:
                raise PhoneFactoryError(f"{key} 必须在 0 到 {maximum} 之间")
        vla = _text(data.get("vla")) or next(iter(state.get("vla", [])), "")
        if not vla:
            raise PhoneFactoryError("请选择本次运行的 VLA")
        vla = vla if "://" in vla else "http://" + vla
        parsed = urlsplit(vla)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            raise PhoneFactoryError("VLA 必须为有效的 HTTP 地址")
        phone, app = _text(data.get("phone_id")), _text(data.get("app"))
        associations = [dict(phone_id=row["phone_id"], app=row["app"], status="空闲") for row in state["phoneApps"] if (not phone or row["phone_id"] == phone) and (not app or row["app"] == app)]
        associations.sort(key=lambda row: (row["phone_id"], row["app"]))
        if not associations:
            raise PhoneFactoryError("请选择已关联 App 的手机")
        options = {"agent_type": agent_type, "run_mode": mode, "result_transport": "http", "vla": vla, "config": config, "phone_id": phone, "app": app, "phone_apps": associations, "phone_ids": sorted({row["phone_id"] for row in associations})}
        options.update(model_overrides)
        options["repeat_times"] = repeats
        if "collection_artifacts_version" in data:
            options["collection_artifacts_version"] = data["collection_artifacts_version"]
        if mode == "modeliter":
            options.update(model_iteration.evaluation_options(self.store, data))
            if options["checklist_source"] == "auto":
                options["execution_mode"] = "collect_and_evaluate"
        return options

    def _protocol(self) -> str:
        """http: 批次结果回传协议；legacy: 旧 Flask（无 /capabilities）；
        unsupported: 有接口但未声明批次回传能力。连接故障不缓存，让调用方如实报错。"""
        if self._protocol_cache is not None:
            return self._protocol_cache
        try:
            value = self.store._remote_result(["capabilities"])
        except PhoneFactoryError:
            return "legacy"
        supported = (isinstance(value, dict) and value.get("protocol_version") == 1
                     and value.get("batch_results") is True)
        # A collector upgraded after a rejected dispatch must be discoverable on
        # retry with the same request identity.
        self._protocol_cache = "http" if supported else None
        return "http" if supported else "unsupported"

    def _capabilities(self):
        protocol = self._protocol()
        if protocol == "unsupported":
            raise PhoneFactoryError("采集服务不支持批次结果回传，请升级采集服务后再运行", 409)

    def agent_capabilities(self):
        try:
            remote = self.store._remote_result(["capabilities"])
        except PhoneFactoryError as exc:
            if getattr(exc, "remote_status", None) in {404, 405}:
                return {"ok": True, "agent_types": engine_capabilities({})}
            return {"ok": True, "agent_types": {name: {"available": False, "collection": False,
                    "capture_logprobs": False, "model_iteration_evaluation": False,
                    "offline_evaluation": False, "reason": f"能力检查失败：{exc}"}
                    for name in ("newagent", "pipelineagent")}}
        return {"ok": True, "agent_types": engine_capabilities(remote)}

    def _require_agent_capability(self, options, *, require_batch_protocol=False):
        agent_type = options["agent_type"]
        try:
            remote = self.store._remote_result(["capabilities"])
        except PhoneFactoryError:
            if (agent_type == "newagent" and not require_batch_protocol
                    and options.get("repeat_times", 1) == 1 and not options.get("collection_artifacts_version")
                    and not any(key in options for key in MODEL_OVERRIDE_FIELDS)):
                return  # Preserve the established legacy newagent transport.
            if require_batch_protocol:
                raise
            raise PhoneFactoryError(f"无法确认 {agent_type} 能力，请检查并升级采集电脑", 409)
        if (agent_type == "pipelineagent" or require_batch_protocol) and (remote.get("protocol_version") != 1 or remote.get("batch_results") is not True):
            raise PhoneFactoryError("独立结果采集需要支持批次回传的采集服务，请升级采集电脑", 409)
        capability = engine_capabilities(remote).get(agent_type, {})
        version = options.get("collection_artifacts_version")
        if version in SUPPORTED_ARTIFACT_VERSIONS:
            def supports(value):
                versions = value.get("collection_artifacts_versions", [])
                return (isinstance(versions, list) and version in versions
                        or version == 1 and value.get("collection_artifacts_version") == 1)
            if not supports(remote) or not supports(capability):
                raise PhoneFactoryError(f"采集电脑不支持逐条回传协议 v{version}，请先合入对应采集端增量包", 409)
        if options.get("repeat_times", 1) > 1 or options.get("collection_artifacts_version") in SUPPORTED_ARTIFACT_VERSIONS:
            maximum = capability.get("max_repeat_times", 0)
            if capability.get("repeat_times") is not True or type(maximum) is not int or maximum < options.get("repeat_times", 1):
                raise PhoneFactoryError("采集电脑不支持本次重复采集次数，请升级采集电脑", 409)
        if capability.get("available") is not True or capability.get("collection") is not True:
            raise PhoneFactoryError(capability.get("reason") or f"采集电脑不支持 {agent_type}", 409)
        if any(key in options for key in MODEL_OVERRIDE_FIELDS) and capability.get("model_overrides") is not True:
            raise PhoneFactoryError(f"采集电脑尚不支持 {agent_type} 模型覆盖配置，请先升级采集端", 409)
        if options["config"]["capture_logprobs"] and capability.get("capture_logprobs") is not True:
            raise PhoneFactoryError(f"{agent_type} 尚不支持 Logprobs 采集", 409)
        if options["run_mode"] == "modeliter" and capability.get("model_iteration_evaluation") is not True:
            raise PhoneFactoryError(f"{agent_type} 尚不支持模型迭代评测，请升级采集电脑", 409)

    def remote_start(self, data, mode, *, bound_batch_id=None, legacy_batch=False):
        if bound_batch_id is not None and (mode != "generate" or legacy_batch):
            raise PhoneFactoryError("结果批次只能绑定生产采集")
        bound = None
        if bound_batch_id is not None:
            bound = self.store.collection_runs.batch(bound_batch_id, require_workbook=True)
            if bound.get("batch_role") != "collection_result":
                raise PhoneFactoryError("内部下发必须绑定采集结果批次", 409)
            task = {"filename": bound["filename"], "source_batch_id": bound_batch_id}
            path, content = Path(bound["workbook_path"]), None
        else:
            task, path, content = self._task(_filename(data.get("filename")), mode,
                                           source_reference=mode == "generate" and not legacy_batch)
        options = self._options(data, mode)
        request_id = data.get("request_id") or uuid.uuid4().hex
        _identifier(request_id, "request_id")
        digest = hashlib.sha256(content).hexdigest() if content is not None else task.get("content_sha256")
        existing = self.records.get("model_iter_runs", "eval_" + payload_digest(request_id)) if mode == "modeliter" else None
        frozen = bound.get("execution_metadata") if bound else None
        if mode == "generate" and bound is None:
            existing = self.store.collection_runs.get("cr_" + payload_digest([task["source_batch_id"], request_id]))
            prepared = self.records.get("collection_result_requests", payload_digest([task["source_batch_id"], request_id]))
            if prepared:
                prepared_batch = self.store.collection_runs.result_batches.get(prepared["batch_id"])
                frozen = prepared_batch["execution_metadata"]
                existing = self.store.collection_runs.get(prepared_batch["collection_run_id"])
        if existing is not None:
            frozen = existing.get("request", {}).get("metadata", existing.get("request", {}))
        if frozen is not None:
            if "repeat_times" not in frozen and options["repeat_times"] == 1:
                options.pop("repeat_times")
            if "collection_artifacts_version" in frozen:
                options["collection_artifacts_version"] = frozen["collection_artifacts_version"]
        elif not legacy_batch:
            options["collection_artifacts_version"] = COLLECTION_ARTIFACTS_VERSION
        incremental = options.get("collection_artifacts_version") in SUPPORTED_ARTIFACT_VERSIONS
        if incremental and existing is None:
            self._require_agent_capability(options, require_batch_protocol=True)
        metadata = {**options, "filename": task["filename"]}
        # Evaluation always uses the upgraded HTTP contract; its capability is
        # checked afresh below so an in-place collector upgrade needs no platform restart.
        if mode == "generate" and not legacy_batch:
            historical = (None if bound is not None else self.store.collection_runs.get(
                "cr_" + payload_digest([task["source_batch_id"], request_id])))
            protocol = ((historical.get("request", {}).get("metadata", {}).get("result_transport") or "http")
                        if historical is not None else "http")
        else:
            protocol = "http" if mode == "modeliter" or options["agent_type"] == "pipelineagent" else self._protocol()
        if protocol == "legacy":
            metadata["result_transport"] = "legacy"
        if mode == "generate":
            if bound is not None:
                frozen = bound["execution_metadata"]
                compared = {**metadata, "filename": frozen["filename"]}
                if request_id != bound["execution_request_id"] or compared != frozen:
                    raise PhoneFactoryError("下发配置与已冻结结果批次不一致", 409)
                metadata = frozen
                run, created = self.store.collection_runs.create(bound_batch_id, dispatch_key=request_id,
                    metadata=metadata, workbook_sha256=bound["workbook_sha256"])
            elif legacy_batch:
                run, created = self.store.collection_runs.create(task["source_batch_id"], dispatch_key=request_id,
                    metadata=metadata, workbook_sha256=digest)
            else:
                run, created = self.store.collection_runs.create_result_run(task["source_batch_id"], dispatch_key=request_id,
                    metadata=metadata, workbook_sha256=digest)
            if not created and (run["status"] not in {"failed", "dispatching"}
                    or (run["status"] == "failed" and not run.get("dispatch_error"))):
                # Accepted historical runs are read-only replays. Their source
                # workbook is not needed to return the already frozen identity.
                return {"ok": True, "reused": True, **run}
            # All dispatch writes/locks and transmitted identities use the result
            # batch. The original filename stays in frozen metadata for lineage.
            task = {**task, "source_batch_id": run["batch_id"]}
            run_id = run["collection_run_id"]
            if created:
                def initialize_dispatch(item):
                    # A concurrent idempotent submission may have already sent it.
                    if item["status"] == "dispatching":
                        item.setdefault("dispatch_attempted", False)
                run = self.records.update("collection_runs", run_id, initialize_dispatch)
        else:
            run_id = "eval_" + payload_digest(request_id)
            request = {**metadata, "workbook_sha256": digest}
            payload = {"run_id": run_id, "collection_run_id": run_id, "run_mode": "modeliter", "status": "dispatching", "filename": task["filename"], "vla": options["vla"], "created_at": utc_now(), "request": request, "request_digest": payload_digest(request), "errors": [], "dispatch_error": None, "dispatch_attempted": False}
            # Accepted historical requests keep their original data ownership.
            # For the new protocol, freeze a real collection result batch before
            # dispatch; the evaluation is a separate consumer of the same run.
            if incremental:
                if existing and (existing["request_digest"] != payload["request_digest"]
                        and not matches_legacy_logprobs_request(existing, request)):
                    raise PhoneFactoryError("相同 request_id 对应的运行参数不同", 409)
                if existing and existing.get("batch_id"):
                    collection = self.store.collection_runs.get(run_id)
                    if collection is None:
                        raise PhoneFactoryError("评测关联的采集运行缺失，不能重新下发", 409)
                else:
                    source, original = self._evaluation_collection_source(task, content)
                    collection, _ = self.store.collection_runs.create_result_run(source["batch_id"],
                        dispatch_key=request_id, metadata={**metadata, "original_workbook": original},
                        workbook_sha256=source["workbook_sha256"])
                payload.update(batch_id=collection["batch_id"], task_source_batch_id=collection["task_source_batch_id"],
                               collection_artifacts_version=options["collection_artifacts_version"], repeat_times=1)
            try:
                from .display_identifiers import DisplayIdentifierStore
                run = DisplayIdentifierStore(self.root).create_run("model_iter_runs", run_id, payload)
                created = True
            except RevisionConflict:
                run = self.records.get("model_iter_runs", run_id)
                if (run["request_digest"] != payload["request_digest"]
                        and not matches_legacy_logprobs_request(run, request)):
                    raise PhoneFactoryError("相同 request_id 对应的运行参数不同", 409)
                created = False
        if not created and (run["status"] not in {"failed", "dispatching"} or (run["status"] == "failed" and not run.get("dispatch_error"))):
            return {"ok": True, "reused": True, **run}
        with batch_lock(self.root, "dispatch_" + run_id):
            current = self.store.collection_runs.get(run_id) if mode == "generate" else self.records.get("model_iter_runs", run_id)
            if current["status"] not in {"failed", "dispatching"}:
                return {"ok": True, "reused": True, **current}
            can_reject = self._can_reject_dispatch(current, mode)
            try:
                if not created:
                    # An acknowledgement can be lost after remote acceptance. Query that
                    # durable identity before considering another idempotent submission.
                    try:
                        known = self.get_run(run_id)
                    except PhoneFactoryError:
                        known = {}
                    if (known.get("run_id", known.get("collection_run_id")) == run_id
                            and known.get("agent_type", "newagent") != options["agent_type"]):
                        raise PhoneFactoryError("远端已有运行的采集引擎与请求不匹配，不能重新下发", 502)
                    if (known.get("run_id", known.get("collection_run_id")) == run_id
                            and known.get("run_mode") == mode
                            and known.get("agent_type", "newagent") == options["agent_type"]
                            and known.get("status") in TERMINAL | {"preparing", "queued", "starting", "running", "stopping", "needs_attention"}):
                        if mode == "generate":
                            saved = self.store.collection_runs.dispatched(run_id, known)
                        else:
                            def recovered(item):
                                if item["status"] not in {"interrupted", "cancelled", "succeeded", "partial", "completed"}:
                                    item.update(status=known["status"], errors=known.get("errors", []), dispatch_error=None,
                                                dispatch_rejected=False, updated_at=utc_now())
                                    item.pop("dispatch_rejection", None)
                            saved = self.records.update("model_iter_runs", run_id, recovered)
                            if incremental:
                                self.store.collection_runs.dispatched(run_id, known)
                                self.sync_policy.reset("collection", run_id)
                        self.sync_policy.reset("collection" if mode == "generate" else "evaluation", run_id)
                        self.records.update("collection_runs" if mode == "generate" else "model_iter_runs", run_id,
                            lambda item: item.update(dispatch_attempted=True))
                        return {**known, **saved, "ok": True, "reused": True}
                requires_identity = incremental or mode == "generate" and current.get("batch_role") == "collection_result"
                if requires_identity and protocol != "http":
                    raise PhoneFactoryError("采集结果批次必须使用支持运行编号的 HTTP 协议", 409)
                self._require_agent_capability(options, require_batch_protocol=requires_identity)
                if mode == "modeliter":
                    model_iteration.require_capability(self.store, profiles=options["checklist_source"] == "auto")
                    model_iteration.verify_profile(self.store, options)
                elif options["agent_type"] == "newagent" and not requires_identity:
                    self._capabilities()
                if mode == "generate" or incremental:
                    # Query an uncertain remote acknowledgement before needing
                    # files. Only a genuinely new dispatch reads the workbook.
                    batch = self.store.collection_runs.batch(current["batch_id"], require_workbook=True)
                    path = Path(batch.get("workbook_path") or self.root / "system" / "task_generation" /
                                "collection_batches" / current["batch_id"] / batch["filename"])
                    collection = self.store.collection_runs.get(run_id)
                    if mode == "modeliter":
                        # The internal batch uses canonical columns, while the
                        # collector still receives the exact accepted upload,
                        # including any intranet-specific extra task fields.
                        from .data_store.paths import contained_path
                        original = collection["request"]["metadata"]["original_workbook"]
                        path = contained_path(self.root, original["relative_path"])
                        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != original["sha256"]:
                            raise PhoneFactoryError("评测采集原始任务快照缺失或摘要不匹配", 409)
                    with active_batch_lock(current["batch_id"], self.root):
                        if collection["status"] == "failed":
                            self.store.collection_runs.retry_dispatch(run_id)
                    tasks = list(collection["batch_tasks"].values())
                else:
                    _, tasks, _ = self.manual._parse(content, run_id)
                temp_root = self.root / "tmp" / "phone_factory"
                temp_root.mkdir(parents=True, exist_ok=True)
                with tempfile.TemporaryDirectory(dir=temp_root) as temporary:
                    directory = Path(temporary)
                    apps_path, manifest_path = directory / "apps.json", directory / "tasks.json"
                    apps_path.write_text(json.dumps(options["phone_apps"], ensure_ascii=False), encoding="utf-8")
                    manifest_path.write_text(json.dumps({"tasks": tasks}, ensure_ascii=False), encoding="utf-8")
                    args = ["start-run", str(path), str(apps_path), options["phone_id"], options["app"], "--agent-type", options["agent_type"], "--vla", options["vla"], "--runmode", mode, "--temperature", str(options["config"]["temperature"]), "--top-p", str(options["config"]["top_p"])]
                    for key in ("repeat_times", "collection_artifacts_version"):
                        if key in options:
                            args += ["--" + key.replace("_", "-"), str(options[key])]
                    if "execution_model" in options:
                        args += ["--execution-model", options["execution_model"]]
                    if "planner_config" in options:
                        args += ["--planner-config", json.dumps(options["planner_config"], ensure_ascii=False)]
                    if mode == "modeliter":
                        args += ["--evaluator-config", json.dumps(options["evaluator_config"], ensure_ascii=False),
                                 "--checklist-source", options["checklist_source"], "--evaluation-mode", "checklist"]
                        if options["checklist_source"] == "auto":
                            args += ["--evaluation-profile-id", options["evaluation_profile_id"],
                                     "--evaluation-profile-digest", options["evaluation_profile_digest"]]
                    if protocol == "http":
                        args += ["--collection-run-id", run_id, "--task-manifest", str(manifest_path)]
                        if mode == "generate" or incremental:
                            args += ["--batch-id", collection["batch_id"], "--output-dir", collection["output_dir"]]
                    if options["config"]["sampling_enabled"]:
                        args.append("--sampling")
                    if options["config"]["use_experience_lib"]:
                        args.append("--exp")
                    if options["config"]["capture_logprobs"]:
                        args.append("--capture-logprobs")
                    if mode == "generate" or incremental:
                        with active_batch_lock(current["batch_id"], self.root):
                            self._begin_dispatch(run_id, mode, incremental)
                    else:
                        self._begin_dispatch(run_id, mode, incremental)
                    response = self.store._remote_result(args)
                    if response.get("agent_type", "newagent") != options["agent_type"]:
                        raise PhoneFactoryError("远端接受运行的采集引擎与请求不匹配", 502)
                    if response.get("ok") is False:
                        raise PhoneFactoryError(response.get("error") or response.get("message") or "远端未接受运行", 502)
                if mode == "generate":
                    saved = self.store.collection_runs.dispatched(run_id, response)
                else:
                    def accepted(item):
                        if item["status"] not in {"interrupted", "cancelled", "succeeded", "partial", "completed"}:
                            item.update(status="running", dispatch_response=response, dispatch_error=None,
                                        dispatch_rejected=False, updated_at=utc_now())
                            item.pop("dispatch_rejection", None)
                    saved = self.records.update("model_iter_runs", run_id, accepted)
                    if incremental:
                        self.store.collection_runs.dispatched(run_id, response)
                        self.sync_policy.reset("collection", run_id)
                self.sync_policy.reset("collection" if mode == "generate" else "evaluation", run_id)
                if mode == "generate":
                    if response.get("state_revision") is not None:
                        saved = self.records.update("collection_runs", run_id, lambda current: (merge_control(current, response), None)[1])
                    if saved.get("stop_requested_at"):
                        self._control_pool.submit(run_id, lambda: self._cancel_collection_remote(run_id))
                return {**response, **saved, "ok": True}
            except Exception as exc:
                explicit_rejection = (getattr(exc, "remote_command", None) == "start-run"
                    and (getattr(exc, "dispatch_not_sent", False) is True
                         or (getattr(exc, "remote_kind", None) == "http"
                             and getattr(exc, "remote_status", None) in {400, 409, 413, 422})))
                rejection = ({"message": str(exc), "at": utc_now(),
                              "remote_status": getattr(exc, "remote_status", None),
                              "dispatch_not_sent": getattr(exc, "dispatch_not_sent", False) is True}
                             if can_reject and explicit_rejection else None)
                if mode == "generate":
                    self.store.collection_runs.dispatch_failed(run_id, str(exc), rejection=rejection)
                else:
                    def failed(item):
                        if item["status"] not in {"interrupted", "cancelled", "succeeded", "partial", "completed"}:
                            item.update(status="failed", dispatch_error=str(exc))
                            if rejection is not None:
                                item.update(dispatch_rejected=True, dispatch_rejection=rejection)
                    self.records.update("model_iter_runs", run_id, failed)
                    if incremental:
                        self.store.collection_runs.dispatch_failed(run_id, str(exc), rejection=rejection)
                if rejection is not None:
                    self.sync_policy.reject_dispatch("collection" if mode == "generate" else "evaluation", run_id, str(exc))
                    if mode == "modeliter" and incremental:
                        self.sync_policy.reject_dispatch("collection", run_id, str(exc))
                raise

    def remote_start_evaluation(self, data):
        """Submit a manifest-bound evaluation without touching phone or production state."""
        candidate = self.records.get("model_iter_runs", "eval_" + payload_digest(data["request_id"])) if data.get("request_id") else None
        preview_record = self.records.get("model_iter_previews", data["preview_id"]) if isinstance(data.get("preview_id"), str) and data["preview_id"] else None
        if ((candidate or {}).get("request", {}).get("offline_evaluation_jobs_version") == 1
                or preview_record and preview_record.get("paged")):
            from .offline_evaluation_dispatch import accept
            return accept(self, data)
        allowed = {"request_id", "evaluator_config", "evaluation_profile_id", "evaluation_profile_digest",
                   "profile_id", "profile_digest", "preview_id", "checklist_source", "evaluation_mode"}
        if set(data) - allowed:
            raise PhoneFactoryError("独立评测不接受手机、App、VLA 或采集参数")
        if candidate and candidate.get("request", {}).get("execution_mode") == "evaluate_only":
            frozen = candidate["request"]
            options_data = {key: frozen[key] for key in allowed if key in frozen and key not in {"request_id", "preview_id"}}
            options_data.update(data)
            for alias, key in (("profile_id", "evaluation_profile_id"), ("profile_digest", "evaluation_profile_digest")):
                if alias in data:
                    options_data[key] = data[alias]
            replay_options = model_iteration.evaluation_options(self.store, options_data, offline=True)
            if frozen.get("preview_id") != data.get("preview_id") or any(frozen.get(k) != v for k, v in replay_options.items()):
                raise PhoneFactoryError("相同 request_id 对应的运行参数不同", 409)
            if candidate["status"] not in {"failed", "dispatching"} or candidate["status"] == "failed" and not candidate.get("dispatch_error"):
                return {"ok": True, "reused": True, **self._public_run(candidate)}
            data = options_data
        options = model_iteration.evaluation_options(self.store, data, offline=True)
        preview_id = _identifier(data.get("preview_id"), "预览编号")
        preview_record = self.records.get("model_iter_previews", preview_id)
        if preview_record is None:
            raise PhoneFactoryError("请先预览并确认要评测的轨迹集", 409)
        preview = preview_record["preview"]
        if not preview.get("valid") or preview.get("status") == "preparing":
            raise PhoneFactoryError("轨迹预览尚未完成或没有可评测轨迹", 409)
        request_id = _identifier(data.get("request_id") or uuid.uuid4().hex, "request_id")
        run_id = "eval_" + payload_digest(request_id)
        request = {**options, "run_mode": "modeliter", "execution_mode": "evaluate_only", "result_transport": "http",
                   "preview_id": preview_id, "source": preview["source"], "source_digest": preview["source_digest"],
                   "source_cases": preview["cases"], "source_request": preview_record["request"]}
        payload = {"run_id": run_id, "collection_run_id": run_id, "run_mode": "modeliter",
                   "execution_mode": "evaluate_only", "source": preview["source"], "source_digest": preview["source_digest"],
                   "evaluation_profile_id": options["evaluation_profile_id"],
                   "evaluation_profile_digest": options["evaluation_profile_digest"],
                   "status": "dispatching", "filename": "已有轨迹评测", "vla": "", "created_at": utc_now(),
                   "request": request, "request_digest": payload_digest(request), "errors": [], "dispatch_error": None,
                   "dispatch_attempted": False}
        try:
            from .display_identifiers import DisplayIdentifierStore
            run = DisplayIdentifierStore(self.root).create_run("model_iter_runs", run_id, payload)
            created = True
        except RevisionConflict:
            run = self.records.get("model_iter_runs", run_id)
            if run["request_digest"] != payload["request_digest"]:
                raise PhoneFactoryError("相同 request_id 对应的运行参数不同", 409)
            created = False
        if not created and (run["status"] not in {"failed", "dispatching"}
                            or run["status"] == "failed" and not run.get("dispatch_error")):
            return {"ok": True, "reused": True, **run}
        with batch_lock(self.root, "dispatch_" + run_id):
            current = self.records.get("model_iter_runs", run_id)
            if current["status"] not in {"failed", "dispatching"}:
                return {"ok": True, "reused": True, **current}
            try:
                model_iteration.require_capability(self.store, offline=True)
                if created:
                    model_iteration.verify_profile(self.store, options)
                # Re-submit exactly the frozen request after a lost acknowledgement.
                # The remote atomically validates its request digest before reuse;
                # a GET with only the same run id cannot establish that identity.
                # Already accepted runs may outlive their selectable rule version.
                remote_request = {**options, "collection_run_id": run_id, "preview_id": preview_id}
                self.records.update("model_iter_runs", run_id,
                    lambda item: item.update(dispatch_attempted=True, submitted_at=utc_now()))
                self.sync_policy.reset("evaluation", run_id)
                response = self.store._remote_result(["start-evaluation", "--payload", json.dumps(remote_request, ensure_ascii=False)])
                if (response.get("ok") is False or response.get("run_id", response.get("collection_run_id")) != run_id
                        or response.get("run_mode") != "modeliter" or response.get("execution_mode") != "evaluate_only"
                        or response.get("status") not in TERMINAL | {"preparing", "queued", "starting", "running", "stopping", "needs_attention"}):
                    raise PhoneFactoryError("远端未接受独立评测，或返回的运行身份不匹配", 502)
                def accepted(item):
                    if item["status"] not in {"interrupted", "cancelled", "succeeded", "partial", "completed"}:
                        item.update(status="stopping" if item.get("stop_requested_at") and response["status"] not in TERMINAL else response["status"], dispatch_response=response,
                                    dispatch_error=None, updated_at=utc_now())
                        if "preparation" in response:
                            item["preparation"] = response["preparation"]
                saved = self.records.update("model_iter_runs", run_id, accepted)
                self.sync_policy.reset("evaluation", run_id)
                return {**response, **saved, "ok": True}
            except Exception as exc:
                def failed(item):
                    if item["status"] not in {"interrupted", "cancelled", "succeeded", "partial", "completed"}:
                        item.update(status="failed", dispatch_error=str(exc), updated_at=utc_now())
                self.records.update("model_iter_runs", run_id, failed)
                raise

    def retry_collection_transfer(self, run_id):
        from .collector_storage import require_available
        require_available(self.records, run_id)
        run_id = _identifier(run_id, "采集运行编号")
        run = self.store.collection_runs.get(run_id)
        if not run:
            raise PhoneFactoryError("采集运行不存在", 404)
        with active_batch_lock(run["batch_id"], self.root):
            from .pipeline_access import ensure_pipeline_write
            ensure_pipeline_write(run["batch_id"], self.root, action="collect")
        if run.get("request", {}).get("metadata", {}).get("collection_artifacts_version") == 2:
            value = self.store._remote_result(["retry-collection-artifacts", run_id])
            if value.get("ok") is not True or value.get("collection_run_id", run_id) != run_id:
                raise PhoneFactoryError("采集端未确认重试封存；不会重新采集", 502)
        saved = self.transfer.sync(run_id, force=True)
        if run.get("archive_status") in {"pending", "building", "failed"}:
            self._archive_pool.submit(run_id, lambda: self._refresh_archive(run_id))
        return saved

    def _collection_progress(self, run_id):
        collection = self.store.collection_runs.get(run_id)
        if collection is None:
            return {}
        metadata = collection.get("request", {}).get("metadata", {})
        summary = dict(collection.get("collection_summary") or {})
        for key in ("transferred_trajectory_count", "transferred_diagnostic_count"):
            if key in collection:
                summary[key] = collection[key]
        batch_state = lifecycle(collection["batch_id"], self.root)
        journal = self.records.get("collection_artifact_transfers", run_id) or {}
        artifacts = self.transfer.artifact_rows(run_id, limit=3) if journal.get("receipt_policy_version") == 2 else []
        if journal.get("receipt_policy_version") != 2:
            for slot in sorted((journal.get("items") or {}).values(), key=lambda row: row.get("item", {}).get("sequence", 0))[:3]:
                item, manifest = slot.get("item", {}), slot.get("manifest") or {}
                entry = manifest.get("trajectory") or manifest.get("diagnostic") or {}
                artifacts.append({"artifact_id": item.get("artifact_id"), "kind": item.get("kind"),
                    "transfer_status": slot.get("status"), **{key: entry[key] for key in (
                        "collection_case_id", "phone_id", "repeat_index", "repeat_times", "execution_status", "error") if key in entry}})
        summary.update({key: collection[key] for key in ("received_artifact_count", "quarantined_artifact_count",
            "evaluation_eligible_count", "preprocessing_eligible_count", "transferred_outcome_count") if key in collection})
        return {**{key: collection[key] for key in ("batch_id", "task_source_batch_id", "collection_status",
                "transfer_status", "transfer_error", "transfer_updated_at", "transferred_trajectory_count",
                "transferred_diagnostic_count", "archive_status", "collection_artifact_count", "receipt_policy_version", "receipt_closed", "phone_states",
                "stop_requested_at", "control_terminal", "control_error", "can_cancel_collection") if key in collection},
                "repeat_times": metadata.get("repeat_times", 1),
                "batch_status": batch_state["status"], "release_id": batch_state["release_id"],
                "published_at": batch_state["published_at"], "collection_artifacts": artifacts,
                **({"collection_artifacts_version": metadata["collection_artifacts_version"]}
                   if "collection_artifacts_version" in metadata else {}),
                "receipt_storage": (self.transfer._receipt_storage_states.get(run_id)
                    or self.records.get("collection_receipt_storage", run_id)),
                **(self._resume_service.view(run_id) if self._resume_service is not None else {}),
                "collection_summary": summary, "collection_sync": self.sync_policy.view("collection", run_id)}

    @staticmethod
    def _public_run(run):
        hidden = {"trajectories", "batch_tasks", "manifest", "dispatch_response", "cancel_response", "pending_preparation_control"}
        view = {key: value for key, value in run.items() if key not in hidden}
        if run.get("request", {}).get("offline_evaluation_jobs_version") == 1:
            view["preparation_control_pending"] = bool(run.get("pending_preparation_control"))
        request = dict(view.get("request") or {})
        if request:
            request.pop("source_cases", None)
            request.pop("task_manifest", None)
            view["request"] = request
        for key in ("errors", "remote_errors"):
            if isinstance(view.get(key), list):
                count=view.get(key+"_count")
                view[key + "_count"] = max(count,len(view[key])) if type(count) is int else len(view[key])
                view[key] = view[key][:100]
        return view

    def _evaluation_snapshot_label(self,run):
        from .collection_resume_snapshots import evaluation_snapshot_status
        request=(run or {}).get("request",{})
        return evaluation_snapshot_status(self.records,{**request.get("source_request",{}),**request.get("source",{})})

    def run_views(self, mode):
        from .collector_storage import public_state as storage_state
        if mode == "modeliter":
            runs = []
            for run in self.records.list("model_iter_runs"):
                request = run.get("request", {})
                snapshot_status=self._evaluation_snapshot_label(run)
                metadata = {key: request[key] for key in ("source", "source_digest", "evaluation_profile_id",
                                                         "evaluation_profile_digest", "checklist_source", *MODEL_OVERRIDE_FIELDS) if key in request}
                runs.append({**run, **metadata, **self._collection_progress(run["run_id"]), **({"agent_type": request.get("agent_type", "newagent")} if request.get("execution_mode") != "evaluate_only" else {}), "execution_mode": request.get("execution_mode", "collect_and_evaluate"),
                             "sync": self.sync_policy.view("evaluation", run["run_id"]),**snapshot_status})
            return [self._public_run({**item, **storage_state(self.records, item["run_id"])}) for item in sorted(runs, key=lambda item: (item["created_at"], item["run_id"]))]
        views = []
        for run in self.store.collection_runs.list_runs():
            transfer = self.records.get("collection_transfers", run["collection_run_id"]) or {}
            metadata = run.get("request", {}).get("metadata", {})
            if metadata.get("run_mode", "generate") != "generate":
                continue
            batch_state = lifecycle(run["batch_id"], self.root)
            views.append({**run, **self._collection_progress(run["collection_run_id"]), "batch_status": batch_state["status"], "release_id": batch_state["release_id"],
                          "published_at": batch_state["published_at"], "filename": metadata.get("filename"), "vla": metadata.get("vla"), "agent_type": metadata.get("agent_type", "newagent"), **{key: metadata[key] for key in MODEL_OVERRIDE_FIELDS if key in metadata}, **{key: transfer[key] for key in ("remote_status", "transfer_status", "transfer_error", "remote_errors", "transfer_updated_at") if key in transfer}, "trajectory_count": len(run.get("trajectories", [])), "can_cancel_collection": can_cancel(run),
                          "sync": self.sync_policy.view("collection", run["collection_run_id"])})
        return [self._public_run({**item, **(self._resume_service.view(item["collection_run_id"])
                    if self._resume_service is not None else {}), **storage_state(self.records, item["collection_run_id"])}) for item in views]

    def public_state(self, state, mode):
        statuses = {"dispatching": "下发中", "queued": "排队中", "starting": "启动中", "running": "运行中", "completed": "已回传", "succeeded": "已完成", "partial": "部分完成", "failed": "失败", "interrupted": "已中断", "cancelled": "已中断"}
        runs = self.run_views(mode)
        for task in state["tasks"]:
            matching = [run for run in runs if run.get("filename") == task["filename"]]
            if not matching:
                continue
            # A task can run on several phones; one phone finishing must not
            # hide another phone's active run, regardless of dispatch order.
            active = [run for run in matching if run["status"] in {"dispatching", "queued", "starting", "running"}]
            if active:
                live = [run for run in active if not run.get("transfer_error")]
                latest = (live or active)[-1]
                # Legacy collectors own results in the phone workspace and never
                # report batch transfers; only http transfers can surface here.
                if mode == "generate" and latest.get("collection_status") in TERMINAL:
                    label = {"completed": "采集已完成", "cancelled": "采集已停止", "interrupted": "采集已中断", "partial": "采集部分完成", "failed": "采集失败"}.get(latest["collection_status"], "采集已结束")
                    task["status"] = label + (" · 回传中" if latest.get("transfer_status") not in {"completed", "partial"} else "")
                    continue
                transport = latest.get("request", {}).get("metadata", {}).get("result_transport")
                if latest.get("transfer_error") and transport == "http":
                    task["status"] = "回传失败"
                else:
                    task["status"] = statuses.get(latest["status"], latest["status"])
                continue
            done = [run for run in matching if run["status"] in {"completed", "succeeded", "partial"}]
            if done:
                latest = done[-1]
                display_status = "partial" if latest["status"] == "completed" and latest.get("remote_status") == "partial" else latest["status"]
                task["status"] = statuses.get(display_status, display_status)
                continue
            failed = [run for run in matching if run["status"] == "failed"]
            if failed:
                task["status"] = "失败"
                continue
            # Interrupted/historical runs must not override a task that was
            # added but never started, e.g. stale transfer errors.
            task["status"] = task.get("status") or "未运行"
        return state

    def batches(self, *, batch_role="source", include_published=False):
        if batch_role not in {"source", "collection_result", "all"}:
            raise PhoneFactoryError("批次角色无效")
        ids = {item["batch_id"] for item in self.manual.list(include_published=include_published)}
        base = self.root / "system" / "task_generation" / "collection_batches"
        if base.is_dir():
            ids.update(path.parent.name for path in base.glob("*/batch.json"))
        items = []
        for identifier in ids if batch_role != "collection_result" else []:
            if include_published or is_batch_active(identifier, self.root):
                batch = self.store.collection_runs.batch(identifier)
                items.append({**batch, "download_url": f"/api/phone-factory/batches/{identifier}/workbook"})
        if batch_role != "source":
            for batch in self.store.collection_runs.result_batches.list(include_published=include_published):
                items.append({**batch, "download_url": f"/api/phone-factory/batches/{batch['batch_id']}/workbook"})
        return sorted(items, key=lambda item: item["created_at"], reverse=True)

    def _delete_phone(self, phone, mode):
        if not phone or len(phone) > 180 or any(ord(char) < 32 for char in phone):
            raise PhoneFactoryError("手机编号无效")
        response = self.store._remote_result(["del-phone", phone])
        if response.get("ok") is not True:
            raise PhoneFactoryError(response.get("error") or response.get("message") or "远端删除失败", 502)
        for namespace in ("collection_runs", "model_iter_runs"):
            for run in self.records.list(namespace):
                request = run.get("request", {})
                options = request.get("metadata", request)
                if run["status"] not in TERMINAL and any(row["phone_id"] == phone for row in options.get("phone_apps", [])):
                    def interrupt(current):
                        if current["status"] not in TERMINAL:
                            if namespace == "collection_runs" and options.get("collection_artifacts_version") in SUPPORTED_ARTIFACT_VERSIONS:
                                current.update(status="running", collection_status="cancelled", interrupted_at=utc_now(),
                                               stop_reason="设备已删除，等待已采集结果封存及回传")
                                return
                            current.update(status="interrupted", dispatch_error="设备已删除，运行已中断", interrupted_at=utc_now())
                    try:
                        with active_batch_lock(run.get("batch_id") if namespace == "collection_runs" else None, self.root):
                            self.records.update(namespace, run.get("run_id", run.get("collection_run_id")), interrupt)
                            if namespace == "collection_runs" and options.get("collection_artifacts_version") in SUPPORTED_ARTIFACT_VERSIONS:
                                self.sync_policy.reset("collection", run["collection_run_id"])
                    except BatchPublishedError:
                        pass  # Finished, published artifacts and logs remain unchanged.
        def remove(state):
            state["phones"] = [item for item in state["phones"] if item != phone]
            state["phoneApps"] = [item for item in state["phoneApps"] if item["phone_id"] != phone]
        self.store._update(remove)
        return {**response, "state": self.store.state(mode)}

    def _collection_stopped(self, run_id, result):
        collection = self.store.collection_runs.get(run_id)
        if not collection:
            return None
        def stopped(current):
            if merge_control(current, result):
                current.update(cancel_response=result, control_error=None,
                               stop_requested_at=current.get("stop_requested_at") or utc_now())
                if current.get("request", {}).get("metadata", {}).get("collection_artifacts_version") not in SUPPORTED_ARTIFACT_VERSIONS:
                    current["status"] = "interrupted" if current.get("control_terminal") else "stopping"
        try:
            # Match dispatch -> batch lock order; never invert it during a late
            # stop acknowledgement racing with a submitted start.
            with batch_lock(self.root, "dispatch_" + run_id):
                with active_batch_lock(collection["batch_id"], self.root):
                    saved = self.records.update("collection_runs", run_id, stopped)
                    if collection.get("transfer_status") != "completed":
                        self.sync_policy.reset("collection", run_id)
                    return saved
        except BatchPublishedError:
            return collection  # Evaluation control cannot rewrite a published source.

    def _schedule_controls(self):
        from .collector_storage import unavailable
        for run in self.records.list("model_iter_runs"):
            if unavailable(self.records, run["run_id"]):
                continue
            if run.get("stop_requested_at") and not run.get("evaluation_control_terminal"):
                delay = min(60, 2 ** min(run.get("stop_attempt_count", 0), 6))
                memory = self._control_memory.get(run["run_id"], {})
                if time.time() >= max(run.get("stop_last_attempt_epoch", 0), memory.get("last_attempt", 0)) + delay:
                    run_id = run["run_id"]
                    self._control_pool.submit("evaluation:" + run_id,
                        lambda run_id=run_id: self._cancel_evaluation_remote(run_id))
        for run in self.store.collection_runs.list_runs():
            if run.get("request", {}).get("metadata", {}).get("run_mode") == "modeliter":
                continue
            if unavailable(self.records, run["collection_run_id"]):
                continue
            if not run.get("stop_requested_at") or (run.get("control_terminal") and not can_cancel(run)):
                continue
            if not is_batch_active(run["batch_id"], self.root):
                continue
            delay = min(60, 2 ** min(run.get("stop_attempt_count", 0), 6))
            memory = self._control_memory.get(run["collection_run_id"], {})
            delay = max(delay, min(60, 2 ** min(memory.get("attempts", 0), 6)))
            if time.time() < max(run.get("stop_last_attempt_epoch", 0), memory.get("last_attempt", 0)) + delay:
                continue
            run_id = run["collection_run_id"]
            self._control_pool.submit(run_id, lambda run_id=run_id: self._cancel_collection_remote(run_id))

    def _control_attempt(self, namespace, run_id, run):
        memory = self._control_memory.setdefault(run_id, {})
        memory.update(run=run, last_attempt=time.time(), attempts=memory.get("attempts",0)+1)
        try:
            self.records.update(namespace, run_id, lambda row: row.update(
                stop_last_attempt_epoch=memory["last_attempt"], stop_attempt_count=row.get("stop_attempt_count",0)+1))
        except (OSError, sqlite3.Error) as exc:
            # The stop intent is already durable. An accounting write must not
            # prevent its delivery, nor cause an unbounded background retry loop.
            memory["error"] = "停止请求已保存；平台存储暂不可写：" + type(exc).__name__

    def _control_failure(self, namespace, run_id, exc):
        self._control_memory.setdefault(run_id,{})["error"] = str(exc)[:500]
        try:
            self.records.update(namespace, run_id, lambda row: row.update(
                control_error=str(exc)[:500], can_cancel_collection=True,
                **({"collection_status":"needs_attention"} if namespace=="collection_runs" else {})))
        except (OSError, sqlite3.Error):
            pass

    def _cancel_evaluation_remote(self, run_id):
        from .collector_storage import unavailable
        if unavailable(self.records, run_id):
            return
        run = self.records.get("model_iter_runs", run_id)
        if not run or not run.get("stop_requested_at"):
            return
        if run.get("request", {}).get("offline_evaluation_jobs_version") == 1 and run.get("evaluation_control_terminal"):
            return
        self._control_attempt("model_iter_runs", run_id, run)
        try:
            if run.get("request", {}).get("offline_evaluation_jobs_version") == 1 and not run.get("dispatch_confirmed"):
                from .offline_evaluation_dispatch import dispatch
                if run.get("dispatch_rejected") and run.get("dispatch_uncertain"):
                    self.records.update("model_iter_runs", run_id, lambda row: row.update(dispatch_rejected=False))
                dispatch(self, run_id)
                run = self.records.get("model_iter_runs", run_id)
                if run.get("evaluation_control_terminal") or not run.get("dispatch_confirmed"):
                    return
            remote = self.get_run(run_id)
            if remote.get("run_id", remote.get("collection_run_id")) != run_id or remote.get("run_mode") != "modeliter":
                raise PhoneFactoryError("远端评测身份不匹配，未请求停止", 502)
            if run.get("request", {}).get("collector_server"):
                from .evaluation_job_previews import remote as invoke
                result = invoke(self.store, ["cancel-run", run_id], run["request"]["collector_server"])
            else:
                result = self.store._remote_result(["cancel-run", run_id])
            if (result.get("ok") is False or result.get("run_id", result.get("collection_run_id")) != run_id
                    or result.get("status") not in TERMINAL | {"stopping", "needs_attention"}):
                raise PhoneFactoryError("采集电脑尚未确认停止评测", 502)
            self._collection_stopped(run_id, result)
            def update(row):
                status = result["status"]
                if row.get("evaluation_control_terminal") and status not in TERMINAL:
                    return
                row.update(status=status, remote_status=status, control_error=None,
                           evaluation_control_terminal=status in TERMINAL, updated_at=utc_now())
                if "preparation" in result:
                    row["preparation"] = result["preparation"]
            self.records.update("model_iter_runs", run_id, update)
            self.sync_policy.reset("evaluation", run_id)
            self.sync_policy.observe_remote("evaluation",run_id,result["status"])
        except Exception as exc:
            self._control_failure("model_iter_runs", run_id, exc)

    def cancel_evaluation(self, run_id):
        run_id = _identifier(run_id, "评估运行编号")
        run = self.records.get("model_iter_runs", run_id)
        if not run:
            raise PhoneFactoryError("评估运行不存在", 404)
        def intent(row):
            if row.get("request", {}).get("offline_evaluation_jobs_version") == 1 and not row.get("stop_requested_at"):
                row["evaluation_control_epoch"] = row.get("evaluation_control_epoch", 0) + 1
                row.pop("pending_preparation_control", None)
            row.update(stop_requested_at=row.get("stop_requested_at") or utc_now(), control_error=None)
            if (row.get("request", {}).get("offline_evaluation_jobs_version") == 1
                    and not row.get("dispatch_confirmed") and (not row.get("dispatch_attempted")
                    or row.get("dispatch_rejected") and not row.get("dispatch_uncertain"))):
                row.update(status="cancelled", evaluation_control_terminal=True,
                           preparation={**row.get("preparation", {}), "status": "cancelled", "phase": "cancelled"})
            if not row.get("evaluation_control_terminal"):
                row["status"] = "stopping"
        saved = self.records.update("model_iter_runs", run_id, intent)
        collection = self.store.collection_runs.get(run_id)
        if collection and is_batch_active(collection["batch_id"], self.root):
            self.records.update("collection_runs", run_id, lambda row: row.update(
                stop_requested_at=row.get("stop_requested_at") or utc_now(), can_cancel_collection=True))
        self._control_pool.submit("evaluation:" + run_id, lambda: self._cancel_evaluation_remote(run_id))
        return {**saved, "ok": True, "stop_accepted": True}

    def _cancel_collection_remote(self, run_id):
        from .collector_storage import unavailable
        if unavailable(self.records, run_id):
            return
        run = self.store.collection_runs.get(run_id)
        if not run or not run.get("stop_requested_at"):
            return
        metadata = run.get("request", {}).get("metadata", {})
        self._control_attempt("collection_runs", run_id, run)
        try:
            remote = self.get_run(run_id)
            if (not isinstance(remote, dict) or remote.get("run_id", remote.get("collection_run_id")) != run_id
                    or remote.get("run_mode") != "generate"
                    or remote.get("agent_type", "newagent") != metadata.get("agent_type", "newagent")):
                raise PhoneFactoryError("远端采集运行身份或引擎不匹配，未请求停止", 502)
            # Even a terminal record must reconcile tracked workers; do not infer
            # device release from artifact closure or from a stale status string.
            result = self.store._remote_result(["cancel-run", run_id])
            if (not isinstance(result, dict) or result.get("ok") is False
                    or result.get("run_id", result.get("collection_run_id")) != run_id
                    or result.get("status") not in TERMINAL | {"stopping", "needs_attention"}):
                raise PhoneFactoryError("采集电脑尚未确认停止，请稍后查看对账状态", 502)
            self._collection_stopped(run_id, result)
        except Exception as exc:
            # A 404 is not proof of absence after uncertain dispatch. Keep the
            # durable stop intent so a late accepted request is also cancelled.
            self._control_failure("collection_runs", run_id, exc)

    def cancel_collection(self, run_id):
        run_id = _identifier(run_id, "采集运行编号")
        run = self.store.collection_runs.get(run_id)
        if run is None:
            raise PhoneFactoryError("采集运行不存在", 404)
        if self.records.get("collection_resume_heads",run_id):
            return self.resume_service.cancel(run_id)
        metadata = run.get("request", {}).get("metadata", {})
        if metadata.get("result_transport") == "legacy":
            raise PhoneFactoryError("旧采集服务不支持按运行停止，请升级采集电脑", 409)
        # Short intent transaction only: no remote request, hashing, or worker
        # termination under the publication/dispatch locks.
        with active_batch_lock(run["batch_id"], self.root):
            from .pipeline_access import ensure_pipeline_write
            ensure_pipeline_write(run["batch_id"], self.root, action="collect")
            def intent(current):
                current.update(stop_requested_at=current.get("stop_requested_at") or utc_now(),
                               can_cancel_collection=True, updated_at=utc_now())
                if not current.get("control_terminal"):
                    current["collection_status"] = "stopping"
            saved = self.records.update("collection_runs", run_id, intent)
        self._control_pool.submit(run_id, lambda: self._cancel_collection_remote(run_id))
        return {**saved, "ok": True, "stop_accepted": True,
                "agent_type": metadata.get("agent_type", "newagent")}

    def dispatch(self, action, method, data, mode):
        parts = action.split("/")
        if parts[0] == "storage":
            return True, self.storage_service.dispatch(parts, method, data)
        if len(parts) >= 3 and parts[0] == "collection-runs" and parts[2] in {
                "diagnostics", "cases", "resume-preview", "resume", "resume-operations", "phones", "phone-control-operations"}:
            return True, self.resume_service.dispatch(parts, method, data, mode)
        if method == "GET" and action == "agent-capabilities":
            return True, self.agent_capabilities()
        if mode == "modeliter" and action == "evaluation-config":
            if method == "GET":
                return True, model_iteration.get_config(self.store)
            if method == "PUT":
                return True, model_iteration.save_config(self.store, data)
        if mode == "modeliter" and action == "evaluation-capabilities" and method == "GET":
            return True, model_iteration.capabilities(self.store)
        if mode == "modeliter":
            if action == "evaluation-profiles" and method == "GET":
                return True, model_iteration.get_profiles(self.store)
            if action == "evaluation-sources" and method == "GET":
                return True, model_iteration.get_sources(self.store)
            if len(parts) == 3 and parts[0] == "evaluation-previews" and method == "POST" and parts[2] in {"cancel", "retry"}:
                from .evaluation_job_previews import control
                return True, control(self.store, parts[1], parts[2], data)
            if len(parts) == 2 and parts[0] == "evaluation-previews" and method == "GET":
                from .evaluation_previews import state
                return True, state(self.store, action.split("/")[1], data.get("offset", 0), data.get("limit", 50))
            if action == "evaluation-preview" and method == "POST":
                return True, model_iteration.preview_source(self.store, data)
            if action == "remote/start-evaluation" and method == "POST":
                return True, self.remote_start_evaluation(data)
        if method == "GET" and action in {"runs", "collection-runs"}:
            runs = self.run_views(mode)
            if mode == "generate" and not data.get("batch_id"):
                runs = [run for run in runs if run.get("source_kind") != "rollout_import"]
            if mode == "generate" and data.get("include_published") != "true":
                runs = [run for run in runs if is_batch_active(run["batch_id"], self.root)]
            if data.get("batch_id"):
                runs = [run for run in runs if run.get("batch_id") == data["batch_id"]]
            if data.get("task_source_batch_id"):
                runs = [run for run in runs if run.get("task_source_batch_id", run.get("batch_id")) == data["task_source_batch_id"]]
            return True, {"runs": runs}
        if method == "GET" and action.startswith("collection-runs/"):
            parts = action.split("/")
            if len(parts) >= 3 and parts[2] == "artifacts":
                from .collection_evidence_api import dispatch
                return True, dispatch(self, parts, data, mode)
        if action.startswith("runs/") and mode == "modeliter":
            parts = action.split("/")
            if method == "POST" and len(parts) == 3 and parts[2] == "retry-preparation":
                from .offline_evaluation_dispatch import retry
                return True, retry(self, _identifier(parts[1], "评估运行编号"), data)
            if method == "GET" and len(parts) == 2:
                run = next((run for run in self.run_views(mode) if run["run_id"] == parts[1]), None)
                if run is None:
                    raise PhoneFactoryError("评估运行不存在", 404)
                return True, run
            if method == "POST" and len(parts) == 3 and parts[2] == "retry-transfer":
                run_id = _identifier(parts[1], "评估运行编号")
                evaluation = self.records.get("model_iter_runs", run_id)
                if not evaluation or not evaluation.get("batch_id") or evaluation.get("request", {}).get("execution_mode") == "evaluate_only":
                    raise PhoneFactoryError("此评测运行没有关联的采集回传", 409)
                self.retry_collection_transfer(run_id)
                return True, next(run for run in self.run_views(mode) if run["run_id"] == run_id)
            if method == "POST" and len(parts) == 3 and parts[2] == "sync":
                saved = self.sync_evaluation(parts[1], force=True)
                return True, {**saved, "sync": self.sync_policy.view("evaluation", parts[1])}
            if method == "POST" and len(parts) == 3 and parts[2] == "cancel":
                return True, self.cancel_evaluation(parts[1])
            if method == "GET" and len(parts) == 3 and parts[2] == "evaluations":
                run_id = _identifier(parts[1], "评估运行编号")
                run = self.records.get("model_iter_runs", run_id)
                if run is None:
                    raise PhoneFactoryError("评估运行不存在", 404)
                if not run.get("request", {}).get("evaluator_config"):
                    raise PhoneFactoryError("历史运行不包含逐用例评测，请下载历史报告", 409)
                snapshot = self.records.get("model_iter_results", run_id)
                source_label=self._evaluation_snapshot_label(run)
                if (run.get("request", {}).get("offline_evaluation_jobs_version") == 1
                        or run.get("request", {}).get("execution_mode") == "evaluate_only" and run.get("preparation")):
                    # GET reads cached state only. The background synchronizer
                    # fetches progress/results without consuming proxy timeouts.
                    from .offline_evaluation_dispatch import dispatch as dispatch_offline
                    self._evaluation_pool.submit(run_id, lambda: self._background_evaluation(run_id)
                        if run.get("dispatch_confirmed") or run.get("request", {}).get("offline_evaluation_jobs_version") != 1
                        else dispatch_offline(self, run_id))
                    result = (snapshot or {}).get("result") or {"run_id": run_id, "execution_mode": "evaluate_only",
                        "status": run["status"], "cases": [], "reports": [],
                        "summary": {**{key: 0 for key in model_iteration.COUNTS},
                                    "total": run["request"].get("source_count", len(run["request"].get("source_cases", []))), "success_rate": None}}
                    return True, {**result, "status": run["status"], "preparation": run.get("preparation"),
                                  "preparation_control_error": run.get("preparation_control_error"),
                                  "preparation_control_pending": bool(run.get("pending_preparation_control")), **source_label}
                if snapshot and snapshot.get("final"):
                    return True, {**snapshot["result"],**source_label}
                try:
                    self.sync_evaluation(run_id, force=False)
                    current_snapshot = self.records.get("model_iter_results", run_id)
                    if not current_snapshot or not current_snapshot.get("result"):
                        raise PhoneFactoryError("评测结果正在等待同步，请稍后重试", 503)
                    return True, {**current_snapshot["result"],**source_label}
                except PhoneFactoryError as exc:
                    snapshot = self.records.get("model_iter_results", run_id) or snapshot
                    if snapshot:
                        info = self.sync_policy.view("evaluation", run_id)
                        stale = bool(info.get("last_error") or info.get("state") == "paused")
                        return True, {**snapshot["result"], **({"stale": True, "sync_error": str(exc)} if stale else {}),**source_label}
                    raise
        if action.startswith("collection-runs/") and mode == "modeliter":
            parts = action.split("/")
            evaluation = self.records.get("model_iter_runs", _identifier(parts[1], "评估运行编号"))
            if not evaluation or not evaluation.get("batch_id"):
                raise PhoneFactoryError("此评测运行没有关联的采集回传", 409)
            if method == "GET" and len(parts) == 2:
                return True, self.store.collection_runs.get(parts[1])
            if method == "POST" and len(parts) == 3 and parts[2] == "sync":
                saved = self.retry_collection_transfer(parts[1])
                return True, {**saved, "sync": self.sync_policy.view("collection", parts[1])}
            raise PhoneFactoryError("评测页只允许查看或重试关联采集的回传", 409)
        if action.startswith("collection-runs/") and method == "POST":
            parts = action.split("/")
            if len(parts) == 3 and parts[2] == "cancel":
                return True, self.cancel_collection(parts[1])
            if len(parts) == 3 and parts[2] == "sync":
                saved = self.retry_collection_transfer(parts[1])
                return True, {**saved, "sync": self.sync_policy.view("collection", parts[1])}
        if method == "GET" and action == "batches":
            return True, {"batches": self.batches(batch_role=data.get("batch_role", "source"),
                include_published=data.get("include_published") == "true")}
        if method == "GET" and action.startswith("batches/"):
            parts = action.split("/")
            batch_id = _identifier(parts[1], "采集批次编号")
            batch = self.store.collection_runs.batch(batch_id, require_workbook=True)
            if len(parts) == 2:
                return True, batch
            if len(parts) == 3 and parts[2] == "workbook":
                path = Path(batch.get("workbook_path") or self.root / "system" / "task_generation" / "collection_batches" / batch_id / batch["filename"])
                return True, FileResponse(path, filename=batch["filename"], headers={"Cache-Control": "no-store"})
        if method == "POST" and action.startswith("remote/"):
            command = action.split("/")[1]
            if command == "status":
                phones = data.get("phones", [])
                if not isinstance(phones, list) or any(not isinstance(item, str) for item in phones):
                    raise PhoneFactoryError("phones 必须为手机编号列表")
                return True, self.store._remote_result(["status", json.dumps(phones)])
            if command in {"adb-devices", "capabilities"}:
                return True, self.store._remote_result([command])
            if command == "monitor":
                phone = _text(data.get("phone_id"))
                if not phone:
                    raise PhoneFactoryError("手机编号不能为空")
                return True, self.store._remote_result(["monitor", phone])
            if command == "del-phone":
                return True, self._delete_phone(_text(data.get("phone_id")), mode)
        if method == "GET" and action == "reports":
            reports=self.store._remote_result(["reports", "--runmode", mode])
            for folder in reports.get("folders",[]):
                if folder.get("run_id"):
                    try:
                        folder.update(self._evaluation_snapshot_label(self.records.get("model_iter_runs",folder["run_id"])))
                    except (OSError,sqlite3.Error):
                        folder["collection_snapshot_status_unavailable"]=True
            return True,reports
        if method == "GET" and action == "report-download":
            args = ["report-download", "--runmode", mode]
            for field, flag in (("run_id", "--run-id"), ("file_id", "--file-id"), ("name", "--name"), ("folder", "--folder")):
                if data.get(field):
                    args += [flag, str(data[field])]
            if not (data.get("run_id") and data.get("file_id")) and not (data.get("folder") and data.get("name")):
                raise PhoneFactoryError("请指定报告运行及文件编号")
            directory = self.root / "tmp" / "phone_factory_reports"
            directory.mkdir(parents=True, exist_ok=True)
            name = _filename(data.get("name") or "evaluation-report.xlsx")
            if data.get("run_id") and data.get("file_id") and not data.get("name"):
                folders = self.store._remote_result(["reports", "--runmode", mode]).get("folders", [])
                matching = [file for folder in folders if folder.get("run_id") == data["run_id"]
                            for file in folder.get("files", []) if file.get("file_id", file.get("id")) == data["file_id"]]
                if matching:
                    name = _filename(matching[0]["name"])
            destination = directory / (uuid.uuid4().hex + Path(name).suffix)
            try:
                result = self.store._remote_result(args + ["--out", str(destination)])
                if result.get("ok") is False or not destination.is_file():
                    raise PhoneFactoryError("报告下载失败", 502)
            except BaseException:
                destination.unlink(missing_ok=True)
                raise
            return True, FileResponse(destination, filename=name, media_type=mimetypes.guess_type(name)[0] or "application/octet-stream", headers={"Cache-Control": "no-store"}, background=BackgroundTask(destination.unlink, missing_ok=True))
        return False, None
