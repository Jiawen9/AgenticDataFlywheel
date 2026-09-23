"""Collection-run registry and validated immutable raw-input manifests.

Current run state is stored in SQLite. Raw files belong to a single batch/run;
readers only consume explicitly completed manifests, never discover old folders.
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Callable

from .batch_operations import active_batch_lock
from .data_store import DATA_ROOT, RecordStore, RevisionConflict, rebase_data_path
from .data_store.paths import contained_path
from .task_generation.collection_batches import (
    CollectionBatchDetail, collection_batch_payload, payload_digest, workbook_digest,
)


class CollectionRunError(ValueError):
    def __init__(self, message: str, status: int = 409):
        super().__init__(message)
        self.status = status


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _component(value, label: str) -> str:
    if (not isinstance(value, str) or not value or len(value) > 180
            or value in {".", ".."} or value.endswith((".", " "))
            or re.search(r'[<>:"/\\|?*\x00-\x1f]', value)
            or re.fullmatch(r"(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(\..*)?", value, re.I)):
        raise CollectionRunError(f"{label} 无效")
    return value


def _identifier(value, label: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", value):
        raise CollectionRunError(f"{label} 无效")
    return value


def _relative(value, label: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise CollectionRunError(f"{label} 必须为使用 / 的相对路径")
    parts = value.split("/")
    for part in parts:
        _component(part, label)
    if PurePosixPath(value).is_absolute():
        raise CollectionRunError(f"{label} 必须为相对路径")
    return "/".join(parts)


def _timestamp(value, default: str) -> str:
    if value is None:
        return default
    if not isinstance(value, str):
        raise CollectionRunError("collected_at 必须为带时区的 ISO 时间")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("timezone required")
    except ValueError as exc:
        raise CollectionRunError("collected_at 必须为带时区的 ISO 时间") from exc
    return value


class CollectionRunStore:
    def __init__(self, root: Path = DATA_ROOT, *, batch_loader: Callable[[str], dict] | None = None):
        self.root = contained_path(Path(root))
        self.records = RecordStore(self.root)
        self.batch_loader = batch_loader

    def batch(self, batch_id: str, *, require_workbook: bool = False) -> dict:
        batch_id = _identifier(batch_id, "采集批次编号")
        if self.batch_loader is not None:
            payload = self.batch_loader(batch_id)
            if payload is None:
                raise CollectionRunError("采集批次不存在", 404)
            return payload
        from .manual_collection import ManualCollectionStore
        manual = ManualCollectionStore(self.root).get(batch_id, require_workbook=require_workbook)
        if manual is not None:
            return manual
        from .rollout_imports import RolloutImportStore
        imported = RolloutImportStore(self.root).get_batch(batch_id, require_workbook=require_workbook)
        if imported is not None:
            return imported
        directory = contained_path(self.root, "system", "task_generation", "collection_batches", batch_id)
        path = directory / "batch.json"
        if not path.is_file():
            raise CollectionRunError("采集批次不存在", 404)
        try:
            stored = json.loads(path.read_text(encoding="utf-8"))
            payload = CollectionBatchDetail.model_validate(stored).model_dump()
            if (payload["batch_id"] != batch_id or payload["source_job_id"] != batch_id
                    or payload["snapshot"]["job_id"] != batch_id
                    or payload != collection_batch_payload(payload["snapshot"], payload["created_at"])
                    or stored.get("_integrity", {}).get("payload_sha256") != payload_digest(payload)):
                raise ValueError("批次快照校验失败")
            workbook_sha = stored["_integrity"].get("workbook_sha256")
            if require_workbook:
                workbook = contained_path(directory, payload["filename"])
                if not workbook.is_file() or workbook_digest(workbook) != workbook_sha:
                    raise ValueError("批次 Excel 校验失败")
            return {**payload, "workbook_sha256": workbook_sha}
        except (OSError, ValueError, KeyError) as exc:
            raise CollectionRunError("采集批次快照缺失、损坏或校验失败") from exc

    def raw_root(self, batch_id: str) -> Path:
        path = self.root / "raw" / "collection_batches" / _identifier(batch_id, "采集批次编号")
        self._no_links(path)
        return contained_path(self.root, path.relative_to(self.root).as_posix())

    def _no_links(self, path: Path) -> None:
        for candidate in (path, *path.parents):
            if candidate == self.root:
                break
            if candidate.is_symlink() or (hasattr(candidate, "is_junction") and candidate.is_junction()):
                raise CollectionRunError("采集源路径不允许符号链接或目录联接")

    def run_root(self, batch_id: str, run_id: str) -> Path:
        root = self.raw_root(batch_id)
        path = root / "runs" / _identifier(run_id, "采集运行编号")
        self._no_links(path)
        return contained_path(root, path.relative_to(root).as_posix())

    def get(self, run_id: str) -> dict | None:
        value = self.records.get("collection_runs", _identifier(run_id, "采集运行编号"))
        return self._current_paths(value) if value is not None else None

    def _current_paths(self, run: dict) -> dict:
        # Frozen run identities/checksums do not include this dispatch location.
        # Relocate only the path returned to the collector, never its manifest.
        try:
            output = rebase_data_path(run["output_dir"], self.root)
        except ValueError as exc:
            raise CollectionRunError("采集输出目录超出当前或已登记数据目录") from exc
        if output != self.run_root(run["batch_id"], run["collection_run_id"]):
            raise CollectionRunError("采集输出目录与已登记批次或运行不匹配")
        return {**run, "output_dir": str(output)}

    def list_runs(self, batch_id: str | None = None) -> list[dict]:
        if batch_id is not None:
            _identifier(batch_id, "采集批次编号")
        return sorted((self._current_paths(run) for run in self.records.list("collection_runs")
                       if batch_id is None or run["batch_id"] == batch_id), key=lambda run: (run["created_at"], run["collection_run_id"]))

    def create(self, batch_id: str, *, dispatch_key: str | None = None,
               metadata: dict | None = None, workbook_sha256: str | None = None) -> tuple[dict, bool]:
        with active_batch_lock(batch_id, self.root):
            """Reserve one dispatch. Repeated keys never create or dispatch twice."""
            from .pipeline_access import ensure_pipeline_write
            ensure_pipeline_write(batch_id, self.root, action="collect")
            batch = self.batch(batch_id, require_workbook=True)
            if dispatch_key is not None:
                _identifier(dispatch_key, "request_id")
            if workbook_sha256 is not None and batch.get("workbook_sha256") != workbook_sha256:
                raise CollectionRunError("上传文件与冻结的采集批次 Excel 不一致")
            tasks = batch["snapshot"]["tasks"]
            by_case = {}
            seen_paths = set()
            for task in tasks:
                case_id = _component(task.get("collection_case_id"), "采集用例编号")
                if case_id.casefold() in seen_paths:
                    raise CollectionRunError("采集用例编号重复或在文件系统中冲突")
                seen_paths.add(case_id.casefold())
                manual = batch.get("kind") == "manual_collection"
                if (not isinstance(task.get("task_id"), str) or not task["task_id"].strip()
                        or (manual and (task.get("source_result_id") is not None or not task.get("source_row_id")))
                        or (not manual and not task.get("source_result_id"))):
                    raise CollectionRunError("采集批次缺少生成任务编号或来源结果编号")
                by_case[case_id] = task
            if not by_case:
                raise CollectionRunError("采集批次没有任务")
            request = {"batch_id": batch_id, "metadata": metadata or {}, "workbook_sha256": workbook_sha256 or batch.get("workbook_sha256")}
            run_id = "cr_" + (payload_digest([batch_id, dispatch_key]) if dispatch_key else uuid.uuid4().hex)
            root = self.run_root(batch_id, run_id)
            payload = {
                "schema_version": 1, "batch_id": batch_id, "collection_run_id": run_id, "run_id": run_id,
                "dispatch_key": dispatch_key, "request_digest": payload_digest(request),
                "request": request, "created_at": _now(), "completed_at": None,
                "status": "dispatching", "output_dir": str(root), "batch_tasks": by_case,
                "batch_snapshot_sha256": payload_digest(batch["snapshot"]),
                "trajectories": [], "errors": [], "dispatch_error": None,
            }
            root.mkdir(parents=True, exist_ok=True)
            try:
                saved = self.records.put("collection_runs", run_id, payload, expected_revision=0)
                return saved, True
            except RevisionConflict:
                existing = self.get(run_id)
                if existing["request_digest"] != payload["request_digest"]:
                    raise CollectionRunError("相同 request_id 对应的运行参数不同")
                return existing, False

    def retry_dispatch(self, run_id: str) -> tuple[dict, bool]:
        current = self.get(run_id)
        if current is None:
            raise CollectionRunError("采集运行不存在", 404)
        with active_batch_lock(current["batch_id"], self.root):
            current = self.get(run_id)
            if current is None:
                raise CollectionRunError("采集运行不存在", 404)
            if current["status"] != "failed":
                return current, False
            update = {**current, "status": "dispatching", "dispatch_error": None}
            try:
                return self.records.put("collection_runs", run_id, update, expected_revision=current["storage_revision"]), True
            except RevisionConflict:
                return self.get(run_id), False

    def dispatched(self, run_id: str, response: dict) -> dict:
        current = self.get(run_id)
        if current is None:
            raise CollectionRunError("采集运行不存在", 404)
        with active_batch_lock(current["batch_id"], self.root):
            def mutate(run):
                # A fast collector may finish before its dispatch response arrives.
                if run["status"] not in {"completed", "interrupted"}:
                    run["status"] = "running"
                run.update(dispatch_response=response, dispatched_at=_now(), dispatch_error=None)
            return self.records.update("collection_runs", run_id, mutate)

    def dispatch_failed(self, run_id: str, error: str) -> dict:
        current = self.get(run_id)
        if current is None:
            raise CollectionRunError("采集运行不存在", 404)
        with active_batch_lock(current["batch_id"], self.root):
            def mutate(run):
                if run["status"] not in {"completed", "interrupted"}:
                    run.update(status="failed", dispatch_error=str(error))
            return self.records.update("collection_runs", run_id, mutate)

    def _normalize(self, run: dict, manifest: dict, completed_at: str) -> dict:
        if not isinstance(manifest, dict) or manifest.get("batch_id") != run["batch_id"]:
            raise CollectionRunError("完成清单的 batch_id 与采集运行不匹配")
        if manifest.get("collection_run_id", run["collection_run_id"]) != run["collection_run_id"]:
            raise CollectionRunError("完成清单的 collection_run_id 不匹配")
        entries, errors = manifest.get("trajectories"), manifest.get("errors", [])
        if not isinstance(entries, list) or not isinstance(errors, list):
            raise CollectionRunError("trajectories 与 errors 必须为列表")
        trajectories, directories = [], set()
        for entry in entries:
            if not isinstance(entry, dict):
                raise CollectionRunError("轨迹清单项必须为对象")
            case_id = _component(entry.get("collection_case_id"), "采集用例编号")
            source_task = run["batch_tasks"].get(case_id)
            if source_task is None:
                raise CollectionRunError(f"用例不属于本采集批次：{case_id}")
            relative_dir = _relative(entry.get("relative_dir"), "轨迹目录")
            parts = relative_dir.split("/")
            if len(parts) not in {2, 3} or parts[0] != case_id:
                raise CollectionRunError("轨迹目录必须为 collection_case_id/设备限定轨迹目录")
            if len(parts) == 3 and parts[1] != entry.get("phone_id"):
                raise CollectionRunError("轨迹目录的手机编号与清单不匹配")
            if relative_dir.casefold() in directories:
                raise CollectionRunError("完成清单包含重复轨迹目录")
            directories.add(relative_dir.casefold())
            source_id = _component(entry.get("source_trajectory_id", entry.get("trajectory_id", parts[-1])), "原轨迹编号")
            file_entries = entry.get("files")
            if not isinstance(file_entries, list) or not file_entries:
                raise CollectionRunError("轨迹必须声明完整文件清单")
            files, names = [], set()
            for item in file_entries:
                if not isinstance(item, dict):
                    raise CollectionRunError("文件清单项必须为对象")
                name = _relative(item.get("path"), "源文件路径")
                if not name.startswith(relative_dir + "/") or name.casefold() in names:
                    raise CollectionRunError("源文件跨越轨迹目录或重复")
                names.add(name.casefold())
                digest = item.get("sha256")
                if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", digest):
                    raise CollectionRunError("源文件 SHA256 无效")
                value = {"path": name, "sha256": digest.lower()}
                if "size" in item:
                    if type(item["size"]) is not int or item["size"] < 0:
                        raise CollectionRunError("源文件 size 无效")
                    value["size"] = item["size"]
                files.append(value)
            trajectories.append({
                "collection_run_id": run["collection_run_id"], "collection_case_id": case_id,
                "task_id": source_task["task_id"], "source_result_id": source_task["source_result_id"],
                "task": source_task.get("task"), "app": source_task.get("app"),
                "source_trajectory_id": source_id, "relative_dir": relative_dir,
                "collected_at": _timestamp(entry.get("collected_at"), completed_at),
                "files": sorted(files, key=lambda item: item["path"]),
                **({"phone_id": entry["phone_id"]} if entry.get("phone_id") is not None else {}),
                **({"source_row_id": source_task["source_row_id"], "source_kind": source_task["source_kind"]}
                   if source_task.get("source_kind") in {"manual_collection", "rollout_import"} else {}),
            })
        normalized_errors = []
        for item in errors:
            if not isinstance(item, dict) or not isinstance(item.get("error", item.get("message")), str):
                raise CollectionRunError("errors 项必须包含 error 文本")
            case_id = item.get("collection_case_id")
            if case_id is not None and case_id not in run["batch_tasks"]:
                raise CollectionRunError("失败用例不属于本采集批次")
            normalized_errors.append({"collection_case_id": case_id, "error": item.get("error", item.get("message"))})
        return {"batch_id": run["batch_id"], "collection_run_id": run["collection_run_id"],
                "trajectories": sorted(trajectories, key=lambda item: item["relative_dir"]),
                "errors": normalized_errors}

    def _validate_sources(self, run: dict, manifest: dict) -> None:
        try:
            self._validate_source_files(run, manifest)
        except CollectionRunError:
            raise
        except (OSError, ValueError) as exc:
            raise CollectionRunError(f"采集源文件无法校验：{exc}") from exc

    def _validate_source_files(self, run: dict, manifest: dict, *, root_override: Path | None = None) -> None:
        root = root_override if root_override is not None else self.run_root(run["batch_id"], run["collection_run_id"])
        for trajectory in manifest["trajectories"]:
            self._no_links(root / trajectory["relative_dir"])
            directory = contained_path(root, trajectory["relative_dir"])
            if not directory.is_dir():
                raise CollectionRunError(f"轨迹目录不存在：{trajectory['relative_dir']}")
            declared = {item["path"] for item in trajectory["files"]}
            actual = set()
            for path in directory.rglob("*"):
                checked = contained_path(root, path.relative_to(root).as_posix())
                if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
                    raise CollectionRunError("轨迹文件不允许符号链接或目录联接")
                if checked.is_file():
                    actual.add(path.relative_to(root).as_posix())
            if actual != declared:
                raise CollectionRunError("轨迹文件清单不完整或有未声明文件")
            for item in trajectory["files"]:
                path = contained_path(root, item["path"])
                stat = path.stat()
                if not path.is_file() or ("size" in item and stat.st_size != item["size"]) or workbook_digest(path) != item["sha256"]:
                    raise CollectionRunError(f"源文件缺失或 SHA256 不匹配：{item['path']}")
            evaluation = directory / "_trajectory_for_evaluate.json"
            if not evaluation.is_file():
                raise CollectionRunError("轨迹缺少 _trajectory_for_evaluate.json")
            try:
                content = json.loads(evaluation.read_text(encoding="utf-8-sig"))
                if not isinstance(content.get("actions_flat"), list) or not content["actions_flat"]:
                    raise ValueError("actions_flat missing")
            except (OSError, ValueError, AttributeError) as exc:
                raise CollectionRunError("轨迹 evaluation JSON 缺少有效动作") from exc
            from .export_vla_trajectories import STEP_RESPONSE_RE, collect_rows
            if not any(path.is_file() and STEP_RESPONSE_RE.fullmatch(path.name) for path in directory.iterdir()):
                raise CollectionRunError("指定目录不是包含最终步骤的原轨迹目录")
            rows, warnings = collect_rows(directory)
            if not rows or any(not row[3] for row in rows) or any("skipped because" in warning for warning in warnings):
                raise CollectionRunError("轨迹缺少完整可转换的步骤文件")

    def complete(self, run_id: str, manifest: dict) -> tuple[dict, bool]:
        current = self.get(run_id)
        if current is None:
            raise CollectionRunError("采集运行不存在", 404)
        with active_batch_lock(current["batch_id"], self.root):
            while True:
                run = self.get(run_id)
                if run is None:
                    raise CollectionRunError("采集运行不存在", 404)
                if run["status"] == "interrupted":
                    raise CollectionRunError("采集运行已中断，不能回写迟到结果")
                completed_at = run.get("completed_at") or _now()
                normalized = self._normalize(run, manifest, completed_at)
                self._validate_sources(run, normalized)
                digest = payload_digest(normalized)
                if run["status"] == "completed":
                    if digest != run.get("manifest_sha256"):
                        raise CollectionRunError("已完成采集运行的清单不可修改；请创建新的采集运行")
                    return run, False
                updated = {**run, **normalized, "status": "completed", "completed_at": completed_at,
                           "manifest_sha256": digest, "trajectory_count": len(normalized["trajectories"])}
                try:
                    return self.records.put("collection_runs", run_id, updated, expected_revision=run["storage_revision"]), True
                except RevisionConflict:
                    continue

    def ready_input(self, batch_id: str) -> dict:
        self.batch(batch_id)
        runs, trajectories, errors = [], [], []
        for run in self.list_runs(batch_id):
            if run["status"] != "completed":
                continue
            normalized = {key: run[key] for key in ("batch_id", "collection_run_id", "trajectories", "errors")}
            if payload_digest(normalized) != run.get("manifest_sha256"):
                raise CollectionRunError("已完成采集运行的清单校验失败")
            self._validate_sources(run, normalized)
            runs.append({"collection_run_id": run["collection_run_id"], "completed_at": run["completed_at"],
                         "manifest_sha256": run["manifest_sha256"], "trajectory_count": len(run["trajectories"])})
            trajectories.extend(run["trajectories"])
            errors.extend({**error, "collection_run_id": run["collection_run_id"]} for error in run["errors"])
        if not trajectories:
            raise CollectionRunError("该采集批次尚无已登记完成的可用轨迹")
        value = {"schema_version": 1, "batch_id": batch_id, "raw_root": str(self.raw_root(batch_id)),
                 "runs": runs, "trajectories": trajectories, "errors": errors}
        return {**value, "input_digest": payload_digest({key: value[key] for key in ("batch_id", "runs", "trajectories", "errors")})}

    def freeze_ready_input(self, batch_id: str) -> dict:
        """Return a validated value for the caller to freeze with its job record."""
        return self.ready_input(batch_id)
