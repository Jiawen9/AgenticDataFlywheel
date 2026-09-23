"""Durable batch orchestration. Polling never occupies a model executor thread."""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import math
import threading
import uuid
from pathlib import Path

from .batch_lifecycle import active_jobs, ensure_batch_active, lifecycle
from .data_store import DATA_ROOT, ArtifactStore, RecordStore, RevisionConflict
from .data_store.artifacts import _identifier
from .data_store.registry import utc_now
from .pipeline_access import TERMINAL, active_pipeline, internal_pipeline

LOG = logging.getLogger(__name__)
STEPS = [("collection", "采集与回传"), ("preprocessing", "预处理与标框"), ("tree", "轨迹树构建"),
         ("quality", "轨迹质检"), ("correction", "人工修正"), ("cot", "COT 生成"),
         ("publication", "数据发布"), ("overview", "看板汇总")]
ACTIVE_JOBS = {"queued", "running", "dispatching", "pending"}
FAILED_JOBS = {"failed", "interrupted", "cancelled", "stale"}


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False,
                                     separators=(",", ":")).encode()).hexdigest()


class PipelineError(RuntimeError):
    def __init__(self, message, status=409):
        super().__init__(message)
        self.status = status


class PipelineRuntime:
    """Adapter to existing production services; tests inject a fully offline adapter."""
    def __init__(self, root, *, preprocessing, tree, quality, cot, factory, overview):
        self.root = Path(root)
        self.artifacts, self.records = ArtifactStore(root), RecordStore(root)
        self.preprocessing, self.tree, self.quality, self.cot = preprocessing, tree, quality, cot
        self.factory, self.overview = factory, overview

    def configurations(self):
        from .tree_build_service import tree_build_config
        from .batch_results import quality_task_fingerprints
        return {"preprocessing": digest(self.preprocessing.config_loader()), "tree": digest(tree_build_config()),
                "quality": quality_task_fingerprints({"tree_hashes": {"config": ""}, "quality_input": {}})["config"],
                "cot": self.cot._configuration_fingerprint(False)}

    def snapshot(self, batch_id):
        from .batch_results import annotation_task_fingerprints
        tasks, collection = set(), None
        ref = self.artifacts.get(batch_id, "00_collection")
        if ref:
            collection = self.artifacts.read_payload(ref)
            tasks.update(str(t["task_id"]) for t in collection.get("snapshot", {}).get("tasks", []))
        for stage in ("01_conversion", "02_annotation"):
            ref = self.artifacts.get(batch_id, stage)
            if ref:
                tasks.update(annotation_task_fingerprints(self.artifacts.read_payload(ref)))
        if not tasks:
            batch = self.factory.collection_runs.batch(batch_id)
            collection = batch
            tasks.update(str(t["task_id"]) for t in batch["snapshot"]["tasks"])
        if not tasks:
            raise PipelineError("该批次没有可处理任务")
        return {"task_ids": sorted(tasks), "collection_digest": digest(collection.get("snapshot", {})) if collection else None}

    def collection_options(self, config):
        options = self.factory.runtime._options(config or {}, "generate")
        return {key: options[key] for key in ("phone_id", "app", "vla", "config", "phone_apps", "phone_ids")}

    def collection_runs(self, batch_id):
        return self.factory.collection_runs.list_runs(batch_id)

    def dispatch_collection(self, pipeline):
        batch = self.factory.collection_runs.batch(pipeline["batch_id"], require_workbook=True)
        path = Path(batch.get("workbook_path") or self.root / "system" / "task_generation" / "collection_batches"
                    / pipeline["batch_id"] / batch["filename"])
        self.factory.add_task({"filename": batch["filename"], "description": pipeline["name"],
            "source_batch_id": pipeline["batch_id"], "content_base64": base64.b64encode(path.read_bytes()).decode()})
        options = pipeline["collection_config"]
        current = self.collection_options(options)
        if current != options:
            raise PipelineError("手机或 App 关联已变化，请结束流程后重新创建")
        return self.factory.remote_start({**options, "filename": batch["filename"],
                                         "request_id": pipeline["pipeline_id"], "run_mode": "generate"})

    def input_ready(self, pipeline):
        runs = self.collection_runs(pipeline["batch_id"])
        bound = set(pipeline.get("collection_run_ids", []))
        if pipeline["start_mode"] == "collect" and bound - {run["collection_run_id"] for run in runs}:
            raise PipelineError("本次采集运行记录缺失，不能改用其他历史运行继续")
        if runs:
            for run in runs:
                if run["status"] in ACTIVE_JOBS:
                    return False
                if (pipeline["start_mode"] == "collect" and run["collection_run_id"] in bound
                        and (run["status"] != "completed" or run.get("errors"))):
                    raise PipelineError("本次采集存在失败或不完整用例，请补采后接续已有结果")
            ready = self.factory.collection_runs.ready_input(pipeline["batch_id"])
            present = {str(t.get("task_id")) for t in ready["trajectories"]}
            if not set(pipeline["task_ids"]).issubset(present):
                raise PipelineError("采集结果未覆盖整批任务，不能把缺失用例当成低分跳过")
            pipeline["input_digest"] = ready["input_digest"]
            return True
        return self.artifacts.get(pipeline["batch_id"], "02_annotation") is not None

    def ready(self, step, pipeline):
        from .batch_results import (annotation_task_fingerprints, current_tree_payload, current_quality_payload,
                                   tree_task_fingerprints, quality_task_fingerprints)
        batch, tasks = pipeline["batch_id"], set(pipeline["task_ids"])
        if step == "preprocessing":
            conversion, annotation = self.artifacts.get(batch, "01_conversion"), self.artifacts.get(batch, "02_annotation")
            if not conversion or not annotation:
                return False
            rows = self.artifacts.read_payload(annotation)
            if not tasks.issubset(annotation_task_fingerprints(rows)):
                return False
            # Completion also checks the exact latest raw input, not only task names.
            from .batch_lifecycle import publication_blockers
            problems = publication_blockers(batch, [], self.root)
            return not any(p["code"] in {"stale_collection_input", "invalid_collection_input", "stale_preprocessing",
                                        "pending_preprocessing", "invalid_collection", "invalid_stage"} for p in problems)
        trees = current_tree_payload(batch, self.root)
        if step == "tree":
            from .tree_build_service import tree_build_config
            annotation = self.artifacts.get(batch, "02_annotation")
            if not annotation:
                return False
            expected = tree_task_fingerprints(self.artifacts.read_payload(annotation), tree_build_config())
            return all(t in trees.get("trees", {}) and trees.get("task_fingerprints", {}).get(t) == expected.get(t) for t in tasks)
        if step == "quality":
            quality = current_quality_payload(batch, self.root)
            expected = quality_task_fingerprints(trees)
            by_task = {t["task_id"]: t for t in quality.get("tasks", [])}
            counts = {t["task_id"]: int(t.get("trajectory_count") or 0) for t in trees.get("tasks", [])}
            return all(t in by_task and by_task[t].get("evaluations")
                       and len(by_task[t]["evaluations"]) == counts.get(t)
                       and quality.get("task_fingerprints", {}).get(t) == expected.get(t) for t in tasks)
        return False

    def jobs(self, step, pipeline):
        if step == "collection":
            return self.collection_runs(pipeline["batch_id"])
        namespace = {"preprocessing": "preprocessing_jobs", "tree": "tree_jobs", "quality": "quality_jobs",
                     "cot": "correction_cot_jobs"}.get(step)
        if not namespace:
            return []
        return [j for j in self.records.list(namespace)
                if (j.get("batch_id") or j.get("run_id")) == pipeline["batch_id"]
                or (pipeline.get("session_id") and j.get("session_id") == pipeline["session_id"])]

    def submit(self, step, pipeline, retry=False):
        if step == "preprocessing":
            previous = [pipeline["steps"][1]["retry_job_id"]] if pipeline["steps"][1].get("retry_job_id") else pipeline["steps"][1]["job_ids"]
            if retry and previous:
                return self.preprocessing.retry(previous[-1])
            return self.preprocessing.submit(pipeline["batch_id"])
        if step == "tree":
            return self.tree.submit(pipeline["task_ids"], batch_id=pipeline["batch_id"])
        if step == "quality":
            return self.quality.submit(task_ids=pipeline["task_ids"], batch_id=pipeline["batch_id"])
        if step == "cot":
            from .pipeline_release import validate_confirmation
            session = validate_confirmation(pipeline, root=self.root)
            group_ids = pipeline["confirmation"]["group_ids"]
            try:
                self.cot._targets(pipeline["session_id"], group_ids)
            except ValueError as exc:
                if "没有动作已修改" in str(exc):
                    return None
                raise
            return self.cot.submit(pipeline["session_id"], group_ids, force_overwrite=False,
                                   expected_revision=session["storage_revision"])
        raise PipelineError("未知处理阶段")

    def selection(self, pipeline):
        from .pipeline_release import build_selection
        return build_selection(pipeline["batch_id"], pipeline["task_ids"], pipeline["mode"],
                               pipeline.get("threshold"), root=self.root)

    def prepare_manual(self, pipeline):
        from .pipeline_release import prepare_manual
        return prepare_manual(pipeline["batch_id"], pipeline["selection"], root=self.root)

    def confirm(self, pipeline, revision):
        from .pipeline_release import confirm_manual
        return confirm_manual(pipeline["session_id"], pipeline["selection"], expected_revision=revision, root=self.root)

    def publish(self, pipeline):
        from .pipeline_release import publish_pipeline
        return publish_pipeline(pipeline, root=self.root, registry=self.overview.registry)

    def conversion(self, release_id, *, retry=False):
        value = self.records.get("training_overview_conversions", release_id)
        if retry or not value or value["status"] in {"queued", "running"}:
            value = self.overview.submit(release_id)
        return value


class PipelineManager:
    def __init__(self, runtime, root: Path = DATA_ROOT, *, interval=1.0):
        self.root, self.runtime = Path(root), runtime
        self.records, self.artifacts = RecordStore(root), ArtifactStore(root)
        self.interval, self._stop, self._thread = interval, threading.Event(), None

    def get(self, pipeline_id, *, public=True):
        _identifier(pipeline_id, "Pipeline ID")
        pipeline = self.records.get("pipelines", pipeline_id)
        if pipeline is None:
            raise PipelineError("Pipeline 不存在", 404)
        if public:
            for step in pipeline["steps"]:
                by_id = {j.get("job_id") or j.get("collection_run_id"): j for j in self.runtime.jobs(step["id"], pipeline)}
                step["jobs"] = [by_id[j] for j in step["job_ids"] if j in by_id]
                if step["status"] == "running" and step["jobs"]:
                    step["percent"] = round(sum(float(j.get("percent") or (100 if j["status"] in {"completed", "succeeded"} else 0))
                                                for j in step["jobs"]) / len(step["jobs"]), 1)
            pipeline["history_only"] = pipeline["status"] in {"terminated", "no_publishable_data"} and not pipeline.get("release_id")
            pipeline["managed"] = pipeline["status"] not in TERMINAL and lifecycle(pipeline["batch_id"], self.root)["status"] != "published"
        return pipeline

    def list(self, batch_id=None, active_only=False):
        return sorted((self.get(p["pipeline_id"]) for p in self.records.list("pipelines")
                       if (not batch_id or p["batch_id"] == batch_id)
                       and (not active_only or p["status"] not in TERMINAL)), key=lambda p: p["created_at"], reverse=True)

    def _busy(self, batch_id):
        if active_jobs(batch_id, self.root):
            return True
        # An acknowledgement can be lost after the collector accepted a run.
        # Do not release ownership until the transfer worker resolves that run.
        terminal_remote = {"completed", "succeeded", "partial", "failed", "interrupted", "cancelled"}
        return any(run.get("dispatch_attempted") and run.get("dispatch_error")
                   and run.get("remote_status") not in terminal_remote
                   and run.get("status") not in {"completed", "interrupted", "cancelled"}
                   for run in self.runtime.collection_runs(batch_id))

    def create(self, data):
        request_id, batch = str(data.get("request_id") or ""), str(data.get("batch_id") or "")
        _identifier(request_id, "请求编号")
        _identifier(batch, "batch ID")
        mode, start = data.get("mode"), data.get("start_mode")
        if mode not in {"manual", "automatic"} or start not in {"collect", "existing"}:
            raise PipelineError("请选择有效的模式和起点", 422)
        name = str(data.get("name") or "").strip()
        if not name or len(name) > 200:
            raise PipelineError("流程名称须为 1～200 个字符", 422)
        threshold = data.get("threshold")
        if mode == "automatic" and (isinstance(threshold, bool) or not isinstance(threshold, (int, float))
                                    or not math.isfinite(threshold) or not 0 <= threshold <= 5):
            raise PipelineError("自动发布总分阈值必须在 0～5 之间", 422)
        key, request_digest = digest(request_id), digest(data)
        with self.artifacts.batch_lock("pipeline_create"), self.artifacts.batch_lock(batch):
            old = self.records.get("pipeline_requests", key)
            if old:
                if old["request_digest"] != request_digest:
                    raise PipelineError("相同请求编号对应的 Pipeline 配置不同")
                return self.get(old["pipeline_id"])
            ensure_batch_active(batch, self.root)
            if active_pipeline(batch, self.root):
                raise PipelineError("该批次已有未结束的 Pipeline")
            if self._busy(batch):
                raise PipelineError("该批次已有执行中作业，请完成后再启动 Pipeline")
            snapshot = self.runtime.snapshot(batch)
            options = self.runtime.collection_options(data.get("collection_config") or {}) if start == "collect" else None
            now, identifier = utc_now(), "pl_" + uuid.uuid4().hex
            pipeline = {"pipeline_id": identifier, "batch_id": batch, "name": name, "mode": mode,
                "start_mode": start, "threshold": threshold if mode == "automatic" else None,
                "task_ids": snapshot["task_ids"], "batch_snapshot": snapshot, "collection_config": options,
                "config_fingerprints": self.runtime.configurations(), "status": "running", "current_step": "collection",
                "created_at": now, "updated_at": now, "error": None, "session_id": None, "release_id": None,
                "selection": None, "confirmation": None, "collection_run_ids": [], "logs": [],
                "steps": [{"id": key, "label": label, "status": "pending", "job_ids": [], "percent": 0,
                           "message": "等待前置步骤", "error": None, "started_at": None, "completed_at": None}
                          for key, label in STEPS]}
            owner = self.records.get("pipeline_owners", batch)
            self.records.put_many([
                {"namespace": "pipelines", "key": identifier, "payload": pipeline, "expected_revision": 0},
                {"namespace": "pipeline_owners", "key": batch, "payload": {"pipeline_id": identifier, "batch_id": batch},
                 "expected_revision": (owner or {}).get("storage_revision", 0)},
                {"namespace": "pipeline_requests", "key": key, "payload": {"pipeline_id": identifier, "request_digest": request_digest}, "expected_revision": 0}])
            return self.get(identifier)

    def _save(self, pipeline):
        pipeline["updated_at"] = utc_now()
        pipeline["logs"] = pipeline.get("logs", [])[-200:]
        return self.records.put("pipelines", pipeline["pipeline_id"], pipeline, expected_revision=pipeline["storage_revision"])

    def _log(self, p, message):
        p["logs"].append({"at": utc_now(), "message": message})

    def control(self, pipeline_id, action, revision, *, session_revision=None):
        with self.artifacts.batch_lock("pipeline_" + pipeline_id):
            p = self.get(pipeline_id, public=False)
            if revision != p["storage_revision"]:
                raise RevisionConflict("流程已更新，请刷新后重试，当前草稿不会丢失")
            if action == "retry" and p["status"] == "published_summary_failed":
                self.runtime.conversion(p["release_id"], retry=True)
                p.update(status="running", error=None)
                p["steps"][-1].update(status="running", error=None)
            elif p["status"] in TERMINAL:
                raise PipelineError("该 Pipeline 已结束")
            elif action == "pause" and p["status"] in {"running", "waiting_for_correction"} and not p.get("release_id"):
                p["paused_from"] = p["status"]
                p["status"] = "paused"
            elif action == "resume" and p["status"] == "paused":
                p["status"] = p.pop("paused_from", "running")
            elif action == "retry" and p["status"] == "failed":
                step = next(s for s in p["steps"] if s["id"] == p["current_step"])
                step["retry_job_id"] = step["job_ids"][-1] if step["job_ids"] else None
                step.setdefault("previous_job_ids", []).extend(step["job_ids"])
                step.update(status="pending", error=None, retry=True, job_ids=[])
                p.update(status="running", error=None)
            elif action == "terminate" and not p.get("release_id"):
                p["status"] = "terminating"
            elif action == "confirm-correction" and p["status"] == "waiting_for_correction":
                if session_revision is None:
                    raise PipelineError("请携带修正会话修订号确认", 422)
                with self.artifacts.batch_lock(p["batch_id"]), internal_pipeline(pipeline_id):
                    confirmation = self.runtime.confirm(p, session_revision)
                    p.update(confirmation=confirmation, selection=confirmation["selection"], status="running")
                    step = next(s for s in p["steps"] if s["id"] == "correction")
                    step.update(status="succeeded", completed_at=utc_now(), percent=100, message="人工确认完成")
                    self._log(p, "人工确认完成，锁定入选轨迹和修改内容")
                    self._save(p)
                    return self.get(pipeline_id)
            else:
                raise PipelineError("当前状态不能执行该操作")
            self._log(p, {"pause": "暂停后续派发", "resume": "继续执行", "retry": "重试当前阶段", "terminate": "等待已启动作业结束后终止"}.get(action, action))
            self._save(p)
            return self.get(pipeline_id)

    def _done(self, step, message="已完成", *, skipped=False):
        step.update(status="skipped" if skipped else "succeeded", percent=100, completed_at=step.get("completed_at") or utc_now(), message=message, error=None)

    def _job_step(self, p, step):
        key = step["id"]
        jobs = {j["job_id"]: j for j in self.runtime.jobs(key, p)}
        known = [jobs.get(j) for j in step["job_ids"]]
        if step["status"] == "running":
            if any(j and j["status"] in ACTIVE_JOBS for j in known):
                return
            if any(j is None for j in known):
                raise PipelineError("关联作业记录缺失，不能继续推进")
            if key != "cot" and self.runtime.ready(key, p):
                self._done(step)
                return
            failed = next((j for j in known if j["status"] in FAILED_JOBS), None)
            if failed:
                raise PipelineError(failed.get("error") or "关联作业中断，请重试")
            if key == "cot" and known and all(j["status"] == "succeeded" for j in known):
                self._done(step)
                return
            if known:
                raise PipelineError("作业已结束但整批有效结果不完整，不能继续发布")
        if self.runtime.configurations().get(key) != p["config_fingerprints"].get(key):
            raise PipelineError("处理配置已变化，请结束流程后重新创建")
        if key != "cot" and self.runtime.ready(key, p):
            self._done(step, "复用当前有效结果")
            return
        # Persist intent before submission. Child services durably deduplicate the
        # exact input, so losing the response does not create another computation.
        step.update(status="running", started_at=step["started_at"] or utc_now(), message="提交并跟踪作业")
        saved = self._save(p)
        p["storage_revision"] = saved["storage_revision"]
        job = self.runtime.submit(key, p, retry=bool(step.pop("retry", False)))
        if job is None:
            self._done(step, "没有需要生成 COT 的步骤", skipped=True)
            return
        if job["job_id"] not in step["job_ids"]:
            step["job_ids"].append(job["job_id"])
        # Other sub-jobs of the same frozen task input may cover the remainder.
        for child in self.runtime.jobs(key, p):
            if child["status"] in ACTIVE_JOBS and child["job_id"] not in step["job_ids"]:
                step["job_ids"].append(child["job_id"])
        if job["status"] in FAILED_JOBS:
            raise PipelineError(job.get("error") or "作业提交失败")

    def tick(self, pipeline_id):
        with self.artifacts.batch_lock("pipeline_" + pipeline_id), internal_pipeline(pipeline_id):
            p = self.get(pipeline_id, public=False)
            original = digest(p)
            if p["status"] in TERMINAL - {"published_summary_failed"}:
                return
            state = lifecycle(p["batch_id"], self.root)
            if state["status"] == "published":
                p["release_id"] = state["release_id"]
                self._done(p["steps"][-2], "已发布，批次处理结束")
                p.update(status="running", current_step="overview", error=None)
            elif p["status"] == "terminating":
                if not self._busy(p["batch_id"]):
                    p["status"] = "terminated"
                    self._log(p, "流程已终止，保留所有已完成结果")
                    self._save(p)
                return
            elif p["status"] in {"paused", "failed", "waiting_for_correction"}:
                return
            step = next((s for s in p["steps"] if s["status"] not in {"succeeded", "skipped"}), None)
            if p.get("release_id"):
                step = p["steps"][-1]
            if step is None:
                p["status"] = "succeeded"
                self._save(p)
                return
            p["current_step"] = step["id"]
            try:
                if not p.get("release_id") and self.runtime.snapshot(p["batch_id"]) != p["batch_snapshot"]:
                    raise PipelineError("批次任务范围或来源已变化，请结束流程后重新创建")
                key = step["id"]
                if key == "collection":
                    if p["start_mode"] == "collect" and (not p["collection_run_ids"] or step.pop("retry", False)):
                        step.update(status="running", started_at=step["started_at"] or utc_now(), message="下发采集")
                        saved = self._save(p)
                        p["storage_revision"] = saved["storage_revision"]
                        run = self.runtime.dispatch_collection(p)
                        p["collection_run_ids"] = [run["collection_run_id"]]
                    if p["start_mode"] == "existing":
                        p["collection_run_ids"] = [r["collection_run_id"] for r in self.runtime.collection_runs(p["batch_id"]) if r["status"] == "completed"]
                    step["job_ids"] = list(p["collection_run_ids"])
                    if self.runtime.input_ready(p):
                        self._done(step, "采集文件完整落盘并完成登记" if step["job_ids"] else "接续已有轨迹")
                    else:
                        step.update(status="running", message="等待采集完成和结果回传")
                        if p["start_mode"] == "existing" and not step["job_ids"]:
                            raise PipelineError("该批次尚无已登记轨迹，请先采集或导入")
                elif key in {"preprocessing", "tree", "quality"}:
                    self._job_step(p, step)
                elif key == "correction":
                    if p["selection"] is None:
                        p["selection"] = self.runtime.selection(p)
                    selected = p["selection"].get("selected_trajectories", p["selection"].get("selected", []))
                    if not selected:
                        p["status"] = "no_publishable_data"
                        self._done(step, "没有达到阈值的轨迹，批次保持活动", skipped=True)
                        for rest in p["steps"][5:]:
                            self._done(rest, "没有可发布数据", skipped=True)
                    elif p["mode"] == "automatic":
                        self._done(step, "自动模式按阈值筛选，无人工修正关卡", skipped=True)
                    else:
                        manual = self.runtime.prepare_manual(p)
                        p.update(session_id=manual["session_id"], selection=manual["selection"], status="waiting_for_correction")
                        step.update(status="waiting", message="请完成修正并确认继续")
                elif key == "cot":
                    if p["mode"] == "automatic":
                        self._done(step, "自动模式保留原始内容", skipped=True)
                    else:
                        self._job_step(p, step)
                elif key == "publication":
                    step.update(status="running", message="准备冻结文件并登记发布", started_at=step["started_at"] or utc_now())
                    saved = self._save(p)
                    p["storage_revision"] = saved["storage_revision"]
                    release = self.runtime.publish(p)
                    p["release_id"] = release["release_id"]
                    self._done(step, "已发布，批次处理结束")
                elif key == "overview":
                    conversion = self.runtime.conversion(p["release_id"])
                    step.update(status="running", message="累计到最终汇总表和看板")
                    if conversion["status"] == "succeeded":
                        self._done(step)
                        p.update(status="succeeded", error=None)
                    elif conversion["status"] == "failed":
                        p.update(status="published_summary_failed", error=conversion.get("error"))
                        step.update(status="failed", error=p["error"], message="已发布，汇总失败；可单独重试转换")
                if step["status"] in {"succeeded", "skipped"}:
                    self._log(p, step["label"] + "：" + step["message"])
            except Exception as exc:
                LOG.exception("Pipeline %s stage %s failed", pipeline_id, step["id"])
                p.update(status="failed", error=str(exc))
                step.update(status="failed", error=str(exc), message="执行失败，可重试或终止")
                self._log(p, step["label"] + "失败：" + str(exc))
                # Dispatch may have been accepted even when its HTTP response was
                # lost; retain the durable run identity before returning an error.
                if step["id"] == "collection":
                    runs = [r for r in self.runtime.collection_runs(p["batch_id"])
                            if r.get("dispatch_key") == pipeline_id]
                    p["collection_run_ids"] = [r["collection_run_id"] for r in runs]
                    step["job_ids"] = list(p["collection_run_ids"])
            if digest(p) != original:
                self._save(p)

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._poll, name="pipeline-orchestrator", daemon=True)
        self._thread.start()

    def _poll(self):
        while not self._stop.is_set():
            for pipeline in self.records.list("pipelines"):
                if self._stop.is_set():
                    break
                if pipeline["status"] not in TERMINAL - {"published_summary_failed"}:
                    try:
                        self.tick(pipeline["pipeline_id"])
                    except Exception:
                        LOG.exception("Pipeline reconciliation failed: %s", pipeline["pipeline_id"])
            self._stop.wait(self.interval)

    def close(self):
        self._stop.set()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=5)
