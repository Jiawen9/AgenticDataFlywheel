"""Logical-run diagnostics and exactly-once manual continuation coordination.

The HTTP read surface never dispatches work. Remote mutation starts only after an
immutable request and epoch barrier have committed together to the local store.
"""
from __future__ import annotations

import copy
import json
import re
from pathlib import PureWindowsPath, PurePosixPath
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone

from .batch_operations import active_batch_lock
from .batch_lifecycle import ensure_batch_active
from .collection_artifact_transfer import ArtifactTransfer
from .collection_execution_transfer import ExecutionTransfer
from .collection_receipts import ReceiptStore
from .collection_resume_projection import PlanProjection
from .collection_runs import _identifier, CollectionRunError
from .collection_phone_control import PhoneControlService
from .collection_result_corrections import ResultCorrections
from .data_store import RevisionConflict
from .data_store.locking import batch_lock
from .data_store.registry import utc_now
from .phone_factory import PhoneFactoryError
from .remote_sync import BoundedSyncPool
from .task_generation.collection_batches import payload_digest


class ResumeService:
    def __init__(self, runtime):
        self.runtime, self.records, self.root = runtime, runtime.records, runtime.root
        self.pool = BoundedSyncPool("collection-diagnostics-resume", max_workers=2)
        self.executions = ExecutionTransfer(self.root, runtime)
        self.cache, self.run_cache, self.errors, self.next_due = {}, {}, {}, {}
        self.persistence_errors = {}
        self.execution_roots = {}
        self.phone_control = PhoneControlService(self)
        self.result_corrections = ResultCorrections(self)
        self.guard = threading.RLock()

    def close(self):
        self.phone_control.close()
        self.pool.close()
        self.executions.close()

    def _remote(self, command, run_id, body=None, extra=()):
        from .collector_storage import require_available
        require_available(self.records, run_id)
        args = [command, run_id, *extra]
        if body is not None:
            args.extend(["--payload", json.dumps(body, ensure_ascii=False)])
        try:
            return self.runtime.store._remote_result(args)
        except PhoneFactoryError as exc:
            if exc.remote_status in {400,404,405,409,422}:
                raise PhoneFactoryError(str(exc),exc.remote_status,remote_kind=exc.remote_kind,
                    remote_status=exc.remote_status,remote_command=command) from exc
            raise

    def _read(self, namespace, key):
        """Short read-only fallback: a locked/full disk cannot stall diagnostics."""
        path = self.records.database_path
        if not path.exists():
            return None
        db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=.15)
        try:
            row = db.execute("SELECT payload FROM records WHERE namespace=? AND record_key=?", (namespace, key)).fetchone()
        finally:
            db.close()
        return json.loads(row[0]) if row else None

    def _run(self, run_id, mode="generate", *, read=False):
        from .collector_storage import require_available
        require_available(self.records, run_id)
        _identifier(run_id, "采集运行编号")
        run = self.run_cache.get(run_id) if read else None
        if run is None:
            run = self._read("collection_runs", run_id) if read else self.runtime.store.collection_runs.get(run_id)
        if not run or run.get("request", {}).get("metadata", {}).get("run_mode", "generate") != mode:
            raise PhoneFactoryError("采集运行不存在", 404)
        self.run_cache[run_id] = run
        return run

    @staticmethod
    def _eligible(run):
        metadata = run.get("request", {}).get("metadata", {})
        return metadata.get("run_mode", "generate") == "generate" and metadata.get("collection_artifacts_version") == 2

    def schedule(self):
        from .collector_storage import unavailable
        self.phone_control.schedule()
        try:
            runs = self.runtime.store.collection_runs.list_runs()
        except (OSError,sqlite3.Error):
            runs = list(self.run_cache.values())
        # Recent active runs first; never create one thread per device or per case.
        for run in sorted(runs, key=lambda r: self.next_due.get(r["collection_run_id"], 0)):
            run_id = run["collection_run_id"]
            if unavailable(self.records, run_id):
                continue
            self.run_cache[run_id] = run
            if time.monotonic() < self.next_due.get(run_id, 0):
                continue
            if not self.pool.available:
                break
            self.next_due[run_id] = time.monotonic() + 5
            self.pool.submit(run_id, lambda r=run: self.refresh(r))

    def refresh(self, run):
        run_id = run["collection_run_id"]
        from .collector_storage import unavailable
        if unavailable(self.records, run_id):
            return
        try:
            value = self._remote("run-diagnostics", run_id)
            if unavailable(self.records, run_id):
                return
            identity = value.get("root_run_id", value.get("collection_run_id", value.get("run_id")))
            if (identity != run_id or value.get("batch_id", run["batch_id"]) != run["batch_id"]
                    or not isinstance(value.get("phones"), list)
                    or any(not isinstance(p,dict) or not isinstance(p.get("phone_id"),str) for p in value["phones"])):
                raise ValueError("远端诊断身份或格式无效")
            value = {**value, "run_id": run_id, "batch_id": run["batch_id"], "supported": value.get("supported",True) is True,
                     "received_at":utc_now(),"observed_at": value.get("observed_at"),
                     "stale":bool(value.get("stale")) or not bool(value.get("observed_at"))}
            with self.guard:
                self.cache[run_id] = value
            try:
                self.records.put("collection_diagnostics", run_id, value)
                self.persistence_errors.pop(run_id,None)
            except (OSError, sqlite3.Error) as exc:
                self.persistence_errors[run_id] = type(exc).__name__
        except Exception as exc:
            unsupported = isinstance(exc, PhoneFactoryError) and exc.remote_status in {404, 405}
            with self.guard:
                old = self.cache.get(run_id, {})
                self.cache[run_id] = {**old, "run_id": run_id, "batch_id": run["batch_id"],
                    "supported": False if unsupported else old.get("supported", True),
                    "stale": True, "sync_error": str(exc)[:500], "phones": old.get("phones", [])}
            self.next_due[run_id] = time.monotonic() + 30
        if unavailable(self.records, run_id) or not self._eligible(run):
            return
        try:
            self._send_cancel(run_id)  # A durable stop must not wait for a projection write.
            plan = ArtifactTransfer._expected(run)
            projection = PlanProjection(self.root, run_id)
            projection.initialize(run, plan)
            ledger = ReceiptStore(self.records, run_id)
            self.result_corrections.observe(run, projection, run_id, 0, plan, ledger)
            self.result_corrections.sync(run, projection)
            self.result_corrections.observe(run, projection, run_id, 0, plan, ledger)
            head = self.records.get("collection_resume_heads", run_id)
            if head:
                self._recover_operations(run)
                self._sync_executions(run, projection)
            projection.observe_activity(self.cache.get(run_id,{}).get("phones",[]))
        except Exception as exc:
            self.errors[run_id] = str(exc)[:500]
            self.next_due[run_id] = time.monotonic() + 30

    def diagnostics(self, run_id, mode):
        try:
            run = self._run(run_id, mode, read=True)
        except (OSError,sqlite3.Error):
            return {"supported":True,"resume_supported":False,"run_id":run_id,"phones":[],"stale":True,
                    "persistence_error":"平台运行登记暂不可读，请检查数据盘及数据库状态"}
        value = copy.deepcopy(self.cache.get(run_id))
        if value is None:
            try:
                value = self._read("collection_diagnostics", run_id)
            except (OSError, sqlite3.Error):
                value = None
            value = {**(value or {}), "stale": True}
        value = {"supported": True, "run_id": run_id, "batch_id": run["batch_id"], "phones": [], **value}
        try:
            timestamp=value.get("observed_at")
            if not isinstance(timestamp,str):
                raise ValueError("observation unavailable")
            observed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            value["stale"] = value.get("stale",False) or (datetime.now(timezone.utc)-observed).total_seconds()>30
        except (TypeError,ValueError):
            value["stale"] = True
        if not value.get("phones"):
            value["phones"] = [{"phone_id": phone, **state, "phase": state.get("phase", state.get("status")),
                               "message": state.get("reason", "等待采集端诊断快照")}
                              for phone, state in (run.get("phone_states") or {}).items()]
        memory = self._pressure(run_id)
        try:
            value["receipt_storage"] = copy.deepcopy(memory) if memory is not None else self._read("collection_receipt_storage", run_id)
        except (OSError, sqlite3.Error):
            value["persistence_error"] = "平台存储状态暂不可读，显示最近诊断"
        if self.persistence_errors.get(run_id):
            value["persistence_error"] = "诊断尚未持久化：" + self.persistence_errors[run_id]
        if self.errors.get(run_id):
            value["resume_sync_error"] = self.errors[run_id]
        if self.runtime._control_memory.get(run_id, {}).get("error"):
            value["control_error"] = self.runtime._control_memory[run_id]["error"]
        phones = value["phones"]
        for phone in phones:
            phone.setdefault("message",phone.get("reason",""))
            phone.setdefault("model_role",(phone.get("model_call") or {}).get("role"))
            if "severity" not in phone:
                phone["severity"] = ("error" if phone.get("error_kind") or phone.get("status") in {"failed","needs_attention"}
                    else "warning" if phone.get("status")=="unknown" or phone.get("confidence")=="needs_confirmation" or phone.get("reason_code") in {"unknown","phone_busy","storage_unavailable","device_disconnected"}
                    else "info")
        value["summary"] = {"total":len(phones),"healthy":sum(p["severity"] in {"info","ok"} for p in phones),
            "warning":sum(p["severity"]=="warning" for p in phones),"error":sum(p["severity"]=="error" for p in phones)}
        value["resume_supported"] = self._eligible(run) and value.get("supported") is True
        return value

    def cases(self, run_id, mode, data, case=None):
        run = self._run(run_id, mode, read=True)
        if not self._eligible(run):
            return {"supported": False, "code": "resume_unsupported", "items": [], "total": 0}
        projection = PlanProjection(self.root, run_id)
        allowed = {k: data[k] for k in ("offset","limit","phone_id") if k in data}
        if case is not None:
            result = projection.instances(case, **allowed)
        else:
            allowed.update({k: data[k] for k in ("app","status","q") if k in data})
            result = projection.cases(**allowed)
        head = self._read("collection_resume_heads", run_id) or {}
        return {**result, "supported": True, "run_id": run_id, "batch_id": run["batch_id"], "revision": head.get("revision", 0)}

    def _pressure(self,run_id):
        states=[v for k,v in self.executions._receipt_storage_states.items()
                if self.execution_roots.get(k)==run_id and (v.get("paused") or v.get("pending"))]
        original=getattr(self.runtime.transfer,"_receipt_storage_states",{}).get(run_id)
        if original:
            states.append(original)
        if not states:
            return None
        return {"paused":any(v.get("paused") for v in states),"pending":any(v.get("pending") for v in states),
                "reason":"platform_storage_unavailable","error":next((v.get("error") for v in states if v.get("error")),None)}

    def preview(self, run_id, data):
        run = self._run(run_id)
        if not self._eligible(run):
            raise PhoneFactoryError("resume_unsupported：旧协议或评估采集不支持续采", 409)
        ensure_batch_active(run["batch_id"], self.root)
        batch = self.runtime.store.collection_runs.batch(run["batch_id"])
        if batch.get("collection_run_id") and batch["collection_run_id"] != run_id:
            raise PhoneFactoryError("续采运行不属于结果批次的冻结绑定",409)
        if not batch.get("collection_run_id") and len(self.runtime.store.collection_runs.list_runs(run["batch_id"])) != 1:
            raise PhoneFactoryError("此旧批次绑定多个采集运行，不能直接原地续采",409)
        if set(data) - {"phone_ids"}:
            raise PhoneFactoryError("续采预览仅接受手机范围")
        if "phone_ids" in data and (not isinstance(data["phone_ids"],list)
                or any(not isinstance(p,str) or not p for p in data["phone_ids"])):
            raise PhoneFactoryError("phone_ids 必须为手机编号数组")
        try:
            capability = self.runtime.store._remote_result(["capabilities"])
        except PhoneFactoryError as exc:
            if exc.remote_status in {404,405}:
                raise PhoneFactoryError("resume_unsupported：采集端未声明断点续采能力",409) from exc
            raise
        resume = capability.get("collection_resume") or {}
        if not isinstance(resume, dict) or resume.get("version") != 1:
            raise PhoneFactoryError("resume_unsupported：请升级采集端的断点续采能力", 409)
        result = self._remote("resume-preview", run_id, data if data.get("phone_ids") else {})
        if result.get("root_run_id", result.get("logical_run_id", run_id)) != run_id or result.get("batch_id", run["batch_id"]) != run["batch_id"]:
            raise PhoneFactoryError("续采预览运行身份不匹配", 502)
        preview_id = _identifier(result.get("preview_id"), "预览编号")
        if (not isinstance(result.get("instances"), list) or not isinstance(result.get("selection_digest"), str)
                or type(result.get("expected_revision")) is not int):
            raise PhoneFactoryError("续采预览缺少冻结选择或版本", 502)
        expected = ArtifactTransfer._expected(run)
        identities = [(v.get("phone_id"), v.get("collection_case_id"), v.get("repeat_index")) for v in result["instances"]]
        if len(set(identities)) != len(identities) or not set(identities).issubset(expected):
            raise PhoneFactoryError("续采预览实例不属于冻结计划", 502)
        saved = {"run_id": run_id, "batch_id": run["batch_id"], "preview": result, "created_at": utc_now()}
        old = self.records.get("collection_resume_previews", preview_id)
        if old and old.get("preview") != result:
            raise PhoneFactoryError("续采预览编号被复用于不同选择", 409)
        if not old:
            self.records.put("collection_resume_previews", preview_id, saved, expected_revision=0)
        return result

    def submit(self, run_id, data):
        run = self._run(run_id)
        if not self._eligible(run):
            raise PhoneFactoryError("resume_unsupported：此运行不支持续采", 409)
        allowed = {"request_id","preview_id","selection_digest","expected_revision","confirm_unknown_phone_ids","device_config_revision"}
        if set(data) - allowed:
            raise PhoneFactoryError("续采不允许修改原模型、任务或重复次数")
        revision = data.get("device_config_revision")
        if revision is not None:
            if (run.get("request",{}).get("metadata",{}).get("agent_type") != "pipelineagent"
                    or not isinstance(revision,dict) or not revision or set(revision)-{"adb_path","app_list"}):
                raise PhoneFactoryError("设备修订仅支持 Pipeline 的 ADB 路径和 App 映射")
            if "adb_path" in revision and (not isinstance(revision["adb_path"],str) or not
                    (PureWindowsPath(revision["adb_path"]).is_absolute() or PurePosixPath(revision["adb_path"]).is_absolute())):
                raise PhoneFactoryError("修订 ADB 路径必须是绝对路径")
            apps=revision.get("app_list")
            if "app_list" in revision and (not isinstance(apps,dict) or not apps or any(
                    not isinstance(name,str) or not name.strip() or not isinstance(package,str) or
                    not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)+",package)
                    for name,package in apps.items())):
                raise PhoneFactoryError("App 映射必须是名称到 Android 包名的 JSON 对象")
            capability=self.runtime.store._remote_result(["capabilities"])
            if capability.get("collection_resume",{}).get("device_config_revision") != 1:
                raise PhoneFactoryError("采集端尚不支持设备配置修订，请升级",409)
        operation_id = _identifier(data.get("request_id"), "续采请求编号")
        preview_id = _identifier(data.get("preview_id"), "预览编号")
        confirmations = data.get("confirm_unknown_phone_ids",[])
        if not isinstance(confirmations,list) or any(not isinstance(p,str) or not p for p in confirmations):
            raise PhoneFactoryError("未知手机确认必须为手机编号数组")
        if type(data.get("expected_revision")) is not int or data["expected_revision"] < 0:
            raise PhoneFactoryError("续采版本必须为非负整数")
        request = {**data, "confirm_unknown_phone_ids": sorted(set(data.get("confirm_unknown_phone_ids", [])))}
        signature = payload_digest([run_id, run["batch_id"], request])
        previous = self.records.get("collection_resume_operations",operation_id)
        if previous:
            if previous.get("request_digest") != signature:
                raise PhoneFactoryError("相同续采请求编号对应不同参数",409)
            return self._public_operation(previous)
        with active_batch_lock(run["batch_id"], self.root), batch_lock(self.root, "resume_" + run_id):
            old = self.records.get("collection_resume_operations", operation_id)
            if old:
                if old.get("request_digest") != signature:
                    raise PhoneFactoryError("相同续采请求编号对应不同参数", 409)
                return self._public_operation(old)
            frozen = self.records.get("collection_resume_previews", preview_id)
            if not frozen or frozen.get("run_id") != run_id or frozen.get("batch_id") != run["batch_id"]:
                raise PhoneFactoryError("请重新预览本次续采范围", 409)
            preview = frozen["preview"]
            if any(request.get(k) != preview.get(k) for k in ("selection_digest","expected_revision")):
                raise PhoneFactoryError("续采预览已变化，请重新预览", 409)
            required = set(preview.get("requires_unknown_phone_ids", []))
            if not required.issubset(request["confirm_unknown_phone_ids"]):
                raise PhoneFactoryError("请确认状态未知手机后再续采", 409)
            if not preview["instances"]:
                raise PhoneFactoryError("当前没有可以续采的实例", 409)
            if any(op.get("run_id")==run_id and op.get("status") in {"pending","uncertain","accepted","preparing","started"}
                   and op.get("request",{}).get("expected_revision")==request["expected_revision"]
                   for op in self.records.list("collection_resume_operations")):
                raise PhoneFactoryError("该执行版本已有续采请求，请等待其状态确认后重新预览",409)
            head = self.records.get("collection_resume_heads", run_id) or {"run_id": run_id, "batch_id": run["batch_id"], "revision": 0}
            saved = {"operation_id": operation_id, "run_id": run_id, "batch_id": run["batch_id"],
                     "request": request, "request_digest": signature, "status": "pending", "created_at": utc_now(),
                     "epoch": head["revision"] + 1, "instances": preview["instances"]}
            # Barrier and durable request are atomic; disk-full means no POST.
            self.records.put_many([
                {"namespace": "collection_resume_operations", "key": operation_id, "payload": saved, "expected_revision": 0},
                {"namespace": "collection_resume_heads", "key": run_id,
                 "payload": {**head,"revision": saved["epoch"],"active": True,"remote_revision":request["expected_revision"]},
                 "expected_revision": head.get("storage_revision", 0)}])
        self.pool.submit(run_id, lambda: self._send_operation(run, operation_id))
        return self._public_operation(saved)

    @staticmethod
    def _public_operation(value):
        return {k: v for k, v in value.items() if k not in {"request", "request_digest", "instances", "storage_revision"}}

    def operation(self, run_id, operation_id):
        saved = self._read("collection_resume_operations", _identifier(operation_id, "续采请求编号"))
        if not saved or saved["run_id"] != run_id:
            raise PhoneFactoryError("续采请求不存在", 404)
        return self._public_operation(saved)

    def _recover_operations(self, run):
        for op in self.records.list("collection_resume_operations"):
            if op["run_id"] != run["collection_run_id"]:
                continue
            if op["status"] in {"pending", "uncertain"}:
                self._send_operation(run, op["operation_id"])
            elif op["status"] in {"accepted","preparing","started"}:
                response = self._remote("resume-operation",run["collection_run_id"],extra=[op["operation_id"]])
                if response.get("operation_id")!=op["operation_id"] or response.get("root_run_id")!=op["run_id"]:
                    raise ValueError("续采操作状态身份不一致")
                status = response.get("status")
                if status not in {"accepted","preparing","started","failed","cancelled","completed"}:
                    raise ValueError("未知续采操作状态")
                error = response.get("error")
                if isinstance(error,dict):
                    error = str(error.get("code") or error.get("type") or "续采准备失败")
                self.records.update("collection_resume_operations",op["operation_id"],lambda v:v.update(
                    status=status,error=error,execution_id=response.get("execution_id",op.get("execution_id"))))

    def _send_operation(self, run, operation_id):
        with batch_lock(self.root, "resume_operation_" + operation_id):
            op = self.records.get("collection_resume_operations", operation_id)
            if not op or op["status"] not in {"pending", "uncertain"}:
                return
            try:
                response = self._remote("resume-collection", run["collection_run_id"], op["request"])
                if response.get("root_run_id", response.get("run_id")) != run["collection_run_id"] or response.get("operation_id", operation_id) != operation_id:
                    raise ValueError("续采接受响应身份不一致")
                self.records.update("collection_resume_operations", operation_id, lambda v: v.update(
                    status=response.get("status", "accepted"), execution_id=response.get("execution_id"),
                    phones=response.get("phones", []), error=None, accepted_at=utc_now()))
                self.next_due[run["collection_run_id"]] = 0
            except Exception as exc:
                rejected = False
                if isinstance(exc,PhoneFactoryError) and exc.remote_status in {400,409,422}:
                    try:
                        self._remote("resume-operation",run["collection_run_id"],extra=[operation_id])
                    except PhoneFactoryError as probe:
                        rejected = probe.remote_status == 404
                # A timeout never discards the epoch or creates another request.
                try:
                    self.records.update("collection_resume_operations", operation_id, lambda v: v.update(status="rejected" if rejected else "uncertain", error=str(exc)[:500]))
                except (OSError, sqlite3.Error):
                    self.errors[run["collection_run_id"]] = "续采请求已冻结，确认结果暂未持久化"

    def _register_execution(self, run, descriptor, ordinal):
        from .collector_storage import require_available
        require_available(self.records, run["collection_run_id"])
        execution_id = _identifier(descriptor.get("execution_id", descriptor.get("collection_run_id")), "执行分段编号")
        if execution_id == run["collection_run_id"]:
            if ordinal != 0:
                raise ValueError("初始执行分段次序发生变化")
            return run
        if descriptor.get("root_run_id") != run["collection_run_id"] or descriptor.get("batch_id", run["batch_id"]) != run["batch_id"]:
            raise ValueError("执行分段的逻辑运行身份不匹配")
        if descriptor.get("agent_type",run["request"]["metadata"].get("agent_type","newagent")) != run["request"]["metadata"].get("agent_type","newagent"):
            raise ValueError("执行分段引擎与冻结配置不一致")
        instances = descriptor.get("instances")
        if not isinstance(instances, list) or not instances:
            raise ValueError("执行分段缺少冻结实例")
        identities = [(v.get("phone_id"),v.get("collection_case_id"),v.get("repeat_index")) for v in instances]
        if len(set(identities)) != len(identities) or not set(identities).issubset(ArtifactTransfer._expected(run)):
            raise ValueError("执行分段包含计划外实例")
        signature = payload_digest([run["request_digest"], execution_id, instances])
        old = self.records.get("collection_executions", execution_id)
        if old:
            if old["request_digest"] != signature or old["root_run_id"] != run["collection_run_id"] or old["execution_ordinal"]!=ordinal:
                raise ValueError("执行分段身份或冻结选择发生变化")
            self.execution_roots[execution_id]=run["collection_run_id"]
            return old
        child = {k: copy.deepcopy(run[k]) for k in ("request","batch_tasks","batch_snapshot_sha256") if k in run}
        child.update(collection_run_id=execution_id, run_id=execution_id, root_run_id=run["collection_run_id"],
            batch_id=run["batch_id"], request_digest=signature, execution_instances=instances, execution_ordinal=ordinal,
            status="running", created_at=descriptor.get("created_at") or utc_now(), completed_at=None,
            dispatch_attempted=True, trajectories=[], errors=[])
        child["output_dir"] = str(self.executions.runs.run_root(run["batch_id"], execution_id))
        saved=self.records.put("collection_executions", execution_id, child, expected_revision=0)
        self.execution_roots[execution_id]=run["collection_run_id"]
        return saved

    def _sync_executions(self, run, projection):
        run_id = run["collection_run_id"]
        value = self._remote("collection-executions", run_id)
        if value.get("root_run_id") != run_id or not isinstance(value.get("executions"), list):
            raise ValueError("执行分段索引身份无效")
        identifiers=[d.get("execution_id") for d in value["executions"] if isinstance(d,dict)]
        if (len(identifiers)!=len(value["executions"]) or len(set(identifiers))!=len(identifiers)
                or not identifiers or identifiers[0]!=run_id or type(value.get("revision")) is not int
                or value["revision"]<len(identifiers)-1):
            raise ValueError("执行分段版本、次序或唯一身份不匹配")
        head = self.records.get("collection_resume_heads",run_id) or {}
        if value["revision"] < head.get("remote_revision",0):
            raise ValueError("执行分段版本回退")
        if type(value.get("revision")) is int:
            self.records.update("collection_resume_heads",run_id,
                lambda h:h.update(remote_revision=value["revision"]))
        failures=[]
        for ordinal, descriptor in enumerate(value["executions"]):
            child = self._register_execution(run, descriptor, ordinal)
            if child["collection_run_id"] == run_id:
                continue
            if not child.get("receipt_closed"):
                try:
                    self.executions.sync(child["collection_run_id"], force=True)
                except Exception as exc:
                    self.errors[run_id] = str(exc)[:500]
                    failures.append(exc)
            ledger = ReceiptStore(self.records, child["collection_run_id"])
            plan = ArtifactTransfer._expected(child)
            self.result_corrections.observe(run, projection, child["collection_run_id"], ordinal, plan, ledger)
            settled = self.records.get("collection_executions",child["collection_run_id"]) or {}
            if settled.get("receipt_closed"):
                for op in self.records.list("collection_resume_operations"):
                    if op["run_id"]==run_id and op.get("execution_id")==child["collection_run_id"] and op["status"]!="completed":
                        self.records.update("collection_resume_operations",op["operation_id"],lambda v:v.update(
                            status="completed",collection_status=settled.get("collection_status"),completed_at=utc_now()))
        snapshot = value.get("current_snapshot")
        if snapshot and snapshot.get("stable"):
            self._commit_snapshot(run, snapshot)
        if not failures:
            self.errors.pop(run_id,None)

    def _commit_snapshot(self, run, summary):
        run_id = run["collection_run_id"]
        snapshot_id = _identifier(summary.get("snapshot_id"), "采集快照编号")
        head = self.records.get("collection_resume_heads", run_id)
        if not head or (head.get("current_snapshot_id") == snapshot_id and not head.get("active")):
            return
        source = self._remote("collection-snapshot", run_id, extra=[snapshot_id])
        if (source.get("root_run_id",run_id) != run_id or source.get("batch_id") != run["batch_id"]
                or source.get("snapshot_id") != snapshot_id or not source.get("stable")):
            raise ValueError("稳定采集快照身份不匹配")
        operations = [o for o in self.records.list("collection_resume_operations") if o["run_id"] == run_id]
        required_revision = max((o["request"]["expected_revision"] + 1 for o in operations
                                 if o["status"] not in {"rejected", "failed", "cancelled"}), default=0)
        required_revision = max(required_revision,head.get("remote_revision",0))
        if type(source.get("revision")) is not int or source["revision"] < required_revision:
            return
        instances = source.get("instances", [])
        expected = ArtifactTransfer._expected(run)
        seen, trajectories, errors, refs, source_ids = set(), [], [], {}, set()
        counts = {"planned_trajectory_count":len(expected),"collection_completed_count":0,
                  "collection_failed_count":0,"collection_not_run_count":0,"received_artifact_count":0,
                  "preprocessing_eligible_count":0,"evaluation_eligible_count":0,"collection_cancelled_count":0,
                  "original_task_count":len({case for _,case,_ in expected}),"sealed_trajectory_count":0,
                  "diagnostic_count":0,"outcome_count":0,"transferred_trajectory_count":0,
                  "transferred_diagnostic_count":0,"quarantined_count":0}
        for item in instances:
            identity = (item.get("phone_id"),item.get("collection_case_id"),item.get("repeat_index"))
            if identity not in expected or identity in seen:
                raise ValueError("快照遗漏或重复冻结实例")
            seen.add(identity)
            execution_id = item.get("execution_id")
            execution = run if execution_id == run_id else self.records.get("collection_executions", execution_id)
            if not execution or (execution_id != run_id and execution.get("root_run_id") != run_id):
                raise ValueError("快照引用未登记执行分段")
            ledger = ReceiptStore(self.records, execution_id)
            row = ledger.get(item.get("artifact_id"))
            if not row or row["status"] not in {"received","quarantined"}:
                return  # More bytes or a durable acknowledgement are still pending.
            if row["item"]["manifest_sha256"] != item.get("manifest_sha256"):
                raise ValueError("快照引用产物摘要发生变化")
            correction = item.get("result_correction")
            if correction:
                if not isinstance(correction,dict) or not correction.get("correction_id"):
                    raise ValueError("逻辑快照结果处置无效")
                corrected = self.result_corrections.corrected(run_id,execution_id,row,correction["correction_id"])
                if corrected is row:
                    return  # Await the append-only, identity-checked disposition.
                row = corrected
            entry = row.get("entry") or {}
            if entry and (entry.get("phone_id"),entry.get("collection_case_id"),entry.get("repeat_index")) != identity:
                raise ValueError("快照实例与已验收产物归属不一致")
            source_id = entry.get("source_trajectory_id")
            if source_id and source_id in source_ids:
                raise ValueError("当前快照不同实例重复使用源轨迹编号")
            if source_id:
                source_ids.add(source_id)
            status = entry.get("execution_status", "failed")
            counts["collection_completed_count" if status=="completed" else
                   "collection_not_run_count" if status=="not_run" else
                   "collection_cancelled_count" if status=="cancelled" else "collection_failed_count"] += 1
            counts["received_artifact_count"] += row["status"] == "received"
            counts["quarantined_count"] += row["status"] == "quarantined"
            kind=row["item"]["kind"]
            counts[{"trajectory":"sealed_trajectory_count","diagnostic":"diagnostic_count","outcome":"outcome_count"}[kind]] += 1
            if row["status"]=="received" and kind!="outcome":
                counts["transferred_"+kind+"_count"] += 1
            counts["evaluation_eligible_count"] += bool(row["status"]=="received" and row.get("eligibility",{}).get("evaluation"))
            if row["status"] == "received" and row.get("eligibility",{}).get("preprocessing"):
                manifest = ledger.manifest(item["artifact_id"])
                normalized = self.runtime.store.collection_runs._normalize(execution,
                    {"batch_id":run["batch_id"],"collection_run_id":execution_id,
                     "agent_type":run["request"]["metadata"].get("agent_type","newagent"),
                     "trajectories":[manifest[manifest["kind"]]],"errors":[]}, execution.get("completed_at") or execution["created_at"])
                trajectories.extend(normalized["trajectories"])
                counts["preprocessing_eligible_count"] += 1
            if row["status"] != "received" or not row.get("eligibility",{}).get("preprocessing") or status != "completed":
                errors.append({"collection_case_id":identity[1],"phone_id":identity[0],"repeat_index":identity[2],
                    "error":entry.get("error") or row.get("error") or ("采集执行未完成："+status if status!="completed" else "当前结果不满足预处理资格")})
            refs[execution_id] = {"collection_run_id":execution_id,"manifest_sha256":execution.get("manifest_sha256"),
                                  "completed_at":execution.get("completed_at")}
        if seen != expected:
            raise ValueError("稳定快照未覆盖全部冻结计划实例")
        value = {"schema_version":2,"batch_id":run["batch_id"],"logical_run_id":run_id,
            "raw_root":str(self.runtime.store.collection_runs.raw_root(run["batch_id"])),
            "runs":sorted(refs.values(),key=lambda v:v["collection_run_id"]),"trajectories":trajectories,"errors":errors,
            "collection_summary":counts}
        saved = {"snapshot_id":snapshot_id,"logical_run_id":run_id,"batch_id":run["batch_id"],
                 "source_digest":payload_digest(source),"input":value,"input_digest":payload_digest(value),"created_at":utc_now()}
        with active_batch_lock(run["batch_id"], self.root), batch_lock(self.root,"resume_"+run_id):
            current = self.records.get("collection_resume_heads",run_id)
            if current["revision"] != head["revision"]:
                return
            old = self.records.get("collection_resume_snapshots",snapshot_id)
            if old and old["source_digest"] != saved["source_digest"]:
                raise ValueError("已发布逻辑快照不可修改")
            # Never settle while an accepted local continuation is unacknowledged.
            if any(o["status"] in {"pending","uncertain","accepted","preparing"} for o in operations):
                return
            self.records.put_many([
                *([] if old else [{"namespace":"collection_resume_snapshots","key":snapshot_id,"payload":saved,"expected_revision":0}]),
                {"namespace":"collection_resume_heads","key":run_id,"expected_revision":current["storage_revision"],
                 "payload":{**current,"active":False,"result_reconciliation_pending":False,"current_snapshot_id":snapshot_id,
                            "current_snapshot_digest":saved["input_digest"],"collection_summary":counts,
                            "current_errors":errors[:100],"current_error_count":len(errors)}}])

    def view(self, run_id):
        head = self.records.get("collection_resume_heads", run_id)
        if not head:
            return {}
        active = head.get("active",False)
        summary = head.get("collection_summary")
        status = "running" if active else ("completed" if summary and summary.get("collection_completed_count")==summary.get("planned_trajectory_count") else "partial")
        if active:
            operations=[o for o in self.records.list("collection_resume_operations") if o["run_id"]==run_id]
            pending=any(o["status"] in {"pending","uncertain","accepted","preparing"} for o in operations)
            phones=self.cache.get(run_id,{}).get("phones",[])
            expected={o.get("execution_id") for o in operations if o["status"]=="started"}
            observed={p.get("execution_id") for p in phones}
            if pending or not expected.issubset(observed):
                status="starting"
            elif phones and all(p.get("status") in {"completed","failed","cancelled","not_run","interrupted"} for p in phones):
                status="completed" if all(p.get("status")=="completed" for p in phones) else "partial"
            elif (head.get("cancel_intent") or {}).get("epoch")==head["revision"]:
                status="stopping"
        if active and head.get("result_reconciliation_pending") and not pending and not expected:
            original=self.records.get("collection_runs",run_id) or {}
            status=original.get("collection_status",original.get("status",status))
        return {"resume_revision": head["revision"], "resume_active": active,
                "result_reconciliation_pending":bool(head.get("result_reconciliation_pending")),
                "collection_status":status,"transfer_status":"downloading" if active else "completed",
                "error":None,"dispatch_error":None,"transfer_error":self.errors.get(run_id),
                "control_error":self.runtime._control_memory.get(run_id,{}).get("error"),
                "errors":[] if active else head.get("current_errors",[]),"remote_errors":[],
                "errors_count":0 if active else head.get("current_error_count",0),
                **({"receipt_storage":self._pressure(run_id)} if self._pressure(run_id) else {}),
                **({"collection_summary":summary} if summary else {}),
                "can_cancel_collection": head.get("active",False),
                "collection_snapshot_id":head.get("current_snapshot_id"),
                "collection_snapshot_digest":head.get("current_snapshot_digest")}

    def cancel(self, run_id):
        run = self._run(run_id)
        previous = self.records.get("collection_resume_heads",run_id) or {}
        refreshed_revision = None
        if (previous.get("cancel_intent") or {}).get("rejected"):
            current = self._remote("collection-executions",run_id)
            if current.get("root_run_id")!=run_id or type(current.get("revision")) is not int:
                raise PhoneFactoryError("无法确认本次停止的执行版本",409)
            refreshed_revision = current["revision"]
        with active_batch_lock(run["batch_id"],self.root),batch_lock(self.root,"resume_"+run_id):
            head = self.records.get("collection_resume_heads",run_id)
            if not head or not head.get("active"):
                raise PhoneFactoryError("当前没有活动的续采分段",409)
            intent = head.get("cancel_intent")
            if not intent or intent.get("epoch") != head["revision"] or intent.get("rejected"):
                revision = head.get("remote_revision",head["revision"])
                if intent and intent.get("rejected"):
                    # Only a new explicit user action may widen a rejected stop
                    # to the current generation. Background retries keep scope.
                    if refreshed_revision is None:
                        raise PhoneFactoryError("无法确认本次停止的执行版本",409)
                    revision = refreshed_revision
                intent = {"request_id":"stop_"+uuid.uuid4().hex,"expected_revision":head.get("remote_revision",head["revision"]),
                          "epoch":head["revision"],"created_at":utc_now(),"acknowledged":False}
                intent["expected_revision"] = revision
                self.records.update("collection_resume_heads",run_id,lambda h:h.update(cancel_intent=intent))
        self.pool.submit(run_id,lambda:self._send_cancel(run_id))
        return {"ok":True,"stop_accepted":True,"collection_run_id":run_id,"status":"stopping"}

    def _send_cancel(self, run_id):
        head = self.records.get("collection_resume_heads",run_id) or {}
        intent = head.get("cancel_intent")
        if not intent or intent.get("acknowledged") or intent.get("rejected"):
            return
        try:
            response = self._remote("executions-cancel",run_id,{k:intent[k] for k in ("request_id","expected_revision")})
            if response.get("ok") is False or response.get("root_run_id",run_id)!=run_id:
                raise ValueError("续采停止响应身份不匹配")
            def acknowledged(head):
                if (head.get("cancel_intent") or {}).get("request_id")==intent["request_id"]:
                    head["cancel_intent"].update(acknowledged=True,error=None)
            self.records.update("collection_resume_heads",run_id,acknowledged)
        except Exception as exc:
            self.runtime._control_memory.setdefault(run_id,{})["error"]=str(exc)[:500]
            try:
                def failed(head):
                    if (head.get("cancel_intent") or {}).get("request_id")==intent["request_id"]:
                        head["cancel_intent"].update(error=str(exc)[:500],
                            rejected=isinstance(exc,PhoneFactoryError) and exc.remote_status in {400,409,422})
                self.records.update("collection_resume_heads",run_id,failed)
            except (OSError,sqlite3.Error):
                pass

    def dispatch(self, parts, method, data, mode):
        run_id = parts[1]
        if len(parts)==3 and method=="GET" and parts[2]=="diagnostics":
            return self.diagnostics(run_id,mode)
        if len(parts)==3 and method=="GET" and parts[2]=="cases":
            return self.cases(run_id,mode,data)
        if len(parts)==5 and method=="GET" and parts[2]=="cases" and parts[4]=="instances":
            return self.cases(run_id,mode,data,parts[3])
        if len(parts)==5 and parts[2]=="phones":
            if method=="GET" and parts[4]=="ownership":
                return self.phone_control.ownership(run_id,parts[3],mode)
            if method=="POST" and parts[4] in {"reconcile","stop"}:
                return self.phone_control.submit(run_id,parts[3],parts[4],data,mode)
        if len(parts)==4 and method=="GET" and parts[2]=="phone-control-operations":
            return self.phone_control.operation(run_id,parts[3],mode)
        if mode!="generate":
            raise PhoneFactoryError("模型迭代采集不支持续采",409)
        if len(parts)==3 and method=="POST" and parts[2]=="resume-preview":
            return self.preview(run_id,data)
        if len(parts)==3 and method=="POST" and parts[2]=="resume":
            return self.submit(run_id,data)
        if len(parts)==4 and method=="GET" and parts[2]=="resume-operations":
            self._run(run_id,mode,read=True)
            return self.operation(run_id,parts[3])
        raise PhoneFactoryError("续采接口不存在",404)
