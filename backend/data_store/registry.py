"""Transactional current records, separate from immutable artifact snapshots."""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator

from .paths import DATA_ROOT, contained_path
from ..file_io import io_path
from . import summary_index


class RevisionConflict(ValueError):
    """The stored revision changed after a caller read the record."""


class SummaryUnavailable(RuntimeError):
    """A short list read must not queue behind a long-running writer."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


class RecordStore:
    def __init__(self, root: Path = DATA_ROOT):
        self.root = contained_path(Path(root))
        self.database_path = contained_path(self.root, "system", "app.sqlite")
        self._summary_worker = None

    @contextmanager
    def _connection(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        # Missing read-only stores are handled before connecting by public reads.
        if write:
            io_path(self.database_path.parent).mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(str(self.database_path), timeout=30, isolation_level=None)
        else:
            connection = sqlite3.connect(self.database_path.as_uri() + "?mode=ro", uri=True, timeout=30, isolation_level=None)
        try:
            connection.execute("PRAGMA busy_timeout = 30000")
            if write:
                # DELETE journal keeps read-only GETs from creating WAL/SHM
                # sidecar files. Transactions still serialize writers.
                deadline = time.monotonic() + 30
                while True:
                    try:
                        connection.execute("PRAGMA journal_mode = DELETE")
                        break
                    except sqlite3.OperationalError as exc:
                        if not any(word in str(exc).lower() for word in ("locked", "busy")) or time.monotonic() >= deadline:
                            raise
                        time.sleep(0.025)
                connection.execute("PRAGMA synchronous = FULL")
                connection.execute("""CREATE TABLE IF NOT EXISTS records (
                    namespace TEXT NOT NULL,
                    record_key TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    payload TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (namespace, record_key)
                )""")
                summary_index.ensure_schema(connection)
            yield connection
        finally:
            connection.close()

    @staticmethod
    def _validate_key(namespace: str, key: str) -> None:
        if not isinstance(namespace, str) or not namespace or not isinstance(key, str) or not key:
            raise ValueError("Record namespace and key must be non-empty strings")

    @staticmethod
    def _read(connection: sqlite3.Connection, namespace: str, key: str) -> dict | None:
        if connection.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'records'").fetchone() is None:
            return None
        row = connection.execute(
            "SELECT payload FROM records WHERE namespace = ? AND record_key = ?", (namespace, key)
        ).fetchone()
        return json.loads(row[0]) if row else None

    def get(self, namespace: str, key: str) -> dict | None:
        self._validate_key(namespace, key)
        if not io_path(self.database_path).exists():
            return None
        with self._connection() as connection:
            return self._read(connection, namespace, key)

    def list(self, namespace: str) -> list[dict]:
        self._validate_key(namespace, "*")
        if not io_path(self.database_path).exists():
            return []
        with self._connection() as connection:
            if connection.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'records'").fetchone() is None:
                return []
            rows = connection.execute(
                "SELECT payload FROM records WHERE namespace = ? ORDER BY updated_at, record_key", (namespace,)
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def get_many(self, namespace: str, keys: list[str]) -> list[dict]:
        """Read only bound identities, retaining caller order and omitting missing."""
        self._validate_key(namespace, "*")
        keys = list(dict.fromkeys(keys))
        for key in keys:
            self._validate_key(namespace, key)
        if not keys or not io_path(self.database_path).exists():
            return []
        found = {}
        with self._connection() as connection:
            if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='records'").fetchone() is None:
                return []
            for start in range(0, len(keys), 400):
                chunk = keys[start:start + 400]
                rows = connection.execute("SELECT record_key,payload FROM records WHERE namespace=? AND record_key IN (" + ",".join("?" for _ in chunk) + ")", [namespace, *chunk]).fetchall()
                found.update((key, json.loads(payload)) for key, payload in rows)
        return [found[key] for key in keys if key in found]

    def summary_page(self, namespace: str, **options) -> dict:
        """Read-only paged projection. Missing historical index is explicit."""
        if not io_path(self.database_path).exists():
            # A nonexistent store has no history; GET still creates no database.
            with sqlite3.connect(":memory:") as connection:
                value = summary_index.page(connection, namespace, **options)
            value.update(total=0, filtered_total=0, scope_counts={"total": 0, "active": 0, "abnormal": 0}, index_state="ready", counts_complete=True)
            return value
        try:
            with self._connection() as connection:
                # List UI keeps its last successful snapshot on temporary busy;
                # authoritative controls retain the original write/read policy.
                connection.execute("PRAGMA busy_timeout = 250")
                # Count/page/latest share one read snapshot.
                connection.execute("BEGIN")
                return summary_index.page(connection, namespace, **options)
        except sqlite3.OperationalError as exc:
            if any(word in str(exc).lower() for word in ("locked", "busy")):
                raise SummaryUnavailable("运行摘要暂时繁忙，请稍后刷新；已有记录仍有效") from exc
            raise

    def get_summary(self, namespace: str, key: str) -> dict | None:
        self._validate_key(namespace, key)
        values = self.summary_page(namespace, key=key, limit=1)["items"]
        return values[0] if values else None

    def summary_list(self, namespace: str, **options) -> list[dict]:
        """Small rows for background reconciliation, ordered oldest first."""
        return list(reversed(self.summary_page(namespace, all_rows=True, **options)["items"]))

    def start_summary_backfill(self, batch_size: int = 50):
        if self._summary_worker and self._summary_worker.thread.is_alive():
            return
        self._summary_worker = summary_index.BackfillWorker(self, batch_size)
        self._summary_worker.thread.start()

    def close_summary_backfill(self):
        if self._summary_worker:
            self._summary_worker.close()

    def matching_keys(self, namespace: str, *, batch_id=None, session_id=None, excluded_statuses=()) -> list[str]:
        """Authoritative metadata selection for controls and legacy readers.

        Unlike UI projections, this remains complete during migration. SQLite
        selects identities without loading historical payloads into Python;
        callers then fetch only the matching current records.
        """
        self._validate_key(namespace, "*")
        if not io_path(self.database_path).exists():
            return []
        where, args = ["namespace=?"], [namespace]
        if batch_id is not None or session_id is not None:
            scopes = []
            if batch_id is not None:
                scopes.append("COALESCE(NULLIF(json_extract(payload,'$.batch_id'),''),json_extract(payload,'$.run_id'))=?")
                args.append(batch_id)
            if session_id is not None:
                scopes.append("json_extract(payload,'$.session_id')=?")
                args.append(session_id)
            where.append("(" + " OR ".join(scopes) + ")")
        if excluded_statuses:
            where.append("COALESCE(json_extract(payload,'$.status'),'') NOT IN (" + ",".join("?" for _ in excluded_statuses) + ")")
            args.extend(sorted(excluded_statuses))
        with self._connection() as connection:
            connection.execute("BEGIN")
            if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='records'").fetchone() is None:
                return []
            if namespace in summary_index.NAMESPACES and summary_index.ready(connection):
                # Same-transaction index maintenance makes this exact, while
                # controls still reread the selected authoritative records.
                index_where, index_args = ["namespace=?", "projection_version=?"], [namespace, summary_index.VERSION]
                if batch_id is not None or session_id is not None:
                    scopes = []
                    if batch_id is not None:
                        scopes.append("batch_id=?")
                        index_args.append(batch_id)
                    if session_id is not None:
                        scopes.append("json_extract(payload,'$.session_id')=?")
                        index_args.append(session_id)
                    index_where.append("(" + " OR ".join(scopes) + ")")
                if excluded_statuses:
                    index_where.append("status NOT IN (" + ",".join("?" for _ in excluded_statuses) + ")")
                    index_args.extend(sorted(excluded_statuses))
                return [row[0] for row in connection.execute("SELECT record_key FROM record_summaries WHERE " + " AND ".join(index_where) + " ORDER BY updated_at,record_key", index_args)]
            return [row[0] for row in connection.execute("SELECT record_key FROM records WHERE " + " AND ".join(where) + " ORDER BY updated_at,record_key", args)]

    @staticmethod
    def _save(connection: sqlite3.Connection, namespace: str, key: str, payload: dict, revision: int) -> dict:
        if not isinstance(payload, dict):
            raise TypeError("Record payload must be a dictionary")
        saved = dict(payload, storage_revision=revision)
        serialized = json.dumps(saved, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        updated_at = utc_now()
        connection.execute(
            """INSERT INTO records (namespace, record_key, revision, payload, updated_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(namespace, record_key) DO UPDATE SET
               revision = excluded.revision, payload = excluded.payload, updated_at = excluded.updated_at""",
            (namespace, key, revision, serialized, updated_at),
        )
        summary_index.save_projection(connection, namespace, key, saved, revision, updated_at)
        return json.loads(serialized)

    def put(self, namespace: str, key: str, payload: dict, expected_revision: int | None = None) -> dict:
        self._validate_key(namespace, key)
        with self._connection(write=True) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                current = self._read(connection, namespace, key)
                revision = current["storage_revision"] if current else 0
                if expected_revision is not None and expected_revision != revision:
                    raise RevisionConflict(f"Record {namespace}/{key} changed: expected revision {expected_revision}, found {revision}")
                saved = self._save(connection, namespace, key, payload, revision + 1)
                connection.commit()
                return saved
            except BaseException:
                connection.rollback()
                raise

    def update(self, namespace: str, key: str, mutator: Callable[[dict], dict | None], default: dict | None = None) -> dict:
        """Run a read/modify/write in one SQLite transaction, across processes too.

        The callback may modify its argument in place and return None, or return
        a replacement dictionary. A missing record requires an explicit default.
        """
        self._validate_key(namespace, key)
        with self._connection(write=True) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                current = self._read(connection, namespace, key)
                revision = current["storage_revision"] if current else 0
                if current is None:
                    if default is None:
                        raise KeyError(f"Record {namespace}/{key} does not exist")
                    current = json.loads(json.dumps(default, ensure_ascii=False, allow_nan=False))
                result = mutator(current)
                saved = self._save(connection, namespace, key, current if result is None else result, revision + 1)
                connection.commit()
                return saved
            except BaseException:
                connection.rollback()
                raise

    def put_many(self, entries: list[dict], *, deletes: list[dict] | None = None) -> list[dict]:
        """Atomically save records with independent optional expected revisions."""
        if not entries and not deletes:
            return []
        keys = set()
        for entry in entries:
            self._validate_key(entry["namespace"], entry["key"])
            identity = (entry["namespace"], entry["key"])
            if identity in keys:
                raise ValueError("A record may only appear once per transaction")
            keys.add(identity)
        with self._connection(write=True) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                results = []
                for entry in entries:
                    namespace, key = entry["namespace"], entry["key"]
                    current = self._read(connection, namespace, key)
                    revision = current["storage_revision"] if current else 0
                    expected = entry.get("expected_revision")
                    if expected is not None and expected != revision:
                        raise RevisionConflict(f"Record {namespace}/{key} changed: expected revision {expected}, found {revision}")
                    results.append(self._save(connection, namespace, key, entry["payload"], revision + 1))
                for entry in deletes or []:
                    namespace, key = entry['namespace'], entry['key']
                    self._validate_key(namespace, key)
                    if (namespace, key) in keys:
                        raise ValueError('Cannot save and delete the same record')
                    current = self._read(connection, namespace, key)
                    if 'expected_revision' in entry and (current or {}).get('storage_revision', 0) != entry['expected_revision']:
                        raise RevisionConflict(f'Record {namespace}/{key} changed')
                    connection.execute('DELETE FROM records WHERE namespace = ? AND record_key = ?', (namespace, key))
                    summary_index.delete_projection(connection, namespace, key)
                connection.commit()
                return results
            except BaseException:
                connection.rollback()
                raise

    def delete(self, namespace: str, key: str, expected_revision: int | None = None) -> bool:
        self._validate_key(namespace, key)
        if not io_path(self.database_path).exists():
            return False
        with self._connection(write=True) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                current = self._read(connection, namespace, key)
                revision = current["storage_revision"] if current else 0
                if expected_revision is not None and expected_revision != revision:
                    raise RevisionConflict(f"Record {namespace}/{key} changed: expected revision {expected_revision}, found {revision}")
                connection.execute("DELETE FROM records WHERE namespace = ? AND record_key = ?", (namespace, key))
                summary_index.delete_projection(connection, namespace, key)
                connection.commit()
                return current is not None
            except BaseException:
                connection.rollback()
                raise
