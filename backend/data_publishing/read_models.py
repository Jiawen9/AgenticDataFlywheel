"""Bounded publication metadata, free of filesystem probes and source paths."""
from __future__ import annotations


def project_release(payload: dict) -> dict:
    fields = ("release_id", "name", "created_at", "updated_at", "source_kind", "pipeline_id",
              "source_count", "task_count", "trajectory_count", "step_count", "upload_status",
              "upload_job_id", "uploaded_at", "uploaded_files", "uploaded_bytes")
    value = {key: payload.get(key) for key in fields}
    value["source_kind"] = value.get("source_kind") or "workflow"
    value["batch_ids"] = [item for item in payload.get("batch_ids", []) if isinstance(item, str)][:100] if isinstance(payload.get("batch_ids"), list) else []
    value["excel_count"] = len(payload.get("excel_paths")) if isinstance(payload.get("excel_paths"), list) else 0
    value["trajectory_directory_count"] = len(payload.get("trajectory_paths")) if isinstance(payload.get("trajectory_paths"), list) else 0
    training = payload.get("training_files") if isinstance(payload.get("training_files"), list) else []
    value["training_count"] = len(training)
    value["training_ready_count"] = sum(item.get("status") == "ready" and bool(item.get("included_count")) for item in training if isinstance(item, dict))
    upload = payload.get("internal_upload") if isinstance(payload.get("internal_upload"), dict) else {}
    value["internal_upload"] = {key: upload[key] for key in (
        "job_id", "status", "completed_at", "completed_files", "total_files", "percent",
        "folder_upload", "include_trajectories", "completed_items", "total_items", "training_only", "training_jsonl", "training_destination_prefix") if key in upload} or None
    value["status"] = upload.get("status") or payload.get("upload_status") or "not_uploaded"
    value["availability_checked"] = False
    # Consumers must request detail for paths, receipts and current availability.
    value["excel_paths"] = []
    value["trajectory_paths"] = []
    return value
