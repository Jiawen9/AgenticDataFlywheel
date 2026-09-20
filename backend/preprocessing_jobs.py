"""SQLite-backed preprocessing jobs with immutable collection-input snapshots."""
from __future__ import annotations

import copy
import json
import threading
import uuid
from concurrent.futures import Executor, ThreadPoolExecutor
from pathlib import Path

from .batch_operations import active_batch_lock, batch_operation
from .batch_lifecycle import is_batch_active
from .collection_runs import CollectionRunStore
from .data_store import ArtifactStore, DATA_ROOT, RecordStore
from .data_store.paths import contained_path
from .data_store.registry import utc_now
from .preprocessing_service import (COLUMNS, SHEET, PreprocessingError, annotate_input,
                                    convert_input, digest, processing_config, verify_input)
from .stage_artifacts import fingerprint, write_payload_workbook
from .trajectories_preprocessing import DEFAULT_ENV_FILE

ACTIVE = {"queued", "running"}


class PreprocessingJobManager:
    def __init__(self, root: Path = DATA_ROOT, *, executor: Executor | None = None,
                 source_store=None, env_file: Path = DEFAULT_ENV_FILE,
                 config_loader=None, reviewer_factory=None, annotator=annotate_input):
        self.root = Path(root).resolve()
        self.records, self.artifacts = RecordStore(self.root), ArtifactStore(self.root)
        self.sources = source_store if source_store is not None else CollectionRunStore(self.root)
        self.env_file = Path(env_file)
        self.config_loader = config_loader or (lambda: processing_config(self.env_file))
        self.reviewer_factory, self.annotator = reviewer_factory, annotator
        self._lock = threading.RLock()
        self._owns_executor = executor is None
        self._executor = executor or ThreadPoolExecutor(max_workers=1, thread_name_prefix="preprocessing")
        for job in self.records.list("preprocessing_jobs"):
            if job["status"] in ACTIVE:
                self._update(job["job_id"], status="interrupted", stage="interrupted",
                             completed_at=utc_now(), error="服务重启导致预处理中断，可使用原输入重试")

    @staticmethod
    def _public(job):
        return {key: value for key, value in job.items()
                if key not in {"input_path", "input_sha256", "config", "storage_revision"}}

    def _get(self, job_id):
        return self.records.get("preprocessing_jobs", job_id)

    def get(self, job_id):
        with self._lock:
            job = self._get(job_id)
            return self._public(job) if job else None

    def _update(self, job_id, **changes):
        with self._lock:
            def update(job):
                job.update(changes)
                if "completed_steps" in changes or "total_steps" in changes:
                    total = job.get("total_steps") or 0
                    job["percent"] = min(99, round(100 * job.get("completed_steps", 0) / total)) if total else 0
                return job
            return self.records.update("preprocessing_jobs", job_id, update)

    def list_jobs(self, batch_id=None):
        with self._lock:
            return [self._public(job) for job in sorted(self.records.list("preprocessing_jobs"),
                     key=lambda item: (item["created_at"], item["job_id"]), reverse=True)
                    if batch_id is None or job["batch_id"] == batch_id]

    def _active(self, batch_id):
        return next((job for job in self.records.list("preprocessing_jobs")
                     if job["batch_id"] == batch_id and job["status"] in ACTIVE), None)

    def _snapshot(self, job):
        path = contained_path(self.root, job["input_path"])
        if not path.is_file() or fingerprint(path) != job["input_sha256"]:
            raise PreprocessingError("预处理输入 JSON 缺失或校验失败，不能通过重新扫描替代")
        return json.loads(path.read_text(encoding="utf-8"))

    def _reuse_success(self, job):
        verify_input(self._snapshot(job), self.root)
        current = self.artifacts.get(job["batch_id"], "02_annotation")
        if current and current.get("metadata", {}).get("preprocessing_fingerprint") == job["fingerprint"]:
            artifacts = [self.artifacts.get(job["batch_id"], stage) for stage in ("01_conversion", "02_annotation")]
            for artifact in artifacts:
                self.artifacts.read_payload(artifact)
            return self._public({**job, "artifacts": artifacts, "annotation_version": current["version"]})
        for artifact in job["artifacts"]:
            self.artifacts.read_payload(artifact)
        return self._public(job)

    def _new_job(self, batch_id, snapshot, config, *, resumed_from=None, artifacts=None,
                 base_annotation_version=None):
        job_id = uuid.uuid4().hex
        directory = contained_path(self.root, "system", "preprocessing", batch_id, job_id)
        directory.mkdir(parents=True, exist_ok=False)
        path = directory / "input.json"
        path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        saved_artifacts = artifacts or []
        annotation = next((item for item in saved_artifacts if item["stage"] == "02_annotation"), None)
        if resumed_from is None:
            latest = sorted(self.artifacts.list(batch_id, "02_annotation"), key=lambda item: item["version"])
            base_annotation_version = latest[-1]["version"] if latest else None
        job = {"job_id": job_id, "batch_id": batch_id, "status": "queued", "stage": "queued",
               "created_at": utc_now(), "started_at": None, "completed_at": None,
               "completed_steps": 0, "total_steps": 0, "percent": 0,
               "current_task": None, "current_trajectory": None, "current_step": None,
               "error": None, "warnings": [], "artifacts": saved_artifacts,
               "annotation_version": annotation["version"] if annotation else None,
               "input_path": path.relative_to(self.root).as_posix(), "input_sha256": fingerprint(path),
               "input_digest": snapshot["input_digest"], "config": config,
               "fingerprint": digest({"input": snapshot["input_digest"], "config": config}),
               "resumed_from": resumed_from}
        job["base_annotation_version"] = base_annotation_version
        self.records.put("preprocessing_jobs", job_id, job, expected_revision=0)
        try:
            self._executor.submit(self._run, job_id)
        except Exception as exc:
            self._update(job_id, status="failed", stage="failed", error=str(exc), completed_at=utc_now())
            raise
        return self.get(job_id)

    def submit(self, batch_id):
        with active_batch_lock(batch_id, self.root), self._lock:
            # Validate the identifier before any filesystem use.
            self.artifacts.list(batch_id=batch_id)
            active = self._active(batch_id)
            if active:
                return self._public(active)
            snapshot = self.sources.freeze_ready_input(batch_id)
            config = self.config_loader()
            key = digest({"input": snapshot["input_digest"], "config": config})
            previous = next((job for job in reversed(self.records.list("preprocessing_jobs"))
                             if job["batch_id"] == batch_id and job["fingerprint"] == key
                             and job["status"] == "succeeded"), None)
            if previous:
                current = self.artifacts.get(batch_id, "02_annotation")
                if current and (current.get("metadata", {}).get("preprocessing_fingerprint") == key
                                or any(item["stage"] == "02_annotation" and item["version"] == current["version"] for item in previous["artifacts"])):
                    return self._reuse_success(previous)
            return self._new_job(batch_id, snapshot, config)

    def _recover_artifacts(self, job):
        by_stage = {item["stage"]: item for item in job.get("artifacts", [])}
        for item in self.artifacts.list(job["batch_id"]):
            if item.get("metadata", {}).get("preprocessing_job_id") == job["job_id"]:
                previous = by_stage.get(item["stage"])
                if previous is None or item["version"] > previous["version"]:
                    by_stage[item["stage"]] = item
        return [by_stage[stage] for stage in ("01_conversion", "02_annotation") if stage in by_stage]

    def retry(self, job_id):
        previous = self._get(job_id)
        if previous is None:
            raise PreprocessingError("预处理作业不存在", 404)
        with active_batch_lock(previous["batch_id"], self.root), self._lock:
            previous = self._get(job_id)
            if previous is None:
                raise PreprocessingError("预处理作业不存在", 404)
            active = self._active(previous["batch_id"])
            if active:
                return self._public(active)
            if previous["status"] == "succeeded":
                return self._reuse_success(previous)
            if previous["status"] not in {"failed", "interrupted"}:
                raise PreprocessingError("当前作业不能重试")
            newer = next((item for item in reversed(self.records.list("preprocessing_jobs"))
                          if item["batch_id"] == previous["batch_id"] and item["status"] == "succeeded"
                          and item["created_at"] > previous["created_at"]), None)
            if newer:
                if newer["fingerprint"] != previous["fingerprint"]:
                    raise PreprocessingError("该批次已有更新的成功结果，不能重试旧输入；请查看最新结果")
                return self._reuse_success(newer)
            if self.config_loader() != previous["config"]:
                raise PreprocessingError("模型或预处理配置已变化，请重新开始预处理")
            snapshot = self._snapshot(previous)
            verify_input(snapshot, self.root)
            saved = self._recover_artifacts(previous)
            for item in saved:
                self.artifacts.resolve_file(item, "result.json")
            return self._new_job(previous["batch_id"], snapshot, previous["config"],
                                 resumed_from=job_id, artifacts=saved,
                                 base_annotation_version=previous.get("base_annotation_version"))

    def _run(self, job_id):
        self._update(job_id, status="running", stage="scanning", started_at=utc_now())
        try:
            job = self._get(job_id)
            with batch_operation(job["batch_id"], "preprocessing", self.root):
                snapshot = self._snapshot(job)
                verify_input(snapshot, self.root)
                if self.config_loader() != job["config"]:
                    raise PreprocessingError("排队后模型或预处理配置已变化，请重新开始预处理")
                work = contained_path(self.root, "system", "preprocessing", job["batch_id"], job_id)
                saved = self._recover_artifacts(job)
                conversion = next((item for item in saved if item["stage"] == "01_conversion"), None)
                annotation = next((item for item in saved if item["stage"] == "02_annotation"), None)
                pending_conversion = None
                if conversion is None:
                    payload, counts = convert_input(snapshot, lambda event: self._update(job_id, **event), data_root=self.root)
                    verify_input(snapshot, self.root)
                    self._update(job_id, stage="publishing", warnings=counts["warnings"])
                    output = work / "trajectories_to_excel.xlsx"
                    write_payload_workbook(output, payload)
                    source_refs = self.artifacts.list(job["batch_id"], "00_collection")
                    source_refs = sorted(source_refs, key=lambda item: item["version"])[-1:]
                    source_refs.append({"kind": "raw_trajectories", "path": snapshot["raw_root"],
                                        "input_digest": snapshot["input_digest"],
                                        "collection_run_ids": [item["collection_run_id"] for item in snapshot["runs"]]})
                    pending_conversion = dict(stage="01_conversion", payload=payload, workbooks={output.name: output},
                        source_refs=source_refs, metadata={**counts, "raw_root": snapshot["raw_root"],
                            "preprocessing_job_id": job_id, "preprocessing_fingerprint": job["fingerprint"],
                            "input_snapshot_ref": {"path": job["input_path"], "sha256": job["input_sha256"]}})
                    # On an established batch, preserve the complete old chain until
                    # the replacement annotation is ready. A failed retry changes nothing.
                    if self.artifacts.get(job["batch_id"], "02_annotation") is None:
                        conversion = self.artifacts.publish_many(job["batch_id"], [pending_conversion])[0]
                        pending_conversion = None
                        saved = [conversion]
                        self._update(job_id, artifacts=saved)
                    else:
                        json_path = work / "trajectories_to_excel.json"
                        json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
                if conversion is not None:
                    payload = self.artifacts.read_payload(conversion)
                    # The model consumes this frozen worker input, never a mutable path.
                    json_path = work / "trajectories_to_excel.json"
                    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
                if annotation is None:
                    self._update(job_id, stage="annotating", completed_steps=0, total_steps=len(payload["sheets"][SHEET]))
                    output = work / "annotated_trajectories.xlsx"
                    cache = contained_path(self.root, "cache", "bounding_box", job["batch_id"],
                                           digest(job["config"]), "qwen_review_cache.json")
                    result, counts = self.annotator(json_path, payload, output, snapshot=snapshot,
                        config=job["config"], cache_path=cache, env_file=self.env_file,
                        reviewer_factory=self.reviewer_factory, data_root=self.root,
                        progress=lambda event: self._update(job_id, **event))
                    verify_input(snapshot, self.root)
                    self._update(job_id, stage="publishing")
                    from .batch_results import annotation_task_fingerprints, invalidation_record_entry, drain_batch_invalidations
                    with active_batch_lock(job["batch_id"], self.root):
                        current = self.artifacts.get(job["batch_id"], "02_annotation")
                        if (current or {}).get("version") != job.get("base_annotation_version"):
                            raise PreprocessingError("预处理期间标框已变化，已保留人工结果；请刷新后重新开始")
                        before = annotation_task_fingerprints(self.artifacts.read_payload(current)) if current else {}
                        after = annotation_task_fingerprints(result)
                        changed = {task for task in set(before) | set(after) if before.get(task) != after.get(task)}
                        entry = dict(stage="02_annotation", payload=result, workbooks={output.name: output},
                            metadata={**counts, "raw_root": snapshot["raw_root"], "model": job["config"]["model"],
                                      "preprocessing_job_id": job_id, "preprocessing_fingerprint": job["fingerprint"]})
                        if current:
                            entry["expected_version"] = current["version"]
                        notifications = [invalidation_record_entry(self.artifacts, job["batch_id"], "annotation", changed)] if changed else []
                        if pending_conversion:
                            entry["source_stages"] = ["01_conversion"]
                            conversion, annotation = self.artifacts.publish_many(job["batch_id"], [pending_conversion, entry], record_entries=notifications)
                        else:
                            entry["source_refs"] = [conversion]
                            annotation = self.artifacts.publish_many(job["batch_id"], [entry], record_entries=notifications)[0]
                        drain_batch_invalidations(job["batch_id"], self.root)
                    saved = [conversion, annotation]
                else:
                    self.artifacts.read_payload(annotation)
                self._update(job_id, status="succeeded", stage="succeeded", completed_at=utc_now(),
                             artifacts=saved, annotation_version=annotation["version"], percent=100, error=None)
                # input.json remains the raw-input audit record; generated workbooks
                # are now represented by the sole batch artifacts.
                for name in ("trajectories_to_excel.xlsx", "trajectories_to_excel.json", "annotated_trajectories.xlsx", "annotated_trajectories.json"):
                    (work / name).unlink(missing_ok=True)
        except Exception as exc:
            self._update(job_id, status="failed", stage="failed", completed_at=utc_now(), error=str(exc))

    def batches(self):
        manifests = self.artifacts.list()
        jobs = self.list_jobs()
        runs = self.sources.list_runs()
        batch_ids = {item["batch_id"] for item in manifests
                     if item["stage"] in {"00_collection", "01_conversion", "02_annotation"}}
        batch_ids.update(item["batch_id"] for item in jobs + runs)
        result = []
        for batch_id in batch_ids:
            if not is_batch_active(batch_id, self.root):
                continue
            items = [item for item in manifests if item["batch_id"] == batch_id]
            latest = {}
            for item in sorted(items, key=lambda entry: entry["version"]):
                if item["stage"] in {"00_collection", "01_conversion", "02_annotation"}:
                    latest[item["stage"]] = item
            batch_runs = [run for run in runs if run["batch_id"] == batch_id]
            completed_runs = [run for run in batch_runs if run["status"] == "completed"]
            source_trajectories = [trajectory for run in completed_runs
                                   for trajectory in run.get("trajectories", [])]
            source_steps = sum(1 for trajectory in source_trajectories
                               for item in trajectory["files"]
                               if Path(item["path"]).parent.as_posix() == trajectory["relative_dir"]
                               and Path(item["path"]).name.lower().endswith("_vla_model_response.json"))
            latest_job = next((job for job in jobs if job["batch_id"] == batch_id), None)
            source_errors = [error for run in completed_runs for error in run.get("errors", [])]
            running = any(run["status"] in {"dispatching", "running"} for run in batch_runs)
            collection_status = ("partial" if source_errors and source_trajectories else
                                 "ready" if source_trajectories else
                                 "collecting" if running else
                                 "error" if any(run["status"] == "failed" for run in batch_runs) or source_errors else "waiting")
            kind, task_count, reason = "existing_trajectories", 0, None
            annotation_version = latest.get("02_annotation", {}).get("version")
            if "00_collection" in latest:
                try:
                    stored = self.artifacts.read_payload(latest["00_collection"])
                    kind = stored.get("kind", "task_generation")
                    task_count = stored.get("task_count", 0)
                except (ValueError, OSError) as exc:
                    reason = f"采集批次快照无法读取：{exc}"
            preview = latest.get("02_annotation") or latest.get("01_conversion")
            old_count, old_steps = 0, 0
            if preview:
                try:
                    payload = self.artifacts.read_payload(preview)
                    from .trajectory_context import row_task_id, row_trajectory_id
                    rows = next(iter(payload["sheets"].values()))
                    task_count = task_count or len({row_task_id(row) for row in rows})
                    old_count = len({(row_task_id(row), row_trajectory_id(row)) for row in rows})
                    old_steps = len(rows)
                except (ValueError, OSError, KeyError, StopIteration) as exc:
                    reason = f"预处理 JSON 无法读取：{exc}"
            if kind == "existing_trajectories" and preview:
                collection_status = "ready"
            can_start = bool(source_trajectories) and not reason
            if latest_job and latest_job["status"] in ACTIVE:
                can_start = False
                reason = reason or "该批次正在预处理"
            if not can_start and not reason:
                reason = ("已有轨迹批次可查看和建树；尚无采集完成登记" if preview
                          else "等待采集结果")
            updated_at = max([item["created_at"] for item in items] +
                             [item["created_at"] for item in batch_runs] +
                             [latest_job["created_at"] if latest_job else ""])
            result.append({"batch_id": batch_id, "kind": kind,
                "label": {"task_generation": "任务生成", "augmentation": "任务泛化扩增",
                          "existing_trajectories": "已有轨迹批次"}.get(kind, kind),
                "task_count": task_count, "ready_trajectory_count": len(source_trajectories) or old_count,
                "ready_step_count": source_steps or old_steps, "collection_status": collection_status,
                "preprocessing_status": latest_job["status"] if latest_job else
                    ("succeeded" if annotation_version else "not_started"),
                "latest_job": latest_job, "annotation_version": annotation_version,
                "artifacts": [latest[stage] for stage in ("01_conversion", "02_annotation") if stage in latest],
                "can_start": can_start, "reason": reason, "updated_at": updated_at,
                "collection_errors": source_errors, "collecting": running})
        return sorted(result, key=lambda item: (item["updated_at"], item["batch_id"]), reverse=True)

    def shutdown(self):
        if self._owns_executor:
            self._executor.shutdown(wait=False, cancel_futures=True)
