from __future__ import annotations

import json
import shutil
import threading
import uuid
from concurrent.futures import Executor, ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

import pandas as pd

from .constants import (
    AUGMENTATION_RESULT_COLUMNS,
    EXPORTS_DIR,
    INITIAL_RESULT_COLUMNS,
    JOBS_DIR,
    KNOWLEDGE_BASE_DIR,
    LOGS_DIR,
    RUNS_DIR,
)
from .knowledge_base import node_id, snapshot_knowledge_base
from .tree_store import VersionConflict, current_root, flatten, read_tree
from .service import run_augmentation, run_initial_generation


Runner = Callable[..., dict[str, Any]]


def _now() -> str:
    return datetime.now(ZoneInfo("Asia/Shanghai")).isoformat()


class TaskGenerationJobManager:
    """Persistent queue for both task-generation workflows."""

    def __init__(
        self,
        jobs_dir: Path = JOBS_DIR,
        runs_dir: Path = RUNS_DIR,
        exports_dir: Path = EXPORTS_DIR,
        knowledge_base_dir: Path = KNOWLEDGE_BASE_DIR,
        logs_dir: Path = LOGS_DIR,
        executor: Executor | None = None,
        initial_runner: Runner = run_initial_generation,
        augmentation_runner: Runner = run_augmentation,
    ) -> None:
        self.jobs_dir = jobs_dir
        self.runs_dir = runs_dir
        self.exports_dir = exports_dir
        self.logs_dir = logs_dir
        self.knowledge_base_dir = knowledge_base_dir
        self.initial_runner = initial_runner
        self.augmentation_runner = augmentation_runner
        self._lock = threading.RLock()
        self._owns_executor = executor is None
        self._executor = executor or ThreadPoolExecutor(max_workers=1, thread_name_prefix="task-generation")
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        self.exports_dir.mkdir(parents=True, exist_ok=True)
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.mark_interrupted_jobs()

    def _job_path(self, job_id: str) -> Path:
        return self.jobs_dir / f"{job_id}.json"

    def _run_dir(self, job_id: str) -> Path:
        return self.runs_dir / job_id

    def _results_path(self, job_id: str) -> Path:
        return self._run_dir(job_id) / "results.json"

    def _write_json(self, path: Path, value: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.tmp")
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)

    def _write_job(self, payload: dict[str, Any]) -> None:
        self._write_json(self._job_path(payload["job_id"]), payload)

    def _log(self, job_id: str, message: str) -> None:
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        with (self.logs_dir / f"{job_id}.log").open("a", encoding="utf-8") as stream:
            stream.write(f"{_now()} {message}\n")

    def get(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            path = self._job_path(job_id)
            if not path.is_file():
                return None
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError, json.JSONDecodeError):
                return None
            return value if isinstance(value, dict) else None

    def list_jobs(self) -> list[dict[str, Any]]:
        with self._lock:
            values = []
            for path in self.jobs_dir.glob("*.json"):
                try:
                    value = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError, json.JSONDecodeError):
                    continue
                if isinstance(value, dict):
                    value.pop("execution_units", None)
                    value.pop("selections", None)
                    values.append(value)
            return sorted(values, key=lambda item: str(item.get("created_at", "")), reverse=True)

    def mark_interrupted_jobs(self) -> None:
        for path in self.jobs_dir.glob("*.json"):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            if isinstance(payload, dict) and payload.get("status") in {"queued", "running"}:
                payload.update({"status": "interrupted", "stage": "interrupted", "completed_at": _now(), "error": "服务重启导致作业中断；重新提交可继续使用新的知识库快照。"})
                self._write_job(payload)

    def _new_job(self, kind: str, *, total_items: int, generate_n: int, input_filename: str | None = None,
                 kb_source: Path | None = None, parameters: dict[str, Any] | None = None) -> dict[str, Any]:
        job_id = uuid.uuid4().hex
        self._run_dir(job_id).mkdir(parents=True, exist_ok=True)
        snapshot = snapshot_knowledge_base(self._run_dir(job_id) / "KnowledgeBase", root=kb_source or self.knowledge_base_dir)
        payload = {
            "job_id": job_id,
            "kind": kind,
            "status": "queued",
            "stage": "queued",
            "created_at": _now(),
            "started_at": None,
            "completed_at": None,
            "current_item": None,
            "completed_items": 0,
            "total_items": total_items,
            "percent": 0,
            "generate_n": generate_n,
            "input_filename": input_filename,
            "result_count": 0,
            "errors": [],
            "warnings": [],
            "error": None,
            "knowledge_base": snapshot,
            "knowledge_base_version": snapshot["version"],
            **(parameters or {}),
        }
        with self._lock:
            self._write_job(payload)
        return payload

    def submit_initial(self, selections: list[dict[str, Any]], generate_n: int, *, version: str) -> dict[str, Any]:
        if not 1 <= generate_n <= 20:
            raise ValueError("每个任务类型/App 生成数量必须为 1–20")
        source = current_root(self.knowledge_base_dir)
        tree = read_tree(source)
        if version != tree["version"]:
            raise VersionConflict("知识库已更新，请刷新场景树后重新提交")
        available = {leaf["id"]: (leaf, labels) for leaf, labels in flatten(tree["scenes"])}
        units = []
        normalized = []
        seen: set[str] = set()
        for selection in selections:
            identifier = selection["node_id"]
            if identifier not in available or identifier in seen:
                raise ValueError(f"任务类型不存在或重复选择：{identifier}")
            seen.add(identifier)
            leaf, labels = available[identifier]
            configs = {config["app"]: config for config in leaf["app_configs"]}
            apps = list(dict.fromkeys(selection["apps"]))
            if not apps or any(app not in configs for app in apps):
                raise ValueError(f"任务类型 {leaf['label']} 必须选择至少一个适用 App，且不能选择未配置的 App")
            normalized.append({"node_id": identifier, "apps": apps})
            for app in apps:
                units.append({"execution_unit_id": node_id(app, *labels), "task_type_id": identifier,
                              **dict(zip(("scene", "capability", "sub_capability"), labels)), **configs[app]})
        if not units:
            raise ValueError("至少选择一个任务类型和适用 App")
        payload = self._new_job("task_generation", total_items=len(units), generate_n=generate_n, kb_source=source,
                                parameters={"selections": normalized, "execution_units": units,
                                            "task_type_count": len(normalized), "expected_main_tasks": len(units) * generate_n})
        self._executor.submit(self._run_initial, payload["job_id"], [unit["execution_unit_id"] for unit in units], generate_n)
        return payload

    def submit_augmentation(self, input_path: Path, original_filename: str, generate_n: int) -> dict[str, Any]:
        payload = self._new_job("augmentation", total_items=0, generate_n=generate_n, input_filename=original_filename)
        target = self._run_dir(payload["job_id"]) / "input.xlsx"
        shutil.copy2(input_path, target)
        self._executor.submit(self._run_augmentation, payload["job_id"], target, generate_n)
        return payload

    def _progress(self, job_id: str, changes: dict[str, Any]) -> None:
        with self._lock:
            payload = self.get(job_id)
            if payload is None:
                return
            payload.update(changes)
            self._write_job(payload)

    def _finish(self, job_id: str, outcome: dict[str, Any]) -> None:
        results = outcome.get("results", [])
        errors = outcome.get("errors", [])
        with self._lock:
            self._write_json(self._results_path(job_id), results)
            payload = self.get(job_id)
            if payload is None:
                return
            status = "succeeded" if not errors else "partial" if results else "failed"
            payload.update({
                "status": status,
                "stage": "succeeded" if status == "succeeded" else status,
                "completed_at": _now(),
                "percent": 100 if status != "failed" else payload.get("percent", 0),
                "result_count": len(results),
                "errors": errors,
                "warnings": outcome.get("warnings", []),
                "error": None if status in {"succeeded", "partial"} else (errors[0].get("error") if errors else "作业未生成结果"),
            })
            self._write_job(payload)

    def _run_initial(self, job_id: str, node_ids: list[str], generate_n: int) -> None:
        self._progress(job_id, {"status": "running", "stage": "preparing", "started_at": _now()})
        self._log(job_id, f"开始任务生成，节点数={len(node_ids)}，每节点={generate_n}")
        try:
            outcome = self.initial_runner(node_ids, generate_n, kb_root=self._run_dir(job_id) / "KnowledgeBase", progress=lambda value: self._progress(job_id, value))
            self._log(job_id, f"任务生成结束，结果数={len(outcome.get('results', []))}，错误数={len(outcome.get('errors', []))}")
            self._finish(job_id, outcome)
        except Exception as exc:
            self._log(job_id, f"任务生成失败：{exc}")
            self._progress(job_id, {"status": "failed", "stage": "failed", "completed_at": _now(), "error": str(exc), "errors": [{"error": str(exc)}]})

    def _run_augmentation(self, job_id: str, input_path: Path, generate_n: int) -> None:
        self._progress(job_id, {"status": "running", "stage": "preparing", "started_at": _now()})
        self._log(job_id, f"开始任务扩增，输入={input_path.name}，每种子={generate_n}")
        try:
            outcome = self.augmentation_runner(input_path, generate_n, kb_root=self._run_dir(job_id) / "KnowledgeBase", progress=lambda value: self._progress(job_id, value))
            self._log(job_id, f"任务扩增结束，结果数={len(outcome.get('results', []))}，错误数={len(outcome.get('errors', []))}")
            self._finish(job_id, outcome)
        except Exception as exc:
            self._log(job_id, f"任务扩增失败：{exc}")
            self._progress(job_id, {"status": "failed", "stage": "failed", "completed_at": _now(), "error": str(exc), "errors": [{"error": str(exc)}]})

    def results(self, job_id: str) -> list[dict[str, Any]]:
        job = self.get(job_id)
        if job is None:
            raise FileNotFoundError("作业不存在")
        path = self._results_path(job_id)
        if not path.is_file():
            return []
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, list) else []

    def patch_result(self, job_id: str, result_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            job = self.get(job_id)
            if job is None:
                raise FileNotFoundError("作业不存在")
            records = self.results(job_id)
            record = next((item for item in records if item.get("result_id") == result_id), None)
            if record is None:
                raise KeyError(result_id)
            if "task" in patch:
                task = str(patch["task"]).strip()
                if not task:
                    raise ValueError("任务文本不能为空")
                record["task"] = task
                if job.get("kind") == "augmentation":
                    record["生成的变体任务"] = task
            if "deleted" in patch:
                deleted = bool(patch["deleted"])
                group = record.get("dependency_group_id")
                for item in records:
                    if item.get("result_id") == result_id or (group and item.get("dependency_group_id") == group):
                        item["deleted"] = deleted
            record["updated_at"] = _now()
            self._write_json(self._results_path(job_id), records)
            return record

    def export(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            job = self.get(job_id)
            if job is None:
                raise FileNotFoundError("作业不存在")
            active = [item for item in self.results(job_id) if not item.get("deleted", False)]
            if job.get("kind") == "task_generation":
                ids = {str(item.get("task_uuid")) for item in active}
                missing = [str(item.get("pre_task_uuid")) for item in active if item.get("pre_task_uuid") and str(item.get("pre_task_uuid")) not in ids]
                if missing:
                    raise ValueError(f"存在缺失的前置任务引用：{', '.join(missing)}")
                columns = INITIAL_RESULT_COLUMNS
                filename = f"task-generation-{job_id}.xlsx"
            else:
                columns = AUGMENTATION_RESULT_COLUMNS
                filename = f"task-augmentation-{job_id}.xlsx"
            frame = pd.DataFrame([{column: item.get(column, "") for column in columns} for item in active], columns=columns)
            destination_dir = self.exports_dir / job_id
            destination_dir.mkdir(parents=True, exist_ok=True)
            destination = destination_dir / filename
            temporary = destination.with_name(f".{filename}.tmp.xlsx")
            frame.to_excel(temporary, index=False)
            temporary.replace(destination)
            return {"filename": filename, "created_at": _now(), "download_url": f"/api/task-generation/jobs/{job_id}/exports/{filename}", "row_count": len(active), "path": str(destination)}

    def download(self, job_id: str, filename: str) -> Path:
        if Path(filename).name != filename or not filename.endswith(".xlsx"):
            raise ValueError("导出文件名无效")
        path = self.exports_dir / job_id / filename
        if not path.is_file():
            raise FileNotFoundError("导出文件不存在")
        return path

    def shutdown(self) -> None:
        if self._owns_executor:
            self._executor.shutdown(wait=False, cancel_futures=True)
