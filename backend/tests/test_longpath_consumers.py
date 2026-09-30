"""Long raw paths stay usable through conversion, trees, correction and COT."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import unittest
import builtins
from contextlib import ExitStack
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from PIL import Image

from backend.file_io import io_path, rglob
from backend.export_vla_trajectories import collect_rows, write_xlsx
from backend.trajectories_preprocessing import annotate_trajectory_workbook
from backend.stage_artifacts import read_workbook_payload, workbook_payload, write_sidecar
from backend.tree_build_service import build_tree_run
from backend.quality_input_builder import build_quality_workbook
from backend.preprocessing_jobs import PreprocessingJobManager
from backend.preprocessing_service import PreprocessingError
from backend.tree_build_jobs import TreeBuildJobManager
from backend.trajectories_tree.intermediate_state_classifier import QwenIntermediateStateClassifier
from backend.trajectory_correction.assets import resolve_asset
from backend.trajectory_correction.cot_jobs import _stable_image, _xml_text
from backend.trajectory_correction.cot_generator import QwenCotGenerator
from backend.trajectory_correction.workbook import _trajectory_task
from backend.tests.test_trajectories_preprocessing import AcceptingReviewer, UI_XML
from backend.tests.test_tree_build_service import FakeAlignmentReviewer, FakeSummarizer


def response(content: str) -> SimpleNamespace:
    return SimpleNamespace(choices=[SimpleNamespace(
        message=SimpleNamespace(content=content), finish_reason="stop")])


class LongPathConsumerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.mkdtemp(prefix="longpath-consumers-")
        self.root = Path(self.temporary)
        for index in range(4):
            self.root /= f"batch-{index}-" + "轨迹数据" * 18
        io_path(self.root).mkdir(parents=True)
        self.raw = self.root / "raw"
        self.case = self.raw / "TASK-A" / "TASK-A-1"
        io_path(self.case).mkdir(parents=True)
        self.image = self.case / "step001_vla_input.jpg"
        self.assertGreater(len(str(self.image)), 300)
        for filename in ("step001_vla_input.jpg", "step001_vla_input_stability.jpg"):
            Image.new("RGB", (200, 300), "white").save(io_path(self.case / filename))
        action = {"action": "click", "coordinate": [50, 50]}
        io_path(self.case / "step001_vla_input_ui.xml").write_text(UI_XML, encoding="utf-8")
        payloads = {
            "_trajectory_for_evaluate.json": {
                "task": "点击按钮", "actions_flat": [{"global_step": 1, "action": action}]},
            "turn001_orch_model_request.json": {
                "messages": [{"role": "user", "content": "**原始目标**: 点击按钮\n\n后续"}]},
            "step001_vla_model_response.json": {
                "content": "<tool_call>" + json.dumps(action) + "</tool_call><summary>点击按钮</summary>"},
        }
        for name, value in payloads.items():
            io_path(self.case / name).write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        self.original_hashes = self._hashes()
        # Reproduce a deployment without process-level long-path awareness,
        # independently of this test machine's registry setting.
        if os.name == "nt":
            stack = ExitStack()
            self.addCleanup(stack.close)

            def guarded(original):
                def operation(path, *args, **kwargs):
                    if isinstance(path, (str, os.PathLike)):
                        spelling = os.fspath(path)
                        if len(spelling) >= 260 and not spelling.startswith("\\\\?\\"):
                            raise OSError(206, "test: ordinary Windows path exceeds MAX_PATH")
                    return original(path, *args, **kwargs)
                return operation

            for target, name in ((builtins, "open"), (os, "stat"), (os, "scandir")):
                stack.enter_context(patch.object(target, name, guarded(getattr(target, name))))

    def tearDown(self):
        # Use one resolved, dedicated temporary directory even on Windows hosts
        # with long-path registry support disabled.
        shutil.rmtree(io_path(self.temporary))

    def _hashes(self):
        return {p.relative_to(self.raw).as_posix(): hashlib.sha256(io_path(p).read_bytes()).hexdigest()
                for p in rglob(self.raw) if io_path(p).is_file()}

    def assert_ordinary_paths(self, value):
        if isinstance(value, dict):
            for item in value.values():
                self.assert_ordinary_paths(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                self.assert_ordinary_paths(item)
        elif isinstance(value, str):
            self.assertNotIn("\\\\?\\", value)

    def test_conversion_annotation_and_tree_preserve_raw_bytes_and_normal_paths(self):
        rows, warnings = collect_rows(self.raw)
        self.assertEqual(warnings, [])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][1], str(self.image.relative_to(self.raw)))
        source, annotated = self.root / "converted.xlsx", self.root / "annotated.xlsx"
        write_xlsx(rows, source)
        counts = annotate_trajectory_workbook(
            source, annotated, reviewer=AcceptingReviewer(), trajectory_root=self.raw, allow_excel_import=True)
        self.assertEqual(counts["annotated"], 1)
        write_sidecar(annotated, workbook_payload(annotated))

        model_client = Mock()
        model_client.chat.completions.create.return_value = response(json.dumps({
            "is_intermediate": False, "category": "none", "confidence": 1,
            "reason": "页面稳定", "observation": "页面显示按钮"}, ensure_ascii=False))

        def classifier_factory(model, cache):
            classifier = object.__new__(QwenIntermediateStateClassifier)
            classifier.model, classifier.cache_path = model, cache
            classifier.client, classifier.cache = model_client, {}
            return classifier

        runs = self.root / "tree-runs"
        with patch("backend.tree_build_service.configure_reviewer_environment", return_value="mock"), \
             patch("backend.tree_build_service.QwenIntermediateStateClassifier", classifier_factory), \
             patch("backend.tree_build_service.QwenStateAlignmentReviewer", FakeAlignmentReviewer):
            run_id, manifest = build_tree_run(
                ["TASK-A"], job_id="longpath", progress=lambda event: None,
                xlsx_path=annotated, trajectory_root=self.raw, runs_dir=runs,
                env_path=self.root / "absent.env", classification_cache=self.root / "classifier.json",
                alignment_cache=self.root / "alignment.json",
                quality_builder=partial(build_quality_workbook, summarizer=FakeSummarizer()))
        self.assertEqual(model_client.chat.completions.create.call_count, 1)
        content = model_client.chat.completions.create.call_args.kwargs["messages"][1]["content"]
        self.assertTrue(all(item["image_url"]["url"].startswith("data:image/jpeg;base64,")
                            for item in content if item["type"] == "image_url"))
        self.assertEqual(manifest["task_count"], 1)
        self.assert_ordinary_paths(manifest)
        self.assert_ordinary_paths(json.loads(io_path(runs / run_id / "TASK-A.json").read_text(encoding="utf-8")))
        quality = read_workbook_payload(runs / run_id / "rubric_trajectories.xlsx")
        self.assertEqual(len(quality["sheets"]["Steps"]), 1)
        self.assert_ordinary_paths(quality)
        self.assertEqual(self._hashes(), self.original_hashes)

    def test_correction_and_cot_use_long_raw_paths_with_cache_reuse(self):
        relative = self.image.relative_to(self.raw).as_posix()
        self.assertEqual(resolve_asset(self.raw, relative), self.image)
        with self.assertRaises(ValueError):
            resolve_asset(self.raw, "../outside.jpg")
        self.assertIn("hierarchy", _xml_text(self.image, {}))
        stable = _stable_image(self.image)
        self.assertEqual(stable.name, "step001_vla_input_stability.jpg")
        self.assertEqual(_trajectory_task(self.raw, "TASK-A-1", relative, {}), "点击按钮")
        generator = object.__new__(QwenCotGenerator)
        generator.model, generator.endpoint = "mock", "https://example.invalid/v1"
        generator.cache_dir = self.root / "cot-cache"
        generator.client = Mock()
        generator.client.chat.completions.create.return_value = response(
            "<thought>按钮可见</thought><tool_call>{}</tool_call><summary>点击按钮</summary>")
        arguments = dict(task="点击按钮", trajectory_id="TASK-A-1", step=1, history="",
                         action={"action": "click", "coordinate": [50, 50]}, image=stable)
        self.assertFalse(generator.generate(**arguments)["cached"])
        self.assertTrue(generator.generate(**arguments)["cached"])
        self.assertEqual(generator.client.chat.completions.create.call_count, 1)
        self.assertEqual(self._hashes(), self.original_hashes)

    def test_job_snapshot_roundtrip_and_checksum_guard_in_long_workspace(self):
        queue = Mock()
        manager = PreprocessingJobManager(self.root, executor=queue, config_loader=lambda: {})
        snapshot = {"input_digest": "fixture", "batch_id": "long-job", "trajectories": []}
        submitted = manager._new_job("long-job", snapshot, {})
        saved = manager._get(submitted["job_id"])
        self.assertEqual(manager._snapshot(saved), snapshot)
        self.assert_ordinary_paths(saved)
        self.assertFalse(Path(saved["input_path"]).is_absolute())
        queue.submit.assert_called_once()
        snapshot_path = self.root / saved["input_path"]
        io_path(snapshot_path).write_text("{}", encoding="utf-8")
        with self.assertRaises(PreprocessingError):
            manager._snapshot(saved)
        tree_jobs = self.root / "system" / "trajectory_tree_jobs"
        tree_manager = TreeBuildJobManager(tree_jobs, executor=queue, data_root=self.root)
        self.assertTrue(io_path(tree_jobs).is_dir())
        self.assertEqual(tree_manager.list_jobs(), [])


if __name__ == "__main__":
    unittest.main()
