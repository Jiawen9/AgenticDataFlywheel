"""Small, rebuildable read projections in the authoritative records database.

Projection is deliberately pure: no filesystem, network or other-record reads.
Related current states are joined at query time, so transactionally written state
never requires a fan-out rewrite of historical run payloads.
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading

VERSION = 1
LOG = logging.getLogger(__name__)
NAMESPACES = frozenset({
    "pipelines", "collection_runs", "model_iter_runs", "remote_sync",
    "collection_transfers", "collection_receipt_storage", "batch_lifecycle",
    "collection_resume_heads", "collection_resume_operations", "collector_storage_runs",
    "preprocessing_jobs", "tree_jobs", "quality_jobs", "correction_cot_jobs",
    "display_identifiers", "collection_diagnostics",
})
ACTIVE = {"preparing", "waiting", "dispatching", "queued", "starting", "running", "stopping", "pending", "accepted"}
ABNORMAL = {"failed", "partial", "interrupted", "needs_attention", "published_summary_failed"}
PIPELINE_TERMINAL = {"terminated", "no_publishable_data", "succeeded", "published_summary_failed"}
SCALAR_FIELDS = set("""pipeline_id collection_run_id run_id job_id name batch_id task_source_batch_id
    status collection_status created_at updated_at completed_at submitted_at dispatched_at
    source_kind filename agent_type execution_mode run_mode repeat_times result_transport collection_artifacts_version
    transfer_status transfer_error remote_status transfer_updated_at archive_status
    receipt_policy_version receipt_closed collection_artifact_count transferred_trajectory_count
    transferred_diagnostic_count transferred_outcome_count received_artifact_count quarantined_artifact_count
    evaluation_eligible_count preprocessing_eligible_count stop_requested_at control_terminal control_error
    can_cancel_collection dispatch_attempted dispatch_confirmed dispatch_rejected dispatch_error error
    collection_snapshot_id collection_snapshot_digest source_count evaluation_control_terminal
    mode start_mode threshold session_id release_id published_at paused resume_available
    collector_data_state cleanup_operation_id cleaned_at cleanup_error percent preparation_control_error
    preparation_control_pending offline_evaluation_jobs_version""".split())
COUNTS = set("""original_task_count source_task_count assigned_task_count planned_trajectory_count
    collection_completed_count collection_failed_count collection_cancelled_count collection_not_run_count
    sealed_trajectory_count diagnostic_count outcome_count evaluation_eligible_count preprocessing_eligible_count
    transferred_trajectory_count transferred_diagnostic_count transferred_outcome_count received_artifact_count
    quarantined_artifact_count total completed failed success_count passed_count skipped_count evaluated_count
    pending_count running_count eligible_count excluded_count prepared_count scanned_count total_count
    completed_instances pending_instances planned_instances collection_completed collection_failed
    evaluation_completed evaluation_failed evaluation_running evaluation_pending success partial_success fail success_rate quarantined_count""".split())


def _text(value, limit=800):
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if not isinstance(value, str):
        return None
    # Error snippets must never make token-bearing URLs/headers into list caches.
    value = re.sub(r"(?i)(bearer\s+)[^\s,;]+", r"\1[redacted]", value)
    value = re.sub(r"(?i)((?:api[_-]?key|app_token|access_token|token|password|secret)[\s\"']*[:=][\s\"']*)[^\s,;&\"']+", r"\1[redacted]", value)
    return value[:limit]


def _fields(value, fields):
    return {key: _text(value[key]) for key in fields if key in value and (value[key] is None or isinstance(value[key], (str, bool, int, float)))} if isinstance(value, dict) else {}


def _counts(value):
    return {key: val for key, val in (value or {}).items() if key in COUNTS and type(val) in (int, float)} if isinstance(value, dict) else {}


def _mapping(value):
    return value if isinstance(value, dict) else {}


def _list(value):
    return value if isinstance(value, (list, tuple)) else []


def _integer(value):
    if type(value) is int:
        return max(0, value)
    if isinstance(value, str) and len(value) < 20:
        try:
            return max(0, int(value))
        except ValueError:
            pass
    return 0


def project(namespace, key, payload, revision):
    """Allowlisted, bounded fields only; source request/config/files never escape."""
    if namespace not in NAMESPACES:
        return None
    value = _fields(payload, SCALAR_FIELDS)
    value["storage_revision"] = revision
    if namespace in {"collection_runs", "model_iter_runs"}:
        request = _mapping(payload.get("request"))
        metadata = _mapping(request.get("metadata"))
        source = metadata if namespace == "collection_runs" else request
        for field in ("filename", "agent_type", "execution_mode", "run_mode", "repeat_times", "result_transport", "collection_artifacts_version", "offline_evaluation_jobs_version", "source_count", "evaluation_profile_id", "evaluation_profile_digest", "checklist_source"):
            if field in source:
                value[field] = _text(source[field])
        value.setdefault("agent_type", "newagent")
        value.setdefault("run_mode", "generate" if namespace == "collection_runs" else "modeliter")
        value.setdefault("execution_mode", "collect_and_evaluate")
        value.setdefault("repeat_times", 1)
        value["has_evaluator_config"] = bool(request.get("evaluator_config") or payload.get("evaluator_config"))
        value["preparation_control_pending"] = bool(payload.get("pending_preparation_control"))
        source_request = {**_mapping(request.get("source_request")), **_mapping(request.get("source"))}
        # Keep the exact precedence of evaluation_snapshot_status; these are
        # frozen source identities, not the evaluation run's own identity.
        source_id = source_request.get("source_run_id") or source_request.get("run_id")
        if isinstance(source_id, str):
            value["source_run_id"] = _text(source_id)
            value["source_collection_snapshot_id"] = _text(source_request.get("snapshot_id"))
        value["trajectory_count"] = len(_list(payload.get("trajectories")))
        value["errors_count"] = max(_integer(payload.get("errors_count")), len(_list(payload.get("errors"))))
        value["remote_errors_count"] = max(_integer(payload.get("remote_errors_count")), len(_list(payload.get("remote_errors"))))
        for name in ("collection_summary", "evaluation_summary", "allocation_summary"):
            if isinstance(payload.get(name), dict):
                value[name] = _counts(payload[name])
        if isinstance(payload.get("preparation"), dict):
            value["preparation"] = {**_counts(payload["preparation"]), **_fields(payload["preparation"], {"status", "stage", "phase", "message", "error", "updated_at", "revision"}),
                                    "progress": {key: number for key, number in _mapping(payload["preparation"].get("progress")).items()
                                                 if key in {"prepared", "total", "discovered", "checked", "eligible", "excluded", "copied", "validated", "completed"} and type(number) in (int, float)}}
        from ..collection_control import can_cancel
        value["can_cancel_collection"] = can_cancel({**value, "dispatch_response": bool(payload.get("dispatch_response"))})
    elif namespace == "pipelines":
        value["pipeline_id"] = payload.get("pipeline_id", key)
        value["steps"] = [{**_fields(step, {"id", "label", "status", "percent", "message", "error"}),
                           "job_count": len(_list(step.get("job_ids")))}
                          for step in _list(payload.get("steps"))[:16] if isinstance(step, dict)]
        value["collection_run_ids"] = [_text(v, 160) for v in _list(payload.get("collection_run_ids"))[:100] if isinstance(v, str)]
        value["task_count"] = len(_list(payload.get("task_ids")))
        value["history_only"] = value.get("status") in {"terminated", "no_publishable_data"} and not value.get("release_id")
    elif namespace == "remote_sync":
        value = {**_fields(payload, {"state", "failure_count", "not_found_count", "final_failure_count", "pause_reason", "last_error", "last_success_at", "last_attempt_at", "next_retry_at", "remote_status", "last_error_category"}), "storage_revision": revision}
    elif namespace == "collection_receipt_storage":
        value = {**_fields(payload, {"paused", "pending", "reason", "updated_at", "error", "notification_error", "error_type", "errno", "winerror", "failed_at", "acknowledged_at"}), "storage_revision": revision}
    elif namespace == "collection_resume_heads":
        value.update(_fields(payload, {"revision", "active", "result_reconciliation_pending", "current_snapshot_id", "current_snapshot_digest", "current_error_count"}))
        value["collection_summary"] = _counts(payload.get("collection_summary"))
        value["cancel_intent"] = _fields(payload.get("cancel_intent"), {"epoch", "rejected"})
    elif namespace == "collection_resume_operations":
        value.update(_fields(payload, {"execution_id", "request_id", "revision"}))
    elif namespace == "collection_transfers":
        value["remote_errors_count"] = len(_list(payload.get("remote_errors")))
    elif namespace == "collector_storage_runs":
        value = _fields(payload, {"collector_data_state", "cleanup_operation_id", "cleaned_at", "cleanup_error"})
    elif namespace == "display_identifiers":
        value = {field: _text(payload[field], 2048) for field in ("kind", "id", "scope", "display_id") if isinstance(payload.get(field), str)}
    elif namespace == "collection_diagnostics":
        phones = [phone for phone in _list(payload.get("phones")) if isinstance(phone, dict)]
        observed = {phone.get("execution_id") for phone in phones if phone.get("execution_id") is None or isinstance(phone.get("execution_id"), str)}
        value = {**_fields(payload, {"run_id", "batch_id", "observed_at", "received_at", "stale"}),
                 "phone_count": len(phones),
                 "terminal_phone_count": sum(isinstance(phone.get("status"), str) and phone["status"] in {"completed", "failed", "cancelled", "not_run", "interrupted"} for phone in phones),
                 "completed_phone_count": sum(phone.get("status") == "completed" for phone in phones),
                 "observed_execution_ids": sorted(observed, key=lambda item: item or "")[:100],
                 "observed_execution_ids_complete": len(observed) <= 100}
    value.setdefault("run_id" if namespace == "model_iter_runs" else "collection_run_id", key) if namespace in {"collection_runs", "model_iter_runs"} else None
    return value


def ensure_schema(connection):
    had_projection = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='record_summaries'").fetchone() is not None
    connection.execute("""CREATE TABLE IF NOT EXISTS record_summaries (
        namespace TEXT NOT NULL, record_key TEXT NOT NULL, projection_version INTEGER NOT NULL,
        revision INTEGER NOT NULL, updated_at TEXT NOT NULL, created_at TEXT NOT NULL,
        batch_id TEXT NOT NULL, task_source_batch_id TEXT NOT NULL, run_mode TEXT NOT NULL,
        filename TEXT NOT NULL, status TEXT NOT NULL, payload TEXT NOT NULL,
        PRIMARY KEY(namespace,record_key))""")
    connection.execute("CREATE INDEX IF NOT EXISTS record_summaries_scope ON record_summaries(namespace,batch_id,created_at DESC,record_key)")
    connection.execute("CREATE INDEX IF NOT EXISTS record_summaries_source ON record_summaries(namespace,task_source_batch_id,created_at DESC,record_key)")
    connection.execute("CREATE INDEX IF NOT EXISTS record_summaries_status ON record_summaries(namespace,status,created_at DESC,record_key)")
    connection.execute("CREATE INDEX IF NOT EXISTS record_summaries_resume_run ON record_summaries(namespace,json_extract(payload,'$.run_id'),status) WHERE namespace='collection_resume_operations'")
    connection.execute("CREATE INDEX IF NOT EXISTS record_summaries_alias ON record_summaries(namespace,json_extract(payload,'$.id'),json_extract(payload,'$.kind'),json_extract(payload,'$.scope')) WHERE namespace='display_identifiers'")
    # The root-migration utility also deletes records in a single SQL
    # transaction. Keep that existing path consistent without adding I/O there.
    connection.execute("""CREATE TRIGGER IF NOT EXISTS record_summary_delete AFTER DELETE ON records
        BEGIN DELETE FROM record_summaries WHERE namespace=OLD.namespace AND record_key=OLD.record_key; END""")
    connection.execute("""CREATE TABLE IF NOT EXISTS summary_index_state (
        singleton INTEGER PRIMARY KEY CHECK(singleton=1), version INTEGER NOT NULL,
        complete INTEGER NOT NULL, cursor_namespace TEXT NOT NULL, cursor_key TEXT NOT NULL)""")
    old = connection.execute("SELECT version FROM summary_index_state WHERE singleton=1").fetchone()
    if old is None or old[0] != VERSION or not had_projection:
        placeholders = ",".join("?" for _ in NAMESPACES)
        existing = connection.execute(f"SELECT 1 FROM records WHERE namespace IN ({placeholders}) LIMIT 1", sorted(NAMESPACES)).fetchone()
        if old is None:
            connection.execute("INSERT OR IGNORE INTO summary_index_state VALUES(1,?,?,?,?)", (VERSION, int(existing is None), "", ""))
        else:
            connection.execute("UPDATE summary_index_state SET version=?,complete=0,cursor_namespace='',cursor_key='' WHERE singleton=1 AND version=?", (VERSION, old[0]))


def save_projection(connection, namespace, key, payload, revision, updated_at):
    value = project(namespace, key, payload, revision)
    if value is None:
        return
    connection.execute("""INSERT INTO record_summaries VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(namespace,record_key) DO UPDATE SET projection_version=excluded.projection_version,
        revision=excluded.revision,updated_at=excluded.updated_at,created_at=excluded.created_at,
        batch_id=excluded.batch_id,task_source_batch_id=excluded.task_source_batch_id,
        run_mode=excluded.run_mode,filename=excluded.filename,status=excluded.status,payload=excluded.payload""",
        (namespace, key, VERSION, revision, updated_at, str(value.get("created_at") or updated_at),
         str(value.get("batch_id") or (value.get("run_id") if namespace.endswith("_jobs") else "") or ""),
         str(value.get("task_source_batch_id", value.get("batch_id") if namespace in {"collection_runs", "model_iter_runs"} else "") or ""),
         str(value.get("run_mode") or ""), str(value.get("filename") or ""), str(value.get("status") or ""),
         json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))))


def delete_projection(connection, namespace, key):
    connection.execute("DELETE FROM record_summaries WHERE namespace=? AND record_key=?", (namespace, key))


def ready(connection):
    if connection.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name IN ('summary_index_state','record_summaries')").fetchone()[0] != 2:
        return False
    row = connection.execute("SELECT complete FROM summary_index_state WHERE singleton=1 AND version=?", (VERSION,)).fetchone()
    return bool(row and row[0])


def backfill_batch(store, batch_size=50):
    """Read bounded payloads outside writer transaction, then CAS by source revision.

    Cursor advancement and successful projections commit together. An update or
    delete between read and commit wins; restarted passes never resurrect data.
    """
    with store._connection(write=True) as connection:
        state = connection.execute("SELECT complete,cursor_namespace,cursor_key FROM summary_index_state WHERE singleton=1").fetchone()
    if state[0]:
        return True
    with store._connection() as connection:
        placeholders = ",".join("?" for _ in NAMESPACES)
        rows = connection.execute(f"""SELECT namespace,record_key,revision,payload,updated_at FROM records
            WHERE namespace IN ({placeholders}) AND (namespace,record_key)>(?,?)
            ORDER BY namespace,record_key LIMIT ?""", (*sorted(NAMESPACES), state[1], state[2], max(1, min(batch_size, 200)))).fetchall()
    decoded = [(ns, key, rev, json.loads(payload), stamp) for ns, key, rev, payload, stamp in rows]
    with store._connection(write=True) as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            current = connection.execute("SELECT complete,cursor_namespace,cursor_key FROM summary_index_state WHERE singleton=1 AND version=?", (VERSION,)).fetchone()
            if current != state:
                connection.rollback()
                return bool(current and current[0])
            for namespace, key, revision, payload, stamp in decoded:
                original = connection.execute("SELECT revision,updated_at FROM records WHERE namespace=? AND record_key=?", (namespace, key)).fetchone()
                if original and original == (revision, stamp):
                    save_projection(connection, namespace, key, payload, revision, stamp)
            if rows:
                connection.execute("UPDATE summary_index_state SET cursor_namespace=?,cursor_key=? WHERE singleton=1", rows[-1][:2])
            else:
                # Metadata only cleanup, including rows removed by pre-index code.
                connection.execute("DELETE FROM record_summaries WHERE projection_version<>? OR NOT EXISTS (SELECT 1 FROM records r WHERE r.namespace=record_summaries.namespace AND r.record_key=record_summaries.record_key)", (VERSION,))
                connection.execute("UPDATE summary_index_state SET complete=1 WHERE singleton=1")
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
    return not rows


class BackfillWorker:
    def __init__(self, store, batch_size=50):
        self.store, self.batch_size = store, batch_size
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.run, name="summary-index-backfill", daemon=True)

    def run(self):
        while not self.stop.is_set():
            try:
                if backfill_batch(self.store, self.batch_size):
                    return
                self.stop.wait(.025)
            except (OSError, sqlite3.Error, ValueError):
                LOG.exception("Summary index backfill deferred; original records remain authoritative")
                self.stop.wait(5)

    def close(self):
        self.stop.set()
        if self.thread is not threading.current_thread():
            self.thread.join(timeout=5)


def _quote(values):
    return ",".join("'" + value + "'" for value in sorted(values))


def query_sql(namespace):
    """Joined projection rows; no original record payload is touched by GET."""
    base_status = "COALESCE(json_extract(c.payload,'$.collection_status'),json_extract(m.payload,'$.collection_status'),m.status)" if namespace == "model_iter_runs" else "COALESCE(json_extract(m.payload,'$.collection_status'),m.status)"
    pending = f"""EXISTS(SELECT 1 FROM record_summaries o WHERE o.namespace='collection_resume_operations'
                    AND o.projection_version={VERSION} AND json_extract(o.payload,'$.run_id')=m.record_key
                    AND o.status IN ('pending','uncertain','accepted','preparing'))"""
    expected = f"""EXISTS(SELECT 1 FROM record_summaries o WHERE o.namespace='collection_resume_operations'
                    AND o.projection_version={VERSION} AND json_extract(o.payload,'$.run_id')=m.record_key AND o.status='started')"""
    missing_observation = f"""EXISTS(SELECT 1 FROM record_summaries o WHERE o.namespace='collection_resume_operations'
                    AND o.projection_version={VERSION} AND json_extract(o.payload,'$.run_id')=m.record_key AND o.status='started'
                    AND NOT EXISTS(SELECT 1 FROM json_each(COALESCE(json_extract(g.payload,'$.observed_execution_ids'),'[]')) observed
                                   WHERE observed.value IS json_extract(o.payload,'$.execution_id')))"""
    resume_status = f"""CASE WHEN h.record_key IS NOT NULL THEN
        CASE WHEN COALESCE(json_extract(h.payload,'$.active'),0)=1 THEN
            CASE WHEN json_extract(h.payload,'$.result_reconciliation_pending')=1 AND NOT {pending} AND NOT {expected} THEN {base_status}
                 WHEN {pending} OR {missing_observation} THEN 'starting'
                 WHEN json_extract(g.payload,'$.phone_count')>0 AND json_extract(g.payload,'$.terminal_phone_count')=json_extract(g.payload,'$.phone_count')
                      THEN CASE WHEN json_extract(g.payload,'$.completed_phone_count')=json_extract(g.payload,'$.phone_count') THEN 'completed' ELSE 'partial' END
                 WHEN json_extract(h.payload,'$.cancel_intent.epoch')=json_extract(h.payload,'$.revision') THEN 'stopping'
                 ELSE 'running' END
        WHEN json_extract(h.payload,'$.collection_summary.planned_trajectory_count')>=0
         AND json_extract(h.payload,'$.collection_summary.collection_completed_count')=json_extract(h.payload,'$.collection_summary.planned_trajectory_count') THEN 'completed'
        ELSE 'partial' END ELSE {base_status} END"""
    effective = resume_status if namespace == "collection_runs" else "m.status"
    active = f"effective_status IN ({_quote(ACTIVE)})"
    if namespace == "pipelines":
        active = f"effective_status NOT IN ({_quote(PIPELINE_TERMINAL)})"
    abnormal = f"effective_status IN ({_quote(ABNORMAL)})"
    if namespace in {"collection_runs", "model_iter_runs"}:
        active += " OR effective_transfer IN ('waiting','syncing','pending','downloading','retry_wait','running','verifying') OR effective_status='needs_attention'"
        active += f" OR effective_collection_status IN ({_quote(ACTIVE | {'needs_attention'})})"
        active += f" OR (head_payload IS NULL AND json_extract(payload,'$.status') IN ({_quote(ACTIVE | {'needs_attention'})}))"
        active += " OR COALESCE(json_extract(collection_payload,'$.archive_status'),json_extract(payload,'$.archive_status'))='building' OR COALESCE(json_extract(payload,'$.evaluation_summary.evaluation_running'),0)>0 OR COALESCE(json_extract(payload,'$.evaluation_summary.evaluation_pending'),0)>0"
        active = f"CASE WHEN json_extract(storage_payload,'$.collector_data_state') IN ('retiring','retired','cleanup_failed') THEN json_extract(storage_payload,'$.collector_data_state')='retiring' ELSE ({active}) END"
        abnormal += " OR COALESCE(json_extract(receipt_payload,'$.paused'),0)=1 OR COALESCE(json_extract(receipt_payload,'$.error'),'')<>'' OR COALESCE(json_extract(payload,'$.control_error'),'')<>''"
        abnormal += f" OR effective_collection_status IN ({_quote(ABNORMAL)})"
        abnormal += " OR effective_transfer IN ('failed','remote_failed','partial','quarantined','needs_attention')"
        abnormal += " OR (head_payload IS NULL AND COALESCE(json_extract(transfer_payload,'$.remote_status'),json_extract(payload,'$.remote_status'))='partial')"
        for count in ("collection_failed_count", "quarantined_count", "quarantined_artifact_count"):
            abnormal += f" OR CASE WHEN COALESCE(json_extract(head_payload,'$.collection_summary'),'{{}}')<>'{{}}' THEN COALESCE(json_extract(head_payload,'$.collection_summary.{count}'),0)>0 ELSE COALESCE(json_extract(collection_payload,'$.collection_summary.{count}'),json_extract(payload,'$.collection_summary.{count}'),0)>0 END"
        abnormal += " OR COALESCE(json_extract(payload,'$.evaluation_summary.evaluation_failed'),0)>0 OR COALESCE(json_extract(payload,'$.evaluation_summary.collection_failed'),0)>0"
        abnormal += " OR CASE WHEN head_payload IS NOT NULL THEN COALESCE(json_extract(head_payload,'$.active'),0)=0 AND COALESCE(json_extract(head_payload,'$.current_error_count'),0)>0 ELSE COALESCE(NULLIF(json_extract(payload,'$.error'),''),NULLIF(json_extract(payload,'$.dispatch_error'),''),NULLIF(json_extract(transfer_payload,'$.transfer_error'),''),json_extract(payload,'$.transfer_error'),'')<>'' OR COALESCE(json_extract(payload,'$.errors_count'),0)>0 OR COALESCE(json_extract(transfer_payload,'$.remote_errors_count'),0)>0 OR COALESCE(json_extract(sync_payload,'$.last_error'),'')<>'' OR COALESCE(json_extract(sync_payload,'$.failure_count'),0)>0 OR COALESCE(json_extract(sync_payload,'$.state'),'') IN ('backoff','paused') OR COALESCE(json_extract(collection_sync_payload,'$.last_error'),'')<>'' END"
    return f"""WITH joined AS (
        SELECT m.*, c.payload collection_payload, h.payload head_payload, t.payload transfer_payload,
          b.payload lifecycle_payload, s.payload sync_payload, cs.payload collection_sync_payload,
          r.payload receipt_payload, d.payload storage_payload,
          {effective} effective_status,
          {resume_status if namespace == 'model_iter_runs' else 'NULL'} effective_collection_status,
          CASE WHEN h.record_key IS NOT NULL THEN CASE WHEN json_extract(h.payload,'$.active')=1 THEN 'downloading' ELSE 'completed' END
            ELSE COALESCE(json_extract(t.payload,'$.transfer_status'),json_extract(c.payload,'$.transfer_status'),json_extract(m.payload,'$.transfer_status'),'') END effective_transfer
        FROM record_summaries m
        LEFT JOIN record_summaries c ON c.namespace='collection_runs' AND c.record_key=m.record_key AND c.projection_version={VERSION} AND m.namespace='model_iter_runs'
        LEFT JOIN record_summaries h ON h.namespace='collection_resume_heads' AND h.record_key=m.record_key AND h.projection_version={VERSION}
        LEFT JOIN record_summaries t ON t.namespace='collection_transfers' AND t.record_key=m.record_key AND t.projection_version={VERSION}
        LEFT JOIN record_summaries b ON b.namespace='batch_lifecycle' AND b.record_key=m.batch_id AND b.projection_version={VERSION}
        LEFT JOIN record_summaries s ON s.namespace='remote_sync' AND s.record_key=(CASE WHEN m.namespace='model_iter_runs' THEN 'evaluation:' ELSE 'collection:' END)||m.record_key AND s.projection_version={VERSION}
        LEFT JOIN record_summaries cs ON cs.namespace='remote_sync' AND cs.record_key='collection:'||m.record_key AND cs.projection_version={VERSION}
        LEFT JOIN record_summaries r ON r.namespace='collection_receipt_storage' AND r.record_key=m.record_key AND r.projection_version={VERSION}
        LEFT JOIN record_summaries d ON d.namespace='collector_storage_runs' AND d.record_key=m.record_key AND d.projection_version={VERSION}
        LEFT JOIN record_summaries g ON g.namespace='collection_diagnostics' AND g.record_key=m.record_key AND g.projection_version={VERSION}
        WHERE m.namespace=? AND m.projection_version={VERSION}
    ), effective AS (SELECT *, ({active}) is_active, ({abnormal}) is_abnormal FROM joined) """


def materialize(row, namespace):
    result = json.loads(row["payload"])
    if namespace in {"collection_runs", "model_iter_runs"}:
        if row["collection_payload"]:
            collection = json.loads(row["collection_payload"])
            result.update({k: v for k, v in collection.items() if k in {
                "collection_status", "collection_summary", "transfer_status", "transfer_error", "archive_status", "batch_id",
                "task_source_batch_id", "repeat_times", "can_cancel_collection", "stop_requested_at", "control_terminal"}})
        if row["transfer_payload"]:
            result.update({k: v for k, v in json.loads(row["transfer_payload"]).items() if k in {
                "remote_status", "transfer_status", "transfer_error", "transfer_updated_at", "remote_errors_count"}})
        if row["head_payload"]:
            head = json.loads(row["head_payload"])
            result.update(resume_revision=head.get("revision"), resume_active=bool(head.get("active")),
                          result_reconciliation_pending=bool(head.get("result_reconciliation_pending")),
                          collection_snapshot_id=head.get("current_snapshot_id"), collection_snapshot_digest=head.get("current_snapshot_digest"),
                          transfer_status=row["effective_transfer"], error=None, dispatch_error=None, transfer_error=None,
                          errors_count=0 if head.get("active") else head.get("current_error_count", 0), remote_errors_count=0,
                          can_cancel_collection=bool(head.get("active")))
            if head.get("collection_summary"):
                result["collection_summary"] = head["collection_summary"]
            if namespace == "collection_runs":
                result["status"] = row["effective_status"]
        result["collection_status"] = row["effective_status"] if namespace == "collection_runs" else row["effective_collection_status"]
        if result.get("execution_mode") == "evaluate_only":
            result.pop("collection_status", None)
        sync = {"state": "active", "failure_count": 0, **json.loads(row["sync_payload"] or "{}")}
        sync.pop("storage_revision", None)
        result["sync"] = sync
        result["collection_sync"] = json.loads(row["collection_sync_payload"] or "{}")
        result["receipt_storage"] = json.loads(row["receipt_payload"]) if row["receipt_payload"] else None
        storage = json.loads(row["storage_payload"] or "{}")
        storage.pop("storage_revision", None)
        result.update(storage)
    lifecycle = json.loads(row["lifecycle_payload"] or "{}")
    result.update(batch_status=lifecycle.get("status", "active"), release_id=lifecycle.get("release_id", result.get("release_id")), published_at=lifecycle.get("published_at", result.get("published_at")))
    if namespace == "pipelines":
        result["managed"] = result.get("status") not in PIPELINE_TERMINAL and result["batch_status"] != "published"
    result["summary_active"], result["summary_abnormal"] = bool(row["is_active"]), bool(row["is_abnormal"])
    return result


def page(connection, namespace, *, offset=0, limit=10, q="", status="", batch_id=None,
         task_source_batch_id=None, active_only=False, include_published=True, run_mode=None, filename=None,
         key=None, all_rows=False):
    if namespace not in NAMESPACES:
        raise ValueError("Unsupported summary namespace")
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 200:
        raise ValueError("Summary offset must be nonnegative and limit must be 1..200")
    exists = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='record_summaries'").fetchone()
    complete = ready(connection)
    empty = {"items": [], "total": 0 if complete else None, "filtered_total": 0 if complete else None,
             "scope_counts": {"total": 0 if complete else None, "active": 0 if complete else None, "abnormal": 0 if complete else None},
             "latest": [], "offset": offset, "limit": limit, "has_more": False,
             "index_state": "ready" if complete else "building", "counts_complete": complete, "updated_at": None}
    if not exists:
        return empty
    connection.row_factory = sqlite3.Row
    scope, args = ["1=1"], [namespace]
    for column, value in (("batch_id", batch_id), ("task_source_batch_id", task_source_batch_id), ("run_mode", run_mode), ("filename", filename), ("record_key", key)):
        if value is not None:
            scope.append(f"{column}=?")
            args.append(value)
    if namespace == "collection_runs" and batch_id is None and key is None:
        scope.append("COALESCE(json_extract(payload,'$.source_kind'),'')<>'rollout_import'")
    if not include_published:
        scope.append("COALESCE(json_extract(lifecycle_payload,'$.status'),'active')<>'published'")
    if active_only:
        scope.append("is_active=1")
    where = " AND ".join(scope)
    sql = query_sql(namespace)
    counts = connection.execute(sql + f"SELECT COUNT(*) total,COALESCE(SUM(is_active),0) active,COALESCE(SUM(is_abnormal),0) abnormal,MAX(updated_at) updated_at FROM effective WHERE {where}", args).fetchone()
    filtered, filtered_args = list(scope), list(args)
    if q:
        pattern = "%" + q.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        filtered.append(f"""(lower(record_key||' '||batch_id||' '||task_source_batch_id||' '||filename||' '||COALESCE(json_extract(payload,'$.name'),'')) LIKE ? ESCAPE '\\'
            OR EXISTS (SELECT 1 FROM record_summaries a WHERE a.namespace='display_identifiers' AND a.projection_version={VERSION}
                AND json_extract(a.payload,'$.scope')=''
                AND json_extract(a.payload,'$.id') IN (effective.record_key,effective.batch_id,effective.task_source_batch_id)
                AND ((json_extract(a.payload,'$.kind')=CASE WHEN effective.namespace='pipelines' THEN 'pipeline' ELSE 'run' END AND json_extract(a.payload,'$.id')=effective.record_key)
                  OR (json_extract(a.payload,'$.kind')='batch' AND json_extract(a.payload,'$.id') IN (effective.batch_id,effective.task_source_batch_id)))
                AND lower(json_extract(a.payload,'$.display_id')) LIKE ? ESCAPE '\\'))""")
        filtered_args.extend([pattern, pattern])
    if status:
        if status == "active":
            filtered.append("is_active=1")
        elif status in {"abnormal", "exception"}:
            filtered.append("is_abnormal=1")
        else:
            filtered.append("effective_status=?")
            filtered_args.append(status)
    filtered_where = " AND ".join(filtered)
    total = connection.execute(sql + f"SELECT COUNT(*) FROM effective WHERE {filtered_where}", filtered_args).fetchone()[0]
    rows = connection.execute(sql + f"SELECT * FROM effective WHERE {filtered_where} ORDER BY created_at DESC,record_key DESC" + ("" if all_rows else " LIMIT ? OFFSET ?"), filtered_args if all_rows else [*filtered_args, limit, offset]).fetchall()
    latest = connection.execute(sql + f"SELECT * FROM effective WHERE {where} ORDER BY created_at DESC,record_key DESC LIMIT 3", args).fetchall()
    return {**empty, "items": [materialize(row, namespace) for row in rows], "latest": [materialize(row, namespace) for row in latest],
            "total": counts["total"] if complete else None, "filtered_total": total if complete else None,
            "scope_counts": {field: counts[field] if complete else None for field in ("total", "active", "abnormal")},
            "has_more": offset + len(rows) < total, "updated_at": counts["updated_at"]}
