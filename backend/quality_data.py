"""Read the latest trajectory-quality results without importing AdaRubric."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .trajectory_data import QUALITY_RESULTS_DIR

RUBRIC_DIR = QUALITY_RESULTS_DIR.parent / "rubric_outputs" / "rubrics"


def rubric_ready(task_id: str) -> bool:
    if not RUBRIC_DIR.is_dir():
        return False
    for path in RUBRIC_DIR.glob("*.json"):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        if isinstance(value, dict) and str(value.get("task_id", "")) == task_id:
            return True
    return False


def quality_manifest(run_id: str, root: Path = QUALITY_RESULTS_DIR) -> dict[str, Any]:
    path = root / run_id / "manifest.json"
    if not path.is_file():
        return {"run_id": run_id, "tasks": []}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return {"run_id": run_id, "tasks": []}
    return value if isinstance(value, dict) else {"run_id": run_id, "tasks": []}


def quality_task(run_id: str, task_id: str, root: Path = QUALITY_RESULTS_DIR) -> dict[str, Any] | None:
    path = (root / run_id / f"{task_id}.json").resolve()
    run_root = (root / run_id).resolve()
    try:
        path.relative_to(run_root)
    except ValueError:
        return None
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None
