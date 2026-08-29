"""Connect trajectory-tree batches to quality results and correction Top-1s."""

from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime
from copy import deepcopy
from pathlib import Path
from typing import Any

from ..trajectory_data import QUALITY_RESULTS_DIR, TREE_RUNS_DIR, task_id_from_resource
from .constants import FIXED_ANNOTATED_XLSX, FIXED_SOURCE_ID, FIXED_TRAJECTORY_ROOT
from .workbook import load_snapshot


class QualitySelectionError(ValueError):
    """Base error for an invalid or unavailable quality-to-correction flow."""


class QualitySelectionUnavailable(QualitySelectionError):
    """Raised when no complete quality run can feed the correction page."""


class QualitySourceMismatch(QualitySelectionError):
    """Raised when quality scores belong to a different source workbook."""


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _result_path(run_dir: Path, task_id: str) -> Path | None:
    if not task_id or Path(task_id).name != task_id:
        return None
    path = (run_dir / f"{task_id}.json").resolve()
    try:
        path.relative_to(run_dir.resolve())
    except ValueError:
        return None
    return path


def _score(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "通过"}
    return bool(value)


def _timestamp(value: Any) -> float:
    raw = str(value or "").strip()
    if not raw:
        return 0.0
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError, OverflowError):
        return 0.0


def _safe_task_file(run_dir: Path, filename: Any) -> Path | None:
    """Resolve a manifest file while keeping it inside the tree-run directory."""
    value = str(filename or "").strip()
    if not value or Path(value).name != value:
        return None
    path = (run_dir / value).resolve()
    try:
        path.relative_to(run_dir.resolve())
    except ValueError:
        return None
    return path


def _tree_manifest_for(run_dir: Path) -> dict[str, Any] | None:
    manifest = _read_json(run_dir / "manifest.json")
    if not manifest:
        return None
    run_id = str(manifest.get("run_id") or run_dir.name).strip()
    if run_id != run_dir.name:
        return None
    tasks = manifest.get("tasks")
    if isinstance(tasks, list) and tasks:
        for task in tasks:
            if not isinstance(task, dict) or not str(task.get("task_id", "")).strip():
                return None
            tree_file = _safe_task_file(run_dir, task.get("tree_file"))
            if tree_file is None or not tree_file.is_file():
                return None
    return manifest


def _valid_quality_task(
    quality_summary: dict[str, Any],
    result: dict[str, Any] | None,
    tree_summary: dict[str, Any],
) -> bool:
    if result is None:
        return False
    task_id = str(quality_summary.get("task_id", "")).strip()
    if str(result.get("task_id", task_id)).strip() != task_id:
        return False
    evaluations = result.get("evaluations")
    if not task_id or not isinstance(evaluations, dict) or not evaluations:
        return False
    try:
        expected_count = int(
            tree_summary.get("trajectory_count")
            or quality_summary.get("trajectory_count")
            or result.get("trajectory_count")
            or 0
        )
    except (TypeError, ValueError):
        expected_count = 0
    if expected_count <= 0 or len(evaluations) < expected_count:
        return False
    return all(
        isinstance(item, dict) and _score(item.get("global_score")) is not None
        for item in evaluations.values()
    )


def _completed_quality_runs(
    root: Path | None = None,
    tree_root: Path | None = None,
) -> list[dict[str, Any]]:
    """Discover usable quality results grouped by their tree-run batch.

    A quality directory is intentionally not enough on its own: the matching
    immutable tree-run manifest is the source of the batch's task list and
    expected trajectory counts.  A batch may contain only a subset of its
    tasks because the quality runner merges task results across submissions.
    """
    root_was_default = root is None
    root = QUALITY_RESULTS_DIR if root is None else root
    if tree_root is None:
        # Test/embedded callers sometimes keep the matching tree manifests
        # beside a temporary quality-results root.  Production uses the
        # project-level TREE_RUNS_DIR.
        sibling_tree_root = root / "_tree_runs"
        tree_root = sibling_tree_root if not root_was_default and sibling_tree_root.is_dir() else TREE_RUNS_DIR
    if not root.is_dir() or not tree_root.is_dir():
        return []

    completed: list[dict[str, Any]] = []
    for tree_dir in tree_root.iterdir():
        if not tree_dir.is_dir() or tree_dir.name.startswith("."):
            continue
        tree_manifest = _tree_manifest_for(tree_dir)
        if tree_manifest is None:
            continue
        run_id = tree_dir.name
        quality_dir = root / run_id
        if not quality_dir.is_dir() or quality_dir.name.startswith("."):
            continue
        quality_manifest = _read_json(quality_dir / "manifest.json")
        quality_summaries = quality_manifest.get("tasks") if quality_manifest else None
        if not isinstance(quality_summaries, list) or not quality_summaries:
            continue
        quality_manifest_id = str(quality_manifest.get("run_id") or run_id).strip()
        if quality_manifest_id != run_id:
            continue

        tree_summaries = tree_manifest.get("tasks")
        # This fallback keeps the selector readable for older hand-built test
        # fixtures that predate task metadata in the tree manifest.  Real tree
        # runs always publish their task list.
        if not isinstance(tree_summaries, list) or not tree_summaries:
            if root_was_default:
                continue
            tree_summaries = quality_summaries
        tree_by_task = {
            str(item.get("task_id", "")).strip(): item
            for item in tree_summaries
            if isinstance(item, dict) and str(item.get("task_id", "")).strip()
        }
        if not tree_by_task:
            continue

        results: dict[str, dict[str, Any]] = {}
        valid_summaries: list[dict[str, Any]] = []
        for raw_summary in quality_summaries:
            if not isinstance(raw_summary, dict):
                continue
            task_id = str(raw_summary.get("task_id", "")).strip()
            tree_summary = tree_by_task.get(task_id)
            result_path = _result_path(quality_dir, task_id)
            result = _read_json(result_path) if result_path is not None else None
            if (
                tree_summary is None
                or (result is not None and str(result.get("run_id") or run_id).strip() != run_id)
                or not _valid_quality_task(raw_summary, result, tree_summary)
            ):
                continue
            results[task_id] = result
            valid_summaries.append(raw_summary)
        if not valid_summaries:
            continue

        quality_completed_at = str(quality_manifest.get("updated_at") or "").strip()
        if not quality_completed_at:
            quality_completed_at = max(
                (str(item.get("completed_at") or "") for item in valid_summaries),
                key=_timestamp,
                default="",
            )
        completed.append(
            {
                "run_id": run_id,
                "tree_run_id": run_id,
                "tree_manifest": tree_manifest,
                "quality_manifest": quality_manifest,
                "tree_completed_at": str(tree_manifest.get("completed_at") or ""),
                "quality_completed_at": quality_completed_at,
                # ``completed_at`` is retained as an internal/backward-
                # compatible alias for callers that used the old name.
                "completed_at": quality_completed_at,
                "tree_tasks": list(tree_summaries),
                "tasks": valid_summaries,
                "results": results,
            }
        )
    completed.sort(
        key=lambda value: (
            _timestamp(value.get("quality_completed_at")),
            _timestamp(value.get("tree_completed_at")),
            str(value.get("tree_run_id", "")),
        ),
        reverse=True,
    )
    return completed


def _batch_summary(run: dict[str, Any], *, default_run_id: str = "") -> dict[str, Any]:
    tree_tasks = run.get("tree_tasks") or []
    return {
        "tree_run_id": run["tree_run_id"],
        "tree_completed_at": run.get("tree_completed_at", ""),
        "quality_completed_at": run.get("quality_completed_at", ""),
        "total_task_count": len(tree_tasks),
        "reviewed_task_count": len(run.get("tasks") or []),
        "status": "ready",
        "is_default": run["tree_run_id"] == default_run_id,
    }


def correction_batches(
    root: Path | None = None,
    tree_root: Path | None = None,
) -> dict[str, Any]:
    """Return every tree-run batch with at least one valid reviewed task."""
    runs = _completed_quality_runs(root, tree_root)
    default_run_id = runs[0]["tree_run_id"] if runs else ""
    return {
        "default_tree_run_id": default_run_id or None,
        "batches": [_batch_summary(run, default_run_id=default_run_id) for run in runs],
    }


def _source_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _verify_source_version(
    run_id: str,
    source_path: Path,
    *,
    tree_root: Path | None = None,
) -> str:
    if not source_path.is_file():
        raise QualitySourceMismatch("项目内置标注表不存在，无法进入轨迹修正")
    tree_manifest = _read_json(
        (TREE_RUNS_DIR if tree_root is None else tree_root) / run_id / "manifest.json"
    )
    expected = ((tree_manifest or {}).get("source_xlsx") or {}).get("sha256")
    actual = _source_sha256(source_path)
    if expected and str(expected) != actual:
        raise QualitySourceMismatch("当前标注表与该质检结果的数据版本不一致，请重新执行轨迹树构建和质检")
    return actual


def _group_task_id(group: dict[str, Any]) -> str:
    for row in group.get("rows", []):
        task_id = task_id_from_resource(str(row.get("image", "")))
        if task_id:
            return task_id
    return ""


def _selection_for_run(
    run: dict[str, Any],
    snapshot: dict[str, Any],
    *,
    source_path: Path,
    tree_root: Path | None = None,
) -> dict[str, Any]:
    groups_by_task: dict[str, list[dict[str, Any]]] = {}
    for group in snapshot.get("groups", []):
        task_id = _group_task_id(group)
        if task_id:
            groups_by_task.setdefault(task_id, []).append(group)

    selected: list[dict[str, Any]] = []
    selected_trajectories: dict[str, str] = {}
    tree_tasks = {
        str(item.get("task_id", "")).strip(): item
        for item in run.get("tree_tasks", [])
        if isinstance(item, dict)
    }
    for task_summary in run["tasks"]:
        task_id = str(task_summary.get("task_id", "")).strip()
        result = run["results"].get(task_id, {})
        evaluations = result.get("evaluations", {})
        tree_summary = tree_tasks.get(task_id, {})
        available_trajectory_ids = {
            str(group.get("meta_task", "")).strip()
            for group in groups_by_task.get(task_id, [])
        }
        unknown_trajectory_ids = sorted(
            str(trajectory_id)
            for trajectory_id in evaluations
            if str(trajectory_id) not in available_trajectory_ids
        )
        if unknown_trajectory_ids:
            raise QualitySourceMismatch(
                f"质检结果中的轨迹不在当前标注表中：{', '.join(unknown_trajectory_ids[:5])}"
            )
        candidates: list[tuple[dict[str, Any], float, dict[str, Any]]] = []
        for group in groups_by_task.get(task_id, []):
            trajectory_id = str(group.get("meta_task", "")).strip()
            evaluation = evaluations.get(trajectory_id)
            if not isinstance(evaluation, dict):
                continue
            score = _score(evaluation.get("global_score"))
            if score is not None:
                candidates.append((group, score, evaluation))
        if not candidates:
            raise QualitySelectionUnavailable(
                f"任务 {task_id} 没有可用的轨迹质检评分，暂不能进入修正"
            )

        # ``max`` is stable, so a score tie keeps the first group in the
        # original annotated workbook order.
        group, score, evaluation = max(candidates, key=lambda item: item[1])
        trajectory_id = str(group["meta_task"])
        selected_trajectories[task_id] = trajectory_id
        selected.append(
            {
                "task_id": task_id,
                "goal": str(
                    tree_summary.get("goal")
                    or task_summary.get("goal")
                    or group.get("task")
                    or task_id
                ),
                "trajectory_id": trajectory_id,
                "global_score": score,
                "passed_threshold": _as_bool(evaluation.get("passed_threshold")),
                "trajectory_count": int(
                    tree_summary.get("trajectory_count")
                    or result.get("trajectory_count")
                    or len(evaluations)
                ),
                "step_count": len(group.get("rows", [])),
            }
        )

    return {
        "status": "ready",
        "run_id": run["run_id"],
        "tree_run_id": run["tree_run_id"],
        "tree_completed_at": run.get("tree_completed_at", ""),
        "quality_completed_at": run.get("quality_completed_at", ""),
        "completed_at": run["completed_at"],
        "total_task_count": len(run.get("tree_tasks") or []),
        "reviewed_task_count": len(selected),
        "source_id": FIXED_SOURCE_ID,
        "source_path": "backend_workspace/annotated_trajectories.xlsx",
        "source_sha256": _verify_source_version(
            run["run_id"], source_path, tree_root=tree_root
        ),
        "tasks": selected,
        "selected_trajectories": selected_trajectories,
    }


def top1_recommendation(
    tree_run_id: str | None = None,
    *,
    root: Path | None = None,
    tree_root: Path | None = None,
    source_path: Path | None = None,
    asset_root: Path | None = None,
) -> dict[str, Any]:
    """Return Top-1s for a selected batch, or the latest usable batch by default."""
    runs = _completed_quality_runs(root, tree_root)
    if not runs:
        return {
            "status": "blocked",
            "message": "请先完成轨迹质检，再进入轨迹修正；当前没有可用的质检批次",
            "tasks": [],
        }
    selected_run = runs[0]
    if tree_run_id:
        selected_run = next(
            (run for run in runs if str(run["tree_run_id"]) == str(tree_run_id)),
            None,
        )
        if selected_run is None:
            raise QualitySelectionUnavailable(f"批次 {tree_run_id} 没有可用的质检结果")
    source_path = FIXED_ANNOTATED_XLSX if source_path is None else source_path
    asset_root = FIXED_TRAJECTORY_ROOT if asset_root is None else asset_root
    snapshot = load_snapshot(
        source_path,
        asset_root=asset_root,
        source_kind="annotated_workbook",
    )
    return _selection_for_run(
        selected_run,
        snapshot,
        source_path=source_path,
        tree_root=tree_root,
    )


def top1_selection_for_run(
    tree_run_id: str,
    *,
    root: Path | None = None,
    tree_root: Path | None = None,
    source_path: Path | None = None,
    asset_root: Path | None = None,
) -> dict[str, Any]:
    runs = _completed_quality_runs(root, tree_root)
    selected = next(
        (run for run in runs if str(run["tree_run_id"]) == str(tree_run_id)),
        None,
    )
    if selected is None:
        raise QualitySelectionUnavailable("请先完成轨迹质检，再进入轨迹修正")
    source_path = FIXED_ANNOTATED_XLSX if source_path is None else source_path
    asset_root = FIXED_TRAJECTORY_ROOT if asset_root is None else asset_root
    snapshot = load_snapshot(
        source_path,
        asset_root=asset_root,
        source_kind="annotated_workbook",
    )
    return _selection_for_run(
        selected,
        snapshot,
        tree_root=tree_root,
        source_path=source_path,
    )


def validate_selection_source(selection: dict[str, Any]) -> None:
    expected = str(selection.get("source_sha256", ""))
    if expected and FIXED_ANNOTATED_XLSX.is_file() and _source_sha256(FIXED_ANNOTATED_XLSX) != expected:
        raise QualitySourceMismatch("标注表已发生变化，该修正草稿对应的数据版本已失效")


def filter_snapshot(snapshot: dict[str, Any], selection: dict[str, Any]) -> dict[str, Any]:
    """Keep exactly one source group for every selected task."""
    selected = selection.get("tasks", [])
    group_by_key = {
        (_group_task_id(group), str(group.get("meta_task", ""))): group
        for group in snapshot.get("groups", [])
    }
    groups: list[dict[str, Any]] = []
    for task in selected:
        task_id = str(task.get("task_id", ""))
        trajectory_id = str(task.get("trajectory_id", ""))
        group = group_by_key.get((task_id, trajectory_id))
        if group is None:
            raise QualitySelectionUnavailable(
                f"质检结果中的轨迹 {trajectory_id} 不在当前标注表中，无法进入修正"
            )
        filtered = deepcopy(group)
        filtered["group_id"] = f"group_{len(groups)}"
        filtered["quality"] = "未知"
        filtered["prefix"] = "[⚪ 未知]"
        filtered["export"] = False
        groups.append(filtered)

    result = dict(snapshot)
    result["groups"] = groups
    result["row_count"] = sum(len(group.get("rows", [])) for group in groups)
    result["selection"] = selection
    return result
