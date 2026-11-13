"""Offline worker protocol, bounded cleanup and persisted lease recovery."""
from __future__ import annotations

import io
import json
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock, patch

from backend import quality_execution, quality_jobs, quality_process
from backend.data_store import RecordStore


class QualityProcessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.frames = []

    def worker(self, script, **kwargs):
        real_popen = subprocess.Popen
        processes = []
        def spawn(command, **options):
            self.assertEqual(options["env"]["ADF_QUALITY_PARENT_PID"], str(__import__("os").getpid()))
            self.assertTrue(options["env"]["ADF_QUALITY_EXECUTION_TOKEN"])
            process = real_popen([sys.executable, "-u", "-c", "import sys; assert sys.stdin.readline() == 'QUALITY_GO\\n'\n" + script], **options)
            processes.append(process)
            return process
        try:
            with patch.object(quality_jobs.subprocess, "Popen", side_effect=spawn), \
                 patch.object(quality_jobs, "_env_values", return_value={}), \
                 patch.object(quality_jobs, "_SUPERVISION_POLL", .01), \
                 patch.object(quality_jobs, "_REQUEST_CLEANUP_GRACE", .05), \
                 patch.object(quality_jobs, "_HEARTBEAT_TIMEOUT", .5):
                return quality_jobs.run_quality_subprocess("batch", ["task"], job_id="job",
                    data_root=self.root, progress=self.frames.append, **kwargs)
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)

    def test_heartbeat_does_not_extend_request_deadline(self):
        script = """
import json,time
print('REQUEST '+json.dumps({'local_request_id':'r1','event':'started','operation':'rubric_generate','timeout_seconds':.1}),flush=True)
while True:
 print('HEARTBEAT {}',flush=True)
 time.sleep(.02)
"""
        started = time.monotonic()
        with self.assertRaises(quality_jobs.QualitySubprocessError) as caught:
            self.worker(script)
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(caught.exception.failure["category"], "timeout")
        self.assertEqual(caught.exception.failure["code"], "quality_request_deadline")
        state = quality_execution.get_execution(self.root, "job")
        self.assertEqual(state["state"], "revoked")
        self.assertTrue(state["process"]["exited"])
        self.assertTrue(any("heartbeat_at" in frame for frame in self.frames))

    def test_diagnostic_log_write_failure_does_not_stop_protocol_drain(self):
        with patch.object(quality_jobs.diagnostics, "append_text", side_effect=PermissionError("fixture log denied")):
            result = self.worker("print('PROGRESS {\"stage\":\"evaluating\",\"percent\":50}',flush=True)\n"
                                 "print('RESULT {\"warnings\":[]}',flush=True)")
        self.assertEqual(result, {"warnings": []})
        self.assertTrue(any(frame.get("percent") == 50 for frame in self.frames))

    def test_local_checklist_progress_is_bounded_and_retains_real_counts(self):
        value = quality_jobs._safe_progress_frame({"stage": "checklist_definition",
            "completed_rubric_trajectories": 10, "total_rubric_trajectories": 10,
            "completed_checklist_trajectories": 2, "failed_checklist_trajectories": 1,
            "total_checklist_trajectories": 10, "current_model_role": "vision", "current_model": "actual-model",
            "checklist_event": {"kind": "request_end", "role": "vision", "elapsed_seconds": 2,
                                "prompt": "must not leak", "api_key": "must not leak"}})
        self.assertEqual(value["completed_checklist_trajectories"], 2)
        self.assertEqual(value["failed_checklist_trajectories"], 1)
        self.assertEqual(value["stage"], "checklist_definition")
        self.assertNotIn("prompt", value["checklist_event"])
        self.assertNotIn("api_key", value["checklist_event"])

    def test_timeout_event_then_failed_cannot_cancel_cleanup_deadline(self):
        script = """
import json,time
def event(kind):
 print('REQUEST '+json.dumps({'local_request_id':'r1','event':kind,'timeout_seconds':1}),flush=True)
event('started');event('timeout');event('failed')
while True:
 print('HEARTBEAT {}',flush=True);time.sleep(.02)
"""
        with self.assertRaises(quality_jobs.QualitySubprocessError) as caught:
            self.worker(script)
        self.assertEqual(caught.exception.failure["category"], "timeout")

    def test_finished_model_then_client_close_hangs_is_not_model_timeout(self):
        script = """
import json,time
for frame in [
 {'local_request_id':'r1','event':'started','operation':'rubric_generate','timeout_seconds':.1},
 {'local_request_id':'r1','event':'finished','operation':'rubric_generate'},
 {'local_request_id':'c1','event':'started','operation':'client_cleanup','timeout_seconds':.1}]:
 print('REQUEST '+json.dumps(frame),flush=True)
while True:
 print('HEARTBEAT {}',flush=True);time.sleep(.02)
"""
        with self.assertRaises(quality_jobs.QualitySubprocessError) as caught:
            self.worker(script)
        self.assertEqual(caught.exception.failure["code"], "quality_client_cleanup_timeout")
        self.assertFalse(caught.exception.failure["retryable"])

    def test_concurrent_and_duplicate_requests_do_not_replace_first_deadline(self):
        script = """
import json,time
def event(ident,kind,timeout):
 print('REQUEST '+json.dumps({'local_request_id':ident,'event':kind,'timeout_seconds':timeout}),flush=True)
event('slow','started',.1)
event('fast','started',5);event('fast','finished',5)
event('slow','started',5)
while True:
 print('HEARTBEAT {}',flush=True);time.sleep(.02)
"""
        with self.assertRaises(quality_jobs.QualitySubprocessError) as caught:
            self.worker(script)
        self.assertEqual(caught.exception.failure["category"], "timeout")

    def test_no_output_is_unresponsive_not_model_timeout(self):
        with self.assertRaises(quality_jobs.QualitySubprocessError) as caught:
            self.worker("import time;time.sleep(10)")
        self.assertEqual(caught.exception.failure["code"], "quality_worker_unresponsive")
        self.assertFalse(caught.exception.failure["retryable"])

    def test_finished_requests_can_run_successively_beyond_one_request_timeout(self):
        script = """
import json,time
for n in range(4):
 print('HEARTBEAT {}',flush=True)
 print('REQUEST '+json.dumps({'local_request_id':str(n),'event':'started','timeout_seconds':.15}),flush=True)
 time.sleep(.08)
 print('REQUEST '+json.dumps({'local_request_id':str(n),'event':'finished','elapsed_ms':80}),flush=True)
print('RESULT {"warnings":[]}',flush=True)
"""
        self.assertEqual(self.worker(script), {"warnings": []})
        self.assertFalse(self.frames[-1]["active_model_requests"])

    def test_explicitly_settled_legacy_request_does_not_kill_next_attempt(self):
        script = """
import json,time
def event(ident,kind,timeout):
 print('REQUEST '+json.dumps({'local_request_id':ident,'event':kind,'timeout_seconds':timeout}),flush=True)
event('old','started',.1);event('old','timeout',.1);event('old','settled',.1)
event('new','started',.3)
time.sleep(.15)
event('new','finished',.3)
print('RESULT {"warnings":[]}',flush=True)
"""
        self.assertEqual(self.worker(script), {"warnings": []})

    def test_parent_shutdown_revokes_before_stopping_worker(self):
        stop = threading.Event()
        timer = threading.Timer(.15, stop.set)
        timer.start()
        try:
            with self.assertRaises(quality_jobs.QualitySubprocessError) as caught:
                self.worker("import time\nwhile True:\n print('HEARTBEAT {}',flush=True);time.sleep(.02)", stop_event=stop)
            self.assertEqual(caught.exception.failure["category"], "service_restart")
            execution = quality_execution.get_execution(self.root, "job")
            self.assertEqual(execution["state"], "revoked")
            self.assertTrue(execution["process"]["exited"])
        finally:
            timer.cancel()

    def test_cleanup_failure_blocks_new_execution(self):
        with patch.object(quality_jobs, "stop_owned_process", return_value=False):
            with self.assertRaises(quality_jobs.QualitySubprocessError) as caught:
                self.worker("import time;time.sleep(10)")
        self.assertEqual(caught.exception.failure["code"], "quality_worker_cleanup_failed")
        with self.assertRaises(quality_execution.QualityExecutionBlocked):
            quality_execution.start_execution(self.root, "next", "batch")

    def test_cleanup_failure_overrides_retryable_parent_callback_error(self):
        self.frames = Mock()
        self.frames.append.side_effect = TimeoutError("diagnostic database unavailable")
        with patch.object(quality_jobs, "stop_owned_process", return_value=False):
            with self.assertRaises(quality_jobs.QualitySubprocessError) as caught:
                self.worker("import time\nwhile True:\n print('HEARTBEAT {}',flush=True);time.sleep(.02)")
        self.assertEqual(caught.exception.failure["code"], "quality_worker_cleanup_failed")
        self.assertFalse(caught.exception.failure["retryable"])

    def test_committed_result_recovers_even_if_worker_never_reports_result(self):
        expected = {"warnings": ["saved"]}
        with patch.object(quality_execution, "committed_result", return_value=expected):
            self.assertEqual(self.worker("raise SystemExit(1)"), expected)

    def test_restart_reclaims_only_persisted_worker_and_unblocks_batch(self):
        process = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(10)"])
        try:
            execution = quality_execution.start_execution(self.root, "old", "batch")
            quality_execution.update_process(self.root, "old", execution["token"], quality_process.process_metadata(process))
            RecordStore(self.root).put("quality_jobs", "old", {"job_id": "old", "run_id": "batch", "status": "running"})
            manager = quality_jobs.QualityJobManager(self.root / "jobs", data_root=self.root)
            self.assertIsNotNone(process.wait(timeout=5))
            self.assertEqual(manager.get("old")["status"], "interrupted")
            quality_execution.start_execution(self.root, "new", "batch")
            manager.shutdown()
        finally:
            if process.poll() is None:
                process.kill();process.wait(timeout=5)

    def test_reused_pid_is_not_terminated(self):
        metadata = {"pid": 123, "hostname": __import__('socket').gethostname(), "process_identity": "old"}
        with patch.object(quality_process, "_process_identity", return_value="new"), \
             patch.object(quality_process.os, "kill") as kill:
            self.assertTrue(quality_process.stop_recorded_process(metadata))
            kill.assert_not_called()

    def test_missing_worker_identity_only_recovers_with_proven_launch_gate(self):
        execution = quality_execution.start_execution(self.root, "pending", "batch")
        metadata = execution["process"]
        self.assertTrue(metadata["launch_gate"])
        with patch.object(quality_process, "_process_identity", return_value=False):
            self.assertTrue(quality_process.stop_recorded_process(metadata))
            self.assertFalse(quality_process.stop_recorded_process({}))
            self.assertFalse(quality_process.stop_recorded_process({**metadata, "launch_gate": False}))
        with patch.object(quality_process, "_process_identity", return_value=metadata["supervisor_identity"]):
            self.assertFalse(quality_process.stop_recorded_process(metadata))

    def test_child_waiting_at_launch_gate_exits_on_parent_pipe_eof(self):
        process = subprocess.Popen([sys.executable, "-c",
            "import sys; raise SystemExit(0 if sys.stdin.readline() == 'QUALITY_GO\\n' else 2)"], stdin=subprocess.PIPE)
        try:
            process.stdin.close()
            self.assertEqual(process.wait(timeout=5), 2)
        finally:
            if process.poll() is None:
                process.kill();process.wait(timeout=5)

    def test_execution_tracks_legacy_manifest_business_batch(self):
        path = self.root / "system" / "trajectory_tree_runs" / "legacy" / "manifest.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"batch_id": "business-batch"}), encoding="utf-8")
        self.assertEqual(quality_jobs._execution_batch(self.root, "job", "legacy"), "business-batch")

    def test_revoked_and_terminal_progress_cannot_restore_job(self):
        manager = quality_jobs.QualityJobManager(self.root / "jobs", data_root=self.root)
        RecordStore(self.root).put("quality_jobs", "job", {"job_id": "job", "run_id": "batch", "status": "running", "percent": 5})
        execution = quality_execution.start_execution(self.root, "job", "batch")
        quality_execution.revoke_execution(self.root, "job", execution["token"], "timeout")
        manager._progress("job", {"percent": 80}, token=execution["token"])
        self.assertEqual(manager.get("job")["percent"], 5)
        manager._progress("job", {"status": "failed"})
        manager._progress("job", {"status": "running", "percent": 80})
        self.assertEqual(manager.get("job")["status"], "failed")
        manager.shutdown()

    def test_shutdown_cancels_quality_queue_not_shared_executor(self):
        executor = ThreadPoolExecutor(max_workers=1)
        release = threading.Event()
        blocking = executor.submit(release.wait)
        try:
            manager = quality_jobs.QualityJobManager(self.root / "jobs", executor=executor, data_root=self.root)
            result = manager._submit_job("batch", ["task"])
            manager.shutdown()
            self.assertEqual(manager.get(result["job_id"])["status"], "interrupted")
            release.set()
            self.assertEqual(executor.submit(lambda: 42).result(timeout=5), 42)
        finally:
            release.set();executor.shutdown()

    def test_transient_sqlite_lock_during_claim_fails_job_and_releases_worker(self):
        for operation in ("read", "write"):
            with self.subTest(operation=operation), ThreadPoolExecutor(max_workers=1) as executor:
                root = self.root / operation
                runner = Mock(return_value={"tasks": [{"task_id": "task"}]})
                manager = quality_jobs.QualityJobManager(root / "jobs", runner=runner,
                    executor=executor, data_root=root)
                try:
                    for ident in ("locked", "next"):
                        manager.records.put("quality_jobs", ident, {"job_id": ident, "run_id": "batch",
                            "task_ids": ["task"], "status": "queued", "stage": "queued"})
                    original = manager.records.get if operation == "read" else manager.records.put
                    injected = []
                    def transient(*args, **kwargs):
                        target = len(args) > 1 and args[0] == "quality_jobs" and args[1] == "locked"
                        if operation == "write":
                            target = target and len(args) > 2 and args[2].get("status") == "running"
                        if target and not injected:
                            injected.append(True)
                            raise sqlite3.OperationalError("database is locked")
                        return original(*args, **kwargs)
                    with patch.object(manager.records, "get" if operation == "read" else "put", side_effect=transient):
                        executor.submit(manager._run_with_diagnostics, "locked", "batch", ["task"]).result(timeout=5)
                    self.assertEqual(injected, [True])
                    self.assertEqual(manager.get("locked")["status"], "failed")
                    self.assertTrue(manager.get("locked")["failure"])
                    self.assertNotIn("locked", manager._stop_events)
                    runner.assert_not_called()
                    executor.submit(manager._run_with_diagnostics, "next", "batch", ["task"]).result(timeout=5)
                    self.assertEqual(manager.get("next")["status"], "succeeded")
                    self.assertFalse(manager._stop_events)
                    runner.assert_called_once()
                finally:
                    manager.shutdown()

    def test_rejected_duplicate_claim_does_not_drop_active_stop_event(self):
        runner = Mock()
        manager = quality_jobs.QualityJobManager(self.root / "jobs", runner=runner, data_root=self.root)
        try:
            manager.records.put("quality_jobs", "active", {"job_id": "active", "run_id": "batch", "status": "running"})
            active = threading.Event()
            manager._stop_events["active"] = active
            manager._run_with_diagnostics("active", "batch", ["task"])
            self.assertIs(manager._stop_events["active"], active)
            self.assertFalse(active.is_set())
            runner.assert_not_called()
        finally:
            manager.shutdown()

    def test_request_diagnostics_strip_secrets_and_bodies(self):
        safe = quality_jobs._safe_request_frame({"local_request_id": "r", "event": "started",
            "operation": "rubric_repair", "base_url": "https://user:secret@host/api/secret/v1?api_key=secret",
            "api_key": "secret", "raw_response": "secret", "messages": "secret", "timeout_seconds": float("inf")})
        self.assertNotIn("secret", json.dumps(safe))
        self.assertNotIn("timeout_seconds", safe)
        self.assertEqual(safe["base_url"], "https://host/api/_/v1")


if __name__ == "__main__":
    unittest.main()
