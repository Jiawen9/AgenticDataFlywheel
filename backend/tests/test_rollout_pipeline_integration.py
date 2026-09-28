"""Import real raw files and run the production Pipeline entirely offline."""
from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from backend.batch_lifecycle import BatchPublishedError, lifecycle
from backend.batch_results import current_tree_payload, merge_quality_results, quality_task_fingerprints
from backend.collection_runs import CollectionRunStore
from backend.data_publishing.service import DatasetReleaseRegistry
from backend.data_store import ArtifactStore, RecordStore
from backend.data_store.registry import utc_now
from backend.pipelines import PipelineManager, PipelineRuntime
from backend.preprocessing_jobs import PreprocessingJobManager
from backend.quality_input_builder import build_quality_workbook
from backend.quality_jobs import QualityJobManager
from backend.rollout_imports import RolloutImportStore
from backend.tests.test_data_publishing import ImmediateExecutor
from backend.tests.test_preprocessing_jobs import CONFIG, ManualQueue
from backend.tests.test_trajectories_preprocessing import AcceptingReviewer, create_step
from backend.tests.test_tree_build_service import FakeClassifier, FakeAlignmentReviewer, FakeSummarizer
from backend.training_data_overview.service import TrainingOverviewManager
from backend.trajectory_correction import cot_jobs, draft_store, service
from backend.tree_build_jobs import TreeBuildJobManager
from backend.tree_build_service import build_tree_run


def checksums(directory):
    return {str(path.relative_to(directory)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in directory.rglob("*") if path.is_file()}


class RolloutPipelineIntegrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "platform"
        self.source = self.root / "raw" / "rollout_trajectories"
        # Deliberately identical trajectory names in different tasks. The frozen
        # batch/run/task identity, rather than the folder name, must distinguish them.
        for task in ("case-A", "case-B"):
            for name in ("same-run-1", "same-run-2"):
                path = self.source / task / name
                path.mkdir(parents=True)
                action = {"action": "click", "coordinate": [50, 50]}
                create_step(path, 1, action)
                (path / "_trajectory_for_evaluate.json").write_text(json.dumps({
                    "actions_flat": [{"global_step": 1, "action": action}]}), encoding="utf-8")
                (path / "turn001_orch_model_request.json").write_text(json.dumps({
                    "messages": [{"role": "user", "content": "**原始目标**: Open " + task + "\n\nnext"}]}), encoding="utf-8")
        self.original = checksums(self.source)
        self.imports = RolloutImportStore(self.root)
        self.sources = CollectionRunStore(self.root)
        self.store, self.records = ArtifactStore(self.root), RecordStore(self.root)
        self.queue = ManualQueue()
        config = {"model": "offline-tree", "version": 1}
        for patcher in (
            patch("backend.trajectories_preprocessing.read_env_file", return_value={}),
            patch("backend.tree_build_service.configure_reviewer_environment", return_value="fake-model"),
            patch("backend.tree_build_service.QwenIntermediateStateClassifier", FakeClassifier),
            patch("backend.tree_build_service.QwenStateAlignmentReviewer", FakeAlignmentReviewer),
            patch("backend.tree_build_service.tree_build_config", return_value=config),
            patch("backend.tree_build_jobs.tree_build_config", return_value=config),
            patch.object(draft_store, "CORRECTION_SESSIONS_DIR", self.root / "sessions"),
            patch.object(service, "CORRECTION_INPUTS_DIR", self.root / "system/trajectory_correction/inputs"),
            patch.object(service, "CORRECTION_EXPORTS_DIR", self.root / "system/trajectory_correction/exports"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.reviewer = Mock(side_effect=lambda **_: AcceptingReviewer())
        self.preprocessing = PreprocessingJobManager(self.root, source_store=self.sources, executor=self.queue,
            config_loader=lambda: dict(CONFIG), reviewer_factory=self.reviewer)

        def build(tasks, **kwargs):
            return build_tree_run(tasks, **kwargs, env_path=self.root / "offline.env",
                quality_builder=partial(build_quality_workbook, summarizer=FakeSummarizer()))

        self.tree = TreeBuildJobManager(self.root / "tree-jobs", runner=build, executor=self.queue, data_root=self.root)

        def score(batch_id, task_ids, *, job_id, progress):
            current = current_tree_payload(batch_id, self.root)
            rows = current["source_annotation"]["sheets"]["VLA trajectories"]
            results = [{"task_id": task, "status": "succeeded", "evaluations": {
                row["trajectory_id"]: {"global_score": 5, "passed_threshold": True}
                for row in rows if row["task_id"] == task}} for task in task_ids]
            progress({"stage": "evaluating", "completed_trajectories": len(rows), "percent": 100})
            return merge_quality_results(batch_id, results, current["tree_hashes"], quality_task_fingerprints(current),
                job_id=job_id, completed_at=utc_now(), root=self.root)

        self.quality = QualityJobManager(self.root / "quality-jobs", runner=score, executor=self.queue, data_root=self.root)
        self.cot_calls = Mock(side_effect=AssertionError("Unedited Top1 must not generate COT"))
        def cot_factory():
            return self.cot_calls()
        self.cot = cot_jobs.CotJobManager(self.root / "cot-jobs", generator_factory=cot_factory, executor=self.queue)
        for manager in (self.tree, self.quality, self.cot):
            self.addCleanup(manager.shutdown)
        self.registry = DatasetReleaseRegistry(data_root=self.root, releases_file=self.root / "registry.json")
        self.overview = TrainingOverviewManager(self.registry, executor=ImmediateExecutor())
        self.overview.start()
        self.addCleanup(self.overview.close)
        self.dispatch = Mock(side_effect=AssertionError("Import/existing mode must never dispatch a phone"))
        self.factory = SimpleNamespace(collection_runs=self.sources, remote_start=self.dispatch)
        self.runtime = PipelineRuntime(self.root, preprocessing=self.preprocessing, tree=self.tree,
            quality=self.quality, cot=self.cot, factory=self.factory, overview=self.overview)
        self.manager = PipelineManager(self.runtime, self.root)

    def import_batch(self):
        preview = self.imports.preview({"source_path": str(self.source), "batch_id": "rollout-offline",
            "name": "导入验证", "app": "离线 App", "scene": "购物", "capability": "搜索"})
        self.assertFalse(preview["errors"], preview)
        result = self.imports.commit(preview["import_id"], "import-once")
        batch = result["batch_id"]
        self.assertIsNotNone(self.store.get(batch, "00_collection"))
        source_tasks = self.store.read_payload(self.store.get(batch, "00_collection"))["snapshot"]["tasks"]
        self.assertEqual({item["task"] for item in source_tasks}, {"Open case-A", "Open case-B"})
        self.assertEqual({(item["app"], item["scene"], item["capability"]) for item in source_tasks},
            {("离线 App", "购物", "搜索")})
        for stage in ("01_conversion", "02_annotation", "03_observation", "04_tree", "05_quality"):
            self.assertIsNone(self.store.get(batch, stage))
        self.assertEqual(self.records.list("preprocessing_jobs"), [])
        runs = self.sources.list_runs(batch)
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["status"], "completed")
        self.assertEqual(runs[0]["source_kind"], "rollout_import")
        self.assertEqual(len(runs[0]["trajectories"]), 4)
        self.assertEqual(len({(row["task_id"], row["source_trajectory_id"]) for row in runs[0]["trajectories"]}), 4)
        self.assertEqual(checksums(self.source), self.original)
        self.dispatch.assert_not_called()
        self.reviewer.assert_not_called()
        return batch

    def create(self, batch, mode="manual"):
        return self.manager.create({"request_id": "pipeline-import-" + mode, "name": "原始 Rollout 全流程",
            "batch_id": batch, "mode": mode, "threshold": 4, "start_mode": "existing"})

    def advance(self, pipeline_id, target):
        for _ in range(24):
            self.manager.tick(pipeline_id)
            self.queue.drain()
            current = self.manager.get(pipeline_id)
            self.assertNotEqual(current["status"], "failed", current.get("error"))
            if current["status"] == target:
                return current
        self.fail(f"Pipeline did not reach {target}: {current}")

    def assert_completed(self, batch, current):
        for name in ("preprocessing", "tree", "quality"):
            step = next(item for item in current["steps"] if item["id"] == name)
            self.assertEqual(step["status"], "succeeded")
            self.assertEqual(len(step["job_ids"]), 1)
            self.assertEqual(step["jobs"][0]["pipeline_id"], current["pipeline_id"])
        self.assertEqual(len(self.records.list("preprocessing_jobs")), 1)
        for stage in ("01_conversion", "02_annotation"):
            rows = self.store.read_payload(self.store.get(batch, stage))["sheets"]["VLA trajectories"]
            self.assertEqual({row["source_kind"] for row in rows}, {"rollout_import"})
            self.assertEqual({row["source_row_id"] for row in rows}, {"case-A", "case-B"})
            self.assertEqual(len({row["trajectory_id"] for row in rows}), 4)
        self.assertEqual({item["goal"] for item in current["selection"]["tasks"]}, {"Open case-A", "Open case-B"})
        self.assertEqual(self.reviewer.call_count, 1)
        self.dispatch.assert_not_called()
        self.cot_calls.assert_not_called()
        self.assertEqual(checksums(self.source), self.original)
        self.assertEqual(lifecycle(batch, self.root)["status"], "published")
        stats = self.overview.query()
        self.assertEqual(stats["overview"]["total_trajectories"], 2)
        self.assertEqual(stats["overview"]["total_steps"], 2)
        self.assertEqual(stats["overview"]["manual_refine_steps"], 0)
        self.assertEqual(stats["filters"]["apps"], ["离线 App"])
        self.assertEqual(stats["filters"]["scenes"][0]["name"], "购物")
        self.assertTrue(self.overview.workbook().is_file())
        # Polling and conversion retries cannot publish or accumulate twice.
        self.manager.tick(current["pipeline_id"])
        self.overview.submit(current["release_id"])
        self.assertEqual(len(self.registry.list_releases()), 1)
        self.assertEqual(self.overview.query()["overview"]["total_steps"], 2)

    def test_raw_import_existing_manual_pipeline_preprocesses_before_waiting_then_publishes(self):
        batch = self.import_batch()
        listed = next(row for row in self.preprocessing.batches() if row["batch_id"] == batch)
        self.assertTrue(listed["can_start"])
        self.assertIsNone(listed["annotation_version"])
        pipeline = self.create(batch)
        waiting = self.advance(pipeline["pipeline_id"], "waiting_for_correction")
        annotation = self.store.read_payload(self.store.get(batch, "02_annotation"))
        self.assertEqual(len(annotation["sheets"]["VLA trajectories"]), 4)
        self.assertEqual(len({row["trajectory_id"] for row in annotation["sheets"]["VLA trajectories"]}), 4)
        self.assertEqual(len(waiting["selection"]["tasks"]), 2)
        session = self.records.get("correction_sessions", waiting["session_id"])
        self.assertEqual(session["row_edits"], {})
        self.assertEqual(self.registry.list_releases(), [])
        self.manager.control(pipeline["pipeline_id"], "confirm-correction", waiting["storage_revision"],
            session_revision=session["storage_revision"])
        self.assert_completed(batch, self.advance(pipeline["pipeline_id"], "succeeded"))

    def test_raw_import_automatic_pipeline_skips_phone_and_correction_and_closes_batch(self):
        batch = self.import_batch()
        completed = self.advance(self.create(batch, "automatic")["pipeline_id"], "succeeded")
        self.assert_completed(batch, completed)
        self.assertEqual(self.records.list("correction_sessions"), [])
        self.assertNotIn(batch, {row["batch_id"] for row in self.preprocessing.batches()})
        with self.assertRaises(BatchPublishedError) as raised:
            self.manager.create({"request_id": "after-publication", "batch_id": batch,
                "name": "禁止恢复", "mode": "automatic", "threshold": 4, "start_mode": "existing"})
        self.assertIn("发布", str(raised.exception))

    def test_tampered_frozen_raw_blocks_existing_pipeline_without_starting_preprocessing(self):
        batch = self.import_batch()
        run = self.sources.list_runs(batch)[0]
        xml = next(Path(run["output_dir"]).rglob("step001_vla_input_ui.xml"))
        xml.write_text("changed after import", encoding="utf-8")
        pipeline = self.create(batch)
        self.manager.tick(pipeline["pipeline_id"])
        failed = self.manager.get(pipeline["pipeline_id"])
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(self.records.list("preprocessing_jobs"), [])
        self.assertEqual(self.registry.list_releases(), [])
        self.assertEqual(lifecycle(batch, self.root)["status"], "active")
        self.assertEqual(checksums(self.source), self.original)
        self.reviewer.assert_not_called()
        self.dispatch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
