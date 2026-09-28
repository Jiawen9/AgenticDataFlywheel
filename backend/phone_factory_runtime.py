"""Platform orchestration; device execution belongs to the independent collector."""
from __future__ import annotations
import hashlib
import json
import logging
import math
import tempfile
import threading
import uuid
from pathlib import Path
from urllib.parse import urlsplit
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask
from .batch_lifecycle import BatchPublishedError, ensure_batch_active, is_batch_active
from .batch_operations import active_batch_lock
from .collection_runs import _identifier
from .data_store import RevisionConflict
from .data_store.locking import batch_lock
from .data_store.registry import utc_now
from .manual_collection import ManualCollectionStore
from .phone_factory import DEFAULT_CONFIG, PhoneFactoryError, _atomic_write, _filename, _text
from .task_generation.collection_batches import payload_digest

TERMINAL = {"completed", "succeeded", "partial", "failed", "interrupted", "cancelled"}

class FactoryRuntime:
    def __init__(self, store):
        self.store, self.root, self.records = store, store.root, store.records
        self.manual = ManualCollectionStore(self.root)
        self._transfer = None
        self._stop = threading.Event()
        self._thread = None

    @property
    def transfer(self):
        if self._transfer is None:
            from .collection_transfer import CollectionTransferManager
            self._transfer = CollectionTransferManager(self.root, self.store.collection_runs, self)
        return self._transfer

    def get_run(self, run_id):
        return self.store._remote_result(["run", run_id])

    def download_archive(self, run_id, destination):
        self.store._remote_result(["run-archive", run_id, "--out", str(destination)])

    def start(self):
        self.transfer.start()
        if self._thread is None or not self._thread.is_alive():
            self._stop.clear()
            self._thread = threading.Thread(target=self._poll_evaluations, daemon=True, name="model-iteration-sync")
            self._thread.start()

    def close(self):
        self._stop.set()
        if self._transfer is not None:
            self._transfer.close()
        if self._thread:
            self._thread.join(timeout=2)

    def _poll_evaluations(self):
        while not self._stop.wait(10):
            for run in self.records.list("model_iter_runs"):
                if self._stop.is_set():
                    return
                if run["status"] in TERMINAL and not (run["status"] == "failed" and run.get("dispatch_error")):
                    continue
                try:
                    self.sync_evaluation(run["run_id"])
                except Exception:
                    logging.getLogger(__name__).warning("评估状态查询失败: %s", run["run_id"], exc_info=True)

    def sync_evaluation(self, run_id):
        run = self.records.get("model_iter_runs", _identifier(run_id, "评估运行编号"))
        if run is None:
            raise PhoneFactoryError("评估运行不存在", 404)
        remote = self.get_run(run_id)
        if remote.get("run_id", remote.get("collection_run_id")) != run_id or remote.get("run_mode") != "modeliter":
            raise PhoneFactoryError("远端评估运行身份不匹配", 502)
        if remote.get("status") not in TERMINAL | {"queued", "running"}:
            raise PhoneFactoryError("远端评估运行状态无效", 502)
        def update(current):
            if current["status"] not in TERMINAL or (current["status"] == "failed" and current.get("dispatch_error")):
                current.update(status=remote["status"], errors=remote.get("errors", []), remote_status=remote["status"], updated_at=utc_now(), dispatch_error=None)
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

    def _task(self, filename, mode):
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
        if task.get("source_batch_id"):
            ensure_batch_active(task["source_batch_id"], self.root)
        path = self.store.task_path(task["filename"])
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

    def _options(self, data, mode):
        if data.get("run_mode", mode) != mode:
            raise PhoneFactoryError("运行模式与接口不匹配", 409)
        state = self.store._current()
        config = data.get("config", {})
        if not isinstance(config, dict) or set(config) - set(DEFAULT_CONFIG):
            raise PhoneFactoryError("本次采样配置无效")
        config = {**DEFAULT_CONFIG, **state.get("config", {}), **config}
        for key in ("sampling_enabled", "use_experience_lib"):
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
        return {"run_mode": mode, "result_transport": "http", "vla": vla, "config": config, "phone_id": phone, "app": app, "phone_apps": associations, "phone_ids": sorted({row["phone_id"] for row in associations})}

    def _capabilities(self):
        try:
            value = self.store._remote_result(["capabilities"])
        except PhoneFactoryError as exc:
            raise PhoneFactoryError("采集服务无法确认回传协议，请检查连接并升级为支持批次结果回传的服务", 502) from exc
        if value.get("protocol_version") != 1 or value.get("batch_results") is not True:
            raise PhoneFactoryError("采集服务不支持批次结果回传，请升级采集服务后再运行", 409)

    def remote_start(self, data, mode):
        task, path, content = self._task(_filename(data.get("filename")), mode)
        options = self._options(data, mode)
        request_id = data.get("request_id") or uuid.uuid4().hex
        _identifier(request_id, "request_id")
        digest = hashlib.sha256(content).hexdigest()
        metadata = {**options, "filename": task["filename"]}
        if mode == "generate":
            run, created = self.store.collection_runs.create(task["source_batch_id"], dispatch_key=request_id, metadata=metadata, workbook_sha256=digest)
            run_id = run["collection_run_id"]
        else:
            run_id = "eval_" + payload_digest(request_id)
            request = {**metadata, "workbook_sha256": digest}
            payload = {"run_id": run_id, "collection_run_id": run_id, "run_mode": "modeliter", "status": "dispatching", "filename": task["filename"], "vla": options["vla"], "created_at": utc_now(), "request": request, "request_digest": payload_digest(request), "errors": [], "dispatch_error": None}
            try:
                run = self.records.put("model_iter_runs", run_id, payload, expected_revision=0)
                created = True
            except RevisionConflict:
                run = self.records.get("model_iter_runs", run_id)
                if run["request_digest"] != payload["request_digest"]:
                    raise PhoneFactoryError("相同 request_id 对应的运行参数不同", 409)
                created = False
        if not created and (run["status"] not in {"failed", "dispatching"} or (run["status"] == "failed" and not run.get("dispatch_error"))):
            return {"ok": True, "reused": True, **run}
        with batch_lock(self.root, "dispatch_" + run_id):
            current = self.store.collection_runs.get(run_id) if mode == "generate" else self.records.get("model_iter_runs", run_id)
            if current["status"] not in {"failed", "dispatching"}:
                return {"ok": True, "reused": True, **current}
            try:
                if not created:
                    # An acknowledgement can be lost after remote acceptance. Query that
                    # durable identity before considering another idempotent submission.
                    try:
                        known = self.get_run(run_id)
                    except PhoneFactoryError:
                        known = {}
                    if (known.get("run_id", known.get("collection_run_id")) == run_id
                            and known.get("run_mode") == mode
                            and known.get("status") in TERMINAL | {"queued", "running"}):
                        if mode == "generate":
                            saved = self.store.collection_runs.dispatched(run_id, known)
                        else:
                            def recovered(item):
                                if item["status"] not in {"interrupted", "cancelled", "succeeded", "partial", "completed"}:
                                    item.update(status=known["status"], errors=known.get("errors", []), dispatch_error=None, updated_at=utc_now())
                            saved = self.records.update("model_iter_runs", run_id, recovered)
                        return {**known, **saved, "ok": True, "reused": True}
                self._capabilities()
                if mode == "generate":
                    with active_batch_lock(task["source_batch_id"], self.root):
                        if current["status"] == "failed":
                            self.store.collection_runs.retry_dispatch(run_id)
                    tasks = list(current["batch_tasks"].values())
                else:
                    _, tasks, _ = self.manual._parse(content, run_id)
                temp_root = self.root / "tmp" / "phone_factory"
                temp_root.mkdir(parents=True, exist_ok=True)
                with tempfile.TemporaryDirectory(dir=temp_root) as temporary:
                    directory = Path(temporary)
                    apps_path, manifest_path = directory / "apps.json", directory / "tasks.json"
                    apps_path.write_text(json.dumps(options["phone_apps"], ensure_ascii=False), encoding="utf-8")
                    manifest_path.write_text(json.dumps({"tasks": tasks}, ensure_ascii=False), encoding="utf-8")
                    args = ["start-run", str(path), str(apps_path), options["phone_id"], options["app"], "--vla", options["vla"], "--runmode", mode, "--collection-run-id", run_id, "--task-manifest", str(manifest_path), "--temperature", str(options["config"]["temperature"]), "--top-p", str(options["config"]["top_p"])]
                    if mode == "generate":
                        args += ["--batch-id", current["batch_id"], "--output-dir", current["output_dir"]]
                    if options["config"]["sampling_enabled"]:
                        args.append("--sampling")
                    if options["config"]["use_experience_lib"]:
                        args.append("--exp")
                    if mode == "generate":
                        with active_batch_lock(task["source_batch_id"], self.root):
                            self.records.update("collection_runs", run_id,
                                lambda item: item.update(dispatch_attempted=True))
                    response = self.store._remote_result(args)
                    if response.get("ok") is False:
                        raise PhoneFactoryError(response.get("error") or response.get("message") or "远端未接受运行", 502)
                if mode == "generate":
                    saved = self.store.collection_runs.dispatched(run_id, response)
                else:
                    def accepted(item):
                        if item["status"] not in {"interrupted", "cancelled", "succeeded", "partial", "completed"}:
                            item.update(status="running", dispatch_response=response, dispatch_error=None, updated_at=utc_now())
                    saved = self.records.update("model_iter_runs", run_id, accepted)
                return {**response, **saved, "ok": True}
            except Exception as exc:
                if mode == "generate":
                    self.store.collection_runs.dispatch_failed(run_id, str(exc))
                else:
                    def failed(item):
                        if item["status"] not in {"interrupted", "cancelled", "succeeded", "partial", "completed"}:
                            item.update(status="failed", dispatch_error=str(exc))
                    self.records.update("model_iter_runs", run_id, failed)
                raise

    def run_views(self, mode):
        if mode == "modeliter":
            return sorted(self.records.list("model_iter_runs"), key=lambda item: (item["created_at"], item["run_id"]))
        views = []
        for run in self.store.collection_runs.list_runs():
            transfer = self.records.get("collection_transfers", run["collection_run_id"]) or {}
            metadata = run.get("request", {}).get("metadata", {})
            views.append({**run, "filename": metadata.get("filename"), "vla": metadata.get("vla"), **{key: transfer[key] for key in ("remote_status", "transfer_status", "transfer_error", "remote_errors", "transfer_updated_at") if key in transfer}, "trajectory_count": len(run.get("trajectories", []))})
        return views

    def public_state(self, state, mode):
        statuses = {"dispatching": "下发中", "queued": "排队中", "running": "运行中", "completed": "已回传", "succeeded": "已完成", "partial": "部分完成", "failed": "失败", "interrupted": "已中断", "cancelled": "已中断"}
        runs = self.run_views(mode)
        for task in state["tasks"]:
            matching = [run for run in runs if run.get("filename") == task["filename"]]
            if matching:
                # A task can run on several phones; one phone finishing must not
                # hide another phone's active run, regardless of dispatch order.
                active = [run for run in matching if run["status"] in {"dispatching", "queued", "running"}]
                live = [run for run in active if not run.get("transfer_error")]
                latest = (live or active or matching)[-1]
                task["status"] = "回传失败" if latest.get("transfer_error") else statuses.get(latest["status"], latest["status"])
        return state

    def batches(self):
        ids = {item["batch_id"] for item in self.manual.list()}
        base = self.root / "system" / "task_generation" / "collection_batches"
        if base.is_dir():
            ids.update(path.parent.name for path in base.glob("*/batch.json"))
        items = []
        for identifier in ids:
            if is_batch_active(identifier, self.root):
                batch = self.store.collection_runs.batch(identifier)
                items.append({**batch, "download_url": f"/api/phone-factory/batches/{identifier}/workbook"})
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
                            current.update(status="interrupted", dispatch_error="设备已删除，运行已中断", interrupted_at=utc_now())
                    try:
                        with active_batch_lock(run.get("batch_id") if namespace == "collection_runs" else None, self.root):
                            self.records.update(namespace, run.get("run_id", run.get("collection_run_id")), interrupt)
                    except BatchPublishedError:
                        pass  # Finished, published artifacts and logs remain unchanged.
        def remove(state):
            state["phones"] = [item for item in state["phones"] if item != phone]
            state["phoneApps"] = [item for item in state["phoneApps"] if item["phone_id"] != phone]
        self.store._update(remove)
        return {**response, "state": self.store.state(mode)}

    def dispatch(self, action, method, data, mode):
        if method == "GET" and action in {"runs", "collection-runs"}:
            runs = self.run_views(mode)
            if mode == "generate" and not data.get("batch_id"):
                runs = [run for run in runs if run.get("source_kind") != "rollout_import"]
            if mode == "generate" and data.get("include_published") != "true":
                runs = [run for run in runs if is_batch_active(run["batch_id"], self.root)]
            if data.get("batch_id"):
                runs = [run for run in runs if run.get("batch_id") == data["batch_id"]]
            return True, {"runs": runs}
        if action.startswith("runs/") and mode == "modeliter":
            parts = action.split("/")
            if method == "POST" and len(parts) == 3 and parts[2] == "sync":
                return True, self.sync_evaluation(parts[1])
        if action.startswith("collection-runs/") and mode == "modeliter":
            raise PhoneFactoryError("评估结果不进入生产采集链", 409)
        if action.startswith("collection-runs/") and method == "POST":
            parts = action.split("/")
            if len(parts) == 3 and parts[2] == "sync":
                return True, self.transfer.sync(parts[1])
        if method == "GET" and action == "batches":
            return True, {"batches": self.batches()}
        if method == "GET" and action.startswith("batches/"):
            parts = action.split("/")
            batch_id = _identifier(parts[1], "采集批次编号")
            ensure_batch_active(batch_id, self.root)
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
            return True, self.store._remote_result(["reports", "--runmode", mode])
        if method == "GET" and action == "report-download":
            args = ["report-download", "--runmode", mode]
            for field, flag in (("run_id", "--run-id"), ("file_id", "--file-id"), ("name", "--name"), ("folder", "--folder")):
                if data.get(field):
                    args += [flag, str(data[field])]
            if not (data.get("run_id") and data.get("file_id")) and not (data.get("folder") and data.get("name")):
                raise PhoneFactoryError("请指定报告运行及文件编号")
            directory = self.root / "tmp" / "phone_factory_reports"
            directory.mkdir(parents=True, exist_ok=True)
            destination = directory / (uuid.uuid4().hex + ".xlsx")
            try:
                result = self.store._remote_result(args + ["--out", str(destination)])
                if result.get("ok") is False or not destination.is_file():
                    raise PhoneFactoryError("报告下载失败", 502)
            except BaseException:
                destination.unlink(missing_ok=True)
                raise
            return True, FileResponse(destination, filename=_filename(data.get("name") or "evaluation-report.xlsx"), headers={"Cache-Control": "no-store"}, background=BackgroundTask(destination.unlink, missing_ok=True))
        return False, None
