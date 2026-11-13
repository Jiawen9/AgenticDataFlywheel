"""Standard-library-only validation for the scheduler and evaluator launcher."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
try:
    from .file_access import io_path, logical_path
except ImportError:
    from file_access import io_path, logical_path


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with io_path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_report(report_path: str | Path, trajectory_path: str | Path, *,
                    config_digest: str, session_id: str = "", checklist_policy: str = "llm",
                    profile_digest: str = "", runtime_digest: str = "", profile_id: str = "") -> dict:
    def invalid(reason: str, status: str = "error") -> dict:
        return {"valid": False, "status": status, "reason": reason, "verdict": ""}
    try:
        report = json.loads(io_path(report_path).read_text(encoding="utf-8-sig"))
        if not isinstance(report, dict):
            return invalid("report_not_object")
        if report.get("trajectory_sha256") != sha256_file(trajectory_path):
            return invalid("trajectory_digest_mismatch")
        if not config_digest or report.get("evaluator_config_digest") != config_digest:
            return invalid("evaluator_config_digest_mismatch")
        if session_id and report.get("session_id") != session_id:
            return invalid("session_id_mismatch")
        allowed_sources = {"gold", "llm"} if checklist_policy == "auto" else {"llm"}
        if (checklist_policy not in {"llm", "auto"}
                or report.get("checklist_policy") != checklist_policy or report.get("checklist_source") not in allowed_sources
                or report.get("evaluation_mode") != "checklist"):
            return invalid("evaluation_policy_mismatch")
        for name, expected in (("profile_digest", profile_digest), ("runtime_digest", runtime_digest),
                               ("profile_id", profile_id)):
            if expected and report.get(name) != expected:
                return invalid(name + "_mismatch")
        if checklist_policy == "auto" and not all((profile_digest, runtime_digest, profile_id)):
            return invalid("profile_identity_required")
        summary = report.get("checklist_summary")
        if not isinstance(summary, dict):
            return invalid("missing_checklist", "incomplete")
        total = summary.get("total")
        items = summary.get("items")
        if (type(total) is not int or total <= 0 or not isinstance(items, list)
                or len(items) != total):
            return invalid("invalid_checklist_count", "incomplete")
        if any(not isinstance(item, dict) or item.get("status") not in {"passed", "failed"}
               for item in items):
            return invalid("unchecked_checklist", "incomplete")
        ids = [item.get("id") for item in items]
        if any(not isinstance(key, str) or not key for key in ids) or len(set(ids)) != total:
            return invalid("invalid_checklist_ids")
        passed = sum(item["status"] == "passed" for item in items)
        if (summary.get("passed") != passed or summary.get("failed") != total - passed
                or summary.get("unchecked") != 0):
            return invalid("inconsistent_checklist_counts")
        states = [item["status"] for item in items]
        expected = "success" if passed == total else "fail"
        if 0 < passed < total and states == ["passed"] * passed + ["failed"] * (total - passed):
            expected = "partial_success"
        if report.get("verdict") != expected:
            return invalid("inconsistent_verdict")
        if report.get("evaluation_status") != "complete":
            return invalid("evaluation_incomplete", "incomplete")
        return {"valid": True, "status": "complete", "reason": "", "verdict": expected}
    except (OSError, ValueError, TypeError):
        return invalid("report_unreadable")
