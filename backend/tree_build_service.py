"""Build immutable per-task trajectory-tree runs for background web jobs."""

from __future__ import annotations

import hashlib
import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from .trajectories_preprocessing import configure_reviewer_environment
from .quality_input_builder import build_quality_workbook
from .trajectory_data import (
    ANNOTATED_XLSX,
    BACKEND_DIR,
    TRAJECTORY_ROOT,
    TREE_RUNS_DIR,
    discover_tasks,
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

    def classify(self, **kwargs: Any):
        result = self.classifier.classify(**kwargs)
        self.completed += 1
        self.callback(
            {
                "stage": "classifying_and_observing",
                "current_task": self.task_id,
                "classified_steps": self.offset + self.completed,
                "total_steps": self.total_steps,
            }
        )
        return result


def _task_for_trajectory(steps: list[Any]) -> str:
    return task_id_from_resource(steps[0].image) if steps else ""


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
) -> tuple[str, dict[str, Any]]:
    if not xlsx_path.is_file():
        raise FileNotFoundError(f"缺少预处理文件：{xlsx_path}")
    all_trajectories = load_trajectories(xlsx_path, None)
    grouped: dict[str, list[tuple[str, list[Any]]]] = {}
    for trajectory, steps in all_trajectories:
        task_id = _task_for_trajectory(steps)
        if task_id:
            grouped.setdefault(task_id, []).append((trajectory, steps))
    missing = [task_id for task_id in task_ids if not grouped.get(task_id)]
    if missing:
        raise ValueError(f"任务尚未完成轨迹预处理：{', '.join(missing)}")

    metadata = discover_tasks(trajectory_root)
    total_steps = sum(
        1
        for task_id in task_ids
        for _, steps in grouped[task_id]
        for position, step in enumerate(steps)
        if not (step.action.get("action") == "terminate" and position < len(steps) - 1)
    )
    runs_dir.mkdir(parents=True, exist_ok=True)
    temporary_dir = runs_dir / f".building-{job_id}"
    if temporary_dir.exists():
        shutil.rmtree(temporary_dir)
    temporary_dir.mkdir(parents=True)
    completed_steps = 0
    task_manifests: list[dict[str, Any]] = []

    try:
<<<<<<< Updated upstream
        model_name = configure_reviewer_environment(env_path)
=======
        model_name = configure_reviewer_environment(env_path, module="tree")
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
>>>>>>> Stashed changes
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
        )

        progress({"stage": "publishing", "classified_steps": total_steps, "total_steps": total_steps})
        completed_at = datetime.now(ZoneInfo("Asia/Shanghai"))
        run_id = _new_run_id(runs_dir, completed_at)
        manifest = {
            "run_id": run_id,
            "completed_at": completed_at.isoformat(),
            "model_name": model_name,
            "task_ids": task_ids,
            "task_count": len(task_ids),
            "total_original_steps": sum(item["original_step_count"] for item in task_manifests),
            "total_tree_steps": sum(item["tree_step_count"] for item in task_manifests),
            "source_xlsx": _file_fingerprint(xlsx_path),
            "quality_input_file": quality_workbook.name,
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
