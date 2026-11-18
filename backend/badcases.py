"""Local, reviewable evaluation feedback. No device or model calls."""
from __future__ import annotations

import hashlib
import json
import logging
import threading
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, wait
from pathlib import Path

from .data_store import RecordStore
from .data_store.registry import utc_now, RevisionConflict
from .data_store.locking import batch_lock

LOG = logging.getLogger(__name__)
_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="badcase-index")
_pending = set()
_futures = {}
_stops = {}
_guard = threading.Lock()


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


class BadCaseError(ValueError):
    def __init__(self, message, status=409):
        super().__init__(message)
        self.status = status


class BadCaseService:
    def __init__(self, root):
        self.root = Path(root)
        self.records = RecordStore(self.root)

    def _sources(self, run):
        """Read frozen task records only; never infer identity from suffix/text."""
        from .phone_factory import PhoneFactoryError
        request = run.get("request") or {}
        tasks = {}
        batch_id = run.get("batch_id") or request.get("batch_id")
        if batch_id:
            try:
                from .collection_runs import CollectionRunStore
                batch = CollectionRunStore(self.root).batch(batch_id)
                for row in batch.get("snapshot", {}).get("tasks", []):
                    identity = row.get("collection_case_id") or row.get("case_id") or row.get("task_id")
                    if identity:
                        tasks[str(identity)] = {**row, "source_scope": batch.get("task_source_batch_id", batch_id)}
            except (ValueError, OSError, KeyError):
                LOG.info("BadCase source snapshot unavailable: %s", batch_id)
        sources = {row["source_trajectory_id"]: row for row in request.get("source_cases", []) if row.get("source_trajectory_id")}
        if request.get("source_preview_id"):
            try:
                from .evaluation_job_previews import SourceCases
                sources = SourceCases(self.records, request["source_preview_id"], request["source_digest"],
                                      revision=request.get("source_preview_revision"), total=request.get("source_count"))
            except (ValueError, KeyError, PhoneFactoryError):
                LOG.info("BadCase preview snapshot unavailable: %s", run.get("run_id"))
        return tasks, sources

    def refresh_run(self, run_id):
        with batch_lock(self.root, "badcase_" + digest(run_id)[:24]):
            return self._refresh_run(run_id)

    def _refresh_run(self, run_id):
        snapshot = self.records.get("model_iter_results", run_id)
        run = self.records.get("model_iter_runs", run_id)
        if not snapshot or not run:
            raise BadCaseError("尚无已同步的评测结果，可稍后刷新", 404)
        result = snapshot.get("result") or {}
        fingerprint = digest(result)
        old_head = self.records.get("badcase_heads", run_id)
        if old_head and old_head.get("result_digest") == fingerprint and old_head.get("status") == "ready":
            return old_head
        tasks, sources = self._sources(run)
        request = run.get("request") or {}
        grouped = {}
        for row in result.get("cases", []):
            business = row.get("evaluation_status") == "completed" and row.get("verdict") in {"fail", "partial_success"}
            technical = row.get("evaluation_status") in {"needs_review", "collection_failed"}
            if not business and not technical:
                continue
            source_id = row.get("source_trajectory_id")
            try:
                source = sources.get(source_id, {}) if source_id else {}
            except (KeyError, ValueError):
                source = {}
            case_id = source.get("original_case_id") or row.get("original_case_id") or row.get("case_id") or ""
            task = tasks.get(case_id) or source
            # A source task id is scoped by its immutable input. Unmatched
            # legacy entries remain separate rather than guessing from text.
            task_identity = task.get("task_id") or task.get("collection_case_id") or task.get("original_case_id") or task.get("case_id")
            scope = task.get("source_scope") or request.get("source_digest") or run_id
            identity = [scope, task_identity] if task_identity else [run_id, source_id or row["case_key"]]
            key = "bc_" + digest([run_id, identity, technical])[:32]
            current = grouped.setdefault(key, {
                "candidate_id": key, "source_run_id": run_id, "case_id": case_id,
                "source_identity": identity, "source_task_id": task_identity,
                "task": task.get("task") or row.get("task") or "",
                "app": task.get("app") or row.get("app") or "",
                "scene": task.get("scene"), "capability": task.get("capability"), "sub_capability": task.get("sub_capability"),
                "category": "technical" if technical else "business", "verdicts": [], "sources": [], "failures": [],
                "evaluation_profile_id": request.get("evaluation_profile_id"),
                "evaluation_profile_digest": request.get("evaluation_profile_digest"),
                "evaluation_model": request.get("evaluator_config"), "result_digest": fingerprint,
            })
            if row.get("verdict") and row["verdict"] not in current["verdicts"]:
                current["verdicts"].append(row["verdict"])
            current["sources"].append({key: row.get(key) for key in (
                "case_key", "case_id", "source_trajectory_id", "phone_id", "report_file_id", "report_name", "evaluation_status", "verdict")})
            if row.get("failure_reason"):
                current["failures"].append({"description": str(row["failure_reason"]), "case_key": row["case_key"]})
            if technical:
                for error in row.get("errors", []):
                    message = error.get("error") or error.get("message") if isinstance(error, dict) else str(error)
                    if message:
                        current["failures"].append({"description": message, "case_key": row["case_key"]})
            for item in (row.get("checklist") or {}).get("items", []):
                if item.get("status") in {"failed", "fail", "unchecked"}:
                    current["failures"].append({"id": item.get("id"), "description": item.get("description", ""),
                                                "status": item.get("status"), "evidence": item.get("evidence"), "case_key": row["case_key"]})
        entries, counts = [], {"candidate": 0, "technical": 0, "needs_completion": 0}
        old_rows = {row["candidate_id"]: row for row in self._all(run_id)}
        for key, row in grouped.items():
            old = old_rows.get(key, {})
            if old.get("corrections"):
                row["corrections"] = old["corrections"]
                row.update(old["corrections"])
            if old.get("report_failures"):
                row["report_failures"] = old["report_failures"]
                row["failures"].extend(old["report_failures"])
            self._classify(row)
            row.update(source_count=len(row["sources"]), updated_at=utc_now())
            counts[row["status"]] += 1
            entries.append({"namespace": "badcase_candidates", "key": key, "payload": row})
        head = {"run_id": run_id, "result_digest": fingerprint, "summary": counts,
                "final": bool(snapshot.get("final")), "updated_at": utc_now(), "status": "ready"}
        entries.append({"namespace": "badcase_heads", "key": run_id, "payload": head})
        self.records.put_many(entries, deletes=[{"namespace": "badcase_candidates", "key": key} for key in old_rows.keys() - grouped.keys()])
        return head

    @staticmethod
    def _classify(row):
        row["missing_fields"] = [key for key in ("task", "app") if not str(row.get(key) or "").strip()]
        row["status"] = "technical" if row["category"] == "technical" else "needs_completion" if row["missing_fields"] else "candidate"
        row["evidence_status"] = "available" if row.get("failures") else "report_reference" if any(s.get("report_file_id") for s in row.get("sources", [])) else "unavailable"

    def _all(self, run_id):
        if not self.records.database_path.exists():
            return []
        with self.records._connection() as connection:
            if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='records'").fetchone():
                return []
            return [json.loads(row[0]) for row in connection.execute("SELECT payload FROM records WHERE namespace='badcase_candidates' AND json_extract(payload,'$.source_run_id')=?", (run_id,))]

    def page(self, run_id, *, offset=0, limit=10, status="", search=""):
        if offset < 0 or limit not in {10, 20, 50} or status not in {"", "candidate", "technical", "needs_completion"}:
            raise BadCaseError("无效的分页或状态参数", 422)
        head = self.records.get("badcase_heads", run_id) or {}
        if not head:
            return {"run_id": run_id, "items": [], "total": 0, "summary": {}, "index_state": "pending", "offset": offset, "limit": limit}
        where, args = ["namespace='badcase_candidates'", "json_extract(payload,'$.source_run_id')=?"], [run_id]
        if status:
            where.append("json_extract(payload,'$.status')=?"); args.append(status)
        if search:
            where.append("(instr(lower(json_extract(payload,'$.task')),lower(?))>0 OR instr(lower(json_extract(payload,'$.case_id')),lower(?))>0)")
            args.extend([search, search])
        with self.records._connection() as connection:
            connection.execute("PRAGMA busy_timeout=250")
            connection.execute("BEGIN")
            clause = " AND ".join(where)
            total = connection.execute("SELECT count(*) FROM records WHERE " + clause, args).fetchone()[0]
            rows = connection.execute("SELECT payload FROM records WHERE " + clause + " ORDER BY record_key LIMIT ? OFFSET ?", [*args, limit, offset]).fetchall()
            counts = dict(connection.execute("SELECT json_extract(payload,'$.status'),count(*) FROM records WHERE namespace='badcase_candidates' AND json_extract(payload,'$.source_run_id')=? GROUP BY json_extract(payload,'$.status')", (run_id,)))
        return {**head, "summary": counts, "items": [json.loads(row[0]) for row in rows], "total": total, "offset": offset, "limit": limit, "index_state": head.get("status", "ready")}

    def enrich_reports(self, run_id, loader, *, stop=None):
        """Bounded background report reads; cache both success and retry backoff."""
        for candidate in self._all(run_id):
            if stop and stop.is_set():
                return
            gathered = []
            for source in candidate.get("sources", []):
                if stop and stop.is_set():
                    return
                file_id = source.get("report_file_id")
                if not file_id:
                    continue
                key = digest([run_id, file_id])
                cached = self.records.get("badcase_reports", key)
                if not cached or cached.get("status") != "ready" and cached.get("retry_at", 0) <= time.time():
                    try:
                        report = loader(run_id, file_id)
                        failed = []
                        analysis = report.get("failure_analysis") or {}
                        evidence = {str(item.get("checklist_id") or item.get("id")): item.get("evidence") for item in analysis.get("failed_items", []) if isinstance(item, dict)}
                        for item in (report.get("checklist_summary") or {}).get("items", []):
                            if isinstance(item, dict) and item.get("status") in {"failed", "fail", "unchecked"}:
                                failed.append({"id": item.get("id"), "description": item.get("description", ""), "status": item.get("status"),
                                               "evidence": item.get("evidence") or evidence.get(str(item.get("id"))), "case_key": source.get("case_key")})
                        cached = {"status": "ready", "failures": failed, "sha256": digest(report), "run_id": run_id, "file_id": file_id}
                    except Exception as exc:
                        cached = {"status": "unavailable", "failures": [], "retry_at": time.time() + 60,
                                  "error": type(exc).__name__, "run_id": run_id, "file_id": file_id}
                    if stop and stop.is_set():
                        return
                    self.records.put("badcase_reports", key, cached)
                gathered.extend(cached.get("failures", []))
            if gathered:
                with batch_lock(self.root, "badcase_" + digest(run_id)[:24]):
                    current = self.records.get("badcase_candidates", candidate["candidate_id"])
                    if current and current.get("result_digest") == candidate.get("result_digest"):
                        prior = current.get("report_failures", [])
                        current["failures"] = [item for item in current.get("failures", []) if item not in prior] + gathered
                        current["report_failures"] = gathered
                        self._classify(current)
                        self.records.put("badcase_candidates", current["candidate_id"], current, expected_revision=current["storage_revision"])

    def patch(self, candidate_id, values):
        if not values or set(values) - {"task", "app"} or any(not isinstance(v, str) or not v.strip() or len(v) > 4000 for v in values.values()):
            raise BadCaseError("只允许补充非空任务文本与 App", 422)
        def update(row):
            row["corrections"] = {**row.get("corrections", {}), **{key: value.strip() for key, value in values.items()}}
            row.update(row["corrections"]); self._classify(row); row["updated_at"] = utc_now()
        try:
            old = self.records.get("badcase_candidates", candidate_id)
            if not old:
                raise KeyError(candidate_id)
            with batch_lock(self.root, "badcase_" + digest(old["source_run_id"])[:24]):
                saved = self.records.update("badcase_candidates", candidate_id, update)
                with self.records._connection() as connection:
                    counts = dict(connection.execute("SELECT json_extract(payload,'$.status'),count(*) FROM records WHERE namespace='badcase_candidates' AND json_extract(payload,'$.source_run_id')=? GROUP BY json_extract(payload,'$.status')", (old["source_run_id"],)))
                self.records.update("badcase_heads", old["source_run_id"], lambda row: row.update(summary=counts, updated_at=utc_now()), default={"run_id": old["source_run_id"]})
                return saved
        except KeyError as exc:
            raise BadCaseError("BadCase 不存在", 404) from exc

    def freeze(self, run_id, candidate_ids, request_id):
        with batch_lock(self.root, "badcase_" + digest(run_id)[:24]):
            return self._freeze(run_id, candidate_ids, request_id)

    def _freeze(self, run_id, candidate_ids, request_id):
        if not isinstance(request_id, str) or not request_id.strip() or len(request_id) > 128:
            raise BadCaseError("需要有效回流请求编号", 422)
        ids = sorted(set(candidate_ids))
        if not ids:
            raise BadCaseError("请选择至少一个 BadCase", 422)
        key = "fb_" + digest(request_id)[:32]
        fingerprint = digest([run_id, ids])
        old = self.records.get("badcase_feedback", key)
        if old:
            if old["request_digest"] != fingerprint:
                raise BadCaseError("同一回流请求编号对应了不同选择")
            return old
        seeds = []
        for identity in ids:
            row = self.records.get("badcase_candidates", identity)
            if not row or row["source_run_id"] != run_id:
                raise BadCaseError("选择的 BadCase 不属于本次评测")
            if row["status"] != "candidate":
                raise BadCaseError("技术异常需重试评测；缺少任务或 App 的候选请先补充")
            seeds.append({key: value for key, value in row.items() if key != "storage_revision"})
        payload = {"feedback_id": key, "run_id": run_id, "request_id": request_id, "request_digest": fingerprint,
                   "candidate_ids": ids, "seeds": seeds, "count": len(seeds), "digest": digest(seeds), "created_at": utc_now()}
        try:
            return self.records.put("badcase_feedback", key, payload, expected_revision=0)
        except RevisionConflict:
            return self.freeze(run_id, ids, request_id)

    def get_feedback(self, feedback_id):
        result = self.records.get("badcase_feedback", feedback_id)
        if not result:
            raise BadCaseError("回流选择快照不存在", 404)
        return result


def pipeline_loop_summary(records, pipeline):
    identity = pipeline["pipeline_id"]
    binding = records.get("pipeline_evaluations", identity)
    evaluation = None
    if binding:
        from .data_store.registry import SummaryUnavailable
        try:
            run = records.get_summary("model_iter_runs", binding["run_id"]) or {}
        except SummaryUnavailable:
            run = {"summary_unavailable": True}
        head = records.get("badcase_heads", binding["run_id"]) or {}
        evaluation = {"run_id": binding["run_id"], "status": run.get("status"),
                      "source": binding.get("source"), "execution_mode": binding.get("execution_mode"),
                      "summary_unavailable": run.get("summary_unavailable", False),
                      "summary": run.get("evaluation_summary"), "badcases": head.get("summary", {}),
                      "index_state": head.get("status", "pending")}
    children, next_rounds = [], []
    if records.database_path.exists():
        with records._connection() as connection:
            rows = connection.execute("""SELECT p.record_key,json_extract(p.payload,'$.generation_job_id'),
                json_extract(p.payload,'$.status'),json_extract(j.payload,'$.result_count'),json_extract(j.payload,'$.status')
                FROM records p LEFT JOIN records j ON j.namespace='task_generation.jobs'
                AND j.record_key=json_extract(p.payload,'$.generation_job_id')
                WHERE p.namespace='pipelines' AND json_extract(p.payload,'$.parent_pipeline_id')=? ORDER BY p.updated_at""", (identity,))
            for pid, jid, status, count, job_status in rows:
                children.append(pid)
                next_rounds.append({"pipeline_id": pid, "generation_job_id": jid, "status": status,
                    "generated_count": count if isinstance(count, int) and (count > 0 or job_status in {"succeeded", "partial"}) else None})
    return {"evaluation_run_id": binding["run_id"] if binding else None, "evaluation": evaluation, "next_pipeline_ids": children, "next_rounds": next_rounds}


def schedule_refresh(root, run_id, *, store=None):
    identity = (str(Path(root).resolve()), run_id)
    with _guard:
        stop = _stops.setdefault(identity[0], threading.Event())
        if stop.is_set():
            return False
        if identity in _pending:
            return True
        if len(_pending) >= 16:
            return False
        _pending.add(identity)
    def work():
        try:
            if stop.is_set():
                return
            service = BadCaseService(root)
            service.refresh_run(run_id)
            if store is not None and not stop.is_set():
                run = service.records.get("model_iter_runs", run_id) or {}
                server = run.get("request", {}).get("collector_server")
                def load_report(rid, fid):
                    from .evaluation_job_previews import remote
                    directory = Path(root) / "tmp"
                    directory.mkdir(parents=True, exist_ok=True)
                    with tempfile.TemporaryDirectory(prefix="bc-", dir=directory) as temporary:
                        target = Path(temporary) / "r.json"
                        args = ["report-download", "--runmode", "modeliter", "--run-id", rid, "--file-id", fid, "--out", str(target)]
                        remote(store, args, server) if server else store._remote_result(args)
                        if target.stat().st_size > 8 * 1024 * 1024:
                            raise ValueError("评测报告超过可提取大小")
                        result = json.loads(target.read_text(encoding="utf-8-sig"))
                        if not isinstance(result, dict):
                            raise ValueError("评测报告必须为 JSON 对象")
                        return result
                service.enrich_reports(run_id, load_report, stop=stop)
        except Exception:
            LOG.exception("BadCase candidate refresh failed: %s", run_id)
            try:
                if stop.is_set():
                    return
                RecordStore(root).update("badcase_heads", run_id, lambda row: row.update(
                    run_id=run_id, status="failed", error="BadCase 整理失败，可重试；原评测结果保留", updated_at=utc_now()), default={})
            except Exception:
                LOG.exception("Cannot persist BadCase refresh failure: %s", run_id)
    try:
        future = _pool.submit(work)
        with _guard:
            _futures[identity] = future
        def finished(value):
            with _guard:
                if _futures.get(identity) is value:
                    _futures.pop(identity, None)
                _pending.discard(identity)
        future.add_done_callback(finished)
    except Exception:
        with _guard:
            _pending.discard(identity)
        raise
    return True


def close_refresh(root, timeout=5):
    """Drain only this store; other runtime instances retain their scheduler."""
    key = str(Path(root).resolve())
    with _guard:
        _stops.setdefault(key, threading.Event()).set()
        futures = [value for identity, value in _futures.items() if identity[0] == key]
    for future in futures:
        future.cancel()
    done, pending = wait(futures, timeout=timeout) if futures else (set(), set())
    with _guard:
        for identity in list(_futures):
            if identity[0] == key and _futures[identity] in done:
                _futures.pop(identity, None)
                _pending.discard(identity)
    return not pending


def open_refresh(root):
    with _guard:
        key = str(Path(root).resolve())
        if key not in _stops or _stops[key].is_set():
            _stops[key] = threading.Event()
