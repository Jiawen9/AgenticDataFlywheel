"""Measure saved work at a Pipeline retry boundary using frozen input identities."""
from __future__ import annotations

from .annotation_checkpoint import AnnotationCheckpoint
from .batch_results import digest
from .quality_fusion import fuse


def _bound_jobs(records, namespace, pipeline, step):
    ids = [*step.get("job_ids", []), step.get("failed_retry_job_id"), step.get("retry_job_id")]
    return [job for job in records.get_many(namespace, [ident for ident in ids if ident])
            if job.get("batch_id") == pipeline["batch_id"]
            and job.get("pipeline_id") in (None, pipeline["pipeline_id"])]


def _partials(records, job):
    keys = [f"{job['job_id']}:{task}" for task in job.get("task_fingerprints", {})]
    for start in range(0, len(keys), 8):
        yield from records.get_many("quality_partial_results", keys[start:start + 8])


def _quality_progress(records, pipeline, jobs):
    # Keep the small union outside the Pipeline's public record. Re-reading an
    # old checkpoint under a new job ID cannot earn another retry budget.
    namespace = "pipeline_quality_retry_progress"
    key = digest([pipeline["pipeline_id"], pipeline["batch_id"],
                  pipeline.get("collection_input_version"), pipeline.get("input_digest"),
                  (pipeline.get("config_fingerprints") or {}).get("quality")])
    record = records.get(namespace, key) or {}
    tasks = record.setdefault("tasks", {})
    changed = False
    for job in jobs:
        for partial in _partials(records, job):
            task = partial.get("task_id")
            fingerprint = job.get("task_fingerprints", {}).get(task)
            tree = job.get("source_tree_hashes", {}).get(task)
            if not tree or not fingerprint:
                continue
            identity = [tree, fingerprint]
            saved = tasks.get(task)
            if saved and saved["identity"] != identity:
                continue
            if (partial.get("input_fingerprint") != fingerprint or partial.get("source_tree_hash") != tree
                    or partial.get("batch_id") != pipeline["batch_id"] or partial.get("job_id") != job["job_id"]):
                continue
            if saved is None:
                saved = tasks[task] = {"identity": identity, "units": {}}
                changed = True
            result = partial["result"]
            definition = result.get("checklist_definition_digest")
            for trajectory, evaluation in result.get("evaluations", {}).items():
                checklist = evaluation.get("checklist") or {}
                reasons = fuse(evaluation, checklist, definition_digest=definition)["invalid_reasons"]
                # Evaluate each component separately: an invalid Checklist must
                # not erase a completed Rubric. A valid business failure counts.
                rubric_ok = not any(reason.startswith("rubric_") for reason in reasons)
                checklist_ok = (bool(definition) and bool(job.get("checklist_options"))
                                and saved.get("definition", definition) == definition
                                and not any(not reason.startswith("rubric_") for reason in reasons)
                                and "mixed_task_checklist_standards" not in
                                (evaluation.get("fusion") or {}).get("invalid_reasons", []))
                if checklist_ok:
                    saved["definition"] = definition
                previous = saved["units"].get(trajectory, 0)
                units = previous | int(rubric_ok) | (int(checklist_ok) << 1)
                if units != previous:
                    saved["units"][trajectory] = units
                    changed = True
    if changed:
        # The caller serializes a pipeline's transitions. CAS also protects
        # against accidentally overwriting a newer observation.
        records.put(namespace, key, record, expected_revision=record.get("storage_revision", 0))
    return sum(mask.bit_count() for task in tasks.values() for mask in task["units"].values())


def progress(root, records, pipeline, step) -> int:
    if step["id"] == "quality":
        return _quality_progress(records, pipeline, _bound_jobs(records, "quality_jobs", pipeline, step))
    if step["id"] == "preprocessing":
        jobs = _bound_jobs(records, "preprocessing_jobs", pipeline, step)
        # New retry job IDs refer to the same durable row store. The store
        # checks its frozen identity before returning committed Excel rows.
        for job in jobs:
            if job.get("annotation_chain_id") and job.get("pipeline_id") == pipeline["pipeline_id"]:
                return AnnotationCheckpoint(root, job, None).progress()[0]
    return 0
