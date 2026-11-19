"""Cancellation checkpoints for work started by a Pipeline."""
from contextlib import contextmanager, nullcontext

from .data_store.locking import batch_lock


class PipelineJobCancelled(RuntimeError):
    def __init__(self, message="Pipeline 已请求终止"):
        super().__init__(message)


def termination_lock(root, pipeline_id):
    return batch_lock(root, "pipeline_termination_" + pipeline_id) if pipeline_id else nullcontext()


def check_pipeline_stop(records, pipeline_id):
    if pipeline_id:
        owner = records.get("pipelines", pipeline_id) or {}
        if owner.get("status") in {"terminating", "terminated"}:
            raise PipelineJobCancelled()


def check_job_stop(records, namespace, job_id):
    job = records.get(namespace, job_id) or {}
    if job.get("stop_requested_at") or job.get("status") == "cancelled":
        raise PipelineJobCancelled()
    check_pipeline_stop(records, job.get("pipeline_id"))


@contextmanager
def pipeline_commit_guard(records, pipeline_id):
    with termination_lock(records.root, pipeline_id):
        check_pipeline_stop(records, pipeline_id)
        yield


@contextmanager
def job_commit_guard(records, namespace, job_id):
    """Order the final commit against acceptance of the Pipeline stop."""
    job = records.get(namespace, job_id) or {}
    with pipeline_commit_guard(records, job.get("pipeline_id")):
        check_job_stop(records, namespace, job_id)
        yield


JOB_NAMESPACES = {
    "generation": "task_generation.jobs",
    "preprocessing": "preprocessing_jobs",
    "tree": "tree_jobs",
    "quality": "quality_jobs",
    "cot": "correction_cot_jobs",
    "overview": "training_overview_conversions",
}
QUEUED = {"queued", "pending", "awaiting_confirmation"}
WORKING = {"starting", "running", "dispatching", "stopping"}


def request_local_stops(records, pipeline, signal_quality):
    pipeline_id = pipeline["pipeline_id"]
    requested_at = pipeline["termination"]["requested_at"]
    pending = []
    for stage, namespace in JOB_NAMESPACES.items():
        keys = records.matching_keys(namespace, pipeline_id=pipeline_id)
        jobs = records.get_many(namespace, keys)
        for job in jobs:
            job_id = job.get("job_id") or job.get("release_id")
            if stage == "overview":
                job_id = job.get("release_id")
            if not job_id or job.get("pipeline_id") != pipeline_id:
                continue
            if job.get("status") in QUEUED | WORKING and not job.get("stop_requested_at"):
                def stop(current):
                    if current.get("pipeline_id") != pipeline_id:
                        return
                    status = current.get("status")
                    if status in QUEUED | WORKING:
                        current.setdefault("stop_requested_at", requested_at)
                        if status in QUEUED:
                            current.update(status="cancelled", error="Pipeline 已终止")
                            if "stage" in current:
                                current["stage"] = "cancelled"

                job = records.update(namespace, job_id, stop)
            if job.get("pipeline_id") != pipeline_id:
                continue
            quality_exited = True
            if stage == "quality" and (job.get("stop_requested_at") or
                    (job.get("failure") or {}).get("code") == "quality_worker_cleanup_failed"):
                quality_exited = signal_quality(job_id)
            if not quality_exited or job.get("status") in WORKING:
                pending.append({"stage": stage, "job_id": job_id,
                                "message": "等待质检进程退出" if not quality_exited else "正在结束当前操作"})
    return pending
