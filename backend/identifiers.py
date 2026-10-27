"""Persistent, deployment-local IDs; never truncate integrity or authorization hashes."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .data_store.paths import DATA_ROOT
from .data_store.registry import RecordStore, RevisionConflict, utc_now
from .file_io import io_path

ID_SCHEME = "dated-v1"
COUNTERS = "short_id_counters"
RESERVATIONS = "short_id_reservations"
MAPPINGS = "short_id_mappings"
BATCH_IDENTITIES = "batch_identities"
_BEIJING = timezone(timedelta(hours=8))


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _occupied(store, connection, identifier):
    # Also respect legacy IDs created before the allocator existed.
    if connection.execute("SELECT 1 FROM records WHERE lower(record_key)=? LIMIT 1", (identifier.casefold(),)).fetchone():
        return True
    for parent in ("batches", "raw/collection_batches", "releases", "logs/pipelines",
                   "system/task_generation/runs", "system/task_generation/collection_batches",
                   "system/trajectory_correction/sessions"):
        if io_path(store.root / parent / identifier).exists():
            return True
    return False


def reserve_id_in_transaction(store: RecordStore, connection, prefix: str, *, key=None,
                              request_fingerprint: str | None = None,
                              now: datetime | None = None, purpose: str = "identity") -> str:
    """Reserve using the caller's write transaction; never commits it."""
    if not isinstance(prefix, str) or not re.fullmatch(r"[a-z]{1,6}", prefix):
        raise ValueError("Invalid short ID prefix")
    if purpose not in {"identity", "display"}:
        raise ValueError("Invalid ID reservation purpose")
    if request_fingerprint is not None and not isinstance(request_fingerprint, str):
        raise ValueError("Request fingerprint must be a string")
    instant = now or datetime.now(_BEIJING)
    if instant.tzinfo is None:
        raise ValueError("ID allocation time must include a timezone")
    date = instant.astimezone(_BEIJING).strftime("%y%m%d")
    key = json.loads(_json(key)) if key is not None else None
    identity = [prefix, key] if purpose == "identity" else [purpose, prefix, key]
    mapping_key = hashlib.sha256(_json(identity).encode()).hexdigest() if key is not None else None
    if mapping_key is not None:
        existing = store._read(connection, MAPPINGS, mapping_key)
        if existing is not None:
            if (existing.get("key") != key or existing.get("prefix") != prefix
                    or existing.get("purpose", "identity") != purpose):
                raise RevisionConflict("编号身份映射不一致")
            if request_fingerprint is not None and existing.get("request_fingerprint") != request_fingerprint:
                raise RevisionConflict("相同编号请求对应不同的处理参数")
            return existing["identifier"]
    counter_key = prefix + ":" + date
    counter = store._read(connection, COUNTERS, counter_key) or {}
    sequence = int(counter.get("sequence", 0))
    while True:
        sequence += 1
        identifier = f"{prefix}{date}-{sequence:03d}"
        if not _occupied(store, connection, identifier):
            break
    store._save(connection, COUNTERS, counter_key,
                {"prefix": prefix, "date": date, "sequence": sequence}, counter.get("storage_revision", 0) + 1)
    reservation = {"identifier": identifier, "prefix": prefix, "id_scheme": ID_SCHEME,
                   "created_at": utc_now(), "mapping_key": mapping_key}
    if purpose != "identity":
        reservation["purpose"] = purpose
    store._save(connection, RESERVATIONS, identifier, reservation, 1)
    if mapping_key is not None:
        store._save(connection, MAPPINGS, mapping_key,
                    {**reservation, "key": key, "request_fingerprint": request_fingerprint}, 1)
    if prefix == "b" and purpose == "identity":
        store._save(connection, BATCH_IDENTITIES, identifier,
                    {"batch_id": identifier, "id_scheme": ID_SCHEME, "created_at": reservation["created_at"]}, 1)
    return identifier


def allocate_id(prefix: str, root: Path = DATA_ROOT, *, key=None,
                request_fingerprint: str | None = None, now: datetime | None = None,
                purpose: str = "identity") -> str:
    """Reserve an ID atomically. A stable key replays the same ID across days/restarts.

    Callers must resolve legacy deterministic request IDs before allocating a new
    one. Reservations survive unsuccessful operations; IDs are never recycled.
    """
    store = RecordStore(root)
    with store._connection(write=True) as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            identifier = reserve_id_in_transaction(store, connection, prefix, key=key,
                request_fingerprint=request_fingerprint, now=now, purpose=purpose)
            connection.commit()
            return identifier
        except BaseException:
            connection.rollback()
            raise


def batch_uses_short_ids(batch_id: str, root: Path = DATA_ROOT) -> bool:
    """Missing metadata means legacy, even if the ID looks like a new ID."""
    value = RecordStore(root).get(BATCH_IDENTITIES, str(batch_id))
    return bool(value and value.get("id_scheme") == ID_SCHEME)


def is_short_id(value: str, prefix: str | None = None) -> bool:
    pattern = re.escape(prefix) if prefix else r"[a-z]{1,6}"
    return isinstance(value, str) and re.fullmatch(pattern + r"\d{6}-\d{3,}", value) is not None
