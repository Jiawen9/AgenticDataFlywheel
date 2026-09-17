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


class RevisionConflict(ValueError):
    """The stored revision changed after a caller read the record."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


class RecordStore:
    def __init__(self, root: Path = DATA_ROOT):
        self.root = contained_path(Path(root))
        self.database_path = contained_path(self.root, "system", "app.sqlite")

    @contextmanager
    def _connection(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        # Missing read-only stores are handled before connecting by public reads.
        if write:
            self.database_path.parent.mkdir(parents=True, exist_ok=True)
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
        if not self.database_path.exists():
            return None
        with self._connection() as connection:
            return self._read(connection, namespace, key)

    def list(self, namespace: str) -> list[dict]:
        self._validate_key(namespace, "*")
        if not self.database_path.exists():
            return []
        with self._connection() as connection:
            if connection.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'records'").fetchone() is None:
                return []
            rows = connection.execute(
                "SELECT payload FROM records WHERE namespace = ? ORDER BY updated_at, record_key", (namespace,)
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    @staticmethod
    def _save(connection: sqlite3.Connection, namespace: str, key: str, payload: dict, revision: int) -> dict:
        if not isinstance(payload, dict):
            raise TypeError("Record payload must be a dictionary")
        saved = dict(payload, storage_revision=revision)
        serialized = json.dumps(saved, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        connection.execute(
            """INSERT INTO records (namespace, record_key, revision, payload, updated_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(namespace, record_key) DO UPDATE SET
               revision = excluded.revision, payload = excluded.payload, updated_at = excluded.updated_at""",
            (namespace, key, revision, serialized, utc_now()),
        )
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

    def put_many(self, entries: list[dict]) -> list[dict]:
        """Atomically save records with independent optional expected revisions."""
        if not entries:
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
                connection.commit()
                return results
            except BaseException:
                connection.rollback()
                raise

    def delete(self, namespace: str, key: str, expected_revision: int | None = None) -> bool:
        self._validate_key(namespace, key)
        if not self.database_path.exists():
            return False
        with self._connection(write=True) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                current = self._read(connection, namespace, key)
                revision = current["storage_revision"] if current else 0
                if expected_revision is not None and expected_revision != revision:
                    raise RevisionConflict(f"Record {namespace}/{key} changed: expected revision {expected_revision}, found {revision}")
                connection.execute("DELETE FROM records WHERE namespace = ? AND record_key = ?", (namespace, key))
                connection.commit()
                return current is not None
            except BaseException:
                connection.rollback()
                raise
