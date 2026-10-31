from __future__ import annotations

import threading
import unittest
from unittest.mock import patch

from backend.phone_factory import PhoneFactoryError
from backend.phone_factory_runtime import FactoryRuntime
from backend.remote_sync import RemoteSyncPolicy
from backend.tests import test_model_iteration as fixtures


class EvaluationSyncPolicyTests(unittest.TestCase):
    client = fixtures.ModelIterationTests.client
    start = fixtures.ModelIterationTests.start

    def setUp(self):
        fixtures.ModelIterationTests.setUp(self)
        self.now = 1_800_000_000.0
        self.runtime = self.store.runtime
        self.runtime.sync_policy = RemoteSyncPolicy(self.store.records, clock=lambda: self.now)
        self.addCleanup(self.runtime.close)

    def old_run(self):
        run = self.start()
        return self.store.records.update("model_iter_runs", run["run_id"],
            lambda item: item.update(created_at="2020-01-01T00:00:00Z", submitted_at="2020-01-01T00:00:00Z"))

    def fail_command(self, command, status=404, kind="http"):
        original = self.store.run_client
        def failing(args):
            if args[0] == command:
                self.calls.append(args)
                return {"ok": False, "output": "upstream failure", "remote_kind": kind,
                        "remote_status": status}
            return original(args)
        self.store.run_client = failing
        return original

    def auto(self, run_id):
        return self.runtime.sync_evaluation(run_id, force=False)

    def test_run_404_pauses_across_restart_without_changing_execution_state(self):
        run = self.old_run(); run_id = run["run_id"]
        before = self.store.records.get("model_iter_runs", run_id)
        self.fail_command("run")
        for delta in (0, 30, 60):
            self.now += delta
            with self.assertRaises(PhoneFactoryError):
                self.auto(run_id)
        self.assertEqual(self.runtime.sync_policy.view("evaluation", run_id)["state"], "paused")
        self.assertEqual(self.store.records.get("model_iter_runs", run_id), before)
        count = len(self.calls)
        restarted = FactoryRuntime(self.store)
        self.addCleanup(restarted.close)
        restarted.sync_policy = RemoteSyncPolicy(self.store.records, clock=lambda: self.now + 1000)
        with self.assertRaises(PhoneFactoryError):
            restarted.sync_evaluation(run_id, force=False)
        self.assertEqual(len(self.calls), count)

    def test_result_404_is_not_a_missing_run(self):
        run_id = self.old_run()["run_id"]
        self.fail_command("run-evaluations")
        for delta in (0, 30, 60, 120):
            self.now += delta
            with self.assertRaises(PhoneFactoryError):
                self.auto(run_id)
        self.assertEqual(self.runtime.sync_policy.view("evaluation", run_id)["state"], "backoff")
        self.assertEqual(self.store.records.get("model_iter_runs", run_id)["status"], "running")

    def test_get_respects_backoff_and_returns_stale_snapshot(self):
        run_id = self.old_run()["run_id"]
        self.auto(run_id)
        self.fail_command("run", status=None, kind="timeout")
        self.now += 10
        action = f"runs/{run_id}/evaluations"
        first = self.store.dispatch(action, "GET", {}, mode="modeliter")
        self.assertTrue(first["stale"])
        count = len(self.calls)
        for _ in range(4):
            self.assertTrue(self.store.dispatch(action, "GET", {}, mode="modeliter")["stale"])
        self.assertEqual(len(self.calls), count)

    def test_get_without_snapshot_is_explicit_and_does_not_retry_early(self):
        run_id = self.old_run()["run_id"]
        self.fail_command("run", status=None, kind="timeout")
        with self.assertRaises(PhoneFactoryError):
            self.store.dispatch(f"runs/{run_id}/evaluations", "GET", {}, mode="modeliter")
        count = len(self.calls)
        with self.assertRaises(PhoneFactoryError) as raised:
            self.store.dispatch(f"runs/{run_id}/evaluations", "GET", {}, mode="modeliter")
        self.assertEqual(raised.exception.status, 503)
        self.assertEqual(len(self.calls), count)
        self.assertIsNone(self.store.records.get("model_iter_results", run_id))

    def test_manual_sync_resumes_without_starting_another_run(self):
        run_id = self.old_run()["run_id"]
        original = self.fail_command("run")
        for delta in (0, 30, 60):
            self.now += delta
            with self.assertRaises(PhoneFactoryError):
                self.auto(run_id)
        self.store.run_client = original
        starts = sum(args[0] == "start-run" for args in self.calls)
        result = self.store.dispatch(f"runs/{run_id}/sync", "POST", {}, mode="modeliter")
        self.assertEqual(result["status"], "running")
        self.assertNotEqual(result["sync"]["state"], "paused")
        self.assertEqual(sum(args[0] == "start-run" for args in self.calls), starts)
        self.assertEqual(len(self.store.records.list("model_iter_runs")), 1)

    def test_terminal_result_failures_pause_without_fabricating_final_results(self):
        run_id = self.old_run()["run_id"]
        self.runs[run_id]["status"] = "succeeded"
        self.fail_command("run-evaluations", status=503)
        for _ in range(8):
            with self.assertRaises(PhoneFactoryError):
                self.auto(run_id)
            self.now += 300
        self.assertEqual(self.runtime.sync_policy.view("evaluation", run_id)["state"], "paused")
        self.assertIsNone(self.store.records.get("model_iter_results", run_id))
        self.assertEqual(self.store.records.get("model_iter_runs", run_id)["status"], "running")

    def test_successful_final_snapshot_stops_queries_even_after_restart(self):
        run_id = self.old_run()["run_id"]
        self.runs[run_id]["status"] = "succeeded"
        self.auto(run_id)
        count = len(self.calls)
        self.runtime._schedule_evaluations()
        self.runtime.sync_evaluation(run_id)
        self.assertEqual(len(self.calls), count)
        self.assertTrue(self.store.records.get("model_iter_results", run_id)["final"])

    def test_pre_dispatch_failure_never_enters_automatic_polling(self):
        # The general collector capability check now runs before reserving a
        # result batch. Fail the later evaluator check to exercise a persisted
        # run that has not submitted a start request yet.
        with patch("backend.phone_factory_runtime.model_iteration.require_capability",
                   side_effect=PhoneFactoryError("评测能力检查失败", 409)):
            with self.assertRaises(PhoneFactoryError):
                self.start()
        run = self.store.records.list("model_iter_runs")[0]
        self.assertIs(run["dispatch_attempted"], False)
        count = len(self.calls)
        self.runtime._schedule_evaluations()
        self.assertEqual(len(self.calls), count)
        self.assertFalse(self.runtime.sync_policy.due("evaluation", run))
        resumed = self.start()
        self.assertIs(self.store.records.get("model_iter_runs", run["run_id"])["dispatch_attempted"], True)
        self.assertTrue(self.runtime.sync_policy.due("evaluation", resumed))

    def test_initial_capability_failure_does_not_reserve_or_poll_a_run(self):
        self.supported = False
        with self.assertRaises(PhoneFactoryError):
            self.start()
        self.assertEqual(self.store.records.list("model_iter_runs"), [])
        self.assertEqual(self.store.records.list("collection_runs"), [])
        count = len(self.calls)
        self.runtime._schedule_evaluations()
        self.assertEqual(len(self.calls), count)
        self.supported = True
        resumed = self.start()
        self.assertTrue(resumed["dispatch_attempted"])

    def test_legacy_runs_without_dispatch_marker_are_still_checked(self):
        run_id = self.old_run()["run_id"]
        def legacy(item):
            item.pop("dispatch_attempted", None)
        self.store.records.update("model_iter_runs", run_id, legacy)
        self.assertEqual(self.auto(run_id)["status"], "running")

    def test_manual_and_background_do_not_overlap(self):
        run_id = self.old_run()["run_id"]
        entered, release = threading.Event(), threading.Event()
        original = self.store.run_client
        def blocked(args):
            if args[0] == "run":
                entered.set()
                if not release.wait(5):
                    raise RuntimeError("test timed out")
            return original(args)
        self.store.run_client = blocked
        errors = []
        def run():
            try:
                self.auto(run_id)
            except Exception as exc:
                errors.append(exc)
        thread = threading.Thread(target=run)
        thread.start()
        try:
            self.assertTrue(entered.wait(3))
            # Automatic readers do not wait or launch a duplicate request.
            with self.assertRaises(PhoneFactoryError) as raised:
                self.runtime.sync_evaluation(run_id, force=False)
            self.assertEqual(raised.exception.status, 409)
            # An explicit retry may join the existing request and reuse its result.
            def manual():
                try:
                    self.runtime.sync_evaluation(run_id, force=True)
                except Exception as exc:
                    errors.append(exc)
            manual_thread = threading.Thread(target=manual)
            manual_thread.start()
        finally:
            release.set(); thread.join(5)
            if "manual_thread" in locals():
                manual_thread.join(5)
        self.assertFalse(errors)
        # The manual thread can arrive after the original request completes; the
        # policy's deterministic merge behaviour is covered in test_remote_sync.
        self.assertLessEqual(sum(args[0] == "run" for args in self.calls), 2)

    def test_sync_metadata_is_available_in_run_list(self):
        run_id = self.old_run()["run_id"]
        self.fail_command("run", status=None, kind="timeout")
        with self.assertRaises(PhoneFactoryError):
            self.auto(run_id)
        value = self.runtime.run_views("modeliter")[0]
        self.assertEqual(value["sync"]["state"], "backoff")
        self.assertEqual(value["status"], "running")

    def test_final_snapshot_recovers_interrupted_run_update_without_network(self):
        run_id = self.old_run()["run_id"]
        self.runs[run_id]["status"] = "succeeded"
        self.auto(run_id)
        frozen = self.store.records.get("model_iter_results", run_id)
        self.store.records.update("model_iter_runs", run_id, lambda item: item.update(status="running"))
        count = len(self.calls)
        self.unreachable = True
        self.runtime._schedule_evaluations()
        self.assertEqual(self.store.records.get("model_iter_runs", run_id)["status"], "succeeded")
        self.assertEqual(self.store.records.get("model_iter_results", run_id), frozen)
        self.assertEqual(len(self.calls), count)

    def test_late_production_initializer_does_not_clear_dispatch_marker(self):
        create = self.store.collection_runs.create
        request = {"filename": "eval.xlsx", "request_id": "production-race", "vla": "http://vla.invalid"}
        nested = []
        def delayed(*args, **kwargs):
            created_run, created = create(*args, **kwargs)
            if created:
                nested.append(self.store.remote_start(request, mode="generate"))
            return created_run, created
        with patch.object(self.store.collection_runs, "create", side_effect=delayed):
            first = self.store.remote_start(request, mode="generate")
        saved = self.store.collection_runs.get(first["collection_run_id"])
        self.assertEqual(saved["status"], "running")
        self.assertIs(saved["dispatch_attempted"], True)
        self.assertEqual(len(nested), 1)
        self.assertEqual(sum(args[0] == "start-run" for args in self.calls), 1)

    def test_cancel_acknowledgement_is_preserved_when_final_refresh_times_out(self):
        run_id = self.old_run()["run_id"]
        original = self.store.run_client
        def cancel_then_timeout(args):
            if args[0] == "cancel-run":
                import json
                return {"ok": True, "output": json.dumps({"run_id": run_id, "status": "cancelled"})}
            if args[0] == "run":
                return {"ok": False, "output": "timeout", "remote_kind": "timeout"}
            return original(args)
        self.store.run_client = cancel_then_timeout
        cancelled = self.store.dispatch(f"runs/{run_id}/cancel", "POST", {}, mode="modeliter")
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertEqual(cancelled["sync"]["state"], "backoff")
        self.assertEqual(cancelled["sync"]["remote_status"], "cancelled")


if __name__ == "__main__":
    unittest.main()
