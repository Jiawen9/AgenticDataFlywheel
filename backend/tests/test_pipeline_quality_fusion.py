"""Quality readiness uses the submitted configuration, never current UI defaults."""
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from backend.batch_results import quality_task_fingerprints
from backend.pipelines import PipelineRuntime


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
