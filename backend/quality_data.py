"""Read current trajectory quality records without importing AdaRubric."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .trajectory_data import QUALITY_RESULTS_DIR
from .data_store import RecordStore, ArtifactStore, rebase_data_path
from .batch_results import current_quality_payload, resolve_current_batch_id
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
    data_root = store_root(root)
    batch_id = resolve_current_batch_id(run_id, data_root)
    if batch_id:
        value = current_quality_payload(batch_id, data_root)
        return {**value, "tasks": [{key: field for key, field in item.items() if key not in {"evaluations", "rubric"}}
                                   for item in value["tasks"]]}
    if RecordStore(data_root).get("batch_run_aliases", run_id):
        return {"run_id": run_id, "tasks": []}
    return RecordStore(data_root).get("quality_manifests", run_id) or {"run_id": run_id, "tasks": []}


def quality_task(run_id: str, task_id: str, root: Path = QUALITY_RESULTS_DIR) -> dict[str, Any] | None:
    data_root = store_root(root)
    batch_id = resolve_current_batch_id(run_id, data_root)
    if batch_id:
        value = next((item for item in current_quality_payload(batch_id, data_root)["tasks"]
                      if item.get("task_id") == task_id), None)
    else:
        value = None if RecordStore(data_root).get("batch_run_aliases", run_id) else RecordStore(data_root).get("quality_results", f"{run_id}:{task_id}")
    if value is not None and value.get("rubric_path"):
        value = {**value, "rubric_path": str(rebase_data_path(value["rubric_path"], data_root))}
    return value
