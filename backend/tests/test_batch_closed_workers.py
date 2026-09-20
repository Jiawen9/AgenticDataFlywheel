from __future__ import annotations

import asyncio
import base64
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from backend.batch_lifecycle import BatchPublishedError, active_jobs
from backend.batch_operations import batch_operation, recover_operations
from backend.data_store import ArtifactStore, RecordStore
from backend.tests import test_batch_results as fixtures
from backend.tests.test_collection_runs import seed_batch, raw_trajectory
from backend.tests.test_preprocessing_jobs import ManualQueue, CONFIG


class ClosedBatchWorkerTests(unittest.TestCase):
    incoming = fixtures.BatchResultsTests.incoming
    build = fixtures.BatchResultsTests.build
    quality = fixtures.BatchResultsTests.quality

    def setUp(self):
        fixtures.BatchResultsTests.setUp(self)
        (self.root / "raw").mkdir()
        self.store.publish(self.batch, "02_annotation", self.annotation,
                           metadata={"raw_root": str(self.root / "raw")})

    def close(self, batch=None):
        batch = batch or self.batch
        RecordStore(self.root).put("batch_lifecycle", batch, {
            "batch_id": batch, "status": "published", "release_id": "release-test", "published_at": "2026-09-20"})

    def test_queued_tree_and_quality_do_not_call_models_after_close(self):
        from backend.tree_build_jobs import TreeBuildJobManager
        from backend.quality_jobs import QualityJobManager
        self.build("A")
        tree_queue, quality_queue = ManualQueue(), ManualQueue()
        tree_runner, quality_runner = Mock(), Mock()
        tree = TreeBuildJobManager(self.root / "system/tree_jobs", tree_runner, tree_queue, self.root)
        quality = QualityJobManager(self.root / "system/quality_jobs", quality_runner, quality_queue, self.root)
        jobs = [(tree, tree.submit(["B"], batch_id=self.batch)),
                (quality, quality.submit(batch_id=self.batch, task_ids=["A"]))]
        self.close()
        for submit in (lambda: tree.submit(["B"], batch_id=self.batch),
                       lambda: quality.submit(batch_id=self.batch, task_ids=["A"])):
            with self.assertRaises(BatchPublishedError):
                submit()
        tree_queue.drain()
        quality_queue.drain()
        tree_runner.assert_not_called()
        quality_runner.assert_not_called()
        for manager, job in jobs:
            self.assertEqual(manager.get(job["job_id"])["status"], "failed")
            self.assertIn("已发布", manager.get(job["job_id"])["error"])

    def test_completed_result_reuse_manual_edit_and_cli_reject_closed_batch(self):
        from backend.tree_build_service import build_tree_run
        from backend.trajectory_data import update_action_bbox
        from backend import trajectories_preprocessing
        self.build("A")
        annotation = self.store.get(self.batch, "02_annotation")
        before = self.store.list(self.batch)
        self.close()
        calls = [lambda: self.build("A"), lambda: self.quality("A"),
                 lambda: build_tree_run(["A"], job_id="cli", progress=lambda _: None,
                                        batch_id=self.batch, data_root=self.root),
                 lambda: update_action_bbox("A", "A-1", 1, 2, (0, 0, 1, 1), batch_id=self.batch,
                                            expected_annotation_version=annotation["version"], data_root=self.root),
                 lambda: trajectories_preprocessing.run_pipeline(source=self.root / "raw",
                     export_output=self.root / "export.xlsx", annotated_output=self.root / "annotated.xlsx",
                     env_file=self.root / ".env", max_review_rounds=1, batch_id=self.batch, data_root=self.root)]
        for call in calls:
            with self.assertRaises(BatchPublishedError):
                call()
        self.assertEqual(self.store.list(self.batch), before)
        self.assertFalse((self.root / "export.xlsx").exists())

    def test_standalone_bbox_cli_refuses_closed_managed_raw_before_review(self):
        from backend.bounding_box.build_annotations import build
        reviewer = Mock()
        source = self.root / "raw" / "collection_batches" / self.batch
        source.mkdir(parents=True)
        self.close()
        with self.assertRaises(BatchPublishedError):
            build(source, self.root / "bbox-export", reviewer, "mock", 1, data_root=self.root)
        reviewer.review.assert_not_called()
        self.assertFalse((self.root / "bbox-export").exists())

    def test_quality_subprocess_entry_rejects_closed_before_evaluation(self):
        rubric_module = Path(__file__).resolve().parents[1] / "DevelopRubrics"
        with patch.object(sys, "path", [str(rubric_module), *sys.path]):
            from backend.DevelopRubrics import quality_job_runner
        self.close()
        with patch.object(quality_job_runner, "DATA_ROOT", self.root), patch.object(quality_job_runner, "_run_frozen") as model:
            with self.assertRaises(BatchPublishedError):
                asyncio.run(quality_job_runner.run(self.batch, ["A"], "legacy-job"))
            model.assert_not_called()

    def test_collection_idempotent_retry_callback_and_dispatch_reject_closed(self):
        from backend.collection_runs import CollectionRunStore
        seed_batch(self.root)
        store = CollectionRunStore(self.root)
        run, _ = store.create(self.batch, dispatch_key="request-one")
        evidence = raw_trajectory(run)
        before = store.get(run["collection_run_id"])
        raw_files = {str(path): path.read_bytes() for path in Path(run["output_dir"]).rglob("*") if path.is_file()}
        self.close()
        for call in (lambda: store.create(self.batch, dispatch_key="request-one"),
                     lambda: store.retry_dispatch(run["collection_run_id"]),
                     lambda: store.dispatched(run["collection_run_id"], {"ok": True}),
                     lambda: store.complete(run["collection_run_id"], {"batch_id": self.batch, "trajectories": [evidence]})):
            with self.assertRaises(BatchPublishedError):
                call()
        self.assertEqual(store.get(run["collection_run_id"]), before)
        self.assertEqual({str(path): path.read_bytes() for path in Path(run["output_dir"]).rglob("*") if path.is_file()}, raw_files)
        self.assertEqual(store.dispatch_failed(run["collection_run_id"], "late failure")["status"], "failed")

    def test_preprocessing_submit_retry_and_queued_work_reject_closed(self):
        from backend.collection_runs import CollectionRunStore
        from backend.preprocessing_jobs import PreprocessingJobManager
        seed_batch(self.root)
        sources = CollectionRunStore(self.root)
        run, _ = sources.create(self.batch)
        sources.complete(run["collection_run_id"], {"batch_id": self.batch, "trajectories": [raw_trajectory(run)]})
        queue, annotator = ManualQueue(), Mock()
        manager = PreprocessingJobManager(self.root, executor=queue, source_store=sources,
                                          config_loader=lambda: dict(CONFIG), annotator=annotator)
        job = manager.submit(self.batch)
        self.close()
        self.assertEqual(manager.batches(), [])
        for call in (lambda: manager.submit(self.batch), lambda: manager.retry(job["job_id"])):
            with self.assertRaises(BatchPublishedError):
                call()
        queue.drain()
        annotator.assert_not_called()
        self.assertEqual(manager.get(job["job_id"])["status"], "failed")

    def test_phone_choices_hide_closed_and_upload_start_reject_without_deletion(self):
        from backend.phone_factory import PhoneFactoryStore
        client = Mock()
        store = PhoneFactoryStore(self.root, run_client_fn=client)
        payload = {"filename": f"collection-batch-{self.batch}.xlsx", "description": "test",
                   "content_base64": base64.b64encode(b"workbook").decode(), "source_batch_id": self.batch}
        store.add_task(payload)
        self.close()
        self.assertEqual(store.state()["tasks"], [])
        for call in (lambda: store.add_task(payload), lambda: store.start_task(payload["filename"]),
                     lambda: store.remove_task(payload["filename"]), lambda: store.remote_start({"filename": payload["filename"]})):
            with self.assertRaises(BatchPublishedError):
                call()
        client.assert_not_called()
        self.assertEqual(store.task_path(payload["filename"]).read_bytes(), b"workbook")
        self.assertEqual(len(store._current()["tasks"]), 1)

    def test_task_generation_edits_exports_reuse_and_late_results_are_blocked(self):
        from backend.task_generation.jobs import TaskGenerationJobManager
        queue, runner = ManualQueue(), Mock()
        manager = TaskGenerationJobManager(self.root / "jobs", self.root / "runs", self.root / "exports",
            self.root / "kb", self.root / "logs", executor=queue, initial_runner=runner, data_root=self.root)
        manager._write_job({"job_id": self.batch, "kind": "task_generation", "status": "succeeded",
                            "stage": "succeeded", "errors": [], "warnings": [], "knowledge_base_version": "kb"})
        rows = [{"result_id": "result-1", "task_uuid": "task-1", "task": "test", "app": "App", "deleted": False}]
        manager._write_json(manager._results_path(self.batch), rows)
        manager.submit_collection_batch(self.batch)
        self.close()
        self.assertEqual(manager.collection_batches(), [])
        for call in (lambda: manager.patch_result(self.batch, "result-1", {"task": "changed"}),
                     lambda: manager.submit_collection_batch(self.batch), lambda: manager.start_augmentation(self.batch),
                     lambda: manager.snapshot(self.batch), lambda: manager.export(self.batch),
                     lambda: manager._finish(self.batch, {"results": []}),
                     lambda: manager._write_json(manager._results_path(self.batch), [])):
            with self.assertRaises(BatchPublishedError):
                call()
        manager._run_initial(self.batch, [], 1)
        runner.assert_not_called()
        self.assertEqual(manager.results(self.batch), rows)
        self.assertEqual(manager.get(self.batch)["status"], "failed")

    def test_new_collection_for_same_task_requires_preprocessing_current_input(self):
        from backend.batch_lifecycle import publication_blockers
        from backend.collection_runs import CollectionRunStore
        seed_batch(self.root)
        sources = CollectionRunStore(self.root)
        def collect():
            run, _ = sources.create(self.batch)
            sources.complete(run["collection_run_id"], {"batch_id": self.batch, "trajectories": [raw_trajectory(run)]})
        def capture():
            snapshot = sources.ready_input(self.batch)
            self.store.publish(self.batch, "01_conversion", self.annotation,
                source_refs=[{"kind": "raw_trajectories", "input_digest": snapshot["input_digest"]}])
        def collection_codes():
            return {item["code"] for item in publication_blockers(self.batch, [], self.root)
                    if item["code"] in {"stale_collection_input", "invalid_collection_input"}}
        collect()
        self.assertEqual(collection_codes(), {"stale_collection_input"})
        capture()
        self.assertEqual(collection_codes(), set())
        collect()  # Same task-one, a second trajectory is still unprocessed input.
        self.assertEqual(collection_codes(), {"stale_collection_input"})
        capture()
        self.assertEqual(collection_codes(), set())

    def test_execution_reservation_blocks_publish_and_is_audited_on_close(self):
        with batch_operation(self.batch, "test", self.root) as operation:
            self.assertTrue(any(job["job_id"] == operation for job in active_jobs(self.batch, self.root)))
            self.assertEqual(recover_operations(self.root), 0)
        self.assertEqual(RecordStore(self.root).get("batch_operations", operation)["status"], "succeeded")
        with self.assertRaises(BatchPublishedError):
            with batch_operation(self.batch, "late-worker", self.root) as late:
                self.close()
        self.assertEqual(RecordStore(self.root).get("batch_operations", late)["status"], "failed")

    def test_recover_only_proven_dead_processes_and_pid_reuse(self):
        records = RecordStore(self.root)
        for name, pid, identity in (("dead", 1, "a"), ("alive", 2, "b"), ("unknown", 3, "c"), ("reused", 4, "old")):
            records.put("batch_operations", name, {"job_id": name, "batch_id": self.batch, "status": "running",
                "pid": pid, "process_identity": identity, "hostname": socket.gethostname()})
        with patch("backend.batch_operations._process_identity", side_effect=lambda pid: {1: False, 2: "b", 3: None, 4: "new"}[pid]):
            self.assertEqual(recover_operations(self.root), 2)
        self.assertEqual(records.get("batch_operations", "alive")["status"], "running")
        self.assertEqual(records.get("batch_operations", "unknown")["status"], "running")
        self.assertEqual(records.get("batch_operations", "reused")["status"], "interrupted")


if __name__ == "__main__":
    unittest.main()
