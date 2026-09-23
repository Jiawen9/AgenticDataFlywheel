"""Pipeline ownership admission; authority is never supplied by an HTTP query."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar, copy_context
from pathlib import Path

from .data_store import DATA_ROOT, RecordStore

TERMINAL = {"terminated", "no_publishable_data", "succeeded", "published_summary_failed"}
_execution: ContextVar[str | None] = ContextVar("pipeline_execution", default=None)


class PipelineManagedError(RuntimeError):
    def __init__(self, pipeline: dict):
        self.detail = {"code": "pipeline_managed", "message": "该批次由 Pipeline 管理，请在流程页面操作",
                       "pipeline_id": pipeline["pipeline_id"], "batch_id": pipeline["batch_id"],
                       "status": pipeline["status"]}
        super().__init__(self.detail["message"])


def active_pipeline(batch_id: str, root: Path | None = None) -> dict | None:
    records = RecordStore(root or DATA_ROOT)
    owner = records.get("pipeline_owners", batch_id)
    if not owner:
        return None
    pipeline = records.get("pipelines", owner["pipeline_id"])
    return pipeline if pipeline and pipeline["status"] not in TERMINAL else None


def ensure_pipeline_write(batch_id: str | None, root: Path | None = None, *,
                          action: str = "process", session_id: str | None = None) -> None:
    if not batch_id:
        return
    pipeline = active_pipeline(batch_id, root)
    if not pipeline or _execution.get() == pipeline["pipeline_id"]:
        return
    if (action == "correction" and pipeline["mode"] == "manual"
            and pipeline["status"] == "waiting_for_correction"
            and session_id == pipeline.get("session_id")):
        return
    raise PipelineManagedError(pipeline)


@contextmanager
def internal_pipeline(pipeline_id: str):
    token = _execution.set(pipeline_id)
    try:
        yield
    finally:
        _execution.reset(token)


def execution_pipeline_id() -> str | None:
    return _execution.get()


def submit_with_context(executor, function, *args, **kwargs):
    """Carry trusted in-process authority into the existing model worker pool."""
    return executor.submit(copy_context().run, function, *args, **kwargs)
