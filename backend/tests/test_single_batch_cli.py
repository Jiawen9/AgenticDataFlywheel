from __future__ import annotations

import json
import tempfile
import unittest
from copy import deepcopy
from functools import partial
from pathlib import Path
from unittest.mock import patch

from backend import trajectories_preprocessing as preprocessing
from backend.batch_results import current_quality_payload, current_tree_payload
from backend.data_store import ArtifactStore, RecordStore
from backend.quality_input_builder import build_quality_workbook
from backend.stage_artifacts import (publish_workbook, publish_workbooks, read_workbook_payload,
                                    workbook_payload, write_payload_workbook)
from backend.tests import test_batch_results as batch_fixture
from backend.tests import test_trajectories_preprocessing as preprocessing_fixture
from backend.tests.test_tree_build_service import (FakeAlignmentReviewer, FakeClassifier,
                                                 FakeSummarizer, prepare_source)
from backend.trajectories_tree import tree_builder
from backend.tree_build_service import build_tree_run


class SingleBatchCliTests(unittest.TestCase):
    def test_preprocessing_reuses_identical_input_before_constructing_reviewer(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, source = preprocessing_fixture.WorkbookAnnotationTests()._create_workbook_fixture(root)
            arguments = dict(source=source, export_output=root / "conversion.xlsx",
                annotated_output=root / "annotation.xlsx", env_file=root / ".env",
                max_review_rounds=2, batch_id="batch", data_root=root / "data")
            with patch.object(preprocessing, "configure_reviewer_environment", return_value="mock"), \
                 patch.object(preprocessing, "QwenBoxReviewer", return_value=preprocessing_fixture.AcceptingReviewer()) as reviewer, \
                 patch.object(preprocessing, "annotate_trajectory_workbook", wraps=preprocessing.annotate_trajectory_workbook) as annotate:
                first = preprocessing.run_pipeline(**arguments)
                second = preprocessing.run_pipeline(**arguments)
            self.assertTrue(second["reused"])
            self.assertEqual(first["artifacts"], second["artifacts"])
            self.assertEqual(reviewer.call_count, 1)
            self.assertEqual(annotate.call_count, 1)
            self.assertEqual(len(ArtifactStore(root / "data").list("batch")), 2)

    def test_default_cli_outputs_leave_only_current_artifacts_and_no_view_records(self):
        from backend import export_vla_trajectories as conversion_cli
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, source = preprocessing_fixture.WorkbookAnnotationTests()._create_workbook_fixture(root)
            data_root = root / "data"
            with patch.object(preprocessing, "configure_reviewer_environment", return_value="mock"), \
                 patch.object(preprocessing, "QwenBoxReviewer", return_value=preprocessing_fixture.AcceptingReviewer()), \
                 patch("sys.argv", ["preprocess", "--source", str(source), "--batch-id", "batch",
                                    "--data-root", str(data_root), "--env-file", str(root / ".env")]):
                self.assertEqual(preprocessing.main(), 0)
            self.assertEqual(RecordStore(data_root).list("trajectory_annotations"), [])
            self.assertEqual(list((data_root / "tmp").rglob("*.xlsx")), [])
            self.assertFalse((data_root / "system/preprocessing").exists())
            with patch("sys.argv", ["convert", str(source), "--batch-id", "conversion-batch", "--data-root", str(data_root)]):
                self.assertEqual(conversion_cli.main(), 0)
            self.assertIsNotNone(ArtifactStore(data_root).get("conversion-batch", "01_conversion"))
            self.assertEqual(list((data_root / "tmp").rglob("*.xlsx")), [])
            self.assertFalse((data_root / "system/preprocessing").exists())

    def test_failed_annotation_preserves_both_current_stages(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, source = preprocessing_fixture.WorkbookAnnotationTests()._create_workbook_fixture(root)
            arguments = dict(source=source, export_output=root / "conversion.xlsx",
                annotated_output=root / "annotation.xlsx", env_file=root / ".env",
                max_review_rounds=2, batch_id="batch", data_root=root / "data")
            with patch.object(preprocessing, "configure_reviewer_environment", return_value="mock"), \
                 patch.object(preprocessing, "QwenBoxReviewer", return_value=preprocessing_fixture.AcceptingReviewer()):
                preprocessing.run_pipeline(**arguments)
                store = ArtifactStore(root / "data")
                before = store.list("batch")
                with patch.object(preprocessing, "annotate_trajectory_workbook", side_effect=RuntimeError("model failed")):
                    with self.assertRaisesRegex(RuntimeError, "model failed"):
                        preprocessing.run_pipeline(**{**arguments, "max_review_rounds": 3})
                self.assertEqual(store.list("batch"), before)

    def test_conversion_change_preserves_b_annotation_and_invalidates_only_a(self):
        fixture = batch_fixture.BatchResultsTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        root, store, batch = fixture.root, fixture.store, fixture.batch
        initial = deepcopy(fixture.annotation)
        store.publish(batch, "01_conversion", initial)
        annotation_path = root / "annotation.xlsx"
        write_payload_workbook(annotation_path, initial)
        publish_workbook(annotation_path, batch_id=batch, stage="02_annotation", data_root=root)
        for task in ("A", "B"):
            fixture.build(task)
            fixture.quality(task)
        changed = deepcopy(initial)
        changed["sheets"]["VLA trajectories"][0]["action"] = "click"
        conversion_path = root / "conversion.xlsx"
        write_payload_workbook(conversion_path, changed)
        publish_workbook(conversion_path, batch_id=batch, stage="01_conversion", data_root=root)
        view = read_workbook_payload(annotation_path, data_root=root)
        self.assertEqual([row["task_id"] for row in view["sheets"]["VLA trajectories"]], ["B"])
        self.assertEqual(set(current_tree_payload(batch, root)["trees"]), {"B"})
        self.assertEqual([row["task_id"] for row in current_quality_payload(batch, root)["tasks"]], ["B"])
        self.assertIsNone(RecordStore(root).get("batch_invalidations", batch))
        before = store.list(batch)
        publish_workbook(conversion_path, batch_id=batch, stage="01_conversion", data_root=root)
        self.assertEqual(store.list(batch), before)

    def test_cas_rejects_late_combined_cli_result(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = ArtifactStore(root)
            first = store.publish("batch", "02_annotation", {"sheets": {}})
            current = store.publish("batch", "02_annotation", {"sheets": {"new": []}})
            with self.assertRaisesRegex(ValueError, "标框已更新"):
                publish_workbooks([{"stage": "01_conversion", "payload": {"sheets": {}}},
                    {"stage": "02_annotation", "payload": {"sheets": {}}}], batch_id="batch",
                    data_root=root, expected_annotation_version=first["version"], check_annotation_version=True)
            self.assertIsNone(store.get("batch", "01_conversion"))
            self.assertEqual(store.get("batch", "02_annotation"), current)

    def test_tree_cli_uses_current_batch_aggregation_and_reuses_model_work(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw, workbook = prepare_source(root)
            data_root = root / "data"
            target_raw = data_root / "raw"
            target_raw.parent.mkdir(parents=True)
            raw.rename(target_raw)
            store = ArtifactStore(data_root)
            payload = workbook_payload(workbook)
            conversion = store.publish("batch", "01_conversion", payload,
                source_refs=[{"kind": "raw_trajectories", "path": str(target_raw)}])
            store.publish("batch", "02_annotation", payload, source_refs=[conversion], metadata={"raw_root": str(target_raw)})
            output = root / "custom-tree.json"
            builder = partial(build_tree_run, quality_builder=partial(build_quality_workbook, summarizer=FakeSummarizer()))
            with patch("backend.tree_build_service.configure_reviewer_environment", return_value="fake-model"), \
                 patch("backend.tree_build_service.QwenIntermediateStateClassifier", wraps=FakeClassifier) as classifier, \
                 patch("backend.tree_build_service.QwenStateAlignmentReviewer", FakeAlignmentReviewer), \
                 patch("backend.tree_build_service.build_tree_run", side_effect=builder):
                args = ["--batch-id", "batch", "--data-root", str(data_root), "--env-file", str(root / ".env"), "-o", str(output)]
                self.assertEqual(tree_builder.main(args), 0)
                first = store.get("batch", "04_tree")
                first_calls = classifier.call_count
                self.assertGreater(first_calls, 0)
                self.assertEqual(tree_builder.main(args), 0)
                self.assertEqual(classifier.call_count, first_calls)
                self.assertEqual(store.get("batch", "04_tree"), first)
                self.assertEqual(tree_builder.main(args[:-2]), 0)
                self.assertEqual(classifier.call_count, first_calls)
                self.assertFalse((data_root / "system/preprocessing").exists())
                self.assertEqual(list((data_root / "tmp").rglob("*.json")), [])
            exported = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(set(exported["trees"]), {"TASK-A", "TASK-B"})
            self.assertEqual(len(store.list("batch", "04_tree")), 1)

    def test_cli_refuses_direct_write_into_managed_stage(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "batches/batch/04_tree/result.json"
            output.parent.mkdir(parents=True)
            output.write_text("sentinel", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "受管理"):
                tree_builder.main(["--data-root", str(root), "--batch-id", "batch", "-o", str(output)])
            self.assertEqual(output.read_text(encoding="utf-8"), "sentinel")


if __name__ == "__main__":
    unittest.main()
