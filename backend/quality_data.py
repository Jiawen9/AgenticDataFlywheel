"""Read current trajectory quality records without importing AdaRubric."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .trajectory_data import QUALITY_RESULTS_DIR
from .data_store import RecordStore, rebase_data_path
from .stage_artifacts import store_root

RUBRIC_DIR = QUALITY_RESULTS_DIR.parent / "rubric_outputs" / "rubrics"


def rubric_ready(task_id: str) -> bool:
    for path in RUBRIC_DIR.glob("*.json"):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        if isinstance(value, dict) and str(value.get("task_id", "")) == task_id:
            return True
    return False


def quality_manifest(run_id: str, root: Path = QUALITY_RESULTS_DIR) -> dict[str, Any]:
    return RecordStore(store_root(root)).get("quality_manifests", run_id) or {"run_id": run_id, "tasks": []}


def quality_task(run_id: str, task_id: str, root: Path = QUALITY_RESULTS_DIR) -> dict[str, Any] | None:
    data_root = store_root(root)
    value = RecordStore(data_root).get("quality_results", f"{run_id}:{task_id}")
    if value is not None and value.get("rubric_path"):
        value = {**value, "rubric_path": str(rebase_data_path(value["rubric_path"], data_root))}
    return value
