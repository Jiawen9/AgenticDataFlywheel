"""Persistent presentation aliases, isolated from business IDs and disk layout.

Only creation/publish paths call the write helpers. Resolving an identifier is
strictly read-only, including for old records and an uninitialized data store.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path

from .data_store import DATA_ROOT, RecordStore
from .data_store.registry import RevisionConflict, utc_now
from .file_io import io_path
from .identifiers import is_short_id, reserve_id_in_transaction

DISPLAY_IDS = "display_identifiers"
DISPLAY_BATCHES = "display_identifier_batches"
KINDS = frozenset({"batch", "run", "task", "trajectory", "pipeline", "job", "session", "export", "release"})
SCOPED_KINDS = frozenset({"task", "trajectory"})
_PREFIXES = {"batch": "b", "task": "t", "trajectory": "tr", "pipeline": "p",
             "job": "j", "session": "s", "export": "e", "release": "rel"}


def _identity(item: dict) -> dict:
    kind, identifier, scope = item.get("kind"), item.get("id"), item.get("scope", "")
    if kind not in KINDS or not isinstance(identifier, str) or not identifier or len(identifier) > 2048:
        raise ValueError("显示编号类型或完整编号无效")
    if not isinstance(scope, str) or len(scope) > 2048 or (kind not in SCOPED_KINDS and scope):
        raise ValueError("仅任务和轨迹编号可以指定所属批次")
    return {"kind": kind, "id": identifier, "scope": scope}


def _key(identity: dict) -> str:
    return hashlib.sha256(json.dumps([identity["kind"], identity["scope"], identity["id"]],
        ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()


def _prefix(identity: dict) -> str:
    if identity["kind"] == "run":
        if identity["id"].startswith("eval_"):
            return "eval"
        if identity["id"].startswith("cr_"):
            return "cr"
        raise ValueError("未识别的运行编号类型")
    return _PREFIXES[identity["kind"]]


class DisplayIdentifierStore:
    def __init__(self, root: Path = DATA_ROOT):
        self.records = RecordStore(root)

    def resolve(self, items: list[dict]) -> list[dict]:
        identities = [_identity(item) for item in items]
        if not io_path(self.records.database_path).exists():
            return [{**item, "display_id": item["id"], "mapped": False} for item in identities]
        with self.records._connection() as connection:
            result = []
            for identity in identities:
                value = (None if is_short_id(identity["id"]) else
                         self.records._read(connection, DISPLAY_IDS, _key(identity)))
                if value and any(value.get(key) != identity[key] for key in ("kind", "scope", "id")):
                    raise RevisionConflict("显示编号身份映射不一致")
                result.append({**identity, "display_id": value["display_id"] if value else identity["id"],
                               "mapped": value is not None})
            return result

    def _entry(self, connection, identity: dict, now: datetime | None = None) -> dict | None:
        if is_short_id(identity["id"]):
            return None
        if identity["kind"] in SCOPED_KINDS and not identity["scope"]:
            raise ValueError("新任务和轨迹必须明确所属批次")
        key = _key(identity)
        current = self.records._read(connection, DISPLAY_IDS, key)
        if current:
            if any(current.get(field) != identity[field] for field in ("kind", "id", "scope")):
                raise RevisionConflict("显示编号身份映射不一致")
            return None
        alias = reserve_id_in_transaction(self.records, connection, _prefix(identity),
            key=[identity["kind"], identity["scope"], identity["id"]], purpose="display", now=now)
        return {"namespace": DISPLAY_IDS, "key": key, "expected_revision": 0,
                "payload": {**identity, "display_id": alias, "created_at": utc_now()}}

    def prepare_entries(self, items: list[dict], *, now: datetime | None = None) -> list[dict]:
        """Reserve aliases, to be committed with the caller's formal artifact.

        A failed publish can leave reserved numbers, never a visible alias. A
        retry reuses those reservations even on a different day.
        """
        identities = list({_key(value): value for value in map(_identity, items)}.values())
        with self.records._connection(write=True) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                entries = [entry for identity in identities if (entry := self._entry(connection, identity, now))]
                connection.commit()
                return entries
            except BaseException:
                connection.rollback()
                raise

    def new_batch_entries(self, batch_id: str, tasks: list[dict]) -> list[dict]:
        items = [{"kind": "batch", "id": batch_id}]
        items.extend({"kind": "task", "id": item["task_id"], "scope": batch_id} for item in tasks)
        return [*self.prepare_entries(items), {"namespace": DISPLAY_BATCHES, "key": batch_id,
            "payload": {"batch_id": batch_id, "created_at": utc_now()}, "expected_revision": 0}]

    def conversion_entries(self, batch_id: str, payload: dict) -> list[dict]:
        # A short-looking legacy ID or an old manual batch does not opt in.
        if self.records.get(DISPLAY_BATCHES, batch_id) is None:
            return []
        items = []
        for rows in payload.get("sheets", {}).values():
            for row in rows:
                for field, kind in (("task_id", "task"), ("trajectory_id", "trajectory")):
                    if row.get(field):
                        items.append({"kind": kind, "id": str(row[field]), "scope": batch_id})
        return self.prepare_entries(items)

    def create_run(self, namespace: str, run_id: str, payload: dict, *, now: datetime | None = None) -> dict:
        """Atomically create a new run and alias; never alias an existing run."""
        if namespace not in {"collection_runs", "model_iter_runs"}:
            raise ValueError("不支持的运行记录类型")
        identity = _identity({"kind": "run", "id": run_id})
        with self.records._connection(write=True) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                if self.records._read(connection, namespace, run_id) is not None:
                    raise RevisionConflict("运行记录已存在")
                entry = self._entry(connection, identity, now)
                if entry:
                    self.records._save(connection, DISPLAY_IDS, entry["key"], entry["payload"], 1)
                saved = self.records._save(connection, namespace, run_id, payload, 1)
                connection.commit()
                return saved
            except BaseException:
                connection.rollback()
                raise
