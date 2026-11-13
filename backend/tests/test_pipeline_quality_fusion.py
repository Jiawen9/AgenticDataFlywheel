"""Quality readiness uses the submitted configuration, never current UI defaults."""
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import Mock, patch

from backend.batch_results import quality_task_fingerprints
from backend.pipelines import PipelineRuntime, PipelineManager, PipelineError


class PipelineFusionReadinessTests(TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.runtime = PipelineRuntime(self.root, preprocessing=None, tree=None, quality=None,
                                       cot=None, factory=None, overview=None)
        self.trees = {"tree_hashes": {"a": "tree"}, "quality_input": {},
                      "tasks": [{"task_id": "a", "trajectory_count": 1}]}
        self.options = {"scoring_version": "checklist_fusion_v1", "evaluation_profile_digest": "frozen"}
        self.pipeline = {"batch_id": "batch", "task_ids": ["a"], "steps": [{"id": "quality", "job_ids": []}]}
        self.result = {"task_id": "a", "scoring_version": "checklist_fusion_v1", "checklist_options": self.options,
                       "fusion_complete": True, "evaluations": {"t": {"fusion": {"valid": True, "score": .8}}}}

    def ready(self, result=None, options=None):
        payload = {"tasks": [result or self.result],
                   "task_fingerprints": quality_task_fingerprints(self.trees, checklist_options=options or self.options)}
        with patch("backend.batch_results.current_tree_payload", return_value=self.trees), \
             patch("backend.batch_results.current_quality_payload", return_value=payload), \
             patch("backend.quality_checklist.freeze_options", side_effect=AssertionError("must not read new defaults")):
            return self.runtime.ready("quality", self.pipeline)

    def test_new_result_uses_its_frozen_options_and_requires_complete_fusion(self):
        self.assertTrue(self.ready())
        incomplete = {**self.result, "fusion_complete": False}
        self.assertFalse(self.ready(incomplete))
        self.assertFalse(self.ready({**self.result, "checklist_options": None}))

    def test_bound_job_prevents_other_scoring_version_from_advancing_pipeline(self):
        self.pipeline["steps"][0]["job_ids"] = ["job"]
        self.runtime.records.put("quality_jobs", "job", {"task_ids": ["a"], "checklist_options": self.options})
        self.assertTrue(self.ready())
        other = {**self.options, "evaluation_profile_digest": "different"}
        self.assertFalse(self.ready({**self.result, "checklist_options": other}, other))

    def test_old_job_and_result_keep_legacy_fingerprint(self):
        self.pipeline["steps"][0]["job_ids"] = ["old"]
        self.runtime.records.put("quality_jobs", "old", {"task_ids": ["a"]})
        result = {"task_id": "a", "evaluations": {"t": {"global_score": 4}}}
        payload = {"tasks": [result], "task_fingerprints": quality_task_fingerprints(self.trees)}
        with patch("backend.batch_results.current_tree_payload", return_value=self.trees), \
             patch("backend.batch_results.current_quality_payload", return_value=payload):
            self.assertTrue(self.runtime.ready("quality", self.pipeline))

    def test_incomplete_job_exposes_checklist_reason_without_advancing(self):
        self.pipeline["steps"][0].update(status="running", job_ids=["job"])
        self.runtime.records.put("quality_jobs", "job", {"job_id": "job", "batch_id": "batch",
            "status": "succeeded", "task_ids": ["a"], "checklist_options": self.options})
        result = deepcopy(self.result)
        result.update(fusion_complete=False)
        result["evaluations"]["t"] = {"global_score": 2.8548,
            "checklist": {"status": "error", "error": "原件目录无法解析 [source_path_invalid]"},
            "fusion": {"valid": False, "score": None, "invalid_reasons": ["checklist_incomplete"]}}
        payload = {"tasks": [result], "task_fingerprints": quality_task_fingerprints(self.trees, checklist_options=self.options)}
        with patch("backend.batch_results.current_tree_payload", return_value=self.trees), \
             patch("backend.batch_results.current_quality_payload", return_value=payload):
            self.assertFalse(self.runtime.ready("quality", self.pipeline))
            manager = PipelineManager(self.runtime, self.root)
            with self.assertRaisesRegex(PipelineError, "任务 a / 轨迹 t.*source_path_invalid"):
                manager._job_step_with_diagnostics(self.pipeline, self.pipeline["steps"][0])
        self.assertEqual(self.pipeline["steps"][0]["status"], "running")
        self.assertEqual(result["evaluations"]["t"]["global_score"], 2.8548)

    def test_failure_summary_distinguishes_stale_and_missing_results(self):
        for payload, reason in [({"tasks": []}, "尚无与当前轨迹树匹配"),
                                ({"tasks": [self.result], "task_fingerprints": {"a": "old"}}, "版本不匹配")]:
            with self.subTest(reason=reason), \
                 patch("backend.batch_results.current_tree_payload", return_value=self.trees), \
                 patch("backend.batch_results.current_quality_payload", return_value=payload):
                self.assertIn(reason, self.runtime.quality_incomplete_message(self.pipeline))

    def test_failure_summary_is_bounded_and_redacts_provider_secrets(self):
        result = deepcopy(self.result)
        result.update(fusion_complete=False, evaluations={f"t{i}": {
            "checklist": {"error": 'api_key="secret-test" ' + "x" * 1000},
            "fusion": {"valid": False}} for i in range(10)})
        payload = {"tasks": [result], "task_fingerprints": quality_task_fingerprints(self.trees, checklist_options=self.options)}
        with patch("backend.batch_results.current_tree_payload", return_value=self.trees), \
             patch("backend.batch_results.current_quality_payload", return_value=payload):
            message = self.runtime.quality_incomplete_message(self.pipeline)
        self.assertNotIn("secret-test", message)
        self.assertIn("[REDACTED]", message)
        self.assertLess(len(message), 1300)
        self.assertNotIn("轨迹 t9", message)

    def test_retry_calls_bound_job_instead_of_new_submission(self):
        self.runtime.quality = Mock()
        step = self.pipeline["steps"][0]
        step.update(retry_job_id="frozen-job", job_ids=[])
        self.runtime.submit("quality", self.pipeline, retry=True)
        self.runtime.quality.retry.assert_called_once_with("frozen-job", batch_id="batch", task_ids=["a"])
        self.runtime.quality.submit.assert_not_called()

    def test_initial_submission_still_uses_normal_settings_freeze(self):
        self.runtime.quality = Mock()
        self.runtime.submit("quality", self.pipeline)
        self.runtime.quality.submit.assert_called_once_with(task_ids=["a"], batch_id="batch")
        self.runtime.quality.retry.assert_not_called()
