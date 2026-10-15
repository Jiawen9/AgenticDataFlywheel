"""Execution fencing tests use only temporary stores and mocked models."""
from __future__ import annotations

import asyncio
import contextlib
import io
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from backend.data_store import ArtifactStore, RecordStore
from backend.quality_execution import (
    NAMESPACE, QualityExecutionBlocked, QualityExecutionRevoked, check_execution,
    committed_result, execution_context, execution_write_guard, get_execution,
    revoke_execution, start_execution, update_process,
    update_job_progress,
)


class QualityExecutionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        rubric = Path(__file__).resolve().parents[1] / "DevelopRubrics"
        with patch.object(sys, "path", [str(rubric), *sys.path]):
            from backend.DevelopRubrics import quality_job_runner
        cls.worker = quality_job_runner

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = ArtifactStore(self.root)
        self.lease = start_execution(self.root, "job", "batch")
        self.token = self.lease["token"]

    def context(self):
        return execution_context(self.root, "job", self.token)

    def test_active_revoked_and_unconfirmed_processes_block_new_dispatch(self):
        with self.assertRaises(QualityExecutionBlocked):
            start_execution(self.root, "other", "batch")
        update_process(self.root, "job", self.token, {"pid": 123, "exited": False})
        revoke_execution(self.root, "job", self.token, "timeout")
        with self.assertRaises(QualityExecutionBlocked):
            start_execution(self.root, "other", "batch")
        update_process(self.root, "job", self.token, {"exited": True, "cleanup_failed": True})
        with self.assertRaises(QualityExecutionBlocked):
            start_execution(self.root, "other", "batch")
        update_process(self.root, "job", self.token, {"exited": True, "cleanup_failed": False})
        replacement = start_execution(self.root, "job", "batch")
        self.assertNotEqual(replacement["token"], self.token)
        with self.assertRaises(QualityExecutionRevoked):
            check_execution(self.root, "job", self.token)

    def test_managed_job_cannot_drop_its_token(self):
        with self.assertRaises(QualityExecutionRevoked), execution_context(self.root, "job", None):
            pass
        with self.assertRaises(QualityExecutionRevoked), execution_write_guard(self.root, "job"):
            pass
        with execution_context(self.root, "standalone", None), execution_write_guard():
            pass

    def test_worker_needs_explicit_launch_frame_and_durable_matching_pid(self):
        for frame in ("", "GO\n", "QUALITY_GO\n"):
            with self.subTest(frame=frame), patch.object(self.worker, "DATA_ROOT", self.root), \
                 patch.object(sys, "stdin", io.StringIO(frame)), self.assertRaises(QualityExecutionRevoked):
                self.worker._accept_launch("job", self.token)
        update_process(self.root, "job", self.token, {"pid": os.getpid(), "launch_released": True})
        with patch.object(self.worker, "DATA_ROOT", self.root), patch.object(sys, "stdin", io.StringIO("QUALITY_GO\n")):
            self.worker._accept_launch("job", self.token)
        revoke_execution(self.root, "job", self.token, "parent_exited")
        with patch.object(self.worker, "DATA_ROOT", self.root), \
             patch.object(sys, "stdin", io.StringIO("QUALITY_GO\n")), self.assertRaises(QualityExecutionRevoked):
            self.worker._accept_launch("job", self.token)

    def test_protocol_frame_survives_a_partial_legacy_log_line(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            print("legacy progress without newline", end="")
            self.worker._frame("HEARTBEAT", {"at": "mock-time"})
            self.worker._frame("REQUEST", {"local_request_id": "request-one", "event": "started"})
        frames = [line for line in output.getvalue().splitlines() if line.startswith(("HEARTBEAT ", "REQUEST "))]
        self.assertEqual(len(frames), 2)
        self.assertEqual(json.loads(frames[1].split(" ", 1)[1])["event"], "started")

    def test_terminal_job_cannot_write_with_an_unrevoked_lease(self):
        RecordStore(self.root).put("quality_jobs", "job", {"job_id": "job", "status": "interrupted"})
        with self.assertRaises(QualityExecutionRevoked), self.context():
            pass
        self.assertIsNone(update_job_progress(self.root, "job", self.token, {"percent": 90}))
        self.assertNotIn("percent", RecordStore(self.root).get("quality_jobs", "job"))

    def test_progress_checks_token_without_waiting_for_child_file_lock(self):
        RecordStore(self.root).put("quality_jobs", "job", {"job_id": "job", "status": "running"})
        completed = threading.Event()
        def update():
            update_job_progress(self.root, "job", self.token, {"percent": 30})
            completed.set()
        with self.store.batch_lock("batch"):
            thread = threading.Thread(target=update)
            thread.start()
            self.assertTrue(completed.wait(2))
        thread.join(2)
        revoke_execution(self.root, "job", self.token, "timeout")
        self.assertIsNone(update_job_progress(self.root, "job", self.token, {"percent": 80}))
        self.assertEqual(RecordStore(self.root).get("quality_jobs", "job")["percent"], 30)

    def test_parent_exit_and_pid_reuse_revoke_write_access(self):
        update_process(self.root, "job", self.token,
                       {"supervisor_pid": 123, "supervisor_identity": "birth"})
        for identity in (False, "another-birth"):
            with self.subTest(identity=identity), \
                 patch("backend.quality_execution._process_identity", return_value=identity), \
                 self.assertRaises(QualityExecutionRevoked):
                check_execution(self.root, "job", self.token)

    def test_revoke_does_not_wait_for_child_file_lock_and_blocks_following_writes(self):
        started, finished = threading.Event(), threading.Event()
        def revoke():
            started.set()
            revoke_execution(self.root, "job", self.token, "timeout")
            finished.set()
        with self.context(), execution_write_guard():
            thread = threading.Thread(target=revoke)
            thread.start()
            self.assertTrue(started.wait(2))
            self.assertTrue(finished.wait(2))
        thread.join(2)
        self.assertTrue(finished.is_set())
        with self.assertRaises(QualityExecutionRevoked), self.context():
            pass

    def _fixture_tree(self):
        from backend.tests.test_batch_results import BatchResultsTests
        fixture = BatchResultsTests()
        fixture.root, fixture.batch, fixture.store = self.root, "batch", self.store
        fixture.annotation = {"schema_version": 1, "columns": {"VLA trajectories": []},
            "sheets": {"VLA trajectories": [{"task_id": "A", "trajectory_id": "A-1",
                                              "image": "A/1.png", "action": "wait"}]}}
        self.store.publish("batch", "02_annotation", fixture.annotation)
        with patch("backend.batch_results._notify_invalidated"):
            fixture.build("A")
        return fixture

    def _quality(self):
        from backend.batch_results import current_tree_payload, merge_quality_results
        trees = current_tree_payload("batch", self.root)
        return merge_quality_results("batch", [{"task_id": "A", "run_id": "batch",
            "evaluations": {}, "average_score": 4.5}], {"A": trees["tree_hashes"]["A"]},
            {"A": "quality-A"}, job_id="job", completed_at="2026-10-14T00:00:00Z", root=self.root)

    def test_final_artifact_and_execution_commit_are_one_transaction(self):
        self._fixture_tree()
        original = RecordStore.put_many
        commits = []
        def write(records, entries, **kwargs):
            if any(item["namespace"] == NAMESPACE for item in entries):
                commits.append({item["namespace"] for item in entries})
            return original(records, entries, **kwargs)
        with self.context(), patch.object(RecordStore, "put_many", write), \
             patch("backend.batch_results._notify_invalidated"):
            result = self._quality()
        self.assertEqual(get_execution(self.root, "job")["state"], "committed")
        self.assertEqual(committed_result(self.root, "job", self.token)["artifact"], result["artifact"])
        self.assertEqual(len(commits), 1)
        self.assertIn("artifacts", commits[0])
        self.assertEqual(revoke_execution(self.root, "job", self.token, "late-timeout")["state"], "committed")

    def test_commit_response_lost_recovers_artifact_and_rejects_late_writes(self):
        self._fixture_tree()
        original = RecordStore.put_many
        def write(records, entries, **kwargs):
            result = original(records, entries, **kwargs)
            if any(item["namespace"] == NAMESPACE for item in entries):
                raise RuntimeError("simulated response loss after commit")
            return result
        with self.context(), patch.object(RecordStore, "put_many", write), \
             self.assertRaisesRegex(RuntimeError, "response loss"):
            self._quality()
        recovered = committed_result(self.root, "job", self.token)
        self.assertEqual(recovered["tasks"][0]["average_score"], 4.5)
        self.assertTrue(self.store.resolve_file(recovered["artifact"], "result.json").is_file())
        with self.assertRaises(QualityExecutionRevoked), self.context():
            self._quality()

    def test_revoked_execution_cannot_replace_existing_result(self):
        fixture = self._fixture_tree()
        with patch("backend.batch_results._notify_invalidated"):
            fixture.quality("A")
        ref = self.store.get("batch", "05_quality")
        before = self.store.resolve_file(ref, "result.json").read_bytes()
        with self.context():
            revoke_execution(self.root, "job", self.token, "timeout")
            with self.assertRaises(QualityExecutionRevoked):
                self._quality()
        self.assertEqual(self.store.get("batch", "05_quality"), ref)
        self.assertEqual(self.store.resolve_file(ref, "result.json").read_bytes(), before)

    def test_revoke_between_file_replacement_and_sqlite_commit_rolls_back(self):
        from backend.data_store.registry import RevisionConflict
        fixture = self._fixture_tree()
        with patch("backend.batch_results._notify_invalidated"):
            fixture.quality("A")
        ref = self.store.get("batch", "05_quality")
        before = self.store.resolve_file(ref, "result.json").read_bytes()
        original = RecordStore.put_many
        def revoke_before_commit(records, entries, **kwargs):
            if any(item["namespace"] == NAMESPACE for item in entries):
                revoke_execution(self.root, "job", self.token, "request_deadline")
            return original(records, entries, **kwargs)
        with self.context(), patch.object(RecordStore, "put_many", revoke_before_commit), \
             self.assertRaises(RevisionConflict):
            self._quality()
        self.assertEqual(get_execution(self.root, "job")["state"], "revoked")
        self.assertIsNone(committed_result(self.root, "job", self.token))
        self.assertEqual(self.store.get("batch", "05_quality"), ref)
        self.assertEqual(self.store.resolve_file(ref, "result.json").read_bytes(), before)

    def test_legacy_indexes_and_execution_commit_share_artifact_transaction(self):
        with patch.object(self.worker, "DATA_ROOT", self.root), self.context():
            result = self.worker._publish_legacy_quality("old-run", "job", ["A"],
                {"batch_id": "batch"}, [{"task_id": "A", "evaluations": {}}], [{"task_id": "A"}])
        stored = RecordStore(self.root).get("quality_results", "old-run:A")
        self.assertEqual(stored["artifact"], result["artifact"])
        self.assertEqual(committed_result(self.root, "job", self.token)["artifact"], result["artifact"])

    def test_artifact_factory_failure_preserves_previous_files(self):
        before = self.store.publish("batch", "05_quality", {"old": True})
        def fail(_):
            raise RuntimeError("factory failed")
        with self.assertRaisesRegex(RuntimeError, "factory failed"):
            self.store.publish_many("batch", [{"stage": "05_quality", "payload": {"new": True}}],
                                    record_entries=fail)
        self.assertEqual(self.store.get("batch", "05_quality"), before)
        self.assertEqual(self.store.read_payload(before), {"old": True})

    def test_checkpoint_reused_across_executions_and_revoked_append_rejected(self):
        E = self.worker.EVAL
        task = E.TaskDescription(task_id="A", instruction="点击按钮")
        rubric = E.DynamicRubric(task_id="A", dimensions=[{"name": "完成度", "description": "检查任务最终页面是否符合用户目标",
            "scoring_criteria": {score: str(score) for score in range(1, 6)}}])
        trajectory = E.Trajectory(task_id="A", trajectory_id="tr-one",
            steps=[E.TrajectoryStep(step_id=1, action="click", observation="完成页面")])
        evaluation = E.TrajectoryEvaluation(task_id="A", trajectory_id="tr-one", rubric_used=rubric,
                                            step_evaluations=[], global_score=4.0)
        checkpoint = self.root / "cache" / "checkpoint.jsonl"
        rubric_path = self.root / "rubric.json"
        args = dict(path=checkpoint, task=task, rubric_path=rubric_path, run_number=1,
                    evaluation=evaluation, evaluation_settings=E._evaluation_settings({}))
        with self.context():
            E.initialize_evaluations_jsonl(checkpoint, resume=True)
            E.append_evaluation_jsonl(**args)
            before = checkpoint.read_bytes()
            revoke_execution(self.root, "job", self.token, "timeout")
            with self.assertRaises(QualityExecutionRevoked):
                E.append_evaluation_jsonl(**args)
        update_process(self.root, "job", self.token, {"exited": True})
        lease = start_execution(self.root, "retry", "batch")
        with execution_context(self.root, "retry", lease["token"]), \
             patch.object(E, "evaluate_trajectory", new_callable=AsyncMock) as model, \
             contextlib.redirect_stdout(io.StringIO()):
            E.initialize_evaluations_jsonl(checkpoint, resume=True)
            result = asyncio.run(E.evaluate_run_incrementally(
                pipeline=SimpleNamespace(filter_evaluations=lambda values: values), task=task,
                trajectories=[trajectory], rubric=rubric, rubric_path=rubric_path, run_number=1,
                temperature=0, eval_max_tokens=10, max_concurrent=1, evaluations_path=checkpoint,
                config={}, existing_evaluations=E.load_existing_evaluations_jsonl(checkpoint)))
        model.assert_not_awaited()
        self.assertEqual(result.all_evaluations[0].global_score, 4)
        self.assertEqual(checkpoint.read_bytes(), before)

    def test_rubric_generation_returning_after_revocation_cannot_save(self):
        E, W = self.worker.EVAL, self.worker
        task = E.TaskDescription(task_id="A", instruction="点击按钮")
        rubric = E.DynamicRubric(task_id="A", dimensions=[{"name": "完成度", "description": "检查任务最终页面是否符合用户目标",
            "scoring_criteria": {score: str(score) for score in range(1, 6)}}])
        async def response_after_revoke(**kwargs):
            revoke_execution(self.root, "job", self.token, "request_deadline")
            return rubric
        with self.context(), patch.object(W, "RUBRIC_DIR", self.root / "rubrics"), \
             patch.object(W.GEN, "build_messages", return_value=[]), \
             patch.object(W.GEN, "_evidence_from_messages", return_value="mock evidence"), \
             patch.object(W.GEN, "generate_rubric", side_effect=response_after_revoke), \
             self.assertRaises(QualityExecutionRevoked):
            asyncio.run(W._generate_rubric(task, [], {}, self.root / "input.xlsx"))
        self.assertFalse((self.root / "rubrics" / "jiawen_gui_initial_rubric__A.json").exists())
        self.assertFalse((self.root / "rubrics" / "jiawen_gui_initial_rubric__A.json.tmp").exists())

    def test_revoked_worker_observer_refuses_model_request_before_submission(self):
        from adarubric.llm.request_control import controlled_request
        called = []
        async def model():
            called.append(True)
            return "unused"
        async def submit():
            await controlled_request(model, timeout_seconds=1, messages=[], max_tokens=10,
                                     model="mock", base_url="https://model.invalid/v1",
                                     http_proxy_enabled=False, http_trust_env=False)
        # Token-less heartbeat keeps this in-process test from terminating the
        # interpreter; the observer still uses the actual execution context.
        with self.context(), patch.object(self.worker, "DATA_ROOT", self.root), \
             contextlib.redirect_stdout(io.StringIO()), self.worker.worker_telemetry("job", None):
            revoke_execution(self.root, "job", self.token, "request_deadline")
            with self.assertRaises(QualityExecutionRevoked):
                asyncio.run(submit())
        self.assertEqual(called, [])


if __name__ == "__main__":
    unittest.main()
