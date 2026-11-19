from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from backend.pipeline_access import internal_pipeline
from backend.pipeline_job_stop import PipelineJobCancelled, job_commit_guard, request_local_stops
from backend.task_generation.model_client import TaskGenerationModel
from backend.training_data_overview.service import CATALOG, CONVERSIONS, TrainingOverviewManager
from backend.tests import test_preprocessing_jobs as preprocessing
from backend.tests import test_training_overview_service as overview


class PipelineLocalStopTests(unittest.TestCase):
    def setUp(self):
        fixture = preprocessing.PreprocessingJobTests(
            "test_pipeline_keeps_business_columns_identity_and_idempotent_reads")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.fixture = fixture
        self.records = fixture.manager.records

    def test_stopping_between_box_requests_preserves_conversion_and_omits_annotation(self):
        fixture = self.fixture
        fixture.complete(steps=(1, 2))
        calls = []

        class Reviewer(preprocessing.AcceptingReviewer):
            def review(inner, **kwargs):
                calls.append(kwargs["image_path"])
                self.records.put("pipelines", "p_stop", {"status": "terminating"})
                return super().review(**kwargs)

        fixture.manager.reviewer_factory = lambda **_: Reviewer()
        with internal_pipeline("p_stop"):
            job = fixture.manager.submit("batch-one")
        fixture.queue.drain()
        self.assertEqual(len(calls), 1)
        self.assertEqual(fixture.manager.get(job["job_id"])["status"], "cancelled")
        self.assertIsNotNone(fixture.store.get("batch-one", "01_conversion"))
        self.assertIsNone(fixture.store.get("batch-one", "02_annotation"))

    def test_queued_preprocessing_job_cannot_start_after_cancellation(self):
        fixture = self.fixture
        fixture.complete()
        with internal_pipeline("p_stop"):
            job = fixture.manager.submit("batch-one")
        pipeline = {"pipeline_id": "p_stop", "status": "terminating",
                    "termination": {"requested_at": "2026-11-19T00:00:00Z"}}
        self.records.put("pipelines", "p_stop", pipeline)
        self.assertEqual(request_local_stops(self.records, pipeline, lambda _: True), [])
        fixture.queue.drain()
        self.assertEqual(fixture.factory_calls, 0)
        self.assertEqual(fixture.manager.get(job["job_id"])["status"], "cancelled")

    def test_task_generation_does_not_send_model_request_after_stop(self):
        sent = []

        def check_stop():
            raise PipelineJobCancelled()

        client = TaskGenerationModel(call=lambda prompt: sent.append(prompt), check_stop=check_stop)
        with self.assertRaises(PipelineJobCancelled):
            client.complete("test")
        self.assertEqual(sent, [])

    def test_stopped_commit_preserves_current_artifact(self):
        store = self.fixture.store
        batch_id = "batch-one"
        reference = store.publish(batch_id, "02_annotation", {"sample": "original"})
        self.records.put("preprocessing_jobs", "j_stop", {"pipeline_id": "p_stop", "status": "running"})

        def cancel_on_commit():
            self.records.put("pipelines", "p_stop", {"status": "terminating"})
            return job_commit_guard(self.records, "preprocessing_jobs", "j_stop")

        with self.assertRaises(PipelineJobCancelled):
            store.publish(batch_id, "02_annotation", {"sample": "replacement"}, commit_guard=cancel_on_commit)
        self.assertEqual(store.get(batch_id, "02_annotation"), reference)
        self.assertEqual(store.read_payload(reference), {"sample": "original"})
        pending = self.fixture.root / "tmp" / "artifact_transactions" / batch_id
        self.assertFalse(list(pending.iterdir()) if pending.is_dir() else [])

    def test_overview_cancels_only_owned_attempt_and_allows_later_independent_conversion(self):
        with TemporaryDirectory() as folder:
            registry = overview.Registry(Path(folder))
            calls = []

            def convert(_, release):
                calls.append(release["release_id"])
                return {"release_id": release["release_id"], "rows": [overview.row()], "warnings": []}

            manager = TrainingOverviewManager(registry, converter=convert)
            try:
                with internal_pipeline("p_stop"):
                    manager.submit("rel_one")
                manager.records.put("pipelines", "p_stop", {"status": "terminating"})
                manager.start()
                manager.wait()
                self.assertEqual(manager.records.get(CONVERSIONS, "rel_one")["status"], "cancelled")
                self.assertIsNone(manager.records.get(CATALOG, "current"))
                self.assertEqual(calls, [])
                manager.submit("rel_one")
                manager.wait()
                self.assertEqual(manager.records.get(CONVERSIONS, "rel_one")["status"], "succeeded")
                self.assertEqual(calls, ["rel_one"])
            finally:
                manager.close()
