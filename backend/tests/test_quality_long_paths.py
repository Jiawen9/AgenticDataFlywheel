"""Offline quality work survives long Windows paths and reuses its checkpoints."""
from __future__ import annotations

import asyncio
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from backend.file_io import io_path
from backend.tests.test_file_io import legacy_path_limit


class QualityLongPathTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        rubric_module = Path(__file__).resolve().parents[1] / "DevelopRubrics"
        with patch.object(sys, "path", [str(rubric_module), *sys.path]):
            from backend.DevelopRubrics import quality_job_runner
        cls.worker = quality_job_runner
        cls.evaluator = quality_job_runner.EVAL
        cls.generator = quality_job_runner.GEN

    def setUp(self):
        self.temp = tempfile.mkdtemp(prefix="quality-long-")
        self.root = Path(self.temp)
        for index in range(4):
            self.root /= f"batch-{index}-" + "质检数据" * 15
        io_path(self.root).mkdir(parents=True)
        self.addCleanup(lambda: shutil.rmtree(io_path(self.temp)))
        self.task_id = "rollout_task_" + "a" * 64
        E = self.evaluator
        self.task = E.TaskDescription(task_id=self.task_id, instruction="点击目标按钮")
        self.rubric = E.DynamicRubric(task_id=self.task_id, dimensions=[{
            "name": "完成度", "description": "验证页面内容与目标动作是否一致",
            "scoring_criteria": {score: f"完成度 {score}" for score in range(1, 6)},
        }])
        self.trajectory = E.Trajectory(task_id=self.task_id, trajectory_id="trajectory-one",
            steps=[E.TrajectoryStep(step_id=1, action="click", observation="目标页面出现")])
        self.evaluation = E.TrajectoryEvaluation(task_id=self.task_id, trajectory_id="trajectory-one",
            rubric_used=self.rubric, step_evaluations=[], global_score=4.0)
        self.checkpoint = self.root / "checkpoints" / "rollout-20260930-b7bd7132" / ("b" * 64) / (self.task_id + ".jsonl")
        self.rubric_path = self.root / "rubrics" / (self.task_id + ".json")
        self.assertGreater(len(str(self.checkpoint)), 300)
        # Force the deployment failure even on a longPathAware development
        # interpreter. Old unprefixed checkpoint I/O must fail under this guard.
        sentinel = self.root / "guard-check.txt"
        io_path(sentinel).write_text("fixture", encoding="utf-8")
        if os.name == "nt":
            stack = contextlib.ExitStack()
            self.addCleanup(stack.close)
            stack.enter_context(legacy_path_limit())
            with self.assertRaises(FileNotFoundError):
                sentinel.read_text(encoding="utf-8")

    def test_checkpoint_append_and_reload_skip_completed_model_work(self):
        E = self.evaluator
        settings = E._evaluation_settings({})
        E.initialize_evaluations_jsonl(self.checkpoint, resume=True)
        E.append_evaluation_jsonl(path=self.checkpoint, task=self.task, rubric_path=self.rubric_path,
            run_number=1, evaluation=self.evaluation, evaluation_settings=settings)
        original = io_path(self.checkpoint).read_bytes()
        E.initialize_evaluations_jsonl(self.checkpoint, resume=True)
        self.assertEqual(io_path(self.checkpoint).read_bytes(), original)
        record = json.loads(original)
        self.assertEqual(record["rubric_path"], str(self.rubric_path))
        self.assertNotIn("\\\\?\\", record["rubric_path"])
        existing = E.load_existing_evaluations_jsonl(self.checkpoint)
        pipeline = SimpleNamespace(filter_evaluations=lambda results: results)
        with patch.object(E, "evaluate_trajectory", new_callable=AsyncMock) as model, contextlib.redirect_stdout(io.StringIO()):
            result = asyncio.run(E.evaluate_run_incrementally(pipeline=pipeline, task=self.task,
                trajectories=[self.trajectory], rubric=self.rubric, rubric_path=self.rubric_path,
                run_number=1, temperature=0, eval_max_tokens=10, max_concurrent=1,
                evaluations_path=self.checkpoint, config={}, existing_evaluations=existing))
        model.assert_not_awaited()
        self.assertEqual(len(result.all_evaluations), 1)
        self.assertEqual(result.all_evaluations[0].global_score, 4)
        self.assertEqual(io_path(self.checkpoint).read_bytes(), original)

    def test_runner_generates_reads_and_reuses_rubric_on_long_path(self):
        G, W, E = self.generator, self.worker, self.evaluator
        with patch.object(W, "RUBRIC_DIR", self.rubric_path.parent), \
             patch.object(G, "build_messages", return_value=[]), \
             patch.object(G, "_evidence_from_messages", return_value="模拟证据"), \
             patch.object(G, "generate_rubric", new_callable=AsyncMock, return_value=self.rubric) as model:
            path = asyncio.run(W._generate_rubric(self.task, [self.trajectory], {}, self.root / "input.xlsx"))
            self.assertNotIn("\\\\?\\", str(path))
            self.assertEqual(W._matching_rubric(self.task_id), path)
            self.assertEqual(E._load_rubric(path, self.task), self.rubric)
            model.assert_awaited_once()
        E.save_report("模拟报告", self.root / "report.md")
        self.assertEqual(io_path(self.root / "report.md").read_text(encoding="utf-8"), "模拟报告")

    def test_old_snapshot_containment_check_still_rejects_escape(self):
        W = self.worker
        run_root = self.root / "legacy" / "run-one"
        io_path(run_root).mkdir(parents=True)
        with patch.object(W, "TREE_RUNS", run_root.parent), \
             patch.object(W, "load_quality_objects") as loader:
            with self.assertRaisesRegex(ValueError, "must belong"):
                W._ensure_workbook("run-one", {"quality_input_json": "../outside.json"}, [])
        loader.assert_not_called()


if __name__ == "__main__":
    unittest.main()
