from __future__ import annotations
import copy
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.collection_control import merge_control, can_cancel
from backend import evaluation_previews, model_iteration
from backend.phone_factory import PhoneFactoryStore, PhoneFactoryError
from backend.tests import test_phone_factory_runtime as fixture
from backend.tests.test_model_iteration import results


class ControlMergeTests(unittest.TestCase):
    def test_stop_intent_and_revisions_cannot_resurrect_execution(self):
        row = {"status": "failed", "stop_requested_at": "now"}
        self.assertTrue(merge_control(row, {"state_revision": 2, "status": "running"}))
        self.assertEqual(row["collection_status"], "stopping")
        self.assertTrue(merge_control(row, {"state_revision": 3, "status": "cancelled"}))
        self.assertFalse(can_cancel(row))
        self.assertFalse(merge_control(row, {"state_revision": 2, "status": "running"}))
        self.assertFalse(merge_control(row, {"state_revision": 4, "status": "running"}))
        self.assertEqual(row["collection_status"], "cancelled")

    def test_unconfirmed_phone_keeps_reconcile_action(self):
        row = {}
        merge_control(row, {"state_revision": 9, "status": "cancelled", "phone_states": {
            "one": {"status": "needs_attention", "stop_confirmed": False},
            "two": {"status": "cancelled", "stop_confirmed": True}}})
        self.assertTrue(can_cancel(row))


class StopAcceptanceTests(unittest.TestCase):
    setUp = fixture.FactoryRuntimeTests.setUp
    client = fixture.FactoryRuntimeTests.client
    request = fixture.FactoryRuntimeTests.request

    def test_manual_retry_requests_only_sealing_and_receipt_not_device_execution(self):
        self.store.add_task(self.upload)
        run = self.store.remote_start(self.request())
        run_id = run["collection_run_id"]
        with patch.object(self.store, "_remote_result", return_value={"ok": True, "collection_run_id": run_id}) as remote, patch.object(self.store.runtime.transfer, "sync", return_value=run) as sync:
            self.store.runtime.retry_collection_transfer(run_id)
        remote.assert_called_once_with(["retry-collection-artifacts", run_id])
        sync.assert_called_once_with(run_id, force=True)

    def test_archive_refresh_after_receipt_closed_does_not_change_receipt_or_execution(self):
        self.store.add_task(self.upload)
        run = self.store.remote_start(self.request())
        run_id = run["collection_run_id"]
        self.store.records.update("collection_runs", run_id, lambda row: row.update(
            receipt_closed=True, transfer_status="completed", collection_status="completed", archive_status="building"))
        remote = {"run_id": run_id, "batch_id": run["batch_id"], "agent_type": "newagent", "archive_status": "ready"}
        with patch.object(self.store.runtime, "get_run", return_value=remote):
            self.store.runtime._refresh_archive(run_id)
        current = self.store.collection_runs.get(run_id)
        self.assertEqual(current["archive_status"], "ready")
        self.assertEqual(current["transfer_status"], "completed")
        self.assertEqual(current["collection_status"], "completed")

    def test_local_failed_remote_running_stop_is_fast_and_recoverable(self):
        self.store.add_task(self.upload)
        run = self.store.remote_start(self.request())
        run_id = run["collection_run_id"]
        self.remote[run_id].update(status="running", state_revision=1)
        self.store.records.update("collection_runs", run_id,
                                  lambda row: row.update(status="failed", dispatch_error="old timeout"))
        entered, release = threading.Event(), threading.Event()
        original = self.store._remote_result
        def remote(args):
            if args[0] == "cancel-run":
                entered.set()
                release.wait(5)
                return {"ok": True, "run_id": run_id, "status": "cancelled", "state_revision": 2}
            return original(args)
        self.addCleanup(self.store.runtime.close)
        with patch.object(self.store, "_remote_result", side_effect=remote):
            samples = []
            for _ in range(20):
                start = time.perf_counter()
                value = self.store.runtime.cancel_collection(run_id)
                samples.append(time.perf_counter() - start)
                self.assertTrue(value["stop_accepted"])
                self.assertEqual(value["collection_status"], "stopping")
            self.assertTrue(entered.wait(2))
            self.assertLess(sorted(samples)[18], 2)
            release.set()
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                row = self.store.collection_runs.get(run_id)
                if row.get("control_terminal"):
                    break
                time.sleep(.01)
            self.assertTrue(row["control_terminal"])
            self.assertEqual(row["collection_status"], "cancelled")
            self.assertNotEqual(row.get("transfer_status"), "completed")


class PreviewTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = PhoneFactoryStore(Path(tmp.name))
        self.addCleanup(self.store.runtime.close)

    def value(self, count=1):
        return {"preview_id": "preview-1", "valid": True, "status": "completed",
                "trajectory_count": count, "source": {"source_run_id": "source-1"}, "source_digest": "a"*64,
                "cases": [{"source_trajectory_id": f"traj-{i}", "case_id": "case-a", "task": "目标"}
                          for i in range(count)], "errors": [{"error": "missing image", "source_trajectory_id": "bad"}],
                "coverage": {"total": count+1, "included": count, "excluded": 1}}

    def test_partial_preview_is_valid_and_paginated_without_manual_exclusion(self):
        value = self.value(61)
        result = evaluation_previews.accept(self.store, {"source_run_id": "source-1"}, value)
        self.assertTrue(result["valid"])
        self.assertEqual(len(result["cases"]), 50)
        page = evaluation_previews.state(self.store, "preview-1", 50, 20)
        self.assertEqual(len(page["cases"]), 11)
        self.assertFalse(page["has_more"])
        self.assertEqual(self.store.records.get("model_iter_previews", "preview-1")["preview"], value)

    def test_repeated_ready_response_commits_preparing_and_late_response_cannot_rewrite_it(self):
        source = {"source_run_id": "source-1"}
        ready = self.value()
        preparing = {**ready, "valid": False, "status": "preparing", "cases": [], "trajectory_count": 0}
        with patch.object(self.store.runtime._preview_pool, "submit", return_value=False):
            evaluation_previews.accept(self.store, source, preparing)
        self.assertTrue(evaluation_previews.accept(self.store, source, ready)["valid"])
        self.assertTrue(self.store.records.get("model_iter_previews", "preview-1")["preview"]["valid"])
        changed = {**ready, "source_digest": "b"*64}
        with self.assertRaises(PhoneFactoryError):
            evaluation_previews.commit(self.store, "preview-1", source, changed)
        self.assertEqual(self.store.records.get("model_iter_previews", "preview-1")["preview"]["source_digest"], "a"*64)

    def test_background_preview_fetch_freezes_all_pages(self):
        all_rows = self.value(121)
        preparing = {**all_rows, "status": "preparing", "valid": False, "cases": [], "trajectory_count": 0}
        def remote(args):
            offset = int(args[args.index("--offset")+1]) if "--offset" in args else 0
            rows = all_rows["cases"][offset:offset+100]
            return {**all_rows, "cases": rows, "case_offset": offset, "has_more": offset+100 < 121}
        with patch.object(self.store.runtime._preview_pool, "submit", return_value=False):
            evaluation_previews.accept(self.store, {"source_run_id": "source-1"}, preparing)
        with patch.object(self.store, "_remote_result", side_effect=remote):
            evaluation_previews.refresh(self.store, "preview-1")
        row = self.store.records.get("model_iter_previews", "preview-1")
        self.assertTrue(row["preview"]["valid"])
        self.assertEqual(len(row["preview"]["cases"]), 121)

    def test_preview_wrong_identity_and_nonterminating_pagination_are_rejected(self):
        preparing = {**self.value(), "status": "preparing", "valid": False, "cases": []}
        with patch.object(self.store.runtime._preview_pool, "submit", return_value=False):
            evaluation_previews.accept(self.store, {"source_run_id": "source-1"}, preparing)
        for remote in ({**self.value(), "preview_id": "wrong"}, {**self.value(), "has_more": True}):
            with patch.object(self.store, "_remote_result", return_value=remote) as call:
                evaluation_previews.refresh(self.store, "preview-1")
                self.assertEqual(call.call_count, 1)
            saved = self.store.records.get("model_iter_previews", "preview-1")
            self.assertFalse(saved["preview"]["valid"])
            self.assertTrue(saved["sync_error"])

    def test_partial_results_require_missing_identity_exclusion(self):
        expected = {"one": {"case_id": "case-a", "phone_id": "phone-1"},
                    "two": {"case_id": "case-a", "phone_id": "phone-1"}}
        value = results("eval-1")
        value["cases"][0]["source_trajectory_id"] = "one"
        with self.assertRaises(PhoneFactoryError):
            model_iteration.validate_results(value, "eval-1", [], expected_sources=expected)
        value.update(excluded=[{"source_trajectory_id": "two", "error": "source changed"}],
                     coverage={"total": 2, "included": 1, "excluded": 1})
        output = model_iteration.validate_results(value, "eval-1", [], expected_sources=expected)
        self.assertEqual(output["coverage"]["excluded"], 1)
        self.assertEqual(len(output["cases"]), 1)

