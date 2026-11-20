"""SQLite results for the completed units of a frozen model stage."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing, contextmanager, nullcontext

from .data_store.paths import contained_path
from .file_io import io_path


def _serialized(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"), allow_nan=False)


class ModelStageCheckpoint:
    def __init__(self, root, stage, identity, *, commit_guard=None):
        identity_hash = hashlib.sha256(_serialized([1, identity]).encode("utf-8")).hexdigest()
        self.path = contained_path(root, "system", "model_stage_checkpoints", stage,
                                   identity_hash + ".sqlite")
        self.commit_guard = commit_guard or nullcontext

    @contextmanager
    def _read(self):
        if not io_path(self.path).is_file():
            yield None
            return
        with closing(sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True)) as db:
            table = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='results'"
            ).fetchone()
            yield db if table else None

    def get(self, kind, key):
        with self._read() as db:
            if db is None:
                return None
            row = db.execute("SELECT result FROM results WHERE kind=? AND unit=?",
                             (kind, _serialized(key))).fetchone()
            return json.loads(row[0]) if row else None

    def save(self, kind, key, result):
        unit, payload = _serialized(key), _serialized(result)
        with self.commit_guard():
            io_path(self.path.parent).mkdir(parents=True, exist_ok=True)
            with closing(sqlite3.connect(io_path(self.path))) as db, db:
                db.execute("""CREATE TABLE IF NOT EXISTS results (
                    kind TEXT NOT NULL, unit TEXT NOT NULL, result TEXT NOT NULL,
                    PRIMARY KEY (kind, unit)) WITHOUT ROWID""")
                db.execute("""INSERT INTO results VALUES (?, ?, ?)
                    ON CONFLICT(kind, unit) DO UPDATE SET result=excluded.result""",
                           (kind, unit, payload))
        return result

    def discard(self, kind, key):
        if not io_path(self.path).is_file():
            return
        with self.commit_guard(), closing(sqlite3.connect(io_path(self.path))) as db, db:
            db.execute("DELETE FROM results WHERE kind=? AND unit=?", (kind, _serialized(key)))

    def count(self, kind=None):
        with self._read() as db:
            if db is None:
                return 0
            if kind is None:
                return db.execute("SELECT COUNT(*) FROM results").fetchone()[0]
            return db.execute("SELECT COUNT(*) FROM results WHERE kind=?", (kind,)).fetchone()[0]
