"""Monotonic device-control observations, independent of evidence transfer."""
from .data_store.registry import utc_now

ACTIVE = {"preparing", "waiting", "dispatching", "queued", "starting", "running", "stopping"}
TERMINAL = {"completed", "succeeded", "partial", "failed", "cancelled", "interrupted", "not_run"}


def merge_control(current, remote):
    """Caller verifies remote run identity before applying this in one transaction."""
    revision = remote.get("state_revision")
    previous = current.get("remote_state_revision", -1)
    if revision is not None and (type(revision) is not int or revision < 0):
        return False
    if type(previous) is int and previous >= 0 and (revision is None or revision < previous):
        return False
    status = remote.get("collection_status") or remote.get("status")
    if status not in ACTIVE | TERMINAL | {"needs_attention"}:
        return False
    # Evidence polling may remain active after the device phase ends. It cannot
    # erase the final observation or a user's persistent stop intent.
    if current.get("control_terminal") and status in ACTIVE:
        return False
    phones = remote.get("phone_states")
    if isinstance(phones, dict):
        current["phone_states"] = phones
    if revision is not None:
        current["remote_state_revision"] = revision
    if remote.get("stop_requested_at") and not current.get("stop_requested_at"):
        current["stop_requested_at"] = remote["stop_requested_at"]
    if current.get("stop_requested_at") and status in ACTIVE:
        status = "stopping"
    current.update(collection_status=status, control_observed_at=utc_now(),
                   can_cancel_collection=status in ACTIVE or status == "needs_attention")
    if status in TERMINAL:
        current["control_terminal"] = True
        current["can_cancel_collection"] = any(
            isinstance(phone, dict) and (phone.get("status") in ACTIVE | {"needs_attention"}
                                        or phone.get("stop_confirmed") is False)
            for phone in (phones or {}).values()) if isinstance(phones, dict) else False
    return True


def can_cancel(run):
    if run.get("can_cancel_collection") is True:
        return True
    if run.get("control_terminal"):
        return False
    if run.get("collection_status") in TERMINAL:
        return False
    if run.get("status") in ACTIVE:
        return True
    # A failed dispatch can be uncertain: permit reconciling/stopping the exact
    # remote run instead of requiring local status=running.
    return bool(run.get("dispatch_attempted") is not False
                and run.get("dispatch_rejected") is not True
                and (run.get("submitted_at") or run.get("dispatch_response") or run.get("dispatched_at")))
