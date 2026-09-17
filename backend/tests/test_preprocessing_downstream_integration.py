"""Independent integration checks across collection preprocessing and tree inputs."""
from __future__ import annotations

import json
import unittest
from functools import partial
from pathlib import Path
from unittest.mock import patch

from backend.preprocessing_service import PreprocessingError, annotate_input
from backend.quality_input_builder import build_quality_workbook
from backend.trajectory_context import resolve_batch_context, validate_batch_sources
from backend.trajectory_data import update_action_bbox
from backend.tree_build_service import build_tree_run
from backend.tree_build_jobs import TreeBuildJobManager
from backend.tests import test_preprocessing_jobs as fixtures
from backend.tests.test_tree_build_service import FakeClassifier, FakeAlignmentReviewer, FakeSummarizer


class PreprocessingDownstreamIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.PreprocessingJobTests("test_pipeline_keeps_business_columns_identity_and_idempotent_reads")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def fail_first(self):
        calls = []
        def flaky(*args, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("first annotation intentionally interrupted")
            return annotate_input(*args, **kwargs)
        self.fixture.manager = self.fixture.new_manager(annotator=flaky)

    def test_older_failed_retry_cannot_replace_newer_cumulative_success(self):
        f = self.fixture
        f.complete()
        self.fail_first()
        failed = f.start()
        self.assertEqual(failed["status"], "failed")
        f.complete()
        complete = f.start()
        self.assertEqual(complete["status"], "succeeded", complete.get("error"))
        context = resolve_batch_context("batch-one", root=f.root)
        self.assertEqual(len(context.payload["sheets"][fixtures.SHEET]), 2)
        before = context.annotation_version
        with self.assertRaises(PreprocessingError) as caught:
            f.manager.retry(failed["job_id"])
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(resolve_batch_context("batch-one", root=f.root).annotation_version, before)
        self.assertFalse(f.queue.calls)

    def test_failed_retry_reuses_newer_success_with_same_input(self):
        f = self.fixture
        f.complete()
        self.fail_first()
        failed = f.start()
        succeeded = f.start()
        self.assertEqual(succeeded["status"], "succeeded", succeeded.get("error"))
        retry = f.manager.retry(failed["job_id"])
        self.assertEqual(retry["job_id"], succeeded["job_id"])
        self.assertFalse(f.queue.calls)
        self.assertEqual(len(f.store.list("batch-one", "02_annotation")), 1)

    def test_real_annotation_payload_progress_and_pinned_downstream_tree(self):
        f = self.fixture
        f.complete()
        f.complete()
        events = []
        original_update = f.manager._update
        def track(job_id, **changes):
            events.append(changes.copy())
            return original_update(job_id, **changes)
        with patch.object(f.manager, "_update", track):
            job = f.start()
        self.assertEqual(job["status"], "succeeded", job.get("error"))
        progress = [event["completed_steps"] for event in events
                    if event.get("stage") == "annotating" and "completed_steps" in event]
        self.assertEqual(set(progress), {0, 1, 2})
        context = resolve_batch_context("batch-one", job["annotation_version"], f.root)
        rows = context.payload["sheets"][fixtures.SHEET]
        self.assertEqual(len(rows), 2)
        self.assertEqual(len({row["trajectory_id"] for row in rows}), 2)
        self.assertEqual({row["source_trajectory_id"] for row in rows}, {"original-run"})
        self.assertEqual(context.task_goals["task-one"], "Open settings")
        edit = update_action_bbox("task-one", rows[0]["trajectory_id"], 1, 2, (5, 6, 35, 45),
            batch_id="batch-one", expected_annotation_version=context.annotation_version, data_root=f.root)
        self.assertNotEqual(edit["annotation_version"], context.annotation_version)
        with patch("backend.tree_build_service.configure_reviewer_environment", return_value="fake-model"), \
             patch("backend.tree_build_service.QwenIntermediateStateClassifier", FakeClassifier), \
             patch("backend.tree_build_service.QwenStateAlignmentReviewer", FakeAlignmentReviewer):
            run_id, manifest = build_tree_run(["task-one"], job_id="integration-tree", progress=lambda _: None,
                batch_id="batch-one", annotation_version=context.annotation_version, data_root=f.root,
                env_path=f.root / "unused.env",
                quality_builder=partial(build_quality_workbook, summarizer=FakeSummarizer()))
        self.assertEqual(manifest["batch_id"], "batch-one")
        self.assertEqual(manifest["annotation_version"], context.annotation_version)
        run = f.root / "system" / "trajectory_tree_runs" / run_id
        snapshot = json.loads((run / manifest["source_annotated_json"]).read_text(encoding="utf-8"))
        self.assertEqual(snapshot["sheets"]["VLA trajectories"], rows)
        quality = json.loads((run / manifest["quality_input_json"]).read_text(encoding="utf-8"))
        self.assertEqual(len(quality["sheets"]["Trajectories"]), 2)
        for row in quality["sheets"]["Steps"]:
            source = json.loads(row["action_input_json"])["source_identity"]
            self.assertIn(source["collection_run_id"], {item["collection_run_id"] for item in rows})
            self.assertEqual(source["trajectory_id"], row["trajectory_id"])
        self.assertEqual(resolve_batch_context("batch-one", root=f.root).annotation_version, edit["annotation_version"])

    def test_changed_raw_is_visible_as_json_but_rejected_before_tree_submission(self):
        f = self.fixture
        f.complete()
        job = f.start()
        context = resolve_batch_context("batch-one", job["annotation_version"], f.root)
        self.assertTrue(context.input_snapshot_refs)
        row = context.payload["sheets"][fixtures.SHEET][0]
        image = context.raw_root / row["image"]
        image.write_bytes(image.read_bytes() + b"tamper")
        # A normal GET reads frozen values only; expensive source checks belong to work boundaries.
        self.assertEqual(resolve_batch_context("batch-one", root=f.root).annotation_version, context.annotation_version)
        manager = TreeBuildJobManager(f.root / "system/tree_jobs", executor=f.queue, data_root=f.root)
        with self.assertRaisesRegex(ValueError, "校验值变化"):
            manager.submit(["task-one"], batch_id="batch-one")
        self.assertEqual(manager.list_jobs(), [])

    def test_modified_frozen_input_json_is_rejected_instead_of_rescanning(self):
        f = self.fixture
        f.complete()
        job = f.start()
        context = resolve_batch_context("batch-one", job["annotation_version"], f.root)
        reference = context.input_snapshot_refs[0]
        path = f.root / reference["path"]
        path.write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "输入 JSON 校验失败"):
            validate_batch_sources(context)

    def test_raw_change_while_tree_is_queued_fails_before_any_model(self):
        f = self.fixture
        f.complete()
        processed = f.start()
        context = resolve_batch_context("batch-one", processed["annotation_version"], f.root)
        manager = TreeBuildJobManager(f.root / "system/tree_jobs", executor=f.queue, data_root=f.root)
        job = manager.submit(["task-one"], batch_id="batch-one")
        image = context.raw_root / context.payload["sheets"][fixtures.SHEET][0]["image"]
        image.write_bytes(image.read_bytes() + b"tamper")
        with patch("backend.tree_build_service.configure_reviewer_environment") as model:
            f.queue.drain()
        model.assert_not_called()
        self.assertEqual(manager.get(job["job_id"])["status"], "failed")
        self.assertFalse(f.store.list("batch-one", "03_observation"))

    def test_raw_change_during_tree_model_work_blocks_stage_publication(self):
        f = self.fixture
        f.complete()
        processed = f.start()
        context = resolve_batch_context("batch-one", processed["annotation_version"], f.root)
        image = context.raw_root / context.payload["sheets"][fixtures.SHEET][0]["image"]
        def progress(event):
            if event.get("stage") == "publishing":
                image.write_bytes(image.read_bytes() + b"changed-after-model-work")
        with patch("backend.tree_build_service.configure_reviewer_environment", return_value="fake-model"), \
             patch("backend.tree_build_service.QwenIntermediateStateClassifier", FakeClassifier), \
             patch("backend.tree_build_service.QwenStateAlignmentReviewer", FakeAlignmentReviewer):
            with self.assertRaisesRegex(ValueError, "校验值变化"):
                build_tree_run(["task-one"], job_id="integrity-tree", progress=progress,
                    batch_id="batch-one", annotation_version=context.annotation_version, data_root=f.root,
                    env_path=f.root / "unused.env",
                    quality_builder=partial(build_quality_workbook, summarizer=FakeSummarizer()))
        self.assertFalse(f.store.list("batch-one", "03_observation"))
        self.assertFalse(f.store.list("batch-one", "04_tree"))

    def test_annotation_rechecks_current_version_after_concurrent_manual_edit(self):
        f = self.fixture
        f.complete()
        initial = f.start()
        old = resolve_batch_context("batch-one", initial["annotation_version"], f.root)
        f.complete()
        changed = {}
        def editing_annotator(*args, **kwargs):
            result = annotate_input(*args, **kwargs)
            row = old.payload["sheets"][fixtures.SHEET][0]
            changed.update(update_action_bbox("task-one", row["trajectory_id"], 1, 2, (6, 7, 36, 47),
                batch_id="batch-one", expected_annotation_version=old.annotation_version, data_root=f.root))
            return result
        f.manager = f.new_manager(annotator=editing_annotator)
        result = f.start()
        self.assertEqual(result["status"], "failed", result)
        self.assertTrue(result["error"])
        self.assertEqual(resolve_batch_context("batch-one", root=f.root).annotation_version,
                         changed["annotation_version"])
        self.assertEqual([item["stage"] for item in result["artifacts"]], ["01_conversion"])


if __name__ == "__main__":
    unittest.main()

