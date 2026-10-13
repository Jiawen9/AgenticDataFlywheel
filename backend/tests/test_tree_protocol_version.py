"""Offline protocol-version, frozen Pipeline and artifact consistency checks."""
from __future__ import annotations

import json
from functools import partial
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from backend.batch_results import StaleTaskInput, current_tree_payload, digest
from backend.data_store import ArtifactStore
from backend.pipelines import PipelineManager
from backend.quality_input_builder import build_quality_workbook
from backend.stage_artifacts import read_workbook_payload, workbook_payload
from backend.tests.test_pipelines import OfflineRuntime
from backend.tests.test_tree_build_service import (
    FakeAlignmentReviewer, FakeClassifier, FakeSummarizer, prepare_source,
)
from backend.trajectories_tree import intermediate_state_classifier
from backend.tree_build_service import build_tree_run, tree_build_config


V4 = "trajectory-intermediate-observation-v4"
V5 = "trajectory-intermediate-observation-v5"


class VersionedRuntime(OfflineRuntime):
    def configurations(self):
        return {**super().configurations(), "tree": digest(tree_build_config(self.root / ".env"))}


class TreeProtocolVersionTests(unittest.TestCase):
    def test_protocol_change_blocks_old_pipeline_dispatch_and_retry_without_rewriting_snapshot(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = VersionedRuntime(root)
            runtime.ready_stages.add("preprocessing")
            manager = PipelineManager(runtime, root)
            with patch.object(intermediate_state_classifier, "PROMPT_VERSION", V4):
                old = manager.create({"request_id": "old", "name": "旧协议流程", "batch_id": "batch", "mode": "automatic",
                                      "start_mode": "existing", "threshold": 4})
            frozen = old["config_fingerprints"]
            with patch.object(intermediate_state_classifier, "PROMPT_VERSION", V5):
                for _ in range(3):
                    manager.tick(old["pipeline_id"])
                failed = manager.get(old["pipeline_id"])
                self.assertEqual(failed["status"], "failed")
                self.assertIn("处理配置已变化", failed["error"])
                self.assertEqual(failed["current_step"], "tree")
                self.assertEqual(failed["config_fingerprints"], frozen)
                self.assertEqual(runtime.calls, [])

                manager.control(failed["pipeline_id"], "retry", failed["storage_revision"])
                manager.tick(failed["pipeline_id"])
                failed = manager.get(failed["pipeline_id"])
                self.assertEqual(failed["status"], "failed")
                self.assertEqual(failed["config_fingerprints"], frozen)
                self.assertEqual(runtime.calls, [])

                manager.control(failed["pipeline_id"], "terminate", failed["storage_revision"])
                manager.tick(failed["pipeline_id"])
                new = manager.create({"request_id": "new", "name": "新协议流程", "batch_id": "batch", "mode": "automatic",
                                      "start_mode": "existing", "threshold": 4})
                self.assertEqual(new["config_fingerprints"]["tree"], digest(tree_build_config(root / ".env")))
                self.assertNotEqual(new["config_fingerprints"]["tree"], frozen["tree"])
                for _ in range(3):
                    manager.tick(new["pipeline_id"])
                self.assertEqual(runtime.calls, [("tree", False)])
                self.assertEqual(manager.get(new["pipeline_id"])["steps"][1]["message"], "复用当前有效结果")
                self.assertEqual(manager.get(old["pipeline_id"])["config_fingerprints"], frozen)

    def test_tree_job_with_frozen_v4_config_rejects_v5_before_model_or_artifact_write(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(intermediate_state_classifier, "PROMPT_VERSION", V4):
                old_config = tree_build_config(root / ".env")
            with (
                patch.object(intermediate_state_classifier, "PROMPT_VERSION", V5),
                patch("backend.tree_build_service.QwenIntermediateStateClassifier") as classifier,
            ):
                with self.assertRaisesRegex(StaleTaskInput, "建树模型配置已变化"):
                    build_tree_run(["TASK-A"], job_id="old-job", progress=lambda _: None,
                                   xlsx_path=root / "source.xlsx", runs_dir=root / "runs",
                                   env_path=root / ".env", expected_build_config=old_config)
                classifier.assert_not_called()
            self.assertFalse((root / "runs").exists())

    def assert_quality_version(self, payload):
        rows = payload["sheets"]["Trajectories"]
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertEqual(json.loads(row["metadata_json"])["observation_prompt_version"], V5)
        self.assertEqual(len(payload["sheets"]["Steps"]), 2)

    def test_current_batch_artifact_configuration_and_quality_metadata_share_v5(self):
        self.assertEqual(intermediate_state_classifier.PROMPT_VERSION, V5)
        with TemporaryDirectory() as directory:
            root = Path(directory)
            old_raw, workbook = prepare_source(root)
            raw = root / "raw"
            old_raw.rename(raw)
            store = ArtifactStore(root)
            source = workbook_payload(workbook)
            conversion = store.publish("batch", "01_conversion", source,
                                       source_refs=[{"kind": "raw_trajectories", "path": str(raw)}])
            annotation = store.publish("batch", "02_annotation", source,
                                       source_refs=[conversion], metadata={"raw_root": str(raw)})
            with (
                patch("backend.tree_build_service.configure_reviewer_environment", return_value="fake-model"),
                patch("backend.tree_build_service.QwenIntermediateStateClassifier", FakeClassifier),
                patch("backend.tree_build_service.QwenStateAlignmentReviewer", FakeAlignmentReviewer),
            ):
                _, manifest = build_tree_run(["TASK-A", "TASK-B"], job_id="v5", progress=lambda _: None,
                    batch_id="batch", data_root=root, annotation_version=annotation["version"],
                    env_path=root / ".env",
                    quality_builder=partial(build_quality_workbook, summarizer=FakeSummarizer()))
            payload = current_tree_payload("batch", root)
            self.assertEqual(payload["build_config"]["prompt_version"], V5)
            self.assertEqual(payload["quality_input_prompt_version"], V5)
            self.assertEqual(manifest["quality_input_prompt_version"], V5)
            self.assert_quality_version(payload["quality_input"])
            self.assertIsNotNone(store.get("batch", "03_observation"))
            self.assertEqual({key: tree["tree_step_count"] for key, tree in payload["trees"].items()},
                             {"TASK-A": 1, "TASK-B": 1})

    def test_standalone_manifest_and_saved_quality_json_share_v5(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            trajectory_root, workbook = prepare_source(root)
            with (
                patch("backend.tree_build_service.configure_reviewer_environment", return_value="fake-model"),
                patch("backend.tree_build_service.QwenIntermediateStateClassifier", FakeClassifier),
                patch("backend.tree_build_service.QwenStateAlignmentReviewer", FakeAlignmentReviewer),
            ):
                run_id, manifest = build_tree_run(["TASK-A", "TASK-B"], job_id="standalone-v5",
                    progress=lambda _: None, xlsx_path=workbook, trajectory_root=trajectory_root,
                    runs_dir=root / "runs", data_root=root, env_path=root / ".env",
                    quality_builder=partial(build_quality_workbook, summarizer=FakeSummarizer()))
            self.assertEqual(manifest["quality_input_prompt_version"], V5)
            saved = json.loads((root / "runs" / run_id / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["quality_input_prompt_version"], V5)
            self.assert_quality_version(read_workbook_payload(root / "runs" / run_id / "rubric_trajectories.json"))


if __name__ == "__main__":
    unittest.main()
