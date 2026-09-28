"""Offline state-machine, ownership, exact-job and restart tests."""
from copy import deepcopy
from datetime import datetime, timezone
from unittest.mock import patch
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from tempfile import TemporaryDirectory
import unittest
import uuid

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.data_store import ArtifactStore, RecordStore, RevisionConflict
from backend.batch_lifecycle import active_jobs, lifecycle
from backend.pipeline_access import (PipelineManagedError, active_pipeline, ensure_pipeline_write,
                                     internal_pipeline, submit_with_context)
from backend.pipelines import PipelineManager, PipelineError
from backend.pipeline_router import router


class OfflineRuntime:
    def __init__(self, root):
        self.root, self.records = root, RecordStore(root)
        self.ready_stages, self.children, self.calls = set(), {}, []
        self.config = {s: s + "-v1" for s in ("preprocessing", "tree", "quality", "cot")}
        self.tasks, self.publish_count, self.no_candidates, self.cot_needed = ["A", "B"], 0, False, False
        self.lost_publish_response = False
        self.collection_values = []

    def configurations(self): return dict(self.config)
    def snapshot(self, batch): return {"task_ids": list(self.tasks), "collection_digest": "frozen"}
    def collection_options(self, config): return deepcopy(config)
    def collection_runs(self, batch): return deepcopy(self.collection_values)
    def input_ready(self, p): return not self.collection_values or all(j["status"] == "completed" for j in self.collection_values)
    def ready(self, step, p): return step in self.ready_stages
    def jobs(self, step, p): return deepcopy(self.collection_values if step == "collection" else self.children.get(step, []))

    def dispatch_collection(self, p):
        if self.collection_values:
            return self.collection_values[0]
        job = {"collection_run_id": "cr-test", "job_id": "cr-test", "batch_id": p["batch_id"],
               "dispatch_key": p["pipeline_id"], "status": "running"}
        self.collection_values.append(job)
        self.records.put("collection_runs", "cr-test", job)
        return job

    def submit(self, step, p, retry=False):
        ensure_pipeline_write(p["batch_id"], self.root)
        self.calls.append((step, retry))
        if step == "cot" and not self.cot_needed:
            return None
        job = {"job_id": uuid.uuid4().hex, "batch_id": p["batch_id"], "pipeline_id": p["pipeline_id"],
               "status": "queued", "percent": 0}
        self.children.setdefault(step, []).append(job)
        self.records.put({"tree": "tree_jobs", "quality": "quality_jobs", "preprocessing": "preprocessing_jobs",
                          "cot": "correction_cot_jobs"}[step], job["job_id"], job)
        return deepcopy(job)

    def complete(self, step, *, fail=False, failure=None):
        job = self.children[step][-1]
        job.update(status="failed" if fail else "succeeded", percent=100, error="mock failure" if fail else None)
        if failure is not None:
            job["failure"] = failure
        self.records.put({"tree": "tree_jobs", "quality": "quality_jobs", "preprocessing": "preprocessing_jobs",
                          "cot": "correction_cot_jobs"}[step], job["job_id"], job)
        if not fail:
            self.ready_stages.add(step)

    def selection(self, p):
        return {"selected_trajectories": {} if self.no_candidates else {"A": "a-top", "B": "b-top"}, "excluded": []}

    def prepare_manual(self, p):
        return {"session_id": "manual-session", "selection": p["selection"], "storage_revision": 4}

    def confirm(self, p, revision):
        if revision != 4:
            raise RevisionConflict("修改已变化")
        return {"selection": p["selection"], "group_ids": ["a", "b"], "session_id": p["session_id"]}

    def publish(self, p):
        ensure_pipeline_write(p["batch_id"], self.root, action="publish")
        if active_jobs(p["batch_id"], self.root):
            raise PipelineError("active child")
        self.publish_count += 1
        release = {"release_id": "release-test", "batch_ids": [p["batch_id"]], "pipeline_id": p["pipeline_id"]}
        self.records.put_many([
            {"namespace": "dataset_releases", "key": release["release_id"], "payload": release},
            {"namespace": "batch_lifecycle", "key": p["batch_id"], "payload": {"batch_id": p["batch_id"],
             "status": "published", "release_id": release["release_id"], "published_at": "now"}}])
        if self.lost_publish_response:
            raise OSError("commit succeeded but response was lost")
        return release

    def conversion(self, release_id, retry=False):
        value = self.records.get("training_overview_conversions", release_id)
        if retry or not value:
            value = self.records.put("training_overview_conversions", release_id, {"status": "running", "release_id": release_id})
        return value


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.runtime = OfflineRuntime(self.root)
        self.manager = PipelineManager(self.runtime, self.root)

    def create(self, mode="automatic", **changes):
        return self.manager.create({"request_id": "request-test", "name": "流程测试", "batch_id": "batch-test",
            "mode": mode, "start_mode": "existing", "threshold": 4, **changes})

    def tick(self, p, count=1):
        for _ in range(count): self.manager.tick(p["pipeline_id"])
        return self.manager.get(p["pipeline_id"])

    def ready(self, mode="automatic"):
        self.runtime.ready_stages.update({"preprocessing", "tree", "quality"})
        return self.create(mode)

    def control(self, p, action, **kwargs):
        p = self.manager.get(p["pipeline_id"])
        return self.manager.control(p["pipeline_id"], action, p["storage_revision"], **kwargs)

    def test_automatic_flow_closes_and_retains_history(self):
        p = self.tick(self.ready(), 8)
        self.assertEqual(p["release_id"], "release-test")
        self.assertEqual(self.runtime.publish_count, 1)
        self.assertEqual(lifecycle("batch-test", self.root)["status"], "published")
        self.runtime.records.put("training_overview_conversions", "release-test", {"status": "succeeded"})
        p = self.tick(p)
        self.assertEqual(p["status"], "succeeded")
        self.assertIsNone(active_pipeline("batch-test", self.root))
        self.assertEqual(len(self.manager.list()), 1)

    def test_manual_wait_confirmation_revision_and_unchanged_top1(self):
        p = self.tick(self.ready("manual"), 5)
        self.assertEqual(p["status"], "waiting_for_correction")
        self.assertEqual(p["session_id"], "manual-session")
        ensure_pipeline_write("batch-test", self.root, action="correction", session_id="manual-session")
        with self.assertRaises(PipelineManagedError): ensure_pipeline_write("batch-test", self.root, action="export")
        with self.assertRaises(RevisionConflict): self.control(p, "confirm-correction", session_revision=3)
        p = self.control(p, "confirm-correction", session_revision=4)
        with self.assertRaises(PipelineManagedError):
            ensure_pipeline_write("batch-test", self.root, action="correction", session_id="manual-session")
        p = self.tick(p, 3)
        self.assertEqual(p["release_id"], "release-test")
        self.assertEqual(p["steps"][5]["status"], "skipped")

    def test_pause_drains_child_without_dispatching_next_and_revision_stays_stable(self):
        p = self.tick(self.create(), 2)
        revision = p["storage_revision"]
        self.assertEqual(self.tick(p)["storage_revision"], revision)
        p = self.control(p, "pause")
        self.runtime.complete("preprocessing")
        self.assertEqual(self.tick(p, 3)["status"], "paused")
        self.assertEqual(self.runtime.calls, [("preprocessing", False)])
        p = self.control(p, "resume")
        self.assertEqual(self.tick(p)["steps"][1]["status"], "succeeded")

    def test_failed_job_retry_does_not_keep_failed_attempt_as_active_binding(self):
        p = self.tick(self.create(), 2)
        old = p["steps"][1]["job_ids"][0]
        self.runtime.complete("preprocessing", fail=True)
        p = self.tick(p)
        self.assertEqual(p["status"], "failed")
        p = self.tick(self.control(p, "retry"))
        self.assertNotIn(old, p["steps"][1]["job_ids"])
        self.assertIn(old, p["steps"][1]["previous_job_ids"])
        self.runtime.complete("preprocessing")
        self.assertEqual(self.tick(p)["steps"][1]["status"], "succeeded")

    def test_transient_job_retries_twice_and_persists_schedule_across_restart(self):
        p = self.tick(self.create(), 2)
        self.assertEqual(p["retry_policy"], {"version": 1, "max_attempts": 3, "delays_seconds": [30, 120]})
        self.assertEqual(p["steps"][1]["retry_info"]["attempts"], 1)
        transient = {"category": "server_error", "http_status": 500, "retryable": True,
                     "request_id": "req-500", "message": "provider body is never copied"}
        self.runtime.complete("preprocessing", fail=True, failure=transient)
        before = datetime.now(timezone.utc)
        p = self.tick(p)
        info = p["steps"][1]["retry_info"]
        self.assertEqual(p["status"], "retry_waiting")
        self.assertEqual(info["attempts"], 1)
        self.assertEqual(info["max_attempts"], 3)
        self.assertIn("HTTP 500", info["last_error"])
        self.assertNotIn("provider body", info["last_error"])
        first_due = datetime.fromisoformat(info["next_retry_at"].replace("Z", "+00:00"))
        self.assertLess(abs((first_due - before).total_seconds() - 30), 3)
        self.assertEqual(len(self.runtime.calls), 1)
        self.manager = PipelineManager(self.runtime, self.root)
        self.assertEqual(self.tick(p)["status"], "retry_waiting")
        self.assertEqual(len(self.runtime.calls), 1)
        with patch("backend.pipelines._retry_due", return_value=True):
            p = self.tick(p)
        self.assertEqual(p["steps"][1]["retry_info"]["attempts"], 2)
        self.assertEqual(len(self.runtime.calls), 2)
        self.runtime.complete("preprocessing", fail=True, failure=transient)
        before = datetime.now(timezone.utc)
        p = self.tick(p)
        second_due = datetime.fromisoformat(p["steps"][1]["retry_info"]["next_retry_at"].replace("Z", "+00:00"))
        self.assertLess(abs((second_due - before).total_seconds() - 120), 3)
        with patch("backend.pipelines._retry_due", return_value=True):
            p = self.tick(p)
        self.assertEqual(p["steps"][1]["retry_info"]["attempts"], 3)
        self.assertEqual(len(self.runtime.calls), 3)
        self.runtime.complete("preprocessing", fail=True, failure=transient)
        p = self.tick(p, 2)
        self.assertEqual(p["status"], "failed")
        self.assertIsNone(p["steps"][1]["retry_info"]["next_retry_at"])
        self.assertEqual(len(self.runtime.calls), 3)
        p = self.tick(self.control(p, "retry"))
        self.assertEqual(p["steps"][1]["retry_info"]["attempts"], 1)
        self.assertEqual(len(self.runtime.calls), 4)

    def test_concurrent_due_polls_submit_only_one_retry_job(self):
        p = self.tick(self.create(), 2)
        self.runtime.complete("preprocessing", fail=True, failure={"category": "server_error", "http_status": 502})
        p = self.tick(p)
        self.assertEqual(p["status"], "retry_waiting")
        with patch("backend.pipelines._retry_due", return_value=True), ThreadPoolExecutor(max_workers=2) as executor:
            list(executor.map(self.manager.tick, [p["pipeline_id"], p["pipeline_id"]]))
        p = self.manager.get(p["pipeline_id"])
        self.assertEqual(p["steps"][1]["retry_info"]["attempts"], 2)
        self.assertEqual(len(self.runtime.calls), 2)
        self.assertEqual(len(p["steps"][1]["job_ids"]), 1)

    def test_pause_and_terminate_stop_scheduled_retry(self):
        p = self.tick(self.create(), 2)
        self.runtime.complete("preprocessing", fail=True, failure={"category": "timeout"})
        p = self.tick(p)
        self.assertEqual(p["status"], "retry_waiting")
        p = self.control(p, "pause")
        with patch("backend.pipelines._retry_due", return_value=True):
            self.assertEqual(self.tick(p)["status"], "paused")
        self.assertEqual(len(self.runtime.calls), 1)
        p = self.control(p, "resume")
        with patch("backend.pipelines._retry_due", return_value=True):
            p = self.tick(p)
        self.assertEqual(len(self.runtime.calls), 2)
        self.runtime.complete("preprocessing", fail=True, failure={"category": "connection"})
        p = self.tick(p)
        self.assertEqual(p["status"], "retry_waiting")
        p = self.control(p, "terminate")
        self.assertEqual(self.tick(p)["status"], "terminated")
        self.assertEqual(len(self.runtime.calls), 2)

    def test_quota_stale_and_unknown_failure_never_auto_retry(self):
        for failure, status in (({"category": "quota", "http_status": 403}, "failed"),
                                ({"category": "server_error", "http_status": 500}, "stale"),
                                (None, "failed")):
            with self.subTest(failure=failure, status=status), TemporaryDirectory() as directory:
                root = Path(directory)
                runtime = OfflineRuntime(root)
                manager = PipelineManager(runtime, root)
                p = manager.create({"request_id": "case", "name": "case", "batch_id": "batch-case",
                                    "mode": "automatic", "start_mode": "existing", "threshold": 4})
                manager.tick(p["pipeline_id"])
                manager.tick(p["pipeline_id"])
                job = runtime.children["preprocessing"][-1]
                job.update(status=status, error="opaque exit 1", failure=failure)
                manager.tick(p["pipeline_id"])
                result = manager.get(p["pipeline_id"])
                self.assertEqual(result["status"], "failed")
                if status == "stale":
                    self.assertIn("输入已变化", result["error"])
                self.assertEqual(len(runtime.calls), 1)
                self.assertIsNone(result["steps"][1]["retry_info"]["next_retry_at"])

    def test_old_pipeline_without_policy_keeps_manual_retry_behavior(self):
        p = self.tick(self.create(), 2)
        stored = self.manager.get(p["pipeline_id"], public=False)
        stored.pop("retry_policy")
        for step in stored["steps"]:
            step.pop("retry_info", None)
        self.manager._save(stored)
        self.runtime.complete("preprocessing", fail=True, failure={"category": "server_error", "http_status": 500})
        result = self.tick(p)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(len(self.runtime.calls), 1)

    def test_collection_lost_ack_uses_bound_run_without_restarting_phone(self):
        options = {"phone_id": "phone-a", "app": "App", "vla": "http://mock.invalid", "config": {}}
        p = self.create(start_mode="collect", collection_config=options)
        original = self.runtime.dispatch_collection
        calls = []

        def lost_ack(pipeline):
            calls.append(1)
            original(pipeline)
            raise TimeoutError("collector acknowledgement was lost")

        self.runtime.dispatch_collection = lost_ack
        p = self.tick(p)
        self.assertEqual(p["status"], "retry_waiting")
        self.assertEqual(len(p["collection_run_ids"]), 1)
        with patch("backend.pipelines._retry_due", return_value=True):
            p = self.tick(p)
        self.assertEqual(len(calls), 1)
        self.assertEqual(p["status"], "running")
        self.runtime.collection_values[0]["status"] = "completed"
        self.assertEqual(self.tick(p)["steps"][0]["status"], "succeeded")

    def test_collection_communication_failure_exhausts_three_attempts(self):
        options = {"phone_id": "phone-a", "app": "App", "vla": "http://mock.invalid", "config": {}}
        p = self.create(start_mode="collect", collection_config=options)
        original = self.runtime.dispatch_collection
        calls = []

        def always_lose_ack(pipeline):
            calls.append(pipeline["pipeline_id"])
            if not self.runtime.collection_values:
                original(pipeline)
            self.runtime.collection_values[0].update(status="failed", dispatch_error="response lost")
            raise TimeoutError("collector acknowledgement lost")

        self.runtime.dispatch_collection = always_lose_ack
        p = self.tick(p)
        self.assertEqual((p["status"], p["steps"][0]["retry_info"]["attempts"]), ("retry_waiting", 1))
        run_id = p["collection_run_ids"][0]
        with patch("backend.pipelines._retry_due", return_value=True):
            p = self.tick(p)
        self.assertEqual((p["status"], p["steps"][0]["retry_info"]["attempts"]), ("retry_waiting", 2))
        with patch("backend.pipelines._retry_due", return_value=True):
            p = self.tick(p)
        self.assertEqual((p["status"], p["steps"][0]["retry_info"]["attempts"]), ("failed", 3))
        self.assertIsNone(p["steps"][0]["retry_info"]["next_retry_at"])
        self.assertEqual(calls, [p["pipeline_id"]] * 3)
        self.assertEqual(p["collection_run_ids"], [run_id])
        self.assertEqual(len(self.runtime.collection_values), 1)

    def test_collection_failed_ack_reconciles_same_run_identity(self):
        options = {"phone_id": "phone-a", "app": "App", "vla": "http://mock.invalid", "config": {}}
        p = self.create(start_mode="collect", collection_config=options)
        original = self.runtime.dispatch_collection
        calls = []

        def recover_by_same_request(pipeline):
            calls.append(pipeline["pipeline_id"])
            if len(calls) == 1:
                original(pipeline)
                self.runtime.collection_values[0].update(status="failed", dispatch_error="response lost")
                raise TimeoutError("collector acknowledgement was lost")
            run = self.runtime.collection_values[0]
            run.update(status="running", dispatch_error=None)
            return run

        self.runtime.dispatch_collection = recover_by_same_request
        p = self.tick(p)
        self.assertEqual(p["status"], "retry_waiting")
        run_id = p["collection_run_ids"][0]
        with patch("backend.pipelines._retry_due", return_value=True):
            p = self.tick(p)
        self.assertEqual(calls, [p["pipeline_id"], p["pipeline_id"]])
        self.assertEqual(p["collection_run_ids"], [run_id])
        self.assertEqual(len(self.runtime.collection_values), 1)
        self.assertEqual(p["steps"][0]["retry_info"]["attempts"], 2)
        self.runtime.collection_values[0]["status"] = "completed"
        self.assertEqual(self.tick(p)["steps"][0]["status"], "succeeded")

    def test_late_valid_result_cancels_scheduled_retry_before_due(self):
        p = self.tick(self.create(), 2)
        self.runtime.complete("preprocessing", fail=True, failure={"category": "server_error", "http_status": 503})
        p = self.tick(p)
        self.assertEqual(p["status"], "retry_waiting")
        self.runtime.ready_stages.add("preprocessing")
        p = self.tick(p)
        self.assertEqual(p["status"], "running")
        self.assertEqual(p["steps"][1]["status"], "succeeded")
        self.assertEqual(len(self.runtime.calls), 1)

    def test_publish_lost_ack_during_retry_wait_reconciles_without_republish(self):
        self.runtime.lost_publish_response = True
        original = self.runtime.publish

        def transient_lost_ack(pipeline):
            try:
                return original(pipeline)
            except OSError as exc:
                raise TimeoutError("publication acknowledgement lost") from exc

        self.runtime.publish = transient_lost_ack
        p = self.tick(self.ready(), 7)
        self.assertEqual(p["status"], "retry_waiting")
        self.assertEqual(self.runtime.publish_count, 1)
        p = self.tick(p)
        self.assertEqual(p["current_step"], "overview")
        self.assertEqual(p["status"], "running")
        self.assertEqual(self.runtime.publish_count, 1)
        self.assertIsNone(p["steps"][-2]["retry_info"]["next_retry_at"])

    def test_overview_retry_uses_registered_release_and_preserves_summary_failure(self):
        p = self.tick(self.ready(), 8)
        self.runtime.records.put("training_overview_conversions", "release-test", {
            "status": "failed", "error": "provider body", "failure": {"category": "server_error", "http_status": 503}})
        p = self.tick(p)
        self.assertEqual(p["status"], "retry_waiting")
        self.assertEqual(self.runtime.publish_count, 1)
        with patch("backend.pipelines._retry_due", return_value=True):
            p = self.tick(p)
        self.assertEqual(p["status"], "running")
        self.assertEqual(self.runtime.publish_count, 1)
        self.runtime.records.put("training_overview_conversions", "release-test", {"status": "succeeded"})
        self.assertEqual(self.tick(p)["status"], "succeeded")

    def test_pause_and_terminate_after_publish_stop_overview_retries(self):
        p = self.tick(self.ready(), 8)
        self.assertEqual(p["current_step"], "overview")
        p = self.control(p, "pause")
        self.runtime.records.put("training_overview_conversions", "release-test", {
            "status": "failed", "failure": {"category": "server_error", "http_status": 503}})
        self.assertEqual(self.tick(p)["status"], "paused")
        p = self.control(p, "resume")
        p = self.tick(p)
        self.assertEqual(p["status"], "retry_waiting")
        p = self.control(p, "terminate")
        self.assertEqual(p["status"], "terminated")
        with patch("backend.pipelines._retry_due", return_value=True):
            self.assertEqual(self.tick(p)["status"], "terminated")
        self.assertEqual(self.runtime.publish_count, 1)
        self.assertEqual(self.runtime.records.get("training_overview_conversions", "release-test")["status"], "failed")

    def test_terminate_after_uncertain_publication_records_release_without_overview(self):
        self.runtime.lost_publish_response = True
        original = self.runtime.publish

        def transient_lost_ack(pipeline):
            try:
                return original(pipeline)
            except OSError as exc:
                raise TimeoutError("publication acknowledgement lost") from exc

        self.runtime.publish = transient_lost_ack
        p = self.tick(self.ready(), 7)
        self.assertEqual(p["status"], "retry_waiting")
        p = self.control(p, "terminate")
        p = self.tick(p)
        self.assertEqual(p["status"], "terminated")
        self.assertEqual(p["release_id"], "release-test")
        self.assertEqual(self.runtime.publish_count, 1)
        self.assertIsNone(self.runtime.records.get("training_overview_conversions", "release-test"))

    def test_restart_after_job_dispatch_intent_does_not_count_a_second_attempt(self):
        p = self.tick(self.create())
        stored = self.manager.get(p["pipeline_id"], public=False)
        step = stored["steps"][1]
        step.update(status="running", started_at="frozen", retry_info={"attempts": 1,
                    "max_attempts": 3, "last_error": None, "next_retry_at": None})
        self.manager._save(stored)
        self.manager = PipelineManager(self.runtime, self.root)
        p = self.tick(p)
        self.assertEqual(p["steps"][1]["retry_info"]["attempts"], 1)
        self.assertEqual(len(self.runtime.calls), 1)
        self.assertEqual(len(p["steps"][1]["job_ids"]), 1)

    def test_restart_adopts_unbound_child_job_without_duplicate_submit(self):
        p = self.tick(self.create(), 2)
        child_id = p["steps"][1]["job_ids"][0]
        stored = self.manager.get(p["pipeline_id"], public=False)
        stored["steps"][1]["job_ids"] = []
        self.manager._save(stored)
        self.manager = PipelineManager(self.runtime, self.root)
        p = self.tick(p)
        self.assertEqual(p["steps"][1]["job_ids"], [child_id])
        self.assertEqual(p["steps"][1]["retry_info"]["attempts"], 1)
        self.assertEqual(len(self.runtime.calls), 1)

    def test_restart_after_collection_and_publication_intents_preserves_attempts(self):
        options = {"phone_id": "phone-a", "app": "App", "vla": "http://mock.invalid", "config": {}}
        p = self.create(start_mode="collect", collection_config=options)
        stored = self.manager.get(p["pipeline_id"], public=False)
        stored["steps"][0].update(status="running", retry_info={"attempts": 1,
            "max_attempts": 3, "last_error": None, "next_retry_at": None})
        self.manager._save(stored)
        self.manager = PipelineManager(self.runtime, self.root)
        p = self.tick(p)
        self.assertEqual(p["steps"][0]["retry_info"]["attempts"], 1)
        self.assertEqual(len(self.runtime.collection_values), 1)

        with TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = OfflineRuntime(root)
            runtime.ready_stages.update({"preprocessing", "tree", "quality"})
            manager = PipelineManager(runtime, root)
            other = manager.create({"request_id": "other", "name": "other", "batch_id": "batch-other",
                                    "mode": "automatic", "start_mode": "existing", "threshold": 4})
            for _ in range(6):
                manager.tick(other["pipeline_id"])
            stored = manager.get(other["pipeline_id"], public=False)
            stored["steps"][6].update(status="running", retry_info={"attempts": 1,
                "max_attempts": 3, "last_error": None, "next_retry_at": None})
            manager._save(stored)
            manager = PipelineManager(runtime, root)
            manager.tick(other["pipeline_id"])
            result = manager.get(other["pipeline_id"])
            self.assertEqual(result["steps"][6]["retry_info"]["attempts"], 1)
            self.assertEqual(runtime.publish_count, 1)

    def test_restart_binds_existing_collection_run_before_remote_dispatch(self):
        options = {"phone_id": "phone-a", "app": "App", "vla": "http://mock.invalid", "config": {}}
        p = self.create(start_mode="collect", collection_config=options)
        existing = self.runtime.dispatch_collection(p)
        stored = self.manager.get(p["pipeline_id"], public=False)
        stored["steps"][0].update(status="running", retry_info={"attempts": 1,
            "max_attempts": 3, "last_error": None, "next_retry_at": None})
        self.manager._save(stored)
        self.manager = PipelineManager(self.runtime, self.root)
        with patch.object(self.runtime, "dispatch_collection", wraps=self.runtime.dispatch_collection) as dispatch:
            p = self.tick(p)
            dispatch.assert_not_called()
        self.assertEqual(p["collection_run_ids"], [existing["collection_run_id"]])
        self.assertEqual(p["steps"][0]["retry_info"]["attempts"], 1)

    def test_restart_reads_persisted_exact_job_without_repeating_submit(self):
        p = self.tick(self.create(), 2)
        ids = p["steps"][1]["job_ids"]
        self.manager = PipelineManager(self.runtime, self.root)
        self.assertEqual(self.tick(p)["steps"][1]["job_ids"], ids)
        self.assertEqual(len(self.runtime.calls), 1)
        self.runtime.complete("preprocessing")
        self.assertEqual(self.tick(p)["steps"][1]["status"], "succeeded")

    def test_no_candidates_does_not_publish_or_close_batch(self):
        self.runtime.no_candidates = True
        p = self.tick(self.ready(), 6)
        self.assertEqual(p["status"], "no_publishable_data")
        self.assertEqual(self.runtime.publish_count, 0)
        self.assertEqual(lifecycle("batch-test", self.root)["status"], "active")
        ensure_pipeline_write("batch-test", self.root)

    def test_terminate_waits_child_and_then_releases_ownership(self):
        p = self.control(self.tick(self.create(), 2), "terminate")
        self.assertEqual(self.tick(p)["status"], "terminating")
        with self.assertRaises(PipelineManagedError): ensure_pipeline_write("batch-test", self.root)
        self.runtime.complete("preprocessing")
        self.assertEqual(self.tick(p)["status"], "terminated")
        ensure_pipeline_write("batch-test", self.root)
        self.assertEqual(len(self.runtime.calls), 1)

    def test_duplicate_and_concurrent_creation_are_one_pipeline(self):
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: self.create(), range(2)))
        self.assertEqual(results[0]["pipeline_id"], results[1]["pipeline_id"])
        self.assertEqual(len(self.manager.list()), 1)
        with self.assertRaises(PipelineError): self.create(name="changed")
        with self.assertRaises(PipelineError): self.create(request_id="new-request")

    def test_changed_configuration_and_task_scope_block_submission(self):
        p = self.tick(self.create())
        self.runtime.config["preprocessing"] = "changed"
        self.assertEqual(self.tick(p)["status"], "failed")
        self.assertFalse(self.runtime.calls)

    def test_revision_conflict_does_not_apply_control(self):
        p = self.create()
        self.tick(p)
        with self.assertRaises(RevisionConflict): self.manager.control(p["pipeline_id"], "pause", p["storage_revision"])
        self.assertEqual(self.manager.get(p["pipeline_id"])["status"], "running")

    def test_uncertain_publish_response_recovers_registered_release_once(self):
        self.runtime.lost_publish_response = True
        p = self.tick(self.ready(), 7)
        self.assertEqual(p["status"], "failed")
        p = self.tick(p)
        self.assertEqual(p["release_id"], "release-test")
        self.assertEqual(self.runtime.publish_count, 1)
        self.assertEqual(p["current_step"], "overview")

    def test_conversion_failure_retry_never_republishes(self):
        p = self.tick(self.ready(), 8)
        self.runtime.records.put("training_overview_conversions", "release-test", {"status": "failed", "error": "mock conversion error"})
        p = self.tick(p)
        self.assertEqual(p["status"], "published_summary_failed")
        self.assertEqual(self.control(p, "retry")["status"], "running")
        self.assertEqual(self.runtime.publish_count, 1)

    def test_cot_is_a_real_bound_job_before_publication(self):
        self.runtime.cot_needed = True
        p = self.tick(self.ready("manual"), 5)
        p = self.tick(self.control(p, "confirm-correction", session_revision=4))
        self.assertEqual(p["steps"][5]["status"], "running")
        self.assertEqual(len(p["steps"][5]["job_ids"]), 1)
        self.assertEqual(self.runtime.publish_count, 0)
        self.runtime.complete("cot")
        p = self.tick(p, 2)
        self.assertEqual(p["release_id"], "release-test")

    def test_controller_is_not_a_publication_blocker_but_external_writes_are_blocked(self):
        p = self.create()
        self.assertEqual(active_jobs("batch-test", self.root), [])
        with self.assertRaises(PipelineManagedError): ensure_pipeline_write("batch-test", self.root)
        with internal_pipeline("wrong"):
            with self.assertRaises(PipelineManagedError): ensure_pipeline_write("batch-test", self.root)
        with ThreadPoolExecutor(max_workers=1) as executor, internal_pipeline(p["pipeline_id"]):
            submit_with_context(executor, ensure_pipeline_write, "batch-test", self.root).result()

    def test_http_get_never_creates_job_and_control_requires_revision(self):
        app = FastAPI()
        app.state.pipelines = self.manager
        app.include_router(router)
        with TestClient(app) as client:
            self.assertEqual(client.get("/api/pipelines").json(), {"pipelines": []})
            p = self.create()
            self.assertEqual(client.get("/api/pipelines/" + p["pipeline_id"]).status_code, 200)
            self.assertEqual(self.runtime.calls, [])
            self.assertEqual(client.post("/api/pipelines/" + p["pipeline_id"] + "/pause", json={}).status_code, 422)
            response = client.post("/api/pipelines/" + p["pipeline_id"] + "/pause", json={"expected_revision": p["storage_revision"]})
            self.assertEqual(response.status_code, 200)

    def test_conversion_retried_in_release_details_updates_pipeline_too(self):
        p = self.tick(self.ready(), 8)
        self.runtime.records.put("training_overview_conversions", "release-test", {"status": "failed", "error": "failed"})
        p = self.tick(p)
        self.assertEqual(p["status"], "published_summary_failed")
        self.runtime.records.put("training_overview_conversions", "release-test", {"status": "succeeded"})
        self.assertEqual(self.tick(p)["status"], "succeeded")
        self.assertEqual(self.runtime.publish_count, 1)

    def test_unknown_collector_acknowledgement_keeps_ownership_while_terminating(self):
        p = self.create()
        self.runtime.collection_values = [{"collection_run_id": "uncertain", "status": "failed",
                                           "dispatch_attempted": True, "dispatch_error": "response lost"}]
        p = self.control(p, "terminate")
        self.assertEqual(self.tick(p)["status"], "terminating")
        self.runtime.collection_values[0].update(remote_status="failed", transfer_status="remote_failed")
        self.assertEqual(self.tick(p)["status"], "terminated")

    def test_tasks_changed_after_creation_do_not_start_computation(self):
        p = self.create()
        self.runtime.tasks.append("C")
        self.assertEqual(self.tick(p)["status"], "failed")
        self.assertEqual(self.runtime.calls, [])

    def test_successful_job_without_full_task_results_does_not_loop_or_publish(self):
        p = self.tick(self.create(), 2)
        self.runtime.complete("preprocessing")
        self.runtime.ready_stages.clear()
        p = self.tick(p, 3)
        self.assertEqual(p["status"], "failed")
        self.assertEqual(len(self.runtime.calls), 1)

    def test_all_bound_subjobs_must_finish_before_step_completes(self):
        p = self.tick(self.create(), 2)
        with internal_pipeline(p["pipeline_id"]):
            other = self.runtime.submit("preprocessing", p)
        saved = self.manager.get(p["pipeline_id"], public=False)
        saved["steps"][1]["job_ids"].append(other["job_id"])
        self.manager._save(saved)
        self.runtime.complete("preprocessing")
        self.assertEqual(self.tick(p)["steps"][1]["status"], "running")
        self.runtime.children["preprocessing"][0]["status"] = "succeeded"
        self.assertEqual(self.tick(p)["steps"][1]["status"], "succeeded")

    def test_collection_waits_for_completion_and_preserves_frozen_configuration(self):
        options = {"phone_id": "phone-a", "app": "App", "vla": "http://mock.invalid", "config": {"temperature": .5}}
        p = self.create(start_mode="collect", collection_config=options)
        p = self.tick(p, 3)
        self.assertEqual(p["steps"][0]["status"], "running")
        self.assertEqual(p["collection_config"], options)
        self.assertFalse(self.runtime.calls)
        self.runtime.collection_values[0]["status"] = "completed"
        self.assertEqual(self.tick(p)["steps"][0]["status"], "succeeded")

    def test_conversion_polling_does_not_change_revision_or_publication_timestamp(self):
        p = self.tick(self.ready(), 8)
        revision, published = p["storage_revision"], p["steps"][-2]["completed_at"]
        p = self.tick(p, 3)
        self.assertEqual(p["storage_revision"], revision)
        self.assertEqual(p["steps"][-2]["completed_at"], published)
        self.runtime.records.put("training_overview_conversions", "release-test", {"status": "failed", "error": "failed"})
        p = self.tick(p)
        revision = p["storage_revision"]
        self.assertEqual(self.tick(p, 3)["storage_revision"], revision)

    def test_missing_bound_collection_cannot_be_replaced_with_old_annotation(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        from backend.pipelines import PipelineRuntime
        sources = SimpleNamespace(list_runs=lambda _batch: [], ready_input=Mock())
        runtime = PipelineRuntime(self.root, preprocessing=None, tree=None, quality=None, cot=None,
                                  factory=SimpleNamespace(collection_runs=sources), overview=None)
        with self.assertRaisesRegex(PipelineError, "运行记录缺失"):
            runtime.input_ready({"batch_id": "batch-test", "start_mode": "collect", "collection_run_ids": ["lost"], "task_ids": ["A"]})
        sources.ready_input.assert_not_called()

    def test_managed_batch_blocks_real_submission_services_and_cli(self):
        from backend.preprocessing_jobs import PreprocessingJobManager
        from backend.tree_build_jobs import TreeBuildJobManager
        from backend.quality_jobs import QualityJobManager
        from backend.collection_runs import CollectionRunStore
        from backend.batch_operations import batch_operation
        from backend.trajectory_data import update_action_bbox
        from backend.tests.test_preprocessing_jobs import ManualQueue
        queue = ManualQueue()
        preprocess = PreprocessingJobManager(self.root, executor=queue)
        tree = TreeBuildJobManager(self.root / "tree_jobs", executor=queue, data_root=self.root)
        quality = QualityJobManager(self.root / "quality_jobs", executor=queue, data_root=self.root)
        self.create()
        for call in (lambda: preprocess.submit("batch-test"),
                     lambda: tree.submit(["A"], batch_id="batch-test"),
                     lambda: quality.submit(task_ids=["A"], batch_id="batch-test"),
                     lambda: CollectionRunStore(self.root).create("batch-test"),
                     lambda: update_action_bbox("A", "t", 1, 2, (0, 0, 5, 5), batch_id="batch-test", expected_annotation_version="1", data_root=self.root)):
            with self.assertRaises(PipelineManagedError): call()
        with self.assertRaises(PipelineManagedError), batch_operation("batch-test", "cli", self.root):
            self.fail("unreachable")
        self.assertFalse(queue.calls)
        self.assertEqual(active_jobs("batch-test", self.root), [])


if __name__ == "__main__":
    unittest.main()
