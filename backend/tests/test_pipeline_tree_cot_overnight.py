"""Exercise model rejection at the Pipeline boundary using real child workers."""
from collections import Counter
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from backend.data_store import ArtifactStore
from backend.pipelines import PipelineManager, PipelineRuntime
from backend.stage_artifacts import workbook_payload
from backend.tests import test_pipeline_runtime_integration as integration
from backend.tests import test_tree_build_service as trees
from backend.tests.test_pipelines import OfflineRuntime
from backend.tests.test_preprocessing_jobs import ManualQueue
from backend.trajectory_correction import service
from backend.tree_build_jobs import TreeBuildJobManager


class Rejected(Exception):
    status_code = 404


def stage(manager, pipeline_id, name):
    return next(step for step in manager.get(pipeline_id)["steps"] if step["id"] == name)


class TreeOvernightTests(unittest.TestCase):
    def test_retry_of_adopted_tree_job_keeps_history_and_uses_pipeline_selection(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = OfflineRuntime(root)
            manager = PipelineManager(runtime, root)
            pipeline = {"pipeline_id": "p-one", "task_ids": ["a", "b"]}
            for owner, selection, expected in (("p-other", ["a", "b"], None),
                                               ("p-one", ["b"], None),
                                               ("p-one", ["a", "b"], "j-one")):
                with self.subTest(owner=owner, selection=selection):
                    runtime.records.put("tree_jobs", "j-one", {"pipeline_id": owner, "requested_task_ids": selection})
                    step = {"id": "tree", "job_ids": ["j-one"], "overnight_retry": {"used": 1}}
                    manager._retry_current_stage(pipeline, step)
                    self.assertEqual(step["previous_job_ids"], ["j-one"])
                    self.assertEqual(step["retry_job_id"], expected)

    def test_unchanged_tree_checkpoint_exhausts_budget(self):
        with (
            TemporaryDirectory() as directory,
            patch("backend.pipelines._overnight_enabled", return_value=True),
            patch("backend.pipelines._retry_due", return_value=True),
            patch("backend.pipelines.OVERNIGHT_RETRIES", 2),
        ):
            runtime = OfflineRuntime(Path(directory))
            runtime.ready_stages.add("preprocessing")
            manager = PipelineManager(runtime, runtime.root)
            flow = manager.create({"request_id": "exhaust", "name": "exhaust", "batch_id": "test",
                "mode": "automatic", "start_mode": "existing", "threshold": 4})["pipeline_id"]
            for _ in range(3):
                manager.tick(flow)
            failure = {"category": "bad_request", "http_status": 404}
            for attempt in range(3):
                runtime.complete("tree", fail=True, failure=failure)
                manager.tick(flow)
                if attempt < 2:
                    self.assertEqual(manager.get(flow)["status"], "retry_waiting")
                    manager.tick(flow)
            self.assertEqual(manager.get(flow)["status"], "failed")
            manager.tick(flow)
            self.assertEqual(runtime.calls, [("tree", False), ("tree", True), ("tree", True)])

    def test_enabling_overnight_after_daytime_rejection_continues_same_tree(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            raw, workbook = trees.prepare_source(root)
            raw_root = root / "raw"
            raw.rename(raw_root)
            store = ArtifactStore(root)
            batch = "tree-batch"
            rows = workbook_payload(workbook)
            conversion = store.publish(batch, "01_conversion", rows, source_refs=[
                {"kind": "raw_trajectories", "path": str(raw_root)}])
            store.publish(batch, "02_annotation", rows, source_refs=[conversion])
            queue = ManualQueue()
            tree = TreeBuildJobManager(root / "tree-jobs", executor=queue, data_root=root)
            runtime = OfflineRuntime(root)
            runtime.tree = tree
            runtime.tasks = ["TASK-A", "TASK-B"]
            runtime.ready_stages.add("preprocessing")
            manager = PipelineManager(runtime, root)
            calls = Counter()

            class RejectTwice(trees.FakeClassifier):
                def classify(self, **kwargs):
                    trajectory = kwargs["trajectory"]
                    calls[trajectory] += 1
                    if trajectory == "TASK-B-1" and calls[trajectory] <= 2:
                        raise Rejected("model returned HTTP 404")
                    return super().classify(**kwargs)

            def jobs(name, pipeline):
                return tree.list_jobs(batch) if name == "tree" else []

            def ready(name, pipeline):
                if name == "tree":
                    return store.get(batch, "04_tree") is not None
                return name in runtime.ready_stages

            runtime.jobs = jobs
            runtime.ready = ready
            runtime.submit = PipelineRuntime.submit.__get__(runtime)
            config = {"model": "fake-model", "model_url": "https://example.invalid/v1"}
            with (
                patch("backend.pipelines._overnight_enabled", return_value=False) as enabled,
                patch("backend.tree_build_service.tree_build_config", return_value=config),
                patch("backend.tree_build_jobs.tree_build_config", return_value=config),
                patch("backend.tree_build_service.configure_reviewer_environment", return_value="fake-model"),
                patch("backend.tree_build_service.QwenIntermediateStateClassifier", RejectTwice),
                patch("backend.tree_build_service.QwenStateAlignmentReviewer", trees.FakeAlignmentReviewer),
                patch("backend.quality_input_builder._env", return_value={
                    "MODEL_NAME": "fake-model", "MODEL_URL": "https://example.invalid/v1", "YUNAI_API_KEY": "test"}),
                patch("backend.quality_input_builder.QwenSummarizer", return_value=trees.FakeSummarizer()),
            ):
                flow = manager.create({"request_id": "test", "name": "tree recovery", "batch_id": batch,
                    "mode": "automatic", "start_mode": "existing", "threshold": 4})["pipeline_id"]
                for _ in range(3):
                    manager.tick(flow)
                queue.drain()
                manager.tick(flow)
                day = manager.get(flow)
                self.assertEqual(day["status"], "failed", day.get("error"))
                self.assertIn("404", day["error"])
                self.assertEqual(stage(manager, flow, "tree")["retry_info"]["max_attempts"], 3)

                enabled.return_value = True
                manager.control(flow, "retry", day["storage_revision"])
                manager.tick(flow)
                queue.drain()
                manager.tick(flow)
                self.assertEqual(manager.get(flow)["status"], "retry_waiting")
                self.assertEqual(stage(manager, flow, "tree")["retry_info"]["max_attempts"], 101)

                manager = PipelineManager(runtime, root)
                manager.tick(flow)
                self.assertEqual(len(queue.calls), 0)
                paused = manager.control(flow, "pause", manager.get(flow)["storage_revision"])
                manager.tick(flow)
                self.assertEqual(manager.get(flow)["status"], "paused")
                manager.control(flow, "resume", paused["storage_revision"])
                real_submit = runtime.submit

                def lose_accepted_response(*args, **kwargs):
                    real_submit(*args, **kwargs)
                    raise TimeoutError("tree worker registered but reply was lost")

                with patch("backend.pipelines._retry_due", return_value=True):
                    with patch.object(runtime, "submit", side_effect=lose_accepted_response):
                        manager.tick(flow)
                    self.assertEqual(manager.get(flow)["status"], "retry_waiting")
                    manager.tick(flow)
                self.assertEqual(manager.get(flow)["status"], "running")
                self.assertEqual(len(queue.calls), 1)
                queue.drain()
                manager.tick(flow)
                self.assertEqual(stage(manager, flow, "tree")["status"], "succeeded")
                self.assertEqual(calls, {"TASK-A-1": 1, "TASK-B-1": 3})


class CotOvernightTests(unittest.TestCase):
    setUp = integration.RealPipelineIntegrationTests.setUp
    seed = integration.RealPipelineIntegrationTests.seed
    create = integration.RealPipelineIntegrationTests.create
    advance = integration.RealPipelineIntegrationTests.advance

    def test_new_cot_result_restores_retry_budget_without_regenerating_completed_rows(self):
        # The integration fixture resolves session images to this local path.
        # A real image digest is required before a saved COT can be reused.
        (self.raw / "offline.jpg").write_bytes(b"unchanged-image")
        calls = Counter()

        def reject_each_new_step_once(generator, **kwargs):
            step = kwargs["step"]
            calls[step] += 1
            if step in (2, 3) and calls[step] == 1:
                raise Rejected("model returned HTTP 404")
            return {"summary": "summary", "thought": "thought"}

        with (
            patch("backend.pipelines._overnight_enabled", return_value=True),
            patch("backend.pipelines.OVERNIGHT_RETRIES", 1),
            patch.object(integration.OfflineCot, "generate", reject_each_new_step_once),
        ):
            flow = self.create("manual")["pipeline_id"]
            waiting = self.advance(flow, "waiting_for_correction")
            session_id = waiting["session_id"]
            session = self.records.get("correction_sessions", session_id)
            group = next(g for g in session["snapshot_payload"]["groups"] if g["meta_task"] == "a-first")
            for row in group["rows"]:
                revision = self.records.get("correction_sessions", session_id)["storage_revision"]
                service.patch_row(session_id, row["excel_row"], {"actions": '{"action":"wait","time":3}',
                                  "expected_revision": revision})
            self.manager.control(flow, "confirm-correction", waiting["storage_revision"],
                session_revision=self.records.get("correction_sessions", session_id)["storage_revision"])
            self.advance(flow, "retry_waiting")
            cot = stage(self.manager, flow, "cot")
            self.assertIn("404", cot["error"])
            self.assertEqual(cot["retry_info"]["max_attempts"], 2)
            self.assertEqual(calls, {1: 1, 2: 1})
            with patch("backend.pipelines._retry_due", return_value=True):
                self.manager.tick(flow)
                self.manager.tick(flow)
                waiting = self.manager.get(flow)
                self.assertEqual(waiting["status"], "retry_waiting", waiting.get("error"))
                self.assertEqual(calls, {1: 1, 2: 2, 3: 1})
                self.manager.tick(flow)
                self.manager.tick(flow)
            self.assertEqual(stage(self.manager, flow, "cot")["status"], "succeeded")
            self.assertEqual(calls, {1: 1, 2: 2, 3: 2})
