from __future__ import annotations

import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock, patch

from backend.data_store import RecordStore
from backend.remote_sync import BoundedSyncPool, RemoteSyncPolicy, SyncDeferred, _process_lock


class RemoteFailure(ValueError):
    def __init__(self, kind="http", status=404, command="run"):
        super().__init__("do not log TOKEN=private-command-payload")
        self.remote_kind, self.remote_status, self.remote_command = kind, status, command


class RemoteSyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.records = RecordStore(Path(self.temp.name))
        self.now = 10000.0
        self.policy = RemoteSyncPolicy(self.records, clock=lambda: self.now)
        self.run = {"run_id": "eval_test", "created_at": 9000, "status": "running"}

    def failure(self, exc=None, run=None):
        exc = exc or RemoteFailure()
        with self.assertRaises(type(exc)):
            self.policy.execute("evaluation", run or self.run, Mock(side_effect=exc))

    def advance(self):
        self.now += 301

    def test_missing_run_pauses_and_survives_restart_without_changing_run(self):
        for _ in range(3):
            self.failure()
            self.advance()
        restarted = RemoteSyncPolicy(RecordStore(Path(self.temp.name)), clock=lambda: self.now)
        self.assertEqual(restarted.view("evaluation", "eval_test")["pause_reason"], "remote_missing")
        self.assertFalse(restarted.due("evaluation", self.run))
        self.assertEqual(self.run["status"], "running")
        callback = Mock()
        with self.assertRaises(SyncDeferred):
            restarted.execute("evaluation", self.run, callback)
        callback.assert_not_called()

    def test_grace_period_and_nonconsecutive_missing_resets(self):
        self.run["submitted_at"] = self.now
        for amount in (30, 60, 120):
            self.failure()
            self.now += amount
        self.assertEqual(self.policy.view("evaluation", "eval_test")["state"], "backoff")
        self.failure(RemoteFailure("timeout", None))
        self.advance()
        self.failure()
        self.assertEqual(self.policy.view("evaluation", "eval_test")["not_found_count"], 1)

    def test_backoff_and_recovery(self):
        for seconds in (30, 60, 120, 300, 300):
            self.failure(RemoteFailure("timeout", None))
            self.assertFalse(self.policy.due("evaluation", self.run))
            self.now += seconds - 1
            self.assertFalse(self.policy.due("evaluation", self.run))
            self.now += 1
            self.assertTrue(self.policy.due("evaluation", self.run))
        self.assertEqual(self.policy.execute("evaluation", self.run, lambda: "ok"), "ok")
        self.assertEqual(self.policy.view("evaluation", "eval_test")["failure_count"], 0)
        self.assertFalse(self.policy.due("evaluation", self.run))
        self.now += 10
        self.assertTrue(self.policy.due("evaluation", self.run))

    def test_results_and_archive_404_never_count_as_missing_run(self):
        for command in ("run-evaluations", "run-archive", "reports"):
            for _ in range(3):
                self.failure(RemoteFailure(command=command))
                self.advance()
            self.assertEqual(self.policy.view("evaluation", "eval_test")["not_found_count"], 0)
            self.assertNotEqual(self.policy.view("evaluation", "eval_test")["state"], "paused")

    def test_confirmed_terminal_final_sync_has_eight_attempts(self):
        def callback():
            self.policy.observe_remote("evaluation", "eval_test", "completed")
            raise RemoteFailure(command="run-evaluations")
        for index in range(8):
            with self.assertRaises(RemoteFailure):
                self.policy.execute("evaluation", self.run, callback)
            state = self.policy.view("evaluation", "eval_test")
            self.assertEqual(state["final_failure_count"], index + 1)
            self.assertEqual(state["state"], "paused" if index == 7 else "backoff")
            self.advance()
        self.assertEqual(state["pause_reason"], "final_sync_exhausted")
        self.assertEqual(self.run["status"], "running")

    def test_manual_retry_resets_failure_counts_and_never_runs_twice(self):
        for _ in range(3):
            self.failure()
            self.advance()
        call = Mock(side_effect=RemoteFailure("timeout", None))
        with self.assertRaises(RemoteFailure):
            self.policy.execute("evaluation", self.run, call, force=True)
        call.assert_called_once()
        state = self.policy.view("evaluation", "eval_test")
        self.assertEqual(state["state"], "backoff")
        self.assertEqual(state["failure_count"], 1)
        self.assertEqual(state["not_found_count"], 0)

    def test_new_undispatched_skipped_legacy_allowed_and_complete_is_sticky(self):
        self.assertFalse(self.policy.due("evaluation", {**self.run, "dispatch_attempted": False}))
        self.assertTrue(self.policy.due("evaluation", self.run))
        self.policy.execute("evaluation", self.run,
            lambda: self.policy.complete("evaluation", "eval_test"))
        self.assertEqual(self.policy.view("evaluation", "eval_test")["state"], "complete")
        self.advance()
        self.assertFalse(self.policy.due("evaluation", self.run))
        self.policy.reset("evaluation", "eval_test")
        self.assertTrue(self.policy.due("evaluation", self.run))

    def test_concurrent_instances_merge_manual_and_defer_auto_without_extra_attempt(self):
        entered, release = threading.Event(), threading.Event()
        second = RemoteSyncPolicy(self.records, clock=lambda: self.now)
        callback = Mock(side_effect=lambda: (entered.set(), release.wait(5), "result")[-1])
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(self.policy.execute, "evaluation", self.run, callback, True)
            self.assertTrue(entered.wait(5))
            with self.assertRaises(SyncDeferred) as error:
                second.execute("evaluation", self.run, Mock())
            self.assertEqual(error.exception.status, 409)
            joined = pool.submit(second.execute, "evaluation", self.run, callback, True)
            release.set()
            self.assertEqual(first.result(), "result")
            self.assertEqual(joined.result(), "result")
        callback.assert_called_once()
        self.assertEqual(self.policy.view("evaluation", "eval_test")["failure_count"], 0)

    def test_repeated_expected_error_logs_once_and_redacts_payload(self):
        with self.assertLogs("backend.remote_sync", "WARNING") as captured:
            for _ in range(5):
                self.failure(RemoteFailure("timeout", None))
                self.now += 301
        self.assertEqual(len(captured.output), 1)
        self.assertNotIn("private-command-payload", str(captured.output))

    def test_unknown_traceback_is_rate_limited(self):
        with self.assertLogs("backend.remote_sync", "WARNING") as captured:
            for seconds in (30, 60, 120):
                self.failure(RuntimeError("TOKEN=private-command-payload"))
                self.now += seconds
        self.assertEqual(len(captured.output), 2)
        self.assertNotIn("private-command-payload", str(captured.output))
        self.assertEqual(self.policy.view("evaluation", "eval_test")["not_found_count"], 0)

    def test_readonly_view_does_not_create_database(self):
        self.assertEqual(self.policy.view("evaluation", "eval_test")["state"], "active")
        self.assertFalse(self.records.database_path.exists())

    def test_os_lock_contention_defers_without_recording_failure(self):
        with _process_lock(self.records, "evaluation:eval_test") as locked:
            self.assertTrue(locked)
            callback = Mock()
            with self.assertRaises(SyncDeferred) as error:
                self.policy.execute("evaluation", self.run, callback, force=True)
            self.assertEqual(error.exception.status, 409)
            callback.assert_not_called()
        self.assertEqual(self.policy.view("evaluation", "eval_test")["failure_count"], 0)

    def test_normal_active_priority_and_overdue_retry_fairness(self):
        self.failure(RemoteFailure("timeout", None))
        self.now += 30
        active = {**self.run, "run_id": "new", "created_at": self.now}
        self.assertLess(self.policy.priority("evaluation", active), self.policy.priority("evaluation", self.run))
        self.policy.execute("evaluation", active, lambda: None)
        self.now += 60
        self.assertLess(self.policy.priority("evaluation", self.run), self.policy.priority("evaluation", active))

    def test_fresh_force_waits_for_old_attempt_and_queries_again(self):
        for fails in (False, True):
            with self.subTest(old_attempt_fails=fails):
                self.policy.reset("evaluation", "eval_test")
                entered, release, fresh_entered = threading.Event(), threading.Event(), threading.Event()
                calls = []
                def old():
                    calls.append("old")
                    entered.set()
                    release.wait(5)
                    if fails:
                        raise RemoteFailure("timeout", None)
                    return "running"
                def fresh():
                    calls.append("fresh")
                    fresh_entered.set()
                    return "cancelled"
                with ThreadPoolExecutor(max_workers=2) as pool:
                    original = pool.submit(self.policy.execute, "evaluation", self.run, old, True)
                    self.assertTrue(entered.wait(5))
                    current = pool.submit(self.policy.execute, "evaluation", self.run, fresh, True, False)
                    self.assertFalse(fresh_entered.wait(0.05))
                    release.set()
                    if fails:
                        with self.assertRaises(RemoteFailure):
                            original.result(5)
                    else:
                        self.assertEqual(original.result(5), "running")
                    self.assertEqual(current.result(5), "cancelled")
                self.assertEqual(calls, ["old", "fresh"])
                self.assertEqual(self.policy.view("evaluation", "eval_test")["failure_count"], 0)

    def test_protocol_and_identity_errors_have_safe_distinct_categories(self):
        for category in ("protocol", "identity"):
            self.policy.reset("evaluation", "eval_test")
            with self.assertLogs("backend.remote_sync", "WARNING") as captured:
                self.failure(RemoteFailure(category, None))
            state = self.policy.view("evaluation", "eval_test")
            self.assertEqual(state["last_error_category"], category + ":run")
            self.assertNotIn("private-command-payload", str(captured.output))
            self.assertEqual(len(captured.output), 1)

    def test_confirmed_terminal_is_not_overwritten_by_late_active_observation(self):
        self.policy.observe_remote("evaluation", "eval_test", "cancelled")
        self.policy.observe_remote("evaluation", "eval_test", "running")
        self.assertEqual(self.policy.view("evaluation", "eval_test")["remote_status"], "cancelled")
        self.failure(RemoteFailure("timeout", None))
        self.assertEqual(self.policy.view("evaluation", "eval_test")["final_failure_count"], 1)
        self.policy.observe_remote("evaluation", "eval_test", "completed")
        self.assertEqual(self.policy.view("evaluation", "eval_test")["remote_status"], "completed")


class BoundedSyncPoolTests(unittest.TestCase):
    def test_no_backlog_distinct_pools_and_capacity_released(self):
        first, second = BoundedSyncPool("one"), BoundedSyncPool("two")
        self.addCleanup(first.close)
        self.addCleanup(second.close)
        gate, entered = threading.Event(), threading.Event()
        self.addCleanup(gate.set)
        self.assertTrue(first.submit("a", lambda: gate.wait(5)))
        self.assertTrue(first.submit("b", lambda: gate.wait(5)))
        self.assertEqual(first.available, 0)
        self.assertFalse(first.submit("c", lambda: None))
        self.assertFalse(first.submit("a", lambda: None))
        self.assertTrue(second.submit("a", entered.set))
        self.assertTrue(entered.wait(5))
        self.assertEqual(set(first.inflight), {"a", "b"})
        gate.set()
        first.close()
        self.assertFalse(first.submit("d", lambda: None))


if __name__ == "__main__":
    unittest.main()
