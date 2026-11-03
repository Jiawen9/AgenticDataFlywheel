"""Indexed, rebuildable views of a frozen plan and immutable execution attempts.

Only background writers initialize/update this database. HTTP reads use mode=ro,
never create a journal, and never enumerate the filesystem or contact a phone.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .collection_runs import _identifier
from .data_store.registry import utc_now
from .task_generation.collection_batches import payload_digest


def slot_key(phone, case, repeat):
    return payload_digest([phone, case, repeat])


class PlanProjection:
    def __init__(self, root, run_id):
        self.path = Path(root) / "system" / "collection_resume" / (_identifier(run_id, "运行编号") + ".sqlite")
        self.run_id = run_id

    @contextmanager
    def connection(self, write=False):
        if write:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(str(self.path) if write else self.path.as_uri() + "?mode=ro",
                             uri=not write, timeout=1 if write else .15)
        db.row_factory = sqlite3.Row
        try:
            if write:
                db.execute("PRAGMA journal_mode=DELETE")
                db.executescript("""
                CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS slots (
                    slot TEXT PRIMARY KEY, case_id TEXT NOT NULL, phone_id TEXT NOT NULL,
                    app TEXT NOT NULL, task TEXT NOT NULL, repeat_index INTEGER NOT NULL,
                    repeat_times INTEGER NOT NULL, current_attempt TEXT, active_attempt TEXT);
                CREATE INDEX IF NOT EXISTS slots_group ON slots(app,phone_id,case_id,repeat_index);
                CREATE TABLE IF NOT EXISTS attempts (
                    attempt TEXT PRIMARY KEY, slot TEXT NOT NULL, execution_id TEXT NOT NULL,
                    ordinal INTEGER NOT NULL, status TEXT NOT NULL, receipt TEXT NOT NULL,
                    artifact_id TEXT, payload TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS attempts_slot ON attempts(slot,ordinal);
                """)
                if "active_attempt" not in {r[1] for r in db.execute("PRAGMA table_info(slots)")}:
                    db.execute("ALTER TABLE slots ADD COLUMN active_attempt TEXT")
            yield db
            if write:
                db.commit()
        finally:
            db.close()

    def initialize(self, run, instances):
        identity = payload_digest([run["batch_id"], run["request_digest"], sorted(instances)])
        with self.connection(True) as db:
            old = db.execute("SELECT value FROM metadata WHERE key='plan'").fetchone()
            if old:
                if old[0] != identity:
                    raise ValueError("续采计划投影与冻结运行身份不一致")
                return
            rows = []
            for phone, case, repeat in sorted(instances):
                task = run["batch_tasks"][case]
                rows.append((slot_key(phone, case, repeat), case, phone, task.get("app", ""),
                             task.get("task", ""), repeat, run["request"]["metadata"].get("repeat_times", 1)))
            db.executemany("INSERT INTO slots(slot,case_id,phone_id,app,task,repeat_index,repeat_times) VALUES(?,?,?,?,?,?,?)", rows)
            db.execute("INSERT INTO metadata VALUES('plan',?)", (identity,))

    def observe(self, execution_id, ordinal, instances, receipts):
        """Upsert a separate attempt for each segment slot; never overwrite old attempts."""
        signature = payload_digest([ordinal,sorted(instances),[
            [r.get("artifact_id"),r.get("storage_revision"),r.get("status"),r.get("entry"),r.get("error"),r.get("eligibility")]
            for r in receipts]])
        key = "observed:"+execution_id
        with self.connection() as db:
            previous=db.execute("SELECT value FROM metadata WHERE key=?",(key,)).fetchone()
            if previous and previous[0]==signature:
                return
        receipts = {(r.get("entry", {}).get("phone_id"), r.get("entry", {}).get("collection_case_id"),
                     r.get("entry", {}).get("repeat_index")): r for r in receipts if r.get("entry")}
        with self.connection(True) as db:
            for phone, case, repeat in instances:
                slot = slot_key(phone, case, repeat)
                if not db.execute("SELECT 1 FROM slots WHERE slot=?", (slot,)).fetchone():
                    raise ValueError("执行分段包含冻结计划以外的实例")
                attempt = execution_id + ":" + slot
                row = receipts.get((phone, case, repeat))
                entry = dict((row or {}).get("entry") or {})
                payload = {**entry, "execution_id": execution_id, "attempt_id": attempt,
                           "artifact_id": (row or {}).get("artifact_id"),
                           "receipt_status": (row or {}).get("status", "pending"),
                           "eligibility": (row or {}).get("eligibility"),
                           "error": (row or {}).get("error") or entry.get("error")}
                status = entry.get("execution_status", "pending")
                encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
                db.execute("""INSERT INTO attempts VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(attempt) DO UPDATE SET
                    status=excluded.status,receipt=excluded.receipt,artifact_id=excluded.artifact_id,payload=excluded.payload
                    WHERE attempts.payload != excluded.payload""", (attempt, slot, execution_id, ordinal,
                    status, payload["receipt_status"], payload["artifact_id"], encoded))
                if row and row.get("status") in {"received","quarantined"}:
                    db.execute("""UPDATE slots SET current_attempt=? WHERE slot=? AND
                        (current_attempt IS NULL OR COALESCE((SELECT ordinal FROM attempts WHERE attempt=current_attempt),-1)<=?)""",
                        (attempt,slot,ordinal))
                    db.execute("""UPDATE slots SET active_attempt=NULL WHERE slot=? AND
                        COALESCE((SELECT ordinal FROM attempts WHERE attempt=active_attempt),-1)<=?""",(slot,ordinal))
                else:
                    # Preserve the adopted sealed result while the replacement
                    # is executing or its bytes have not been verified locally.
                    db.execute("""UPDATE slots SET active_attempt=? WHERE slot=? AND
                        COALESCE((SELECT ordinal FROM attempts WHERE attempt=current_attempt),-1)<? AND
                        COALESCE((SELECT ordinal FROM attempts WHERE attempt=active_attempt),-1)<=?""",
                        (attempt,slot,ordinal,ordinal))
            db.execute("INSERT OR REPLACE INTO metadata VALUES('observed_at',?)", (utc_now(),))
            db.execute("INSERT OR REPLACE INTO metadata VALUES(?,?)",(key,signature))

    def observe_activity(self, phones):
        with self.connection(True) as db:
            for phone in phones:
                case = phone.get("case_id",phone.get("current_case_id"))
                repeat = phone.get("repeat_index")
                execution = phone.get("execution_id")
                if not case or type(repeat) is not int or not execution or phone.get("status") != "running":
                    continue
                slot = slot_key(phone.get("phone_id"),case,repeat)
                row = db.execute("SELECT a.attempt,a.payload FROM attempts a JOIN slots s ON COALESCE(s.active_attempt,s.current_attempt)=a.attempt WHERE s.slot=? AND a.execution_id=? AND a.status='pending'",(slot,execution)).fetchone()
                if not row:
                    continue
                value = json.loads(row["payload"])
                value.update(execution_status="running",phase=phone.get("phase"),last_activity_at=phone.get("last_activity_at"))
                db.execute("UPDATE attempts SET status='running',payload=? WHERE attempt=?",(json.dumps(value,ensure_ascii=False),row["attempt"]))

    @staticmethod
    def _where(app="", phone_id="", status="", q=""):
        clauses, values = [], []
        for column, value in (("s.app", app), ("s.phone_id", phone_id)):
            if value:
                clauses.append(column + "=?"); values.append(str(value))
        scope, scope_values = list(clauses), list(values)
        if q:
            clauses.append("instr(lower(s.case_id||' '||s.task||' '||s.app||' '||s.phone_id),?)>0")
            values.append(str(q).lower()[:500])
        # Status filters select groups; all ten slots remain visible in their counts.
        having = ""
        if status in {"failed", "abnormal"}:
            having = " HAVING SUM(COALESCE(b.status,a.status) IN ('failed','timeout','interrupted'))>0"
        elif status == "completed":
            having = " HAVING SUM(COALESCE(b.status,a.status)='completed')=COUNT(*)"
        elif status in {"pending", "active"}:
            having = " HAVING SUM(COALESCE(b.status,a.status,'pending') IN ('pending','running','queued','not_run','cancelled'))>0"
        elif status == "running":
            having = " HAVING SUM(COALESCE(b.status,a.status) IN ('running','queued','starting'))>0"
        elif status == "not_run":
            having = " HAVING SUM(COALESCE(b.status,a.status,'pending') IN ('pending','not_run','cancelled'))>0"
        def sql(items):
            return " WHERE " + " AND ".join(items) if items else ""
        return sql(clauses), values, sql(scope), scope_values, having

    _JOIN = " FROM slots s LEFT JOIN attempts a ON a.attempt=s.current_attempt LEFT JOIN attempts b ON b.attempt=s.active_attempt"
    _COUNTS = """COUNT(*) planned_count,
        COALESCE(SUM(COALESCE(b.status,a.status)='completed'),0) completed_count,
        COALESCE(SUM(COALESCE(b.status,a.status) IN ('failed','timeout','interrupted')),0) failed_count,
        COALESCE(SUM(COALESCE(b.status,a.status,'pending') IN ('pending','not_run','cancelled')),0) not_run_count,
        COALESCE(SUM(COALESCE(b.status,a.status) IN ('running','queued','starting')),0) active_count,
        COALESCE(SUM(a.receipt='received'),0) received_count"""

    def cases(self, *, offset=0, limit=10, app="", phone_id="", status="", q=""):
        offset, limit = max(0, int(offset)), min(50, max(1, int(limit)))
        empty = {"supported": True, "items": [], "total": 0, "offset": offset, "limit": limit,
                 "scope_counts": {}, "filtered_counts": {}, "revision": 0, "preparing": True}
        if not self.path.exists():
            return empty
        where, values, scope, scope_values, having = self._where(app, phone_id, status, q)
        grouped = "SELECT s.case_id,s.phone_id,s.app,s.task,MAX(s.repeat_times) repeat_times," + self._COUNTS + self._JOIN + where + " GROUP BY s.case_id,s.phone_id" + having
        with self.connection() as db:
            total = db.execute("SELECT COUNT(*) FROM (" + grouped + ")", values).fetchone()[0]
            rows = [dict(r) for r in db.execute(grouped + " ORDER BY s.case_id,s.phone_id LIMIT ? OFFSET ?", (*values, limit, offset))]
            scope_counts = dict(db.execute("SELECT " + self._COUNTS + self._JOIN + scope, scope_values).fetchone())
            filtered = dict(db.execute("SELECT " + ",".join("COALESCE(SUM(" + n + "),0) " + n for n in scope_counts) + " FROM (" + grouped + ")", values).fetchone())
            for row in rows:
                row["collection_case_id"] = row["case_id"]
                row["attempt_count"] = db.execute("SELECT COUNT(*) FROM attempts a JOIN slots s ON a.slot=s.slot WHERE s.case_id=? AND s.phone_id=?", (row["case_id"], row["phone_id"])).fetchone()[0]
                row["can_resume"] = bool(row["failed_count"] or row["not_run_count"])
        return {**empty, "items": rows, "total": total, "scope_counts": scope_counts,
                "filtered_counts": filtered, "preparing": False}

    def instances(self, case, phone_id="", *, offset=0, limit=100):
        offset, limit = max(0, int(offset)), min(100, max(1, int(limit)))
        if not self.path.exists():
            return {"items": [], "total": 0, "preparing": True}
        where, args = " WHERE s.case_id=?", [case]
        if phone_id:
            where += " AND s.phone_id=?"; args.append(phone_id)
        with self.connection() as db:
            total = db.execute("SELECT COUNT(*) FROM slots s" + where, args).fetchone()[0]
            rows = db.execute("SELECT s.*,a.payload,b.payload active_payload" + self._JOIN + where + " ORDER BY s.phone_id,s.repeat_index LIMIT ? OFFSET ?", (*args,limit,offset)).fetchall()
            result = []
            for row in rows:
                attempts = [json.loads(r[0]) for r in db.execute("SELECT payload FROM attempts WHERE slot=? ORDER BY ordinal DESC", (row["slot"],))]
                current = json.loads(row["payload"]) if row["payload"] else {}
                active = json.loads(row["active_payload"]) if row["active_payload"] else None
                hidden={v.get("attempt_id") for v in (current,active or {})}
                result.append({"case_id": case, "phone_id": row["phone_id"], "repeat_index": row["repeat_index"],
                    "repeat_times": row["repeat_times"], **current,
                    "status":(active or current).get("execution_status", "pending"),
                    "current_result":current or None,"active_attempt":active,
                    "attempt_count": len(attempts), "attempts": [v for v in attempts if v.get("attempt_id") not in hidden]})
        return {"items": result, "total": total, "offset": offset, "limit": limit, "preparing": False}
