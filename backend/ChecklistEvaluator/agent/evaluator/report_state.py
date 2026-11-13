"""Shared report selection and completion checks for evaluator consumers."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_report(path: str | Path) -> dict[str, Any] | None:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def report_checklist_state(report: dict[str, Any]) -> tuple[bool, str]:
    """Completion is defined by all checklist items having a pass/fail result."""
    summary = report.get("checklist_summary")
    if not isinstance(summary, dict):
        return False, "missing_checklist_summary"
    items = summary.get("items")
    total = int(summary.get("total") or 0)
    if not isinstance(items, list) or total <= 0 or len(items) < total:
        return False, "missing_checklist_items"
    for item in items[:total]:
        if not isinstance(item, dict) or str(item.get("status") or "").lower() not in {
            "passed", "failed", "pass", "fail",
        }:
            return False, "unchecked_checklist_item"
    if report.get("verdict") in {None, "", "unchecked", "fail_to_parse_checklist"}:
        return False, "incomplete_verdict"
    return True, "complete"


def report_matches_trajectory(report: dict[str, Any], trajectory_path: str | Path) -> bool:
    expected = str(report.get("trajectory_sha256") or "").strip()
    if not expected:
        return False
    try:
        return expected == sha256_file(trajectory_path)
    except OSError:
        return False


def report_is_complete(report: dict[str, Any], trajectory_path: str | Path | None = None) -> bool:
    complete, _ = report_checklist_state(report)
    if not complete:
        return False
    if str(report.get("evaluation_status") or "").lower() == "incomplete":
        return False
    return trajectory_path is None or report_matches_trajectory(report, trajectory_path)


def latest_report(case_dir: str | Path, trajectory_path: str | Path | None = None,
                  *, complete_only: bool = False) -> tuple[Path | None, dict[str, Any] | None]:
    case = Path(case_dir)
    reports = sorted(case.glob("_report_*.json"), key=lambda path: (path.stat().st_mtime_ns, path.name))
    for path in reversed(reports):
        report = read_report(path)
        if report is None:
            continue
        if trajectory_path is not None and not report_matches_trajectory(report, trajectory_path):
            continue
        if complete_only and not report_is_complete(report, trajectory_path):
            continue
        return path, report
    return None, None
