"""Projection consistency, historical rebuild and bounded list-query contracts."""
from contextlib import contextmanager, closing
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.data_store import RecordStore
from backend.data_store import summary_index
from backend.data_store.registry import SummaryUnavailable
from backend.pipeline_router import router
from backend.pipelines import PipelineManager, PipelineRuntime


class SummaryIndexTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = RecordStore(self.root)

    def run_row(self, key, **changes):
        return {"collection_run_id": key, "batch_id": "b-one", "task_source_batch_id": "source-one",
                "created_at": "2026-11-12T00:00:00Z", "status": "running", "collection_status": "running",
                "request": {"metadata": {"filename": "中文任务.xlsx", "run_mode": "generate", "repeat_times": 10}}, **changes}

    def legacy(self, entries):
        self.store.database_path.parent.mkdir(parents=True)
        with closing(sqlite3.connect(str(self.store.database_path))) as connection:
            connection.execute("CREATE TABLE records(namespace TEXT,record_key TEXT,revision INTEGER,payload TEXT,updated_at TEXT,PRIMARY KEY(namespace,record_key))")
            for namespace, key, payload in entries:
                connection.execute("INSERT INTO records VALUES(?,?,?,?,?)", (namespace, key, 1, json.dumps({**payload, "storage_revision": 1}), "old"))
            connection.commit()

    def test_transaction_updates_deletes_and_projection_failure_roll_back(self):
        self.store.put("collection_runs", "cr-a", self.run_row("cr-a"))
        self.assertEqual(self.store.get_summary("collection_runs", "cr-a")["storage_revision"], 1)
        self.store.update("collection_runs", "cr-a", lambda value: value.update(collection_status="failed"))
        self.assertEqual(self.store.summary_page("collection_runs")["scope_counts"]["abnormal"], 1)
        with patch.object(summary_index, "save_projection", side_effect=RuntimeError("injected")):
            with self.assertRaisesRegex(RuntimeError, "injected"):
                self.store.update("collection_runs", "cr-a", lambda value: value.update(collection_status="completed"))
        self.assertEqual(self.store.get("collection_runs", "cr-a")["collection_status"], "failed")
        self.assertEqual(self.store.get_summary("collection_runs", "cr-a")["storage_revision"], 2)
        self.store.put_many([], deletes=[{"namespace": "collection_runs", "key": "cr-a"}])
        self.assertIsNone(self.store.get_summary("collection_runs", "cr-a"))
        self.store.put("collection_runs", "cr-b", self.run_row("cr-b"))
        self.store.delete("collection_runs", "cr-b")
        self.assertEqual(self.store.summary_page("collection_runs")["total"], 0)

    def test_direct_migration_delete_and_missing_projection_rebuild(self):
        self.store.put_many([{"namespace": "collection_runs", "key": key, "payload": self.run_row(key)} for key in ("a", "b")])
        with self.store._connection(write=True) as connection:
            connection.execute("DELETE FROM records WHERE namespace='collection_runs' AND record_key='a'")
        self.assertIsNone(self.store.get_summary("collection_runs", "a"))
        with self.store._connection(write=True) as connection:
            connection.execute("DROP TABLE record_summaries")
        self.assertEqual(self.store.summary_page("collection_runs")["index_state"], "building")
        while not summary_index.backfill_batch(self.store):
            pass
        self.assertEqual(self.store.summary_page("collection_runs")["total"], 1)
        self.assertIsNotNone(self.store.get_summary("collection_runs", "b"))

    def test_allowlist_keeps_configuration_credentials_and_evidence_out(self):
        row = self.run_row("cr-a", error="Bearer topsecret token=othersecret",
            phone_states={"device": {"raw": "large"}}, trajectories=[{"password": "hide"}] * 100)
        row["request"]["metadata"].update(planner_config={"api_key": "hidden"}, config={"token": "hidden"})
        self.store.put("collection_runs", "cr-a", row)
        value = self.store.get_summary("collection_runs", "cr-a")
        text = json.dumps(value)
        for hidden in ("topsecret", "othersecret", "planner_config", "phone_states", '"trajectories"', '"request"'):
            self.assertNotIn(hidden, text)
        self.assertEqual(value["trajectory_count"], 100)
        self.assertEqual(value["repeat_times"], 10)

    def test_legacy_optional_bad_shapes_never_block_authoritative_save(self):
        for index, request in enumerate(("unknown", {"metadata": 2}, {"source": "directory", "source_request": ["old"]})):
            key = f"bad-{index}"
            payload = self.run_row(key, request=request, errors_count="unknown", errors=10,
                                   remote_errors=2, remote_errors_count="not-set", trajectories=4)
            self.store.put("collection_runs", key, payload)
            self.assertEqual(self.store.get("collection_runs", key)["request"], request)
            self.assertEqual(self.store.get_summary("collection_runs", key)["trajectory_count"], 0)
        self.store.put("pipelines", "legacy-pipeline", {"steps": {"old": "shape"}, "task_ids": 12,
            "collection_run_ids": "bad", "status": {"old": "invalid"}})
        self.assertEqual(self.store.get_summary("pipelines", "legacy-pipeline")["steps"], [])

    def test_offline_source_identity_preparation_and_legacy_source_scope(self):
        self.store.put("model_iter_runs", "eval-one", {"run_id": "eval-one", "status": "preparing", "batch_id": "old-batch",
            "request": {"execution_mode": "evaluate_only", "source_request": {"source_run_id": "cr-original", "snapshot_id": "snap-original"},
                        "source": {"run_id": "cr-fallback", "snapshot_id": "snap-frozen"}},
            "preparation": {"status": "preparing", "phase": "copying", "progress": {"prepared": 3, "total": 80, "files": ["not-a-summary"]}}})
        page = self.store.summary_page("model_iter_runs", task_source_batch_id="old-batch")
        self.assertEqual(page["filtered_total"], 1)
        row = page["items"][0]
        self.assertEqual(row["source_run_id"], "cr-original")
        self.assertEqual(row["source_collection_snapshot_id"], "snap-frozen")
        self.assertEqual(row["preparation"], {"status": "preparing", "phase": "copying", "progress": {"prepared": 3, "total": 80}})

    def test_missing_index_get_is_read_only_and_unknown_until_backfilled(self):
        self.legacy([("collection_runs", "cr-a", self.run_row("cr-a"))])
        before = self.store.database_path.read_bytes()
        value = self.store.summary_page("collection_runs")
        self.assertEqual(value["index_state"], "building")
        self.assertIsNone(value["total"])
        self.assertEqual(before, self.store.database_path.read_bytes())
        self.assertFalse(summary_index.backfill_batch(self.store, 1))
        self.assertFalse(self.store.summary_page("collection_runs")["counts_complete"])
        self.assertTrue(summary_index.backfill_batch(self.store, 1))
        self.assertEqual(self.store.summary_page("collection_runs")["total"], 1)

    def test_backfill_update_delete_and_recreate_cannot_restore_stale_payload(self):
        self.legacy([("collection_runs", key, self.run_row(key)) for key in ("a", "b", "c")])
        original = self.store._connection
        injected = False

        @contextmanager
        def interleaved(*, write=False):
            nonlocal injected
            with original(write=write) as connection:
                yield connection
            if not write and not injected:
                injected = True
                self.store._connection = original
                self.store.update("collection_runs", "a", lambda value: value.update(collection_status="completed"))
                self.store.delete("collection_runs", "b")
                self.store.delete("collection_runs", "c")
                self.store.put("collection_runs", "c", self.run_row("c", collection_status="cancelled"))

        self.store._connection = interleaved
        summary_index.backfill_batch(self.store, 10)
        summary_index.backfill_batch(self.store, 10)
        self.assertEqual(self.store.get_summary("collection_runs", "a")["collection_status"], "completed")
        self.assertIsNone(self.store.get_summary("collection_runs", "b"))
        self.assertEqual(self.store.get_summary("collection_runs", "c")["collection_status"], "cancelled")
        self.assertEqual(self.store.summary_page("collection_runs")["total"], 2)

    def test_scope_counts_ignore_search_status_and_pagination_but_obey_source(self):
        entries = [{"namespace": "collection_runs", "key": str(i), "payload": self.run_row(str(i),
            status="failed" if i < 4 else "completed", collection_status="failed" if i < 4 else "completed", created_at=f"2026-11-12T00:00:{i:02d}Z")}
            for i in range(20)]
        entries.append({"namespace": "collection_runs", "key": "other", "payload": self.run_row("other", task_source_batch_id="source-other")})
        self.store.put_many(entries)
        page = self.store.summary_page("collection_runs", task_source_batch_id="source-one", status="completed", q="中文", offset=10, limit=3)
        self.assertEqual(page["scope_counts"], {"total": 20, "active": 0, "abnormal": 4})
        self.assertEqual(page["filtered_total"], 16)
        self.assertEqual([row["collection_run_id"] for row in page["items"]], ["9", "8", "7"])
        self.assertEqual([row["collection_run_id"] for row in page["latest"]], ["19", "18", "17"])
        self.assertTrue(page["has_more"])

    def test_related_states_and_resume_override_old_failures(self):
        self.store.put("collection_runs", "cr-a", self.run_row("cr-a", collection_status="completed", status="completed",
            collection_summary={"collection_failed_count": 5}))
        self.store.put("remote_sync", "collection:cr-a", {"state": "backoff", "last_error": "old timeout", "failure_count": 2})
        self.assertEqual(self.store.summary_page("collection_runs")["scope_counts"]["abnormal"], 1)
        self.store.put("collection_transfers", "cr-a", {"transfer_status": "failed"})
        self.store.put("collection_resume_heads", "cr-a", {"run_id": "cr-a", "batch_id": "b-one", "revision": 2, "active": False,
            "collection_summary": {"planned_trajectory_count": 10, "collection_completed_count": 10}, "current_error_count": 0})
        value = self.store.summary_page("collection_runs")
        self.assertEqual(value["scope_counts"]["abnormal"], 0)
        self.assertEqual(value["items"][0]["transfer_status"], "completed")
        self.store.put("collection_receipt_storage", "cr-a", {"paused": True, "error": "disk full"})
        self.assertEqual(self.store.summary_page("collection_runs")["scope_counts"]["abnormal"], 1)
        self.store.put("batch_lifecycle", "b-one", {"status": "published", "release_id": "r-one"})
        self.assertEqual(self.store.summary_page("collection_runs", include_published=False)["total"], 0)

    def test_resume_phone_execution_finishes_before_transfer_and_matches_control_precedence(self):
        self.store.put_many([
            {"namespace": "collection_runs", "key": "cr-a", "payload": self.run_row("cr-a", status="failed", collection_status="failed")},
            {"namespace": "collection_resume_heads", "key": "cr-a", "payload": {"run_id": "cr-a", "revision": 2, "active": True}},
            {"namespace": "collection_resume_operations", "key": "op-one", "payload": {"run_id": "cr-a", "status": "started", "execution_id": "execution-new"}},
        ])
        self.assertEqual(self.store.get_summary("collection_runs", "cr-a")["collection_status"], "starting")
        self.store.put("collection_diagnostics", "cr-a", {"run_id": "cr-a", "phones": [
            {"phone_id": "one", "execution_id": "execution-new", "status": "completed", "logs": ["not-in-index"]}]})
        item = self.store.get_summary("collection_runs", "cr-a")
        self.assertEqual(item["collection_status"], "completed")
        self.assertEqual(item["transfer_status"], "downloading")
        self.assertTrue(item["summary_active"])
        self.store.update("collection_resume_heads", "cr-a", lambda value: value.update(cancel_intent={"epoch": 2}))
        self.assertEqual(self.store.get_summary("collection_runs", "cr-a")["collection_status"], "completed")
        self.store.update("collection_diagnostics", "cr-a", lambda value: value["phones"].append({"phone_id": "two", "execution_id": "execution-new", "status": "failed"}))
        self.assertEqual(self.store.get_summary("collection_runs", "cr-a")["collection_status"], "partial")
        with self.store._connection() as connection:
            projected = connection.execute("SELECT payload FROM record_summaries WHERE namespace='collection_diagnostics'").fetchone()[0]
        self.assertNotIn("not-in-index", projected)
        self.assertNotIn("phone_id", projected)
        self.store.delete("collection_resume_operations", "op-one")
        self.store.update("collection_resume_heads", "cr-a", lambda value: value.update(result_reconciliation_pending=True))
        self.assertEqual(self.store.get_summary("collection_runs", "cr-a")["collection_status"], "failed")

    def test_activity_and_abnormal_counts_preserve_existing_factory_predicates(self):
        rows = [
            self.run_row("transfer", status="completed", collection_status="completed", transfer_status="retry_wait"),
            self.run_row("archive", status="completed", collection_status="completed", archive_status="building"),
            self.run_row("partial", status="completed", collection_status="completed", collection_summary={"collection_failed_count": 1}),
            self.run_row("remote", status="completed", collection_status="completed", transfer_status="remote_failed"),
            self.run_row("retired"),
        ]
        self.store.put_many([{"namespace": "collection_runs", "key": row["collection_run_id"], "payload": row} for row in rows])
        self.store.put("collector_storage_runs", "retired", {"collector_data_state": "retired"})
        self.assertEqual(self.store.summary_page("collection_runs")["scope_counts"], {"total": 5, "active": 2, "abnormal": 2})
        self.store.put("model_iter_runs", "eval-one", {"run_id": "eval-one", "status": "completed", "request": {"execution_mode": "evaluate_only", "source_count": 80},
            "evaluation_summary": {"evaluation_pending": 2, "evaluation_failed": 1}})
        page = self.store.summary_page("model_iter_runs")
        self.assertEqual(page["scope_counts"], {"total": 1, "active": 1, "abnormal": 1})
        self.assertEqual(page["items"][0]["source_count"], 80)

    def test_search_matches_display_alias_and_preserves_identity_and_scope_counts(self):
        self.store.put_many([
            {"namespace": "collection_runs", "key": "cr_legacy_long_hash", "payload": self.run_row("cr_legacy_long_hash")},
            {"namespace": "collection_runs", "key": "cr_second", "payload": self.run_row("cr_second", batch_id="b-two", task_source_batch_id="source-two")},
            {"namespace": "display_identifiers", "key": "alias-run", "payload": {"kind": "run", "id": "cr_legacy_long_hash", "scope": "", "display_id": "cr261112-007"}},
            {"namespace": "display_identifiers", "key": "alias-batch", "payload": {"kind": "batch", "id": "b-one", "scope": "", "display_id": "b261112-009"}},
            {"namespace": "display_identifiers", "key": "wrong-scope", "payload": {"kind": "trajectory", "id": "cr_second", "scope": "b-two", "display_id": "cr261112-007"}},
        ])
        for query in ("cr261112-007", "b261112-009", "legacy_long_hash"):
            result = self.store.summary_page("collection_runs", q=query)
            self.assertEqual([row["collection_run_id"] for row in result["items"]], ["cr_legacy_long_hash"])
            self.assertEqual(result["scope_counts"]["total"], 2)
        self.assertEqual(self.store.summary_page("collection_runs", q="cr261112-007", task_source_batch_id="source-two")["items"], [])

    def test_storage_related_record_does_not_overwrite_run_revision_or_status(self):
        self.store.put("collection_runs", "cr-a", self.run_row("cr-a"))
        self.store.put("collector_storage_runs", "cr-a", {"status": "bad", "collector_data_state": "retired"})
        self.store.update("collector_storage_runs", "cr-a", lambda item: item.update(cleaned_at="now"))
        value = self.store.get_summary("collection_runs", "cr-a")
        self.assertEqual(value["storage_revision"], 1)
        self.assertEqual(value["status"], "running")
        self.assertEqual(value["collector_data_state"], "retired")

    def test_matching_keys_and_get_many_remain_complete_before_backfill(self):
        self.legacy([("pipelines", "p-one", {"pipeline_id": "p-one", "batch_id": "b-one", "status": "running"}),
                     ("pipelines", "p-two", {"pipeline_id": "p-two", "batch_id": "b-two", "status": "succeeded"})])
        for phase in range(2):
            self.assertEqual(self.store.matching_keys("pipelines", excluded_statuses={"succeeded"}), ["p-one"])
            self.assertEqual([row["pipeline_id"] for row in self.store.get_many("pipelines", ["missing", "p-two", "p-one"])], ["p-two", "p-one"])
            while not summary_index.backfill_batch(self.store):
                pass

    def test_existing_empty_database_reads_do_not_create_records_or_fail(self):
        self.store.database_path.parent.mkdir(parents=True)
        with closing(sqlite3.connect(str(self.store.database_path))):
            pass
        before = self.store.database_path.read_bytes()
        self.assertEqual(self.store.get_many("pipelines", ["p-one"]), [])
        self.assertEqual(self.store.matching_keys("pipelines", excluded_statuses={"succeeded"}), [])
        self.assertEqual(self.store.summary_page("pipelines")["index_state"], "building")
        self.assertEqual(before, self.store.database_path.read_bytes())

    def test_summary_get_never_reads_original_payloads(self):
        self.store.put_many([{"namespace": "collection_runs", "key": str(i), "payload": self.run_row(str(i), trajectories=[{"blob": "x" * 1024}] * 4)} for i in range(100)])
        statements = []
        original = self.store._connection
        @contextmanager
        def traced(*, write=False):
            self.assertFalse(write)
            with original(write=write) as connection:
                connection.set_trace_callback(statements.append)
                yield connection
        with patch.object(self.store, "_connection", traced), patch.object(self.store, "list", side_effect=AssertionError("raw namespace scan")):
            value = self.store.summary_page("collection_runs", limit=10, offset=50)
        self.assertEqual(len(value["items"]), 10)
        self.assertEqual(value["total"], 100)
        self.assertFalse(any("FROM records " in sql for sql in statements))

    def test_summary_busy_read_has_short_budget_without_changing_write_policy(self):
        self.store.put("collection_runs", "cr-a", self.run_row("cr-a"))
        with closing(sqlite3.connect(str(self.store.database_path), isolation_level=None)) as writer:
            writer.execute("BEGIN EXCLUSIVE")
            start = time.monotonic()
            with self.assertRaises(SummaryUnavailable):
                self.store.summary_page("collection_runs")
            self.assertLess(time.monotonic() - start, 1.0)
            writer.rollback()
        self.assertEqual(self.store.summary_page("collection_runs")["total"], 1)

    def test_pipeline_route_summary_legacy_compatibility_and_bound_jobs(self):
        class Runtime:
            def jobs(self, *args):
                raise AssertionError("detail must not enumerate historical jobs")
            def bound_jobs(other, step, pipeline, keys):
                return self.store.get_many("tree_jobs", keys)
        manager = PipelineManager(Runtime(), self.root)
        self.store.put("pipelines", "p-one", {"pipeline_id": "p-one", "name": "流程一", "batch_id": "b-one", "status": "running", "created_at": "now",
            "steps": [{"id": "tree", "label": "tree", "status": "running", "job_ids": ["j-one"]}]})
        self.store.put_many([{"namespace": "tree_jobs", "key": key, "payload": {"job_id": key, "batch_id": "b-one", "status": "running", "percent": percent}} for key, percent in (("j-one", 30), ("j-other", 90))])
        app = FastAPI()
        app.state.pipelines = manager
        app.include_router(router)
        with TestClient(app) as client:
            summary = client.get("/api/pipelines?view=summary&limit=10").json()
            self.assertEqual(summary["total"], 1)
            self.assertNotIn("jobs", summary["items"][0]["steps"][0])
            self.assertEqual(client.get("/api/pipelines").json()["pipelines"][0]["steps"][0]["percent"], 30)
            self.assertEqual(client.get("/api/pipelines?view=summary&limit=0").status_code, 422)
        self.assertEqual(self.store.matching_keys("tree_jobs", batch_id="b-one"), ["j-one", "j-other"])


if __name__ == "__main__":
    unittest.main()
