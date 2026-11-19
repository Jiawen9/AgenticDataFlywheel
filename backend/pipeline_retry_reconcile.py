"""Reconcile an overnight model-stage retry against durable child job records."""


def reconcile(manager, pipeline, step):
    """Return True only when the normal stage retry can be submitted safely."""
    from .pipelines import ACTIVE_JOBS, FAILED_JOBS, PipelineError

    stage = step["id"]
    jobs = {job["job_id"]: job for job in manager.runtime.jobs(stage, pipeline)}
    bound = list(step["job_ids"])
    previous = set(step.get("previous_job_ids", []))
    checkpoint = step.get("retry_job_id")
    known = previous.union(bound)

    # The service can commit a job before its caller receives the response.
    # Only recover this pipeline's own dispatch for the same frozen attempt.
    for job in sorted(jobs.values(), key=lambda row: (row.get("created_at") or "", row["job_id"])):
        job_id = job["job_id"]
        if job_id in known or job.get("pipeline_id") != pipeline["pipeline_id"]:
            continue
        origin = job.get("resumed_from") or job.get("origin_job_id")
        if origin != checkpoint:
            continue
        if not checkpoint:
            if step.get("job_ids") or not step.get("started_at"):
                continue
            if (job.get("created_at") or "") < step["started_at"]:
                continue
            if (stage == "preprocessing" and pipeline.get("input_digest")
                    and pipeline["input_digest"] != job.get("input_digest")):
                continue
            if stage == "quality" and (not job.get("task_ids")
                    or not set(job["task_ids"]).issubset(pipeline["task_ids"])):
                continue
        if origin in bound:
            bound.remove(origin)
            if origin not in previous:
                step.setdefault("previous_job_ids", []).append(origin)
                previous.add(origin)
            if step.get("failed_retry_job_id") == origin:
                step.pop("failed_retry_job_id")
        bound.append(job_id)

    if not bound and checkpoint:
        bound.append(checkpoint)
    if any(job_id not in jobs for job_id in bound):
        raise PipelineError("重试前核对失败：关联作业记录缺失，等待核对原作业")
    step["job_ids"] = bound
    if any(jobs[job_id]["status"] in ACTIVE_JOBS for job_id in bound):
        step["retry_info"].update(next_retry_at=None, last_error=None)
        step.update(status="running", error=None, message="继续跟踪已启动的作业")
        pipeline.update(status="running", error=None)
        return False
    if manager._completed_while_waiting(pipeline, step):
        return False
    if any(job["status"] in ACTIVE_JOBS for job in jobs.values()):
        raise PipelineError("同一批次仍有关联关系未确认的活动作业，等待其结束后再续跑")
    if any(jobs[job_id]["status"] not in FAILED_JOBS | {"succeeded", "completed"} for job_id in bound):
        raise PipelineError("重试前核对失败：关联作业状态无法确认")

    if stage == "quality":
        from .quality_execution import NAMESPACE

        # Include an older lease even if its job is terminal or absent from
        # this attempt. A late worker must not race the replacement worker.
        ids = manager.records.matching_keys(NAMESPACE, batch_id=pipeline["batch_id"])
        for execution in manager.records.get_many(NAMESPACE, ids):
            process = execution.get("process") or {}
            if (execution.get("state") == "active" or process.get("cleanup_failed") or
                    (process.get("pid") and process.get("exited") is not True)):
                raise PipelineError(f"质检子进程退出未确认，等待核对作业 {execution['job_id']}")

    # A succeeded quality job can have an incomplete fused result. Its retry
    # retains its rubric checkpoints and frozen checklist options.
    failed = [job_id for job_id in bound if jobs[job_id]["status"] in FAILED_JOBS]
    target = step.get("failed_retry_job_id")
    if target not in failed:
        target = failed[-1] if failed else None
    if not target and len(bound) > 1 and stage == "quality":
        for job_id in reversed(bound):
            tasks = jobs[job_id].get("task_ids")
            if tasks and not manager.runtime.ready(stage, dict(pipeline, task_ids=tasks)):
                target = job_id
                break
    if bound:
        step["failed_retry_job_id"] = target or bound[-1]
    return True
