"""Build immutable per-task trajectory-tree runs for background web jobs."""

from __future__ import annotations

import hashlib
import json
import shutil
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from .batch_operations import batch_operation
from .trajectories_preprocessing import configure_reviewer_environment, read_env_file
from .quality_input_builder import build_quality_workbook
from .data_store import ArtifactStore
from .batch_results import (annotation_task_fingerprints, tree_task_fingerprints, digest,
                            assert_task_inputs, merge_tree_results, current_tree_batch, current_tree_payload, tree_result_hashes)
from .trajectory_context import resolve_batch_context, row_task_id, validate_batch_sources
from .stage_artifacts import (store_root, read_workbook_payload, write_sidecar,
                              write_payload_workbook, observation_payload, structured_input_exists, sidecar_path)
from .trajectory_data import (
    ANNOTATED_XLSX,
    BACKEND_DIR,
    TRAJECTORY_ROOT,
    TREE_RUNS_DIR,
    discover_tasks,
    batch_task_metadata,
    task_id_from_resource,
)
from .trajectories_tree.intermediate_state_classifier import (
    QwenIntermediateStateClassifier,
)
from .trajectories_tree.state_alignment_reviewer import QwenStateAlignmentReviewer
from .trajectories_tree.tree_builder import (
    DEFAULT_ALIGNMENT_CACHE,
    DEFAULT_CLASSIFICATION_CACHE,
    DEFAULT_ENV,
    MAX_INCIDENTAL_SKIP,
    apply_bounded_skip_policy,
    build_tree,
    classify_trajectories,
    count_nodes,
    load_trajectories,
    write_output,
)


ProgressCallback = Callable[[dict[str, Any]], None]
DEFAULT_TREE_CLASSIFICATION_MAX_CONCURRENT = 4
DEFAULT_TREE_SUMMARY_MAX_CONCURRENT = 2


def tree_build_config(env_path: Path = DEFAULT_ENV, *, confidence_threshold: float = 0.8,
                      max_incidental_skip: int = MAX_INCIDENTAL_SKIP) -> dict[str, Any]:
    values = read_env_file(env_path) if env_path.is_file() else {}
    return {"model": values.get("MODEL_NAME", ""), "model_url": values.get("MODEL_URL", ""),
            "confidence_threshold": confidence_threshold, "max_incidental_skip": max_incidental_skip,
            "prompt_version": "trajectory-intermediate-observation-v4",
            "tree_pipeline_version": 2}


def _positive_int(value: str | None, *, name: str, default: int) -> int:
    if not value:
        return default
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be a positive integer, got {value!r}") from exc
    if parsed < 1:
        raise ValueError(f"{name} must be a positive integer, got {value!r}")
    return parsed


class _ProgressClassifier:
    def __init__(
        self,
        classifier: QwenIntermediateStateClassifier,
        callback: ProgressCallback,
        task_id: str,
        offset: int,
        total_steps: int,
    ) -> None:
        self.classifier = classifier
        self.model = classifier.model
        self.callback = callback
        self.task_id = task_id
        self.offset = offset
        self.total_steps = total_steps
        self.completed = 0
        self._lock = threading.Lock()

    def classify(self, **kwargs: Any):
        result = self.classifier.classify(**kwargs)
        with self._lock:
            self.completed += 1
            completed = self.completed
        self.callback(
            {
                "stage": "classifying_and_observing",
                "current_task": self.task_id,
                "classified_steps": self.offset + completed,
                "total_steps": self.total_steps,
            }
        )
        return result


def _task_for_trajectory(steps: list[Any]) -> str:
    return (steps[0].task_id or task_id_from_resource(steps[0].image)) if steps else ""


def _file_fingerprint(path: Path) -> dict[str, Any]:
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return {
        "name": path.name,
        "sha256": digest,
        "modified_at": datetime.fromtimestamp(
            path.stat().st_mtime, ZoneInfo("Asia/Shanghai")
        ).isoformat(),
    }


def _new_run_id(runs_dir: Path, completed_at: datetime) -> str:
    base = completed_at.strftime("%Y%m%d_%H%M%S")
    candidate = base
    suffix = 2
    while (runs_dir / candidate).exists():
        candidate = f"{base}_{suffix}"
        suffix += 1
    return candidate


def build_tree_run(
    task_ids: list[str],
    *,
    job_id: str,
    progress: ProgressCallback,
    xlsx_path: Path = ANNOTATED_XLSX,
    trajectory_root: Path = TRAJECTORY_ROOT,
    runs_dir: Path = TREE_RUNS_DIR,
    env_path: Path = DEFAULT_ENV,
    classification_cache: Path = DEFAULT_CLASSIFICATION_CACHE,
    alignment_cache: Path = DEFAULT_ALIGNMENT_CACHE,
    confidence_threshold: float = 0.8,
    max_incidental_skip: int = MAX_INCIDENTAL_SKIP,
    quality_builder: Callable[..., tuple[int, int, int]] = build_quality_workbook,
    data_root: Path | None = None,
    batch_id: str | None = None,
    annotation_version: str | None = None,
    expected_task_inputs: dict[str, str] | None = None,
    expected_build_config: dict[str, Any] | None = None,
) -> tuple[str, dict[str, Any]]:
    operation_batch = batch_id
    if operation_batch is None and structured_input_exists(xlsx_path):
        operation_batch = (read_workbook_payload(xlsx_path).get("source_ref") or {}).get("batch_id")
    with batch_operation(operation_batch, "tree_build", data_root or store_root(runs_dir)):
        if data_root is not None:
            from .data_store import DATA_ROOT
            data_root = Path(data_root).resolve()
            if runs_dir == TREE_RUNS_DIR:
                runs_dir = data_root / "system" / "trajectory_tree_runs"
            if classification_cache == DEFAULT_CLASSIFICATION_CACHE:
                classification_cache = data_root / DEFAULT_CLASSIFICATION_CACHE.relative_to(DATA_ROOT)
            if alignment_cache == DEFAULT_ALIGNMENT_CACHE:
                alignment_cache = data_root / DEFAULT_ALIGNMENT_CACHE.relative_to(DATA_ROOT)
        config = tree_build_config(env_path, confidence_threshold=confidence_threshold,
                                   max_incidental_skip=max_incidental_skip)
        if expected_build_config is not None and config != expected_build_config:
            from .batch_results import StaleTaskInput
            raise StaleTaskInput("建树模型配置已变化，请重新提交")
        context = None
        if batch_id is not None and (annotation_version is not None or xlsx_path == ANNOTATED_XLSX):
            if expected_task_inputs is not None:
                assert_task_inputs(batch_id, expected_task_inputs, data_root)
                context = resolve_batch_context(batch_id, root=data_root)
            else:
                context = resolve_batch_context(batch_id, annotation_version, data_root)
            validate_batch_sources(context, data_root)
            xlsx_path, trajectory_root = context.json_path, context.raw_root
            fingerprints = tree_task_fingerprints(context.payload, config)
            previous = current_tree_payload(batch_id, data_root)
            task_ids = [task for task in task_ids if previous.get("task_fingerprints", {}).get(task) != fingerprints.get(task)]
            if not task_ids:
                return batch_id, current_tree_batch(batch_id, data_root)
        source_paths = [xlsx_path]
        if any(not structured_input_exists(path) for path in source_paths):
            raise FileNotFoundError(f"缺少预处理 JSON 快照：{[sidecar_path(path) for path in source_paths]}")
        source_refs = []
        combined_rows = []
        columns: list[str] = []
        for path in source_paths:
            source = context.payload if context is not None else read_workbook_payload(path)
            source_file = sidecar_path(path) if sidecar_path(path).is_file() else path
            source_refs.append(context.annotation_ref if context is not None else source.get("source_ref") or {"kind": "annotation_snapshot", **_file_fingerprint(source_file), "path": str(source_file)})
            sheet_name = next(iter(source["sheets"]))
            columns = list(dict.fromkeys(columns + source.get("columns", {}).get(sheet_name, [])))
            for row in source["sheets"][sheet_name]:
                task_id = row_task_id(row)
                if task_id in task_ids:
                    combined_rows.append(row)
        source_payload = {"schema_version": 1, "columns": {"VLA trajectories": columns}, "sheets": {"VLA trajectories": combined_rows}}
        work_parent = (Path(data_root or store_root(runs_dir)) / "tmp" / "tree-builds") if context is not None else runs_dir
        work_parent.mkdir(parents=True, exist_ok=True)
        temporary_dir = work_parent / f".building-{job_id}"
        if temporary_dir.exists():
            shutil.rmtree(temporary_dir)
        temporary_dir.mkdir(parents=True)
        frozen_source = temporary_dir / "source_annotated.xlsx"
        write_payload_workbook(frozen_source, source_payload)
        write_sidecar(frozen_source, source_payload)
        all_trajectories = load_trajectories(frozen_source, None)
        grouped: dict[str, list[tuple[str, list[Any]]]] = {}
        for trajectory, steps in all_trajectories:
            task_id = _task_for_trajectory(steps)
            if task_id:
                grouped.setdefault(task_id, []).append((trajectory, steps))
        missing = [task_id for task_id in task_ids if not grouped.get(task_id)]
        if missing:
            shutil.rmtree(temporary_dir)
            raise ValueError(f"任务尚未完成轨迹预处理：{', '.join(missing)}")

        metadata = batch_task_metadata(context) if context is not None else discover_tasks(trajectory_root)
        total_steps = sum(
            1
            for task_id in task_ids
            for _, steps in grouped[task_id]
            for position, step in enumerate(steps)
            if not (step.action.get("action") == "terminate" and position < len(steps) - 1)
        )
        completed_steps = 0
        task_manifests: list[dict[str, Any]] = []

        try:
            model_name = configure_reviewer_environment(env_path)
            env_values = read_env_file(env_path) if env_path.is_file() else {}
            classification_max_concurrent = _positive_int(
                env_values.get("TREE_CLASSIFICATION_MAX_CONCURRENT"),
                name="TREE_CLASSIFICATION_MAX_CONCURRENT",
                default=DEFAULT_TREE_CLASSIFICATION_MAX_CONCURRENT,
            )
            summary_max_concurrent = _positive_int(
                env_values.get("TREE_SUMMARY_MAX_CONCURRENT"),
                name="TREE_SUMMARY_MAX_CONCURRENT",
                default=DEFAULT_TREE_SUMMARY_MAX_CONCURRENT,
            )
            classifier = QwenIntermediateStateClassifier(model_name, classification_cache)
            alignment_reviewer = QwenStateAlignmentReviewer(model_name, alignment_cache)
            for task_index, task_id in enumerate(task_ids, 1):
                trajectories = grouped[task_id]
                progress(
                    {
                "stage": "classifying_and_observing",
                        "current_task": task_id,
                        "task_index": task_index,
                        "total_tasks": len(task_ids),
                        "classified_steps": completed_steps,
                        "total_steps": total_steps,
                    }
                )
                progress_classifier = _ProgressClassifier(
                    classifier, progress, task_id, completed_steps, total_steps
                )
                classify_trajectories(
                    trajectories,
                    progress_classifier,
                    trajectory_root,
                    confidence_threshold,
                    task=(metadata[task_id].goal if task_id in metadata else task_id),
                    max_concurrent=classification_max_concurrent,
                )
                task_step_count = sum(
                    1
                    for _, steps in trajectories
                    for position, step in enumerate(steps)
                    if not (step.action.get("action") == "terminate" and position < len(steps) - 1)
                )
                completed_steps += task_step_count
                for _, steps in trajectories:
                    apply_bounded_skip_policy(
                        steps,
                        confidence_threshold=confidence_threshold,
                        max_skip=max_incidental_skip,
                    )
                progress(
                    {
                        "stage": "building",
                        "current_task": task_id,
                        "task_index": task_index,
                        "total_tasks": len(task_ids),
                        "classified_steps": completed_steps,
                        "total_steps": total_steps,
                    }
                )
                root, decisions, statistics = build_tree(
                    trajectories,
                    confidence_threshold=confidence_threshold,
                    trajectory_root=trajectory_root,
                    alignment_reviewer=alignment_reviewer,
                    max_incidental_skip=max_incidental_skip,
                )
                tree_path = temporary_dir / f"{task_id}.json"
                write_output(
                    root,
                    decisions,
                    statistics,
                    trajectories,
                    model_name=model_name,
                    confidence_threshold=confidence_threshold,
                    max_incidental_skip=max_incidental_skip,
                    json_path=tree_path,
                    extra_metadata={"task_id": task_id},
                )
                tree_payload = json.loads(tree_path.read_text(encoding="utf-8"))
                item = metadata.get(task_id)
                task_manifests.append(
                    {
                        "task_id": task_id,
                        "goal": item.goal if item else task_id,
                        "tree_file": tree_path.name,
                        "trajectory_count": tree_payload["trajectory_count"],
                        "original_step_count": tree_payload["original_step_count"],
                        "tree_step_count": tree_payload["tree_step_count"],
                        "ignored_step_count": tree_payload["ignored_incidental_step_count"],
                        "action_node_count": count_nodes(root) - 1,
                    }
                )

            quality_workbook = temporary_dir / "rubric_trajectories.xlsx"
            quality_task_count, quality_trajectory_count, quality_step_count = quality_builder(
                grouped={task_id: grouped[task_id] for task_id in task_ids},
                task_goals={task_id: (metadata[task_id].goal if task_id in metadata else task_id) for task_id in task_ids},
                trajectory_root=trajectory_root,
                output=quality_workbook,
                env_path=env_path,
                progress=progress,
                max_concurrent=summary_max_concurrent,
            )

            progress({"stage": "publishing", "classified_steps": total_steps, "total_steps": total_steps})
            if context is not None:
                validate_batch_sources(context, data_root)
                assert_task_inputs(batch_id, {task: annotation_task_fingerprints(context.payload)[task]
                                             for task in task_ids}, data_root)
                if tree_build_config(env_path, confidence_threshold=confidence_threshold,
                                     max_incidental_skip=max_incidental_skip) != config:
                    from .batch_results import StaleTaskInput
                    raise StaleTaskInput("建树期间模型配置已变化，请重新提交")
            completed_at = datetime.now(ZoneInfo("Asia/Shanghai"))
            run_id = _new_run_id(runs_dir, completed_at)
            artifact_store = ArtifactStore(store_root(runs_dir, data_root))
            input_batches = {ref["batch_id"] for ref in source_refs if ref.get("batch_id")}
            batch_id = batch_id or (next(iter(input_batches)) if len(input_batches) == 1 else run_id)
            observation, observation_rows = observation_payload({task_id: grouped[task_id] for task_id in task_ids}, confidence_threshold)
            if context is not None:
                tree_payloads = {item["task_id"]: json.loads((temporary_dir / item["tree_file"]).read_text(encoding="utf-8"))
                                 for item in task_manifests}
                incoming = {"schema_version": 2, "batch_id": batch_id, "run_id": batch_id,
                            "completed_at": completed_at.isoformat(), "model_name": model_name,
                            "raw_root": str(trajectory_root.resolve()), "tasks": task_manifests,
                            "trees": tree_payloads, "quality_input": read_workbook_payload(quality_workbook),
                            "source_annotation": source_payload,
                            "source_task_fingerprints": annotation_task_fingerprints(source_payload),
                            "task_fingerprints": tree_task_fingerprints(source_payload, config),
                            "tree_hashes": {task: digest(value) for task, value in tree_payloads.items()},
                            "build_config": config, "quality_input_prompt_version": "trajectory-intermediate-observation-v4"}
                incoming["tree_hashes"] = tree_result_hashes(tree_payloads, incoming["quality_input"], incoming["task_fingerprints"])
                manifest = merge_tree_results(batch_id, incoming, observation, observation_rows, data_root)
                shutil.rmtree(temporary_dir)
                return batch_id, manifest
            tree_payloads = {item["task_id"]: json.loads((temporary_dir / item["tree_file"]).read_text(encoding="utf-8")) for item in task_manifests}
            quality_input = read_workbook_payload(quality_workbook)
            observation_artifact, tree_artifact = artifact_store.publish_many(batch_id, [
                {"stage": "03_observation", "payload": observation,
                 "tables": {"Observation与中间态": observation_rows}, "source_refs": source_refs,
                 "metadata": {"run_id": run_id, "model": model_name, "includes_all_steps": True}},
                {"stage": "04_tree", "payload": {"schema_version": 1, "run_id": run_id, "trees": tree_payloads, "quality_input": quality_input},
                 "source_stages": ["03_observation"], "metadata": {"run_id": run_id, "task_ids": task_ids}},
            ])
            manifest = {
                "run_id": run_id,
                "completed_at": completed_at.isoformat(),
                "model_name": model_name,
                "task_ids": task_ids,
                "task_count": len(task_ids),
                "total_original_steps": sum(item["original_step_count"] for item in task_manifests),
                "total_tree_steps": sum(item["tree_step_count"] for item in task_manifests),
                "source_xlsx": _file_fingerprint(frozen_source),
                "source_annotated_file": frozen_source.name,
                "source_annotated_json": frozen_source.with_suffix(".json").name,
                "source_json": _file_fingerprint(frozen_source.with_suffix(".json")),
                "batch_id": batch_id,
                "raw_root": str(trajectory_root.resolve()),
                "annotation_version": context.annotation_version if context is not None else None,
                "source_refs": source_refs,
                "artifacts": [observation_artifact, tree_artifact],
                "quality_input_file": quality_workbook.name,
                "quality_input_json": quality_workbook.with_suffix(".json").name,
                "quality_input_prompt_version": "trajectory-intermediate-observation-v4",
                "quality_task_count": quality_task_count,
                "quality_observation_count": quality_step_count,
                "quality_final_answer_count": quality_trajectory_count,
                "tasks": task_manifests,
            }
            (temporary_dir / "manifest.json").write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            temporary_dir.replace(runs_dir / run_id)
            return run_id, manifest
        except Exception:
            if temporary_dir.exists():
                shutil.rmtree(temporary_dir)
            raise
