"""Immutable logical input versions; execution source manifests remain untouched."""
from .collection_runs import CollectionRunError
from .task_generation.collection_batches import payload_digest


def current_input(records, logical_run_id, *, require_stable=True):
    head = records.get("collection_resume_heads", logical_run_id)
    if not head:
        return None
    if require_stable and head.get("active"):
        raise CollectionRunError("本轮正在续采，等待新采集快照完整回传后再处理")
    snapshot_id = head.get("current_snapshot_id")
    if not snapshot_id:
        if require_stable:
            raise CollectionRunError("续采结果尚未形成稳定快照")
        return None
    saved = records.get("collection_resume_snapshots", snapshot_id)
    if (not saved or saved.get("logical_run_id") != logical_run_id
            or saved.get("batch_id") != head["batch_id"]
            or saved.get("input_digest") != head.get("current_snapshot_digest")
            or saved.get("input_digest") != payload_digest(saved.get("input"))):
        raise CollectionRunError("采集快照身份或摘要校验失败")
    if require_stable and not saved["input"].get("trajectories"):
        raise CollectionRunError("当前稳定快照没有可预处理的轨迹；原件及诊断继续保留")
    return {**saved["input"], "input_digest": saved["input_digest"],
            "collection_snapshot_id": snapshot_id, "collection_epoch": head["revision"]}


def is_current_collection_snapshot(records, batch_id, snapshot):
    """Used before promoting downstream outputs; old jobs may retain their versions."""
    heads = [h for h in records.list("collection_resume_heads") if h.get("batch_id") == batch_id]
    if not heads:
        return not snapshot.get("collection_snapshot_id")
    return all(not h.get("active") and h.get("current_snapshot_id") == snapshot.get("collection_snapshot_id")
               and h.get("current_snapshot_digest") == snapshot.get("input_digest") for h in heads)


def evaluation_snapshot_status(records, source):
    """Read-only version labels; never alter a frozen preview, report or verdict."""
    source=source or {}
    run_id=source.get("source_run_id") or source.get("run_id")
    if not run_id:
        return {}
    head=records.get("collection_resume_heads",run_id)
    if not head:
        return {}
    previous=source.get("snapshot_id")
    return {"stale_collection_snapshot":bool(head.get("active") or previous!=head.get("current_snapshot_id")),
            "source_collection_snapshot_id":previous,
            "current_collection_snapshot_id":head.get("current_snapshot_id"),
            "collection_snapshot_pending":bool(head.get("active"))}
