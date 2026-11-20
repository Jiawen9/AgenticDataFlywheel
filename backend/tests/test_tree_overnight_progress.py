import json
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from openai import OpenAI

from backend.batch_results import StaleTaskInput
from backend.data_store import ArtifactStore
from backend.model_stage_checkpoint import ModelStageCheckpoint
from backend.pipeline_access import internal_pipeline
from backend.stage_artifacts import workbook_payload
from backend.tests.test_trajectory_batch_context import DeferredExecutor
from backend.tests.test_tree_build_service import FakeAlignmentReviewer, FakeSummarizer, prepare_source
from backend.trajectories_tree.intermediate_state_classifier import QwenIntermediateStateClassifier
from backend.tree_build_jobs import TreeBuildJobManager, tree_retry_progress


class TreeOvernightTests(unittest.TestCase):
    def test_retry_after_404_uses_original_task_checkpoint(self):
        calls = Counter()

        def respond(request):
            prompt = json.loads(request.content)["messages"][-1]["content"][0]["text"]
            task = "B" if "TASK-B-1" in prompt else "A"
            calls[task] += 1
            if task == "B" and calls["B"] == 1:
                return httpx.Response(404, json={"error": {"message": "model unavailable"}})
            content = json.dumps({"category": "none", "confidence": 0.95, "reason": "normal",
                                  "observation": "page"})
            return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

        with tempfile.TemporaryDirectory() as directory, internal_pipeline("p-test"), OpenAI(
            api_key="test", base_url="http://test.invalid/v1", max_retries=0,
            http_client=httpx.Client(transport=httpx.MockTransport(respond)),
        ) as client:
            root = Path(directory)
            source, workbook = prepare_source(root)
            raw = root / "raw"
            source.rename(raw)
            store = ArtifactStore(root)
            data = workbook_payload(workbook)
            conversion = store.publish("b-test", "01_conversion", data,
                                       source_refs=[{"kind": "raw_trajectories", "path": str(raw)}])
            store.publish("b-test", "02_annotation", data, source_refs=[conversion])
            queue = DeferredExecutor()
            manager = TreeBuildJobManager(root / "jobs", executor=queue, data_root=root)
            tasks = ["TASK-A", "TASK-B"]

            def classifier(model, cache):
                worker = object.__new__(QwenIntermediateStateClassifier)
                worker.model, worker.cache_path, worker.client = model, cache, client
                return worker

            env = {"MODEL_NAME": "test", "MODEL_URL": "http://test.invalid/v1", "YUNAI_API_KEY": "test"}
            config = {"model": "test", "model_url": env["MODEL_URL"]}
            with (
                patch("backend.tree_build_jobs.tree_build_config", return_value=config),
                patch("backend.tree_build_service.tree_build_config", return_value=config),
                patch("backend.tree_build_service.configure_reviewer_environment", return_value="test"),
                patch("backend.tree_build_service.read_env_file", return_value=env),
                patch("backend.tree_build_service.QwenIntermediateStateClassifier", side_effect=classifier),
                patch("backend.tree_build_service.QwenStateAlignmentReviewer", FakeAlignmentReviewer),
                patch("backend.quality_input_builder._env", return_value=env),
                patch("backend.quality_input_builder.QwenSummarizer", return_value=FakeSummarizer()),
            ):
                first = manager.submit(tasks, batch_id="b-test")
                queue.finish()
                failed = manager.get(first["job_id"])
                self.assertEqual(failed["failure"]["http_status"], 404, failed.get("error"))
                self.assertEqual(tree_retry_progress(root, [failed]), 2)  # classification + tree A
                queue.work.clear()
                retry = manager.submit(tasks, batch_id="b-test", origin_job_id=first["job_id"])
                self.assertEqual(retry["origin_job_id"], first["job_id"])
                self.assertEqual(tree_retry_progress(root, [failed, retry]), 2)
                queue.finish()
                done = manager.get(retry["job_id"])
                self.assertEqual(done["status"], "succeeded", done.get("error"))
                self.assertEqual(tree_retry_progress(root, [failed, done]), 6)
                self.assertEqual(calls, {"A": 1, "B": 2})

    def test_binding_preupgrade_sqlite_does_not_reset_budget_for_old_work(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manager = TreeBuildJobManager(root / "jobs", executor=DeferredExecutor(), data_root=root)
            job = {"job_id": "j-test", "batch_id": "b-test", "status": "running"}
            manager.records.put("tree_jobs", job["job_id"], job)
            store = ModelStageCheckpoint(root, "tree", ["batch", "task", "snapshot"])
            store.save("classification", "old", "ok")
            manager._register_checkpoint(job["job_id"], "task", store)
            job = manager.get(job["job_id"])
            self.assertEqual(tree_retry_progress(root, [job]), 0)
            store.save("classification", "old", "ok")
            store.save("alignment", "comparison", True)
            store.save("trajectory_summary", "trajectory", "complete")
            self.assertEqual(tree_retry_progress(root, [job, job]), 2)
            with self.assertRaises(StaleTaskInput):
                manager._register_checkpoint(job["job_id"], "task", ModelStageCheckpoint(root, "tree", ["changed"]))

    def test_replayed_dispatch_returns_even_a_failed_child_for_its_own_pipeline(self):
        with tempfile.TemporaryDirectory() as directory, internal_pipeline("p-test"):
            root = Path(directory)
            manager = TreeBuildJobManager(root / "jobs", executor=DeferredExecutor(), data_root=root)
            original = {"job_id": "j-first", "status": "failed", "batch_id": "b-test",
                        "pipeline_id": "p-test", "requested_task_ids": ["task"]}
            child = {**original, "job_id": "j-second", "origin_job_id": "j-first"}
            for job in (original, child):
                manager.records.put("tree_jobs", job["job_id"], job)
            context = SimpleNamespace(batch_id="b-test")
            reused = manager._submit_batch(["task"], context, origin_job_id="j-first")
            self.assertEqual((reused["job_id"], reused["status"]), ("j-second", "failed"))
            with self.assertRaisesRegex(ValueError, "任务范围"):
                manager._submit_batch(["task", "another"], context, origin_job_id="j-first")
            with internal_pipeline("different"), self.assertRaisesRegex(ValueError, "当前流程"):
                manager._submit_batch(["task"], context, origin_job_id="j-first")
