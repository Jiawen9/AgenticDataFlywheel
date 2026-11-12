from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from starlette.requests import Request

from backend.data_store.registry import SummaryUnavailable
from backend.phone_factory import PhoneFactoryStore, create_router


class FactoryPageReadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.calls = []
        self.report_value = {"ok": True, "folders": []}
        self.store = PhoneFactoryStore(Path(self.tmp.name), run_client_fn=self.client)
        self.runtime = self.store.runtime
        self.addCleanup(self.runtime.close)

    def client(self, args):
        self.calls.append(args)
        return {"ok": True, "output": json.dumps(self.report_value)}

    def evaluation(self, count=500):
        self.store.records.put("model_iter_runs", "eval_one", {
            "run_id": "eval_one", "status": "running", "created_at": "2026-11-12T00:00:00Z",
            "request": {"evaluator_config": {"text": {}}, "execution_mode": "evaluate_only", "source_count": count,
                        "source_cases": [{"large": "X" * 1000}] * count}})
        rows = [{"case_key": str(i), "case_id": "case-" + str(i), "phone_id": "phone-" + str(i % 2),
                 "collection_status": "completed", "evaluation_status": "running" if i < 20 else "completed",
                 "verdict": "fail" if i % 7 == 0 and i >= 20 else None if i < 20 else "success",
                 "errors": []} for i in range(count)]
        saved = self.store.records.put("model_iter_results", "eval_one", {
            "final": False, "result": {"run_id": "eval_one", "summary": {"total": count, "evaluation_running": 20},
                                       "cases": rows, "excluded": [{"error": "missing image"}], "evidence_warnings": ["missing XML"]}})
        self.runtime.read_models.index_evaluation("eval_one", saved)
        return saved

    def wait_reports(self):
        deadline = time.monotonic() + 3
        while self.runtime.read_models._pool.inflight and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertFalse(self.runtime.read_models._pool.inflight)

    def test_case_page_reads_only_index_and_counts_ignore_filter(self):
        self.evaluation()
        with patch.object(self.runtime._evaluation_pool, "submit", return_value=False), \
             patch.object(self.store.records, "get", side_effect=AssertionError("full payload read")), \
             patch.object(self.store.records, "list", side_effect=AssertionError("history scan")):
            summary = self.runtime.evaluation_view("eval_one", {"view": "summary"})
            first = self.runtime.evaluation_view("eval_one", {"view": "cases", "limit": 10})
            filtered = self.runtime.evaluation_view("eval_one", {"view": "cases", "q": "phone-1", "status": "active", "limit": 10})
        self.assertEqual(summary["summary"]["total"], 500)
        self.assertNotIn("cases", summary)
        self.assertNotIn("excluded", summary)
        self.assertEqual(summary["excluded_count"], 1)
        self.assertEqual(len(first["items"]), 10)
        self.assertEqual(first["total"], 500)
        self.assertEqual(filtered["filtered_total"], 10)
        self.assertEqual(first["scope_counts"], filtered["scope_counts"])
        self.assertEqual(first["scope_counts"]["active"], 20)

    def test_deleted_or_changed_snapshot_never_resurrects_old_index(self):
        old = self.evaluation(30)
        changed = self.store.records.put("model_iter_results", "eval_one", {
            "final": True, "result": {"summary": {"total": 1}, "cases": [old["result"]["cases"][0]]}})
        self.assertTrue(self.runtime.read_models.page("evaluations", "eval_one")["stale"])
        self.runtime.read_models.index_evaluation("eval_one", old)
        self.assertEqual(self.runtime.read_models.page("evaluations", "eval_one")["total"], 30)
        self.runtime.read_models.index_evaluation("eval_one", changed)
        self.assertEqual(self.runtime.read_models.page("evaluations", "eval_one")["total"], 1)
        self.store.records.delete("model_iter_results", "eval_one")
        self.runtime.read_models.index_evaluation("eval_one", old)
        result = self.runtime.read_models.page("evaluations", "eval_one")
        self.assertIsNone(result["total"])
        self.assertEqual(result["items"], [])

    def test_historical_summary_pending_never_loads_full_frozen_request(self):
        self.store.records.put("model_iter_runs", "eval_one", {"run_id": "eval_one", "status": "running",
            "request": {"evaluator_config": {"text": {}}, "source_cases": [{"large": "X" * 2000}] * 500}})
        with patch.object(self.store.records, "get_summary", return_value=None), \
             patch.object(self.store.records, "get", side_effect=AssertionError("full frozen request")), \
             patch.object(self.runtime._evaluation_pool, "submit", return_value=False), \
             patch.object(self.runtime.read_models._pool, "submit", return_value=False):
            result = self.runtime.evaluation_view("eval_one", {"view": "summary"})
        self.assertIsNone(result["summary"])
        self.assertFalse(result["counts_complete"])
        self.assertEqual(result["status"], "unknown")

    def test_blocked_report_refresh_does_not_block_get_or_duplicate_call(self):
        entered, release = threading.Event(), threading.Event()
        def blocked(args):
            entered.set()
            release.wait(4)
            return {"ok": True, "folders": []}
        with patch.object(self.store, "_remote_result", side_effect=blocked) as remote:
            started = time.monotonic()
            handled, result = self.runtime.dispatch("reports", "GET", {"view": "summary"}, "modeliter")
            elapsed = time.monotonic() - started
            try:
                self.assertTrue(entered.wait(1))
                self.assertTrue(handled)
                self.assertLess(elapsed, .5)
                self.assertIsNone(result["total"])
                self.assertFalse(result["counts_complete"])
                self.assertEqual(result["index_state"], "preparing")
                self.runtime.dispatch("reports", "GET", {"view": "summary"}, "modeliter")
                self.assertEqual(remote.call_count, 1)
            finally:
                release.set()
                self.wait_reports()

    def test_report_pagination_legacy_directories_and_download_name(self):
        self.report_value = {"ok": True, "folders": [{"index": index, "dir_name": "round-" + str(index),
            "files": [{"name": "报告-" + str(index) + ".xlsx", "size": index, "file_id": str(index)}]}
            for index in range(30)]}
        self.report_value["folders"][0]["run_id"] = "eval_one"
        self.runtime.read_models.schedule_reports("modeliter")
        self.wait_reports()
        page = self.runtime.read_models.page("reports", "modeliter", offset=20, limit=10)
        self.assertEqual(page["total"], 30)
        self.assertEqual(len(page["items"]), 10)
        self.assertFalse(page["has_more"])
        self.assertNotIn("folders", page)
        self.assertNotIn("files", page["items"][0]["folder"])
        old = self.runtime.read_models.legacy_reports("modeliter")
        self.assertEqual(len(old["folders"]), 30)
        self.assertEqual(len(old["folders"][1]["files"]), 1)
        self.assertEqual(self.runtime.read_models.report_name("modeliter", "eval_one", "0"), "报告-0.xlsx")

    def test_report_failure_keeps_old_rows(self):
        self.report_value = {"ok": True, "folders": [{"index": 0, "dir_name": "old", "files": [{"name": "old.xlsx", "size": 3}]}]}
        self.runtime.read_models.schedule_reports("generate")
        self.wait_reports()
        self.runtime.read_models._report_failure("generate", "timeout")
        result = self.runtime.read_models.page("reports", "generate")
        self.assertEqual(result["total"], 1)
        self.assertTrue(result["stale"])
        self.assertEqual(result["refresh_error"], "timeout")

    def test_legacy_evaluation_get_never_waits_on_remote(self):
        self.evaluation(30)
        entered, release = threading.Event(), threading.Event()
        def blocked(_):
            entered.set(); release.wait(4)
        with patch.object(self.runtime, "_background_evaluation", side_effect=blocked):
            started = time.monotonic()
            result = self.runtime.evaluation_view("eval_one", {})
            try:
                self.assertLess(time.monotonic() - started, .5)
                self.assertTrue(entered.wait(1))
                self.assertEqual(len(result["cases"]), 30)
                self.assertEqual(result["excluded"], [{"error": "missing image"}])
            finally:
                release.set()

    def test_state_uses_light_rows_and_preserves_active_phone_priority(self):
        records = self.store.records
        for index, status in enumerate(("running", "completed")):
            records.put("collection_runs", "cr_" + str(index), {"collection_run_id": "cr_" + str(index),
                "batch_id": "b1", "status": status, "created_at": str(index),
                "request": {"metadata": {"filename": "tasks.xlsx", "run_mode": "generate"}},
                "trajectories": [{"large": "X" * 1000}] * 50})
        with patch.object(self.runtime, "run_views", side_effect=AssertionError("full run views")), \
             patch.object(records, "list", side_effect=AssertionError("full history")):
            result = self.runtime.public_state({"tasks": [{"filename": "tasks.xlsx", "status": "未运行"}]}, "generate")
        self.assertEqual(result["tasks"][0]["status"], "运行中")

    def test_busy_read_model_is_retryable_503(self):
        router = create_router(self.store)
        request = Request({"type": "http", "method": "GET", "query_string": b"view=summary", "headers": []})
        with patch.object(self.store, "dispatch", side_effect=SummaryUnavailable("index busy")):
            response = router.routes[0].endpoint("collection-runs", request, None)
        self.assertEqual(response.status_code, 503)
        self.assertTrue(json.loads(response.body)["retryable"])

    def test_ui_detail_is_compact_but_legacy_collection_detail_stays_full(self):
        self.store.records.put("collection_runs", "cr_one", {
            "collection_run_id": "cr_one", "run_id": "cr_one", "batch_id": "batch_one", "status": "completed",
            "output_dir": str(self.store.root / "raw" / "collection_batches" / "batch_one" / "runs" / "cr_one"),
            "request": {"metadata": {"agent_type": "pipelineagent", "execution_model": "actual-model"},
                        "task_manifest": "X" * 20000, "source_cases": [{"large": "X" * 1000}] * 20},
            "manifest": {"files": ["file"] * 100}, "batch_tasks": {"case": {"large": "X" * 1000}},
            "trajectories": [{"large": "X" * 1000}] * 20, "errors": []})
        _, original = self.runtime.dispatch("collection-runs/cr_one", "GET", {}, "generate")
        _, compact = self.runtime.dispatch("collection-runs/cr_one", "GET", {"view": "detail"}, "generate")
        for field in ("manifest", "batch_tasks", "trajectories"):
            self.assertIn(field, original)
            self.assertNotIn(field, compact)
        self.assertNotIn("source_cases", compact["request"])
        self.assertNotIn("task_manifest", compact["request"])
        self.assertEqual(compact["execution_model"], "actual-model")
        self.assertEqual(compact["trajectory_count"], 20)
        self.assertLess(len(json.dumps(compact)), 10000)


if __name__ == "__main__":
    unittest.main()
