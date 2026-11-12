"""Small, rebuildable read models for factory cases and report metadata.

Only background jobs read the full evaluation snapshot or contact the collector.
Foreground pages read a summary and SQL LIMIT/OFFSET rows; original records remain
authoritative. These tables contain no trajectories or report file contents.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import time
from contextlib import contextmanager

from .data_store.registry import SummaryUnavailable, utc_now
from .remote_sync import BoundedSyncPool


def _error_text(error):
    value = re.sub(r"(?i)(bearer\s+)[^\s,;]+", r"\1[redacted]", str(error))
    value = re.sub(r"(?i)((?:api[_-]?key|app_token|access_token|token|password|secret)[\s\"']*[:=][\s\"']*)[^\s,;&\"']+", r"\1[redacted]", value)
    return value[:500]


def page_options(data):
    try:
        offset, limit = int(data.get("offset", 0)), int(data.get("limit", 10))
    except (ValueError, TypeError) as exc:
        from .phone_factory import PhoneFactoryError
        raise PhoneFactoryError("分页参数必须为整数") from exc
    if offset < 0 or limit not in {10, 20, 50}:
        from .phone_factory import PhoneFactoryError
        raise PhoneFactoryError("offset 必须非负，limit 必须为 10、20 或 50")
    return {"offset": offset, "limit": limit,
            "q": str(data.get("q", data.get("search", "")) or "").strip(),
            "status": str(data.get("status", "") or "").strip()}


class FactoryReadModels:
    VERSION = 1
    REPORT_REFRESH_SECONDS = 30

    def __init__(self, runtime):
        self.runtime, self.records = runtime, runtime.records
        self._pool = BoundedSyncPool("factory-page-index", max_workers=2)
        self._due = {}
        self._errors = {}
        self._guard = threading.Lock()

    def close(self):
        self._pool.close()

    @contextmanager
    def _read(self, *, require_schema=True):
        """No table creation, network, source reads or 30-second lock waits."""
        if not self.records.database_path.exists():
            yield None
            return
        connection = sqlite3.connect(self.records.database_path.as_uri() + "?mode=ro",
                                     uri=True, timeout=.1, isolation_level=None)
        try:
            connection.execute("PRAGMA busy_timeout=100")
            connection.execute("BEGIN")
            found = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='factory_page_heads'").fetchone()
            yield connection if found or not require_schema else None
        except sqlite3.OperationalError as exc:
            if any(word in str(exc).lower() for word in ("locked", "busy")):
                raise SummaryUnavailable("页面索引暂忙，请稍后重试；已有记录仍然保留") from exc
            raise
        finally:
            connection.close()

    @staticmethod
    def _schema(connection):
        connection.execute("""CREATE TABLE IF NOT EXISTS factory_page_heads (
            kind TEXT NOT NULL, scope TEXT NOT NULL, version INTEGER NOT NULL,
            source_revision INTEGER NOT NULL, payload TEXT NOT NULL,
            refreshed_epoch REAL NOT NULL, PRIMARY KEY(kind,scope))""")
        connection.execute("""CREATE TABLE IF NOT EXISTS factory_page_rows (
            kind TEXT NOT NULL, scope TEXT NOT NULL, ordinal INTEGER NOT NULL,
            row_key TEXT NOT NULL, status TEXT NOT NULL, abnormal INTEGER NOT NULL,
            active INTEGER NOT NULL, search_text TEXT NOT NULL, payload TEXT NOT NULL,
            PRIMARY KEY(kind,scope,row_key))""")
        connection.execute("CREATE INDEX IF NOT EXISTS factory_page_order ON factory_page_rows(kind,scope,ordinal)")
        connection.execute("CREATE INDEX IF NOT EXISTS factory_page_status ON factory_page_rows(kind,scope,status,ordinal)")

    def _head(self, connection, kind, scope):
        if connection is None:
            return None
        row = connection.execute("SELECT payload,source_revision,refreshed_epoch FROM factory_page_heads WHERE kind=? AND scope=? AND version=?",
                                 (kind, scope, self.VERSION)).fetchone()
        if not row:
            return None
        value = dict(json.loads(row[0]), source_revision=row[1], refreshed_epoch=row[2])
        if kind == "evaluations":
            source = connection.execute("SELECT revision FROM records WHERE namespace='model_iter_results' AND record_key=?", (scope,)).fetchone()
            if not source:
                return None  # Deleted originals cannot be resurrected by a derived index.
            value["stale"] = source[0] != row[1]
        return value

    def head(self, kind, scope):
        with self._read() as connection:
            return self._head(connection, kind, scope)

    def record_exists(self, namespace, key):
        with self._read(require_schema=False) as connection:
            if connection is None:
                return False
            return connection.execute("SELECT 1 FROM records WHERE namespace=? AND record_key=?", (namespace, key)).fetchone() is not None

    def _replace(self, kind, scope, revision, summary, rows):
        # Serialization and row derivation occur before acquiring the write lock.
        stamp = utc_now()
        serialized = json.dumps(dict(summary, updated_at=stamp), ensure_ascii=False, separators=(",", ":"))
        values = [(kind, scope, index, key, status, int(abnormal), int(active), search.casefold(),
                   json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
                  for index, (key, status, abnormal, active, search, payload) in enumerate(rows)]
        with self.records._connection(write=True) as connection:
            self._schema(connection)
            connection.execute("BEGIN IMMEDIATE")
            try:
                previous = self._head(connection, kind, scope)
                if previous and previous["source_revision"] > revision:
                    connection.rollback()
                    return False
                if kind == "evaluations":
                    current = connection.execute("SELECT revision FROM records WHERE namespace='model_iter_results' AND record_key=?", (scope,)).fetchone()
                    # An obsolete worker must not publish over a newer result or deletion.
                    if not current or current[0] != revision:
                        connection.rollback()
                        return False
                connection.execute("DELETE FROM factory_page_rows WHERE kind=? AND scope=?", (kind, scope))
                connection.executemany("INSERT INTO factory_page_rows VALUES (?,?,?,?,?,?,?,?,?)", values)
                connection.execute("""INSERT INTO factory_page_heads VALUES (?,?,?,?,?,?)
                    ON CONFLICT(kind,scope) DO UPDATE SET version=excluded.version,
                    source_revision=excluded.source_revision,payload=excluded.payload,refreshed_epoch=excluded.refreshed_epoch""",
                    (kind, scope, self.VERSION, revision, serialized, time.time()))
                connection.commit()
                return True
            except BaseException:
                connection.rollback()
                raise

    def index_evaluation(self, run_id, snapshot=None):
        snapshot = snapshot or self.records.get("model_iter_results", run_id)
        if not snapshot or not isinstance(snapshot.get("result"), dict):
            return
        previous = self.head("evaluations", run_id)
        if previous and previous["source_revision"] >= snapshot.get("storage_revision", 0):
            return
        result = snapshot["result"]
        # Excluded/evidence warnings can also be large; summary carries counts only.
        summary = {key: value for key, value in result.items() if key not in {"cases", "reports", "excluded", "evidence_warnings"}}
        summary.update(final=bool(snapshot.get("final")), excluded_count=len(result.get("excluded", [])),
                       evidence_warning_count=len(result.get("evidence_warnings", [])))
        rows = []
        for index, case in enumerate(result.get("cases", [])):
            status = case.get("evaluation_status", "pending")
            rows.append((case.get("case_key") or str(index), status,
                         case.get("collection_status") == "failed" or status in {"failed", "needs_review", "collection_failed"}
                         or case.get("verdict") in {"fail", "partial_success"} or bool(case.get("failure_reason") or case.get("errors")),
                         case.get("collection_status") in {"pending", "running"} or status in {"pending", "running", "retry_wait"},
                         " ".join(str(case.get(key, "")) for key in ("case_id", "phone_id", "task", "task_name", "app", "failure_reason", "verdict")), case))
        self._replace("evaluations", run_id, snapshot["storage_revision"], summary, rows)

    def schedule_evaluation_index(self, run_id):
        # Historical snapshots are indexed once in the background. Revision lookup
        # deliberately selects no payload, even for a very large case snapshot.
        with self._read() as connection:
            if connection is not None:
                row = connection.execute("SELECT revision FROM records WHERE namespace='model_iter_results' AND record_key=?", (run_id,)).fetchone()
                head = self._head(connection, "evaluations", run_id)
                if head and (not row or head["source_revision"] == row[0]):
                    return
        def rebuild():
            try:
                self.index_evaluation(run_id)
            except Exception:
                # A failed derived index never alters the authoritative run.
                import logging
                logging.getLogger(__name__).exception("评测分页索引更新失败：%s", run_id)
        self._pool.submit("evaluation:" + run_id, rebuild)

    def _report_failure(self, mode, error):
        # Preserve the last valid snapshot on collector/network failure.
        with self.records._connection(write=True) as connection:
            self._schema(connection)
            connection.execute("BEGIN IMMEDIATE")
            try:
                head = self._head(connection, "reports", mode)
                if head:
                    head.pop("source_revision", None)
                    head.pop("refreshed_epoch", None)
                    head.update(refresh_error=_error_text(error), last_attempt_at=utc_now())
                    connection.execute("UPDATE factory_page_heads SET payload=? WHERE kind='reports' AND scope=?",
                                       (json.dumps(head, ensure_ascii=False), mode))
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    def schedule_reports(self, mode):
        key = "reports:" + mode
        now = time.monotonic()
        with self._guard:
            if self._due.get(key, 0) > now:
                return
            head = self.head("reports", mode)
            if head and time.time() - head["refreshed_epoch"] < self.REPORT_REFRESH_SECONDS:
                return
            self._due[key] = now + self.REPORT_REFRESH_SECONDS
        def refresh():
            try:
                value = self.runtime.store._remote_result(["reports", "--runmode", mode])
                if not isinstance(value, dict) or value.get("ok") is False or not isinstance(value.get("folders"), list):
                    raise ValueError("采集端报告索引格式无效")
                folders, rows = [], []
                ordered_folders = sorted(value["folders"], key=lambda row: (
                    row.get("modified") if isinstance(row.get("modified"), (int, float)) else 0,
                    row.get("index") if isinstance(row.get("index"), int) else 0), reverse=True)
                for folder_index, original in enumerate(ordered_folders):
                    folder = {key: item for key, item in original.items() if key != "files"}
                    folder.setdefault("index", folder_index)
                    if folder.get("run_id"):
                        folder.update(self.runtime._evaluation_snapshot_label(self.records.get("model_iter_runs", folder["run_id"])))
                    folders.append(folder)
                    for file in original.get("files", []):
                        identity = [folder.get("run_id") or folder.get("dir_name"), file.get("file_id", file.get("id")) or file.get("name")]
                        key = hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()[:32]
                        item = {"key": key, "folder": folder, "file": file}
                        rows.append((key, "available", False, False,
                                     " ".join(str(value) for value in (*folder.values(), *file.values())), item))
                self._replace("reports", mode, time.time_ns(), {"ok": True, "folders": folders, "refresh_error": None}, rows)
            except Exception as exc:
                try:
                    self._report_failure(mode, exc)
                except Exception:
                    pass
                with self._guard:
                    self._errors[mode] = _error_text(exc)
        if not self._pool.submit(key, refresh):
            with self._guard:
                self._due.pop(key, None)

    def page(self, kind, scope, *, offset=0, limit=10, q="", status=""):
        with self._read() as connection:
            head = self._head(connection, kind, scope)
            ready = head is not None
            stale = bool(head and (head.get("stale") or head.get("refresh_error") or kind == "reports" and time.time() - head["refreshed_epoch"] >= self.REPORT_REFRESH_SECONDS))
            if not ready:
                return {"items": [], "total": None, "filtered_total": None,
                        "scope_counts": {"total": None, "active": None, "abnormal": None}, "latest": [],
                        "offset": offset, "limit": limit, "has_more": False,
                        "index_state": "preparing", "counts_complete": False, "updated_at": None,
                        "stale": True, "refresh_error": getattr(self, "_errors", {}).get(scope)}
            clauses, parameters = ["kind=?", "scope=?"], [kind, scope]
            total, active, abnormal = connection.execute("SELECT COUNT(*),COALESCE(SUM(active),0),COALESCE(SUM(abnormal),0) FROM factory_page_rows WHERE kind=? AND scope=?", (kind, scope)).fetchone()
            if q:
                clauses.append("instr(search_text,?)>0")
                parameters.append(q.casefold())
            if status in {"active", "abnormal"}:
                clauses.append(status + "=1")
            elif status and status != "all":
                clauses.append("status=?")
                parameters.append(status)
            where = " AND ".join(clauses)
            filtered = connection.execute("SELECT COUNT(*) FROM factory_page_rows WHERE " + where, parameters).fetchone()[0]
            items = [json.loads(row[0]) for row in connection.execute("SELECT payload FROM factory_page_rows WHERE " + where + " ORDER BY ordinal LIMIT ? OFFSET ?", [*parameters, limit, offset])]
            return {"items": items, "total": total, "filtered_total": filtered,
                    "scope_counts": {"total": total, "active": active, "abnormal": abnormal}, "latest": [],
                    "offset": offset, "limit": limit, "has_more": offset + len(items) < filtered,
                    "index_state": "stale" if stale else "ready", "counts_complete": True,
                    "updated_at": head.get("updated_at"), "stale": stale, "refresh_error": head.get("refresh_error")}

    def report_name(self, mode, run_id, file_id):
        with self._read() as connection:
            if connection is None:
                return None
            # JSON identity predicates are restricted to the report metadata rows.
            row = connection.execute("""SELECT json_extract(payload,'$.file.name') FROM factory_page_rows
                WHERE kind='reports' AND scope=? AND json_extract(payload,'$.folder.run_id')=?
                AND COALESCE(json_extract(payload,'$.file.file_id'),json_extract(payload,'$.file.id'))=? LIMIT 1""",
                (mode, run_id, file_id)).fetchone()
            return row[0] if row else None

    def legacy_reports(self, mode):
        with self._read() as connection:
            head = self._head(connection, "reports", mode)
            if not head:
                return {"ok": True, "folders": [], "index_state": "preparing", "counts_complete": False, "stale": True}
            folders = [dict(folder, files=[]) for folder in head.get("folders", [])]
            by_identity = {(folder.get("index"), folder.get("dir_name"), folder.get("run_id")): folder for folder in folders}
            for row in connection.execute("SELECT payload FROM factory_page_rows WHERE kind='reports' AND scope=? ORDER BY ordinal", (mode,)):
                item = json.loads(row[0]); folder = item["folder"]
                by_identity[(folder.get("index"), folder.get("dir_name"), folder.get("run_id"))]["files"].append(item["file"])
            return {"ok": True, "folders": folders, "updated_at": head.get("updated_at"), "counts_complete": True,
                    "stale": bool(head.get("refresh_error") or time.time() - head["refreshed_epoch"] >= self.REPORT_REFRESH_SECONDS),
                    "refresh_error": head.get("refresh_error")}
