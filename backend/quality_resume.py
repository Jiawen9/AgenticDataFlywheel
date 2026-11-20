"""Rubric checkpoint identity shared by the worker and Pipeline retry accounting."""
from .model_stage_checkpoint import ModelStageCheckpoint


def quality_chunk_checkpoint(root, batch_id, task_id, task_fingerprint, *, commit_guard=None):
    return ModelStageCheckpoint(
        root, "quality", [batch_id, task_id, task_fingerprint], commit_guard=commit_guard
    )
