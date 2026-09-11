from __future__ import annotations

import json
import shutil
import tempfile
import threading
import uuid
from concurrent.futures import Executor, ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

import pandas as pd

from . import collection_batches as batches
from .collection_input import CollectionInputError, build_collection_input
from .constants import (
    AUGMENTATION_RESULT_COLUMNS,
    EXPORTS_DIR,
    INITIAL_RESULT_COLUMNS,
    JOBS_DIR,
    KNOWLEDGE_BASE_DIR,
    LOGS_DIR,
    RUNS_DIR,
)
from .knowledge_base import node_id, snapshot_knowledge_base, tree_payload
from .tree_store import _app_nodes
from .tree_store import VersionConflict, current_root, flatten, read_tree
from .service import (
    prepare_augmentation_seeds, run_augmentation, run_augmentation_classification,
    run_augmentation_generation, run_initial_generation,
)


Runner = Callable[..., dict[str, Any]]


class AugmentationStateError(ValueError):
    """A valid request cannot run in the persisted job's current phase."""


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
        classification_runner: Runner = run_augmentation_classification,
        generation_runner: Runner = run_augmentation_generation,
        collection_batches_dir: Path | None = None,
    ) -> None:
        self.jobs_dir = jobs_dir
        self.runs_dir = runs_dir
        self.exports_dir = exports_dir
        self.logs_dir = logs_dir
        self.knowledge_base_dir = knowledge_base_dir
        self.collection_batches_dir = collection_batches_dir if collection_batches_dir is not None else jobs_dir.parent / "collection_batches"
        self.initial_runner = initial_runner
        self.augmentation_runner = augmentation_runner
        self.classification_runner = classification_runner
        self.generation_runner = generation_runner
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

    def _seeds_path(self, job_id: str) -> Path:
        return self._run_dir(job_id) / "seeds.json"

    def _read_seeds(self, job_id: str) -> list[dict[str, Any]]:
        value = json.loads(self._seeds_path(job_id).read_text(encoding="utf-8"))
        if not isinstance(value, list) or any(not isinstance(seed, dict) for seed in value):
            raise ValueError("扩增分类记录格式无效")
        return value

    @staticmethod
    def _seed_stats(seeds: list[dict[str, Any]]) -> dict[str, int]:
        return {
            "total": len(seeds),
            "matched": sum(seed.get("mapping_status") == "matched" for seed in seeds),
            "unmatched": sum(seed.get("mapping_status") in {"unclassified", "not_found"} for seed in seeds),
            "classification_failed": sum(seed.get("classification_status") == "failed" for seed in seeds),
            "eligible": sum(seed.get("classification_status") == "classified" for seed in seeds),
        }

    def _stop_pending_seeds(self, job_id: str, error: str) -> dict[str, int] | None:
        """Close unfinished row states on interruption without restarting work."""
        try:
            seeds = self._read_seeds(job_id)
        except (OSError, ValueError):
            return None
        for seed in seeds:
            if seed.get("classification_status") in {"pending", "classifying"}:
                seed.update({"classification_status": "failed", "mapping_status": "classification_failed",
                             "node_path_ids": [], "generation_status": "skipped", "error": error})
            elif seed.get("classification_status") == "classified" and seed.get("generation_status") in {"waiting", "generating"}:
                seed.update({"generation_status": "failed", "error": error})
        self._write_json(self._seeds_path(job_id), seeds)
        return self._seed_stats(seeds)

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
                # A final seed checkpoint can reach disk immediately before the
                # ready job update. Recover that completed classification only;
                # never resume model calls during application startup.
                if payload.get("augmentation_preview_version") and not payload.get("augmentation_generation_started"):
                    try:
                        seeds = self._read_seeds(payload["job_id"])
                    except (OSError, ValueError):
                        seeds = []
                    if seeds and all(seed.get("classification_status") in {"classified", "failed"} for seed in seeds):
                        stats = self._seed_stats(seeds)
                        errors = [{"seed_id": seed["seed_id"], "item_id": str(seed.get("source_row", "")),
                                   "stage": "classifying", "error": seed.get("error", "分类失败")}
                                  for seed in seeds if seed.get("classification_status") == "failed"]
                        payload.update({"status": "awaiting_confirmation" if stats["eligible"] else "failed",
                                        "stage": "awaiting_confirmation" if stats["eligible"] else "failed",
                                        "classification_completed": True, "seed_stats": stats, "errors": errors,
                                        "percent": 40, "current_item": None,
                                        "completed_at": None if stats["eligible"] else _now(),
                                        "error": None if stats["eligible"] else "所有种子分类失败，无法扩增"})
                        self._write_job(payload)
                        continue
                payload.update({"status": "interrupted", "interrupted_stage": payload.get("stage"), "stage": "interrupted",
                                "completed_at": _now(), "error": "服务重启导致作业中断；已保存的预览和结果保留，重新提交可创建新作业。"})
                if payload.get("augmentation_preview_version"):
                    stats = self._stop_pending_seeds(payload["job_id"], payload["error"])
                    if stats is not None:
                        payload["seed_stats"] = stats
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
            configs = {config["label"]: config for config in _app_nodes(leaf)}
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

    def submit_augmentation(self, input_path: Path, original_filename: str, generate_n: int, *, auto_start: bool = True) -> dict[str, Any]:
        if not 1 <= generate_n <= 20:
            raise ValueError("每个种子生成数量必须为 1–20")
        # Preserve the existing injected, one-shot runner contract. Production
        # defaults and preview calls both use the checkpointed two-phase flow.
        if auto_start and self.augmentation_runner is not run_augmentation:
            payload = self._new_job("augmentation", total_items=0, generate_n=generate_n, input_filename=original_filename)
            target = self._run_dir(payload["job_id"]) / "input.xlsx"
            shutil.copy2(input_path, target)
            self._executor.submit(self._run_augmentation, payload["job_id"], target, generate_n)
            return payload
        seeds = prepare_augmentation_seeds(input_path)
        payload = self._new_job("augmentation", total_items=len(seeds) * 2, generate_n=generate_n, input_filename=original_filename,
                                parameters={"auto_start": auto_start, "augmentation_preview_version": 1,
                                            "classification_completed": False, "augmentation_generation_started": False,
                                            "seed_stats": self._seed_stats(seeds)})
        target = self._run_dir(payload["job_id"]) / "input.xlsx"
        try:
            shutil.copy2(input_path, target)
            self._write_json(self._seeds_path(payload["job_id"]), seeds)
            self._executor.submit(self._run_augmentation_classification, payload["job_id"])
        except Exception as exc:
            self._fail_augmentation(payload["job_id"], exc)
            raise
        return payload

    def augmentation_preview(self, job_id: str, *, include_tree: bool = True) -> dict[str, Any]:
        with self._lock:
            job = self.get(job_id)
            if job is None:
                raise FileNotFoundError("作业不存在")
            if job.get("kind") != "augmentation":
                raise AugmentationStateError("只有任务扩增作业具有分类预览")
            available = bool(job.get("augmentation_preview_version")) and self._seeds_path(job_id).is_file()
            seeds = self._read_seeds(job_id) if available else []
            preview = {"job_id": job_id, "available": available, "seeds": seeds, "stats": self._seed_stats(seeds)}
        if available and include_tree:
            preview["tree"] = tree_payload(self._run_dir(job_id) / "KnowledgeBase")
        return preview

    def start_augmentation(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            job = self.get(job_id)
            if job is None:
                raise FileNotFoundError("作业不存在")
            if job.get("kind") != "augmentation" or not job.get("augmentation_preview_version"):
                raise AugmentationStateError("此作业不支持从分类预览启动扩增，请重新上传")
            if job.get("status") == "interrupted":
                raise AugmentationStateError("作业已中断，保留现有结果；重新上传可创建新作业")
            if job.get("augmentation_generation_started"):
                return job
            if job.get("status") != "awaiting_confirmation" or not job.get("classification_completed"):
                raise AugmentationStateError("分类尚未完成或作业不可启动")
            seeds = self._read_seeds(job_id)
            if not seeds or any(seed.get("classification_status") not in {"classified", "failed"} for seed in seeds):
                raise AugmentationStateError("分类记录未完整保存，无法开始扩增")
            if not self._seed_stats(seeds)["eligible"]:
                raise AugmentationStateError("没有分类成功的种子，无法开始扩增")
            job.update({"status": "queued", "stage": "generating", "augmentation_generation_started": True,
                        "current_item": None, "completed_at": None, "error": None})
            self._write_job(job)
        # Submission is outside the lock, also allowing synchronous executors in
        # tests to report worker checkpoints without holding the admission lock.
        try:
            self._executor.submit(self._run_augmentation_generation, job_id)
        except Exception as exc:
            self._fail_augmentation(job_id, exc)
            raise AugmentationStateError(f"扩增执行器提交失败：{exc}") from exc
        return self.get(job_id) or job

    def _checkpoint_seed(self, job_id: str, seed: dict[str, Any], rows: list[dict[str, Any]] | None) -> None:
        with self._lock:
            seeds = self._read_seeds(job_id)
            index = next(index for index, item in enumerate(seeds) if item["seed_id"] == seed["seed_id"])
            seeds[index] = seed
            # Persist generated rows before marking that seed completed. Partial
            # outputs then survive interruption of later seeds in the same job.
            if rows is not None:
                records = [item for item in self.results(job_id) if item.get("seed_id") != seed["seed_id"]]
                records.extend(rows)
                records.sort(key=lambda item: (int(item.get("source_row") or 0), item.get("用例编号", "")))
                self._write_json(self._results_path(job_id), records)
            self._write_json(self._seeds_path(job_id), seeds)
            changes: dict[str, Any] = {"seed_stats": self._seed_stats(seeds)}
            if rows is not None:
                changes["result_count"] = len(records)
            self._progress(job_id, changes)

    def _fail_augmentation(self, job_id: str, exc: Exception) -> None:
        self._log(job_id, f"任务扩增失败：{exc}")
        with self._lock:
            job = self.get(job_id) or {}
            errors = list(job.get("errors", []))
            errors.append({"stage": job.get("stage", "preparing"), "error": str(exc)})
            changes = {"status": "failed", "stage": "failed", "completed_at": _now(), "error": str(exc), "errors": errors}
            if job.get("augmentation_preview_version"):
                stats = self._stop_pending_seeds(job_id, f"作业停止：{exc}")
                if stats is not None:
                    changes["seed_stats"] = stats
            self._progress(job_id, changes)

    def _run_augmentation_classification(self, job_id: str) -> None:
        self._progress(job_id, {"status": "running", "stage": "classifying", "started_at": _now()})
        self._log(job_id, "开始匹配失败任务与作业快照场景树")
        try:
            outcome = self.classification_runner(
                self._read_seeds(job_id), kb_root=self._run_dir(job_id) / "KnowledgeBase",
                progress=lambda value: self._progress(job_id, value),
                on_seed=lambda seed, rows: self._checkpoint_seed(job_id, seed, rows),
            )
            seeds = outcome["seeds"]
            if not seeds or any(seed.get("classification_status") not in {"classified", "failed"} for seed in seeds):
                raise ValueError("分类未完成全部种子，不能进入确认阶段")
            stats = self._seed_stats(seeds)
            with self._lock:
                self._write_json(self._seeds_path(job_id), seeds)
                self._progress(job_id, {"classification_completed": True, "seed_stats": stats,
                                       "status": "awaiting_confirmation" if stats["eligible"] else "failed",
                                       "stage": "awaiting_confirmation" if stats["eligible"] else "failed",
                                       "percent": 40, "current_item": None, "completed_items": len(seeds),
                                       "errors": outcome.get("errors", []), "warnings": outcome.get("warnings", []),
                                       "completed_at": None if stats["eligible"] else _now(),
                                       "error": None if stats["eligible"] else "所有种子分类失败，无法扩增"})
            self._log(job_id, f"分类完成，可扩增={stats['eligible']}，未匹配={stats['unmatched']}，失败={stats['classification_failed']}")
            if stats["eligible"] and (self.get(job_id) or {}).get("auto_start"):
                self.start_augmentation(job_id)
        except Exception as exc:
            self._fail_augmentation(job_id, exc)

    def _run_augmentation_generation(self, job_id: str) -> None:
        self._progress(job_id, {"status": "running", "stage": "generating"})
        self._log(job_id, "开始从已保存的分类记录扩增任务")
        try:
            job = self.get(job_id)
            if job is None:
                return
            outcome = self.generation_runner(
                self._read_seeds(job_id), job["generate_n"], kb_root=self._run_dir(job_id) / "KnowledgeBase",
                progress=lambda value: self._progress(job_id, value),
                on_seed=lambda seed, rows: self._checkpoint_seed(job_id, seed, rows),
            )
            outcome["errors"] = list(job.get("errors", [])) + outcome.get("errors", [])
            outcome["warnings"] = list(job.get("warnings", [])) + outcome.get("warnings", [])
            self._finish(job_id, outcome)
            self._log(job_id, f"扩增结束，结果数={len(outcome.get('results', []))}")
        except Exception as exc:
            self._fail_augmentation(job_id, exc)

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

    def collection_input(self, job_id: str) -> dict[str, Any]:
        # Share the edit/finish lock for the entire job + result read. No export
        # file, current knowledge base, or generation runner participates here.
        with self._lock:
            job = self.get(job_id)
            if job is None:
                raise FileNotFoundError("作业不存在")
            if job.get("status") not in ("succeeded", "partial"):
                raise CollectionInputError("只有 succeeded 或 partial 作业可以读取采集输入")
            try:
                records = json.loads(self._results_path(job_id).read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise CollectionInputError("作业结果文件缺失、无法读取或已损坏") from exc
            return build_collection_input(job, records)

    def _collection_batch_dir(self, batch_id: str) -> Path:
        batches.validate_batch_id(batch_id)
        path = self.collection_batches_dir / batch_id
        if not path.resolve().is_relative_to(self.collection_batches_dir.resolve()):
            raise CollectionInputError("采集批次路径无效")
        return path

    def collection_batch(self, batch_id: str) -> dict[str, Any]:
        with self._lock:
            directory = self._collection_batch_dir(batch_id)
            if not directory.is_dir():
                raise FileNotFoundError("采集批次不存在")
            try:
                stored = json.loads((directory / "batch.json").read_text(encoding="utf-8"))
                payload = batches.CollectionBatchDetail.model_validate(stored).model_dump()
                snapshot = payload["snapshot"]
                if (payload["batch_id"] != batch_id or payload["source_job_id"] != batch_id
                        or payload["filename"] != f"collection-batch-{batch_id}.xlsx"
                        or snapshot["job_id"] != batch_id
                        or any(payload[key] != snapshot[key] for key in ("kind", "job_status", "knowledge_base_version", "task_count"))
                        or payload != batches.collection_batch_payload(snapshot, payload["created_at"])
                        or not (directory / payload["filename"]).is_file()):
                    raise ValueError("批次文件与元数据不一致")
                integrity = stored.get("_integrity")
                if (not isinstance(integrity, dict)
                        or integrity.get("payload_sha256") != batches.payload_digest(payload)
                        or integrity.get("workbook_sha256") != batches.workbook_digest(directory / payload["filename"])):
                    raise ValueError("采集批次完整性校验失败")
            except (OSError, ValueError) as exc:
                raise CollectionInputError("采集批次文件缺失或已损坏") from exc
            return payload

    def collection_batches(self, job_id: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            if job_id is not None:
                paths = [self._collection_batch_dir(job_id)]
            elif self.collection_batches_dir.is_dir():
                paths = list(self.collection_batches_dir.iterdir())
            else:
                paths = []
            summaries = []
            for path in paths:
                if not path.is_dir() or path.name.startswith("."):
                    continue
                try:
                    detail = self.collection_batch(path.name)
                except (FileNotFoundError, CollectionInputError):
                    continue
                summaries.append({key: value for key, value in detail.items() if key != "snapshot"})
            return sorted(summaries, key=lambda value: (value["created_at"], value["batch_id"]), reverse=True)

    def submit_collection_batch(self, job_id: str) -> tuple[dict[str, Any], bool]:
        with self._lock:
            destination = self._collection_batch_dir(job_id)
            if self.get(job_id) is None:
                raise FileNotFoundError("作业不存在")
            if destination.exists():
                return self.collection_batch(job_id), False
            payload = batches.collection_batch_payload(self.collection_input(job_id), _now())
            try:
                self.collection_batches_dir.mkdir(parents=True, exist_ok=True)
                with tempfile.TemporaryDirectory(prefix=f".{job_id}-", dir=self.collection_batches_dir) as temporary:
                    staging = Path(temporary)
                    batches.write_collection_workbook(payload["snapshot"], staging / payload["filename"])
                    stored = {**payload, "_integrity": {
                        "payload_sha256": batches.payload_digest(payload),
                        "workbook_sha256": batches.workbook_digest(staging / payload["filename"]),
                    }}
                    self._write_json(staging / "batch.json", stored)
                    staging.rename(destination)
            except CollectionInputError:
                raise
            except (OSError, ValueError) as exc:
                raise CollectionInputError("采集批次保存失败，未发布新批次，请重试") from exc
            return payload, True

    def collection_batch_workbook(self, batch_id: str) -> Path:
        with self._lock:
            batch = self.collection_batch(batch_id)
            return self._collection_batch_dir(batch_id) / batch["filename"]

    def patch_result(self, job_id: str, result_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            job = self.get(job_id)
            if job is None:
                raise FileNotFoundError("作业不存在")
            if job.get("augmentation_preview_version") and job.get("status") in {"queued", "running", "awaiting_confirmation"}:
                raise AugmentationStateError("作业执行期间结果只读，请在扩增结束后审核")
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
