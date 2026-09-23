"""Register existing finalized Rollout directories as immutable collection inputs.

The source directory is read-only. Preview freezes identities and checksums; commit
copies to the collection-run namespace and atomically registers its 00 snapshot.
No phone, generation job, preprocessing worker, or model is started here.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .collection_runs import CollectionRunError, CollectionRunStore, _component, _identifier
from .data_store import ArtifactStore, DATA_ROOT, RecordStore
from .data_store.paths import contained_path
from .data_store.registry import utc_now
from .export_vla_trajectories import collect_rows, STEP_RESPONSE_RE
from .task_generation.collection_batches import payload_digest, workbook_digest
from .trajectory_data import extract_original_goal

LOG = logging.getLogger(__name__)


class RolloutImportError(CollectionRunError):
    pass


def _text(value):
    return str(value).strip() if value is not None else ""


def _linked(path: Path):
    return (path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction())
            or bool(path.exists() and getattr(path.stat(follow_symlinks=False), "st_file_attributes", 0) & 0x400))


class RolloutImportStore:
    def __init__(self, root: Path = DATA_ROOT):
        self.root = contained_path(Path(root))
        self.records, self.artifacts = RecordStore(self.root), ArtifactStore(self.root)
        self.runs = CollectionRunStore(self.root)
        self.max_files = int(os.environ.get("ADF_ROLLOUT_IMPORT_MAX_FILES", "100000"))
        self.max_bytes = int(os.environ.get("ADF_ROLLOUT_IMPORT_MAX_BYTES", str(20 * 1024 ** 3)))

    def options(self) -> dict:
        extra = [str(Path(value).expanduser().absolute()) for value in
                 os.environ.get("ADF_ROLLOUT_IMPORT_ROOTS", "").split(os.pathsep) if value.strip()]
        return {"default_source_path": str(self.root / "raw" / "rollout_trajectories"),
                "allowed_roots": list(dict.fromkeys([str(self.root / "raw"), *extra]))}

    def _no_links(self, path: Path):
        for item in (path, *path.parents):
            if _linked(item):
                raise RolloutImportError(f"路径不允许符号链接或目录联接：{item}", 422)

    def _source(self, value) -> Path:
        if not isinstance(value, str) or not value.strip():
            raise RolloutImportError("请选择服务器本地 Rollout 目录", 422)
        original = Path(value).expanduser()
        if not original.is_absolute():
            raise RolloutImportError("Rollout 目录必须为服务器上的绝对路径", 422)
        self._no_links(original)
        source = original.resolve()
        allowed = [Path(value).resolve() for value in self.options()["allowed_roots"]]
        if not any(source.is_relative_to(parent) for parent in allowed):
            raise RolloutImportError("Rollout 目录不在允许导入的目录内", 422)
        protected = [self.root / "raw" / "collection_batches", *
                     [self.root / part for part in ("batches", "system", "releases", "tmp")]]
        if any(source.is_relative_to(parent) or parent.is_relative_to(source) for parent in protected):
            raise RolloutImportError("不能导入平台管理目录或其父目录；请选择独立的原始 Rollout 子目录", 422)
        if not source.is_dir():
            raise RolloutImportError("Rollout 目录不存在或不是目录", 422)
        return source

    def _files(self, directory: Path, source: Path, budget: list[int]) -> list[dict]:
        files, pending = [], [directory]
        while pending:
            parent = pending.pop()
            self._no_links(parent)
            for path in sorted(parent.iterdir(), key=lambda p: p.name.casefold()):
                self._no_links(path)
                if path.is_dir():
                    pending.append(path)
                elif path.is_file():
                    relative = path.relative_to(source).as_posix()
                    size = path.stat().st_size
                    budget[0] += 1
                    budget[1] += size
                    if budget[0] > self.max_files or budget[1] > self.max_bytes:
                        raise RolloutImportError("Rollout 文件数量或总大小超过导入限额", 413)
                    files.append({"path": relative, "size": size, "sha256": workbook_digest(path)})
                else:
                    raise RolloutImportError(f"不支持的源文件类型：{path.relative_to(source)}", 422)
        return sorted(files, key=lambda item: item["path"])

    def _scan(self, data: dict, import_id: str, created_at: str) -> dict:
        source = self._source(data.get("source_path"))
        batch_id = _identifier(data.get("batch_id"), "新批次编号")
        errors, warnings, tasks, trajectories, budget = [], [], [], [], [0, 0]
        overrides = {}
        for item in data.get("task_overrides") or []:
            if not isinstance(item, dict):
                raise RolloutImportError("任务补充信息必须为对象", 422)
            case = _component(item.get("collection_case_id"), "任务目录编号")
            if case in overrides:
                raise RolloutImportError(f"任务补充信息重复：{case}", 422)
            overrides[case] = item
        seen_cases = set()
        for task_dir in sorted(source.iterdir(), key=lambda path: path.name.casefold()):
            self._no_links(task_dir)
            if task_dir.name == "_prefetch_staging":
                continue
            if not task_dir.is_dir():
                warnings.append(f"{task_dir.name}：不是任务目录，未作为轨迹导入")
                continue
            case = _component(task_dir.name, "任务目录编号")
            if case.casefold() in seen_cases:
                errors.append(f"{case}：任务目录编号重复或大小写冲突")
            seen_cases.add(case.casefold())
            override, goals, source_fields = overrides.get(case, {}), set(), {key: set() for key in ("app", "scene", "capability")}
            task = {"collection_case_id": case, "case_id": case,
                    "task_id": "rollout_task_" + payload_digest([batch_id, case]),
                    "source_result_id": None, "source_row_id": case, "source_kind": "rollout_import",
                    "task": "", "app": None, "scene": None, "capability": None,
                    "trajectory_count": 0, "step_count": 0}
            seen_names = set()
            for directory in sorted(task_dir.iterdir(), key=lambda path: path.name.casefold()):
                self._no_links(directory)
                if directory.name == "_prefetch_staging":
                    continue
                if not directory.is_dir():
                    warnings.append(f"{directory.relative_to(source)}：不是轨迹目录，未作为轨迹导入")
                    continue
                relative = directory.relative_to(source).as_posix()
                try:
                    _component(directory.name, "轨迹目录编号")
                    if directory.name.casefold() in seen_names:
                        raise RolloutImportError("轨迹目录编号重复或大小写冲突")
                    seen_names.add(directory.name.casefold())
                    files = self._files(directory, source, budget)
                    responses = [p for p in directory.iterdir() if p.is_file() and STEP_RESPONSE_RE.fullmatch(p.name)]
                    if not responses:
                        raise RolloutImportError("没有最终步骤动作响应文件")
                    steps = [int(STEP_RESPONSE_RE.fullmatch(p.name).group(1)) for p in responses]
                    if len(set(steps)) != len(steps) or sorted(steps) != list(range(1, len(steps) + 1)):
                        raise RolloutImportError("步骤编号重复或不连续，无法完整转换")
                    rows, row_warnings = collect_rows(directory)
                    if len(rows) != len(responses) or any(not row[3] for row in rows):
                        raise RolloutImportError("步骤缺少有效动作、截图或 XML：" + "；".join(row_warnings))
                    # Only this two-level directory is a trajectory. Nested runtime
                    # candidates may be files, but must never become extra trajectories.
                    entry = {"collection_case_id": case, "source_trajectory_id": directory.name,
                             "relative_dir": relative, "collected_at": created_at, "files": files}
                    test_run = {"batch_id": batch_id, "collection_run_id": "ri_" + import_id,
                                "batch_tasks": {case: task}}
                    normalized = self.runs._normalize(test_run, {"batch_id": batch_id,
                        "trajectories": [entry], "errors": []}, created_at)
                    self.runs._validate_source_files(test_run, normalized, root_override=source)
                    for warning in row_warnings:
                        warnings.append(f"{relative}：{warning}")
                    request = directory / "turn001_orch_model_request.json"
                    if request.exists():
                        try:
                            goal = extract_original_goal(request)
                            if goal:
                                goals.add(goal)
                            content = json.loads(request.read_text(encoding="utf-8-sig"))
                            for key in source_fields:
                                if _text(content.get(key)):
                                    source_fields[key].add(_text(content[key]))
                        except (OSError, ValueError, AttributeError, TypeError) as exc:
                            raise RolloutImportError("turn001_orch_model_request.json 无法读取") from exc
                    trajectories.append(entry)
                    task["trajectory_count"] += 1
                    task["step_count"] += len(rows)
                except (CollectionRunError, OSError, ValueError) as exc:
                    if isinstance(exc, CollectionRunError) and exc.status == 413:
                        raise
                    errors.append(f"{relative}：{exc}")
            task["task"] = _text(override.get("task")) or (next(iter(goals)) if len(goals) == 1 else "")
            if len(goals) > 1 and not _text(override.get("task")):
                errors.append(f"{case}：同一任务下的轨迹目标不一致，请确认并补充任务文本")
            if not task["task"]:
                errors.append(f"{case}：缺少原始目标，请补充任务文本后重新校验")
            for field, values in source_fields.items():
                if len(values) > 1:
                    errors.append(f"{case}：同一任务的 {field} 分类冲突，请修正源信息")
                task[field] = (next(iter(values)) if len(values) == 1 else None) or _text(override.get(field)) or _text(data.get(field)) or None
            if not task["app"]:
                warnings.append(f"{case}：缺少 App，汇总将显示为未记录 App")
            if not task["scene"] or not task["capability"]:
                warnings.append(f"{case}：缺少场景分类，汇总将显示为未分类")
            if not task["trajectory_count"]:
                errors.append(f"{case}：没有完整可导入的最终轨迹")
            tasks.append(task)
        unknown = set(overrides) - {task["collection_case_id"] for task in tasks}
        if unknown:
            errors.append("任务补充信息不属于所选目录：" + "、".join(sorted(unknown)))
        if not tasks:
            errors.append("没有任务目录；目录应为 任务目录/轨迹目录/步骤文件")
        return {"source_path": str(source), "batch_id": batch_id, "tasks": tasks,
                "trajectories": trajectories, "errors": errors, "warnings": warnings,
                "task_count": len(tasks), "trajectory_count": len(trajectories),
                "step_count": sum(task["step_count"] for task in tasks)}

    def _ensure_new_batch(self, batch_id: str, import_id: str | None = None):
        from .batch_lifecycle import ensure_batch_active
        ensure_batch_active(batch_id, self.root)
        # Include legacy stage-only batches, generated snapshots, and records that
        # are not yet represented by a current 00 artifact.
        intent = self.records.get("rollout_import_intents", import_id) if import_id else None
        batch_dir = self.root / "batches" / batch_id
        owned_empty = bool(intent and intent.get("batch_id") == batch_id and batch_dir.is_dir() and not any(batch_dir.iterdir()))
        if (batch_dir.exists() and not owned_empty) or (self.root / "system" / "task_generation" / "collection_batches" / batch_id).exists():
            raise RolloutImportError("批次编号已存在，请使用新的业务批次编号")
        if self.records.database_path.exists():
            with self.records._connection() as connection:
                if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='records'").fetchone():
                    for namespace, key, serialized in connection.execute("SELECT namespace, record_key, payload FROM records"):
                        if namespace in {"rollout_import_previews", "rollout_import_intents", "rollout_import_requests", "rollout_import_request_intents"}:
                            continue
                        record = json.loads(serialized)
                        if (key == batch_id or record.get("batch_id") == batch_id or record.get("storage_batch_id") == batch_id
                                or batch_id in (record.get("batch_ids") or []) or record.get("job_id") == batch_id):
                            raise RolloutImportError("批次编号已被使用，请使用新的业务批次编号")
        raw = self.root / "raw" / "collection_batches" / batch_id
        if raw.exists():
            intent = self.records.get("rollout_import_intents", import_id) if import_id else None
            if not intent or intent.get("batch_id") != batch_id:
                raise RolloutImportError("该批次已有原始数据目录，请使用新的批次编号")
            run_root = self.runs.run_root(batch_id, "ri_" + import_id)
            if any(path != raw / "runs" for path in raw.iterdir()) or (raw / "runs").is_dir() and any(path != run_root for path in (raw / "runs").iterdir()):
                raise RolloutImportError("该批次原始数据目录存在其他运行，不能覆盖")

    def preview(self, data: dict) -> dict:
        batch_id = _identifier(data.get("batch_id"), "新批次编号")
        with self.artifacts.batch_lock(batch_id):
            self._ensure_new_batch(batch_id)
        import_id, created = uuid.uuid4().hex, utc_now()
        scanned = self._scan(data, import_id, created)
        expires = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()
        result = {key: value for key, value in scanned.items() if key != "trajectories"}
        result.update(import_id=import_id, valid=not scanned["errors"], expires_at=expires)
        self.records.put("rollout_import_previews", import_id,
            {"import_id": import_id, "batch_id": batch_id, "created_at": created, "expires_at": expires,
             "data": data, "scan": scanned, "scan_digest": payload_digest(scanned), "result": result}, expected_revision=0)
        return result

    def get_batch(self, batch_id: str, *, require_workbook: bool = False) -> dict | None:
        _identifier(batch_id, "批次编号")
        record = self.records.get("rollout_import_batches", batch_id)
        if record is None:
            return None
        if require_workbook:
            raise RolloutImportError("该批次来自已有 Rollout 导入，仅支持接续已有结果，不能下发手机采集")
        artifact = self.artifacts.get(batch_id, "00_collection")
        if artifact is None:
            raise RolloutImportError("导入批次的来源快照缺失")
        payload = self.artifacts.read_payload(artifact)
        if payload.get("batch_id") != batch_id or payload.get("kind") != "rollout_import" or payload_digest(payload) != record.get("payload_sha256"):
            raise RolloutImportError("导入批次的来源快照校验失败")
        return payload

    def _response(self, record: dict, reused: bool) -> dict:
        return {**{key: record[key] for key in ("batch_id", "collection_run_id", "task_count", "trajectory_count", "step_count")}, "reused": reused}

    def _remove_owned(self, path: Path, parent: Path):
        self._no_links(path)
        target, allowed = path.resolve(), parent.resolve()
        if target == allowed or not target.is_relative_to(allowed):
            raise RolloutImportError("拒绝清理导入暂存目录之外的路径")
        if target.exists():
            shutil.rmtree(target)

    def commit(self, import_id: str, request_id: str) -> dict:
        from .batch_lifecycle import ensure_batch_active
        _identifier(import_id, "import_id")
        _identifier(request_id, "request_id")
        with self.artifacts.batch_lock("rollout-request-" + payload_digest(request_id)):
            request = self.records.get("rollout_import_requests", request_id)
            if request:
                if request["import_id"] != import_id:
                    raise RolloutImportError("相同 request_id 对应不同的导入预览")
                ensure_batch_active(request["batch_id"], self.root)
                return self._response(request, True)
            request_intent = self.records.get("rollout_import_request_intents", request_id)
            if request_intent and request_intent.get("import_id") != import_id:
                raise RolloutImportError("相同 request_id 已绑定另一个导入预览")
            if any(item.get("request_id") == request_id and item.get("import_id") != import_id
                   for item in self.records.list("rollout_import_intents")):
                raise RolloutImportError("相同 request_id 已绑定另一个导入预览")
            preview = self.records.get("rollout_import_previews", import_id)
            if preview is None:
                raise RolloutImportError("导入预览不存在或已过期，请重新校验", 404)
            batch_id = preview["batch_id"]
            with self.artifacts.batch_lock(batch_id):
                existing = self.records.get("rollout_import_batches", batch_id)
                if existing and existing.get("import_id") == import_id:
                    ensure_batch_active(batch_id, self.root)
                    self.records.put("rollout_import_requests", request_id, {**existing, "request_id": request_id}, expected_revision=0)
                    return self._response(existing, True)
                if datetime.fromisoformat(preview["expires_at"].replace("Z", "+00:00")) <= datetime.now(timezone.utc):
                    raise RolloutImportError("导入预览已过期，请重新校验")
                if not preview["result"]["valid"]:
                    raise RolloutImportError("校验未通过，不能登记批次", 422)
                self.artifacts._recover(batch_id)
                self._ensure_new_batch(batch_id, import_id)
                current = self._scan(preview["data"], import_id, preview["created_at"])
                if payload_digest(current) != preview["scan_digest"]:
                    raise RolloutImportError("源目录内容或分类已变化，请重新校验后导入")
                run_id, created = "ri_" + import_id, preview["created_at"]
                final = self.runs.run_root(batch_id, run_id)
                staging_parent = self.root / "tmp" / "rollout_imports"
                staging = staging_parent / import_id
                intent = self.records.get("rollout_import_intents", import_id)
                intent_writes = []
                intent_payload = {"import_id": import_id, "batch_id": batch_id, "request_id": request_id,
                                  "created_at": created, "expires_at": preview["expires_at"]}
                if intent is None:
                    intent_writes.append({"namespace": "rollout_import_intents", "key": import_id,
                                          "payload": intent_payload, "expected_revision": 0})
                if request_intent is None:
                    intent_writes.append({"namespace": "rollout_import_request_intents", "key": request_id,
                                          "payload": intent_payload, "expected_revision": 0})
                if intent_writes:
                    self.records.put_many(intent_writes)
                self._remove_owned(staging, staging_parent)
                run = {"schema_version": 1, "batch_id": batch_id, "collection_run_id": run_id, "run_id": run_id,
                       "source_kind": "rollout_import", "dispatch_key": None, "status": "completed",
                       "created_at": created, "completed_at": created, "output_dir": str(final),
                       "batch_tasks": {task["collection_case_id"]: task for task in current["tasks"]},
                       "trajectories": [], "errors": [], "dispatch_error": None}
                manifest = self.runs._normalize(run, {"batch_id": batch_id,
                    "trajectories": current["trajectories"], "errors": []}, created)
                source = self._source(current["source_path"])
                try:
                    if final.exists():
                        self.runs._validate_source_files(run, manifest, root_override=final)
                    else:
                        staging.mkdir(parents=True, exist_ok=False)
                        for trajectory in manifest["trajectories"]:
                            for item in trajectory["files"]:
                                self._no_links(source / item["path"])
                                incoming = contained_path(source, item["path"])
                                destination = contained_path(staging, item["path"])
                                destination.parent.mkdir(parents=True, exist_ok=True)
                                with incoming.open("rb") as reader, destination.open("xb") as writer:
                                    shutil.copyfileobj(reader, writer)
                                    writer.flush()
                                    os.fsync(writer.fileno())
                        self.runs._validate_source_files(run, manifest, root_override=staging)
                        if payload_digest(self._scan(preview["data"], import_id, created)) != preview["scan_digest"]:
                            raise RolloutImportError("复制期间源目录发生变化，请重新校验后导入")
                        final.parent.mkdir(parents=True, exist_ok=True)
                        staging.rename(final)
                    # Freeze classification and task identity without fabricating a
                    # generated task workbook or any downstream process artifact.
                    snapshot = {"schema_version": 1, "batch_id": batch_id, "job_id": None,
                                "source_job_id": None, "kind": "rollout_import",
                                "task_count": current["task_count"], "tasks": current["tasks"],
                                "errors": [], "warnings": current["warnings"]}
                    payload = {"schema_version": 1, "batch_id": batch_id, "source_job_id": None,
                               "kind": "rollout_import", "source_kind": "rollout_import", "created_at": created,
                               "name": _text(preview["data"].get("name")) or batch_id,
                               "description": _text(preview["data"].get("name")), "filename": None,
                               "task_count": current["task_count"], "trajectory_count": current["trajectory_count"],
                               "step_count": current["step_count"], "apps": list(dict.fromkeys(task["app"] for task in current["tasks"] if task["app"])),
                               "snapshot": snapshot, "warnings": current["warnings"],
                               "import_id": import_id, "collection_run_id": run_id,
                               "source_path": current["source_path"], "source_manifest_sha256": preview["scan_digest"]}
                    run.update(manifest, manifest_sha256=payload_digest(manifest),
                               batch_snapshot_sha256=payload_digest(snapshot), trajectory_count=current["trajectory_count"])
                    record = {"batch_id": batch_id, "collection_run_id": run_id, "import_id": import_id,
                              "created_at": created, "payload_sha256": payload_digest(payload),
                              **{key: current[key] for key in ("task_count", "trajectory_count", "step_count")}}
                    self.artifacts.publish_many(batch_id, [{"stage": "00_collection", "payload": payload,
                        "metadata": {"kind": "rollout_import", "task_count": current["task_count"]},
                        "source_refs": [{"kind": "rollout_import", "import_id": import_id, "sha256": preview["scan_digest"]}]}],
                        record_entries=[{"namespace": "collection_runs", "key": run_id, "payload": run, "expected_revision": 0},
                            {"namespace": "rollout_import_batches", "key": batch_id, "payload": record, "expected_revision": 0},
                            {"namespace": "rollout_import_requests", "key": request_id,
                             "payload": {**record, "request_id": request_id}, "expected_revision": 0}])
                    return self._response(record, False)
                except BaseException:
                    # A commit acknowledgement may be lost after SQLite committed.
                    # Never delete registered raw inputs or current artifacts.
                    saved = self.records.get("rollout_import_batches", batch_id)
                    if saved and saved.get("import_id") == import_id:
                        return self._response(saved, True)
                    raise
                finally:
                    try:
                        self._remove_owned(staging, staging_parent)
                    except (OSError, CollectionRunError):
                        LOG.exception("Rollout import staging cleanup deferred: %s", import_id)

    def recover(self) -> dict:
        """Clean expired unregistered imports; interrupted commits remain retryable."""
        self.artifacts.recover()
        removed, retained = 0, 0
        now = datetime.now(timezone.utc)
        for preview in self.records.list("rollout_import_previews"):
            batch_id, import_id = preview["batch_id"], preview["import_id"]
            with self.artifacts.batch_lock(batch_id):
                if self.records.get("rollout_import_batches", batch_id):
                    retained += 1
                    continue
                if datetime.fromisoformat(preview["expires_at"].replace("Z", "+00:00")) > now:
                    continue
                intent = self.records.get("rollout_import_intents", import_id)
                if intent and intent.get("batch_id") == batch_id:
                    run_id = "ri_" + import_id
                    if not self.records.get("collection_runs", run_id):
                        self._remove_owned(self.runs.run_root(batch_id, run_id), self.runs.raw_root(batch_id))
                    self._remove_owned(self.root / "tmp" / "rollout_imports" / import_id, self.root / "tmp" / "rollout_imports")
                    # Only remove these exact, now-empty owned parents. Never
                    # recursively remove a batch root that may contain other data.
                    for parent in (self.runs.raw_root(batch_id) / "runs", self.runs.raw_root(batch_id),
                                   self.root / "batches" / batch_id):
                        self._no_links(parent)
                        if parent.is_dir() and not any(parent.iterdir()):
                            parent.rmdir()
                    self.records.delete("rollout_import_intents", import_id)
                for request in self.records.list("rollout_import_request_intents"):
                    if request.get("import_id") == import_id:
                        self.records.delete("rollout_import_request_intents", request["request_id"])
                self.records.delete("rollout_import_previews", import_id)
                removed += 1
        return {"removed": removed, "retained": retained}
