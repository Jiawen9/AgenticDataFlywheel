"""Durable annotation output for one frozen preprocessing retry chain."""
from __future__ import annotations

from contextlib import closing
import hashlib
from itertools import chain
import json
import sqlite3

from .data_store.paths import contained_path
from .file_io import io_path


class AnnotationCheckpoint:
    def __init__(self, root, job, commit_guard):
        chain = job["annotation_chain_id"]
        self.path = contained_path(root, "system", "preprocessing", job["batch_id"], chain, "rows.sqlite")
        self.identity = json.dumps([
            1, job["batch_id"], job["pipeline_id"], chain, job["fingerprint"], job["input_sha256"],
        ], ensure_ascii=False)
        self.job_id = job["job_id"]
        self.commit_guard = commit_guard
        self.completed = {}
        self.connection = None

    def progress(self):
        """Read only committed counts, including a commit just before a worker crashed."""
        if not io_path(self.path).is_file():
            return 0, 0
        with closing(sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True)) as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='frozen_sheet'").fetchone():
                return 0, 0
            row = db.execute("SELECT identity, total, (SELECT COUNT(*) FROM results) FROM frozen_sheet").fetchone()
        if row is None:
            return 0, 0
        if row[0] != self.identity:
            raise ValueError("标框检查点与本次冻结输入或配置不匹配")
        return row[2], row[1]

    def bind(self, rows, columns):
        # Length-delimited JSON values in original order include the source identity,
        # actual actions, images and summaries; an Excel row number alone is ambiguous.
        ordered = hashlib.sha256()
        for value in chain((columns,), rows):
            raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ordered.update(len(raw).to_bytes(8, "big"))
            ordered.update(raw)
        expected = (self.identity, ordered.hexdigest(), len(rows))
        db = sqlite3.connect(io_path(self.path))
        self.connection = db
        with db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("""CREATE TABLE IF NOT EXISTS frozen_sheet (
                singleton INTEGER PRIMARY KEY CHECK(singleton=1), identity TEXT NOT NULL,
                sheet_digest TEXT NOT NULL, total INTEGER NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS results (
                excel_row INTEGER PRIMARY KEY CHECK(excel_row >= 2),
                kind TEXT NOT NULL, actions_box TEXT, job_id TEXT NOT NULL, diagnostic TEXT,
                CHECK ((kind='annotated' AND actions_box IS NOT NULL)
                    OR (kind IN ('no_box_needed','model_output_invalid') AND actions_box IS NULL)))""")
            db.execute("INSERT OR IGNORE INTO frozen_sheet VALUES (1, ?, ?, ?)", expected)
            actual = db.execute("SELECT identity, sheet_digest, total FROM frozen_sheet WHERE singleton=1").fetchone()
            if actual != expected:
                raise ValueError("标框检查点与本次冻结输入、配置或转换行顺序不匹配")
            self.completed = {n: (kind, box) for n, kind, box in
                              db.execute("SELECT excel_row, kind, actions_box FROM results")}

    def save(self, excel_row, kind, actions_box, diagnostic=None):
        with self.commit_guard(), self.connection:
            self.connection.execute("INSERT INTO results VALUES (?, ?, ?, ?, ?)",
                                    (excel_row, kind, actions_box, self.job_id, diagnostic))

    def close(self):
        if self.connection is not None:
            self.connection.close()
