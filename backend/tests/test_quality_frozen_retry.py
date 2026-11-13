"""Checklist recovery retains the original job input, model and rule snapshot."""
from copy import deepcopy
from unittest import TestCase
from unittest.mock import patch

from backend.batch_results import current_tree_payload, merge_quality_results, StaleTaskInput
from backend.pipeline_access import internal_pipeline, PipelineManagedError
from backend.quality_jobs import QualityJobManager
from backend.tests import test_batch_results as fixtures


class QualityFrozenRetryTests(TestCase):
    setUp = fixtures.BatchResultsTests.setUp
    incoming = fixtures.BatchResultsTests.incoming
    build = fixtures.BatchResultsTests.build

    def manager(self):
        self.build("A")
        self.build("B")
        self.executor = fixtures.DeferredExecutor()
        self.jobs = QualityJobManager(self.root / "system" / "quality_jobs",
                                      executor=self.executor, data_root=self.root)
        self.options = {"scoring_version": "checklist_fusion_v1", "evaluation_profile_id": "original",
                        "evaluation_profile_digest": "frozen", "collector_server": "http://original.invalid",
                        "evaluator_config": {"vision": {"model": "actual-original-id"}}}
        return self.jobs

    def submit(self, tasks=None, *, legacy=False):
        with patch("backend.quality_checklist.freeze_options", return_value=None if legacy else self.options):
            return self.jobs.submit(batch_id=self.batch, task_ids=tasks or ["A"])

    def finish(self, job, *, complete=False, status="succeeded"):
        self.jobs.records.update("quality_jobs", job["job_id"], lambda row: row.update(status=status))
        if status == "succeeded":
            merge_quality_results(self.batch, [{"task_id": task, "scoring_version": "checklist_fusion_v1",
                "fusion_complete": complete, "evaluations": {task + "-1": {"global_score": 3.5,
                "checklist": {"status": "complete" if complete else "error", "error": None if complete else "source_directory_relative"},
                "fusion": {"valid": complete, "score": .7 if complete else None}}}}
                for task in job["task_ids"]], job["source_tree_hashes"], job["task_fingerprints"],
                job_id=job["job_id"], completed_at="2026-11-13T00:00:00Z", root=self.root)
        self.executor.pending.clear()

    def test_partial_success_uses_frozen_options_and_preserves_original_job_and_tree(self):
        self.manager()
        first = self.submit()
        self.finish(first)
        original = self.jobs.get(first["job_id"])
        trees_before = deepcopy(current_tree_payload(self.batch, self.root))
        with patch("backend.quality_checklist.freeze_options", side_effect=AssertionError("must not read page defaults")):
            retry = self.jobs.retry(first["job_id"], batch_id=self.batch, task_ids=["A", "B"])
        self.assertNotEqual(retry["job_id"], first["job_id"])
        self.assertEqual(retry["task_ids"], ["A"])
        self.assertEqual(retry["checklist_options"], self.options)
        self.assertEqual(retry["task_fingerprints"], first["task_fingerprints"])
        self.assertEqual(retry["source_tree_hashes"], first["source_tree_hashes"])
        self.assertEqual(self.jobs.get(first["job_id"]), original)
        self.assertEqual(current_tree_payload(self.batch, self.root), trees_before)
        self.assertEqual(len(self.executor.pending), 1)

    def test_repeated_retry_returns_same_active_job_without_another_submission(self):
        self.manager()
        first = self.submit()
        self.finish(first)
        one = self.jobs.retry(first["job_id"])
        two = self.jobs.retry(first["job_id"])
        self.assertEqual(one["job_id"], two["job_id"])
        self.assertEqual(len(self.executor.pending), 1)

    def test_retry_active_original_returns_original(self):
        self.manager()
        first = self.submit()
        retry = self.jobs.retry(first["job_id"])
        self.assertEqual(retry["job_id"], first["job_id"])
        self.assertEqual(len(self.executor.pending), 1)

    def test_complete_result_is_reused_without_reevaluation(self):
        self.manager()
        first = self.submit()
        self.finish(first, complete=True)
        retry = self.jobs.retry(first["job_id"])
        self.assertEqual(retry["job_id"], first["job_id"])
        self.assertEqual(self.executor.pending, [])

    def test_legacy_none_stays_none_instead_of_enabling_checklist(self):
        self.manager()
        first = self.submit(legacy=True)
        self.finish(first, status="failed")
        with patch("backend.quality_checklist.freeze_options", side_effect=AssertionError("must keep legacy mode")):
            retry = self.jobs.retry(first["job_id"])
        self.assertIsNone(retry.get("checklist_options"))
        self.assertNotIn("scoring_version", retry)
        self.assertEqual(retry["task_fingerprints"], first["task_fingerprints"])

    def test_changed_source_tree_or_input_is_rejected_before_enqueue(self):
        self.manager()
        first = self.submit()
        self.finish(first)
        for field, value in (("tree_hashes", {"A": "changed"}),
                             ("quality_input", {"sheets": {"Tasks": [{"task_id": "A", "task_text": "changed"}]}})):
            with self.subTest(field=field):
                trees = current_tree_payload(self.batch, self.root)
                trees[field] = value
                with patch("backend.quality_jobs.current_tree_payload", return_value=trees):
                    with self.assertRaisesRegex(StaleTaskInput, "冻结输入"):
                        self.jobs.retry(first["job_id"])
                self.assertEqual(self.executor.pending, [])

    def test_changed_rubric_configuration_is_rejected_not_silently_frozen_again(self):
        self.manager()
        first = self.submit()
        self.finish(first)
        with patch("backend.quality_jobs.quality_task_fingerprints", return_value={"A": "new-model"}), \
             patch("backend.quality_checklist.freeze_options", side_effect=AssertionError("must not refreeze")):
            with self.assertRaises(StaleTaskInput):
                self.jobs.retry(first["job_id"])
        self.assertEqual(self.executor.pending, [])

    def test_missing_job_wrong_batch_wrong_task_and_missing_fingerprint_are_rejected(self):
        self.manager()
        first = self.submit()
        self.finish(first)
        with self.assertRaisesRegex(ValueError, "不存在"):
            self.jobs.retry("missing")
        with self.assertRaisesRegex(ValueError, "不属于"):
            self.jobs.retry(first["job_id"], batch_id="another-batch")
        with self.assertRaisesRegex(ValueError, "任务范围"):
            self.jobs.retry(first["job_id"], task_ids=["B"])
        def remove_fingerprint(row):
            row.pop("task_fingerprints", None)
        self.jobs.records.update("quality_jobs", first["job_id"], remove_fingerprint)
        with self.assertRaises(StaleTaskInput):
            self.jobs.retry(first["job_id"])
        self.assertEqual(self.executor.pending, [])

    def test_retry_does_not_expand_into_previously_reused_tasks(self):
        self.manager()
        a = self.submit(["A"])
        self.finish(a, complete=True)
        both = self.submit(["A", "B"])
        self.assertEqual(both["task_ids"], ["B"])
        self.finish(both)
        retry = self.jobs.retry(both["job_id"], batch_id=self.batch, task_ids=["A", "B"])
        self.assertEqual(retry["task_ids"], ["B"])
        self.assertEqual(retry["checklist_options"], self.options)
        self.assertEqual(len(self.executor.pending), 1)

    def test_retry_preserves_pipeline_write_authority(self):
        self.manager()
        first = self.submit()
        self.finish(first)
        self.jobs.records.put("pipeline_owners", self.batch, {"pipeline_id": "flow"})
        self.jobs.records.put("pipelines", "flow", {"pipeline_id": "flow", "batch_id": self.batch, "status": "failed"})
        with self.assertRaises(PipelineManagedError):
            self.jobs.retry(first["job_id"])
        with internal_pipeline("flow"):
            retry = self.jobs.retry(first["job_id"])
        self.assertEqual(retry["pipeline_id"], "flow")

    def test_managed_submit_rejects_before_freezing_or_contacting_remote(self):
        self.manager()
        self.jobs.records.put("pipeline_owners", self.batch, {"pipeline_id": "flow"})
        self.jobs.records.put("pipelines", "flow", {"pipeline_id": "flow", "batch_id": self.batch, "status": "running"})
        with patch("backend.quality_checklist.freeze_options", side_effect=AssertionError("unauthorized remote request")) as freeze:
            with self.assertRaises(PipelineManagedError):
                self.jobs.submit(batch_id=self.batch, task_ids=["A"])
            freeze.assert_not_called()
        self.assertEqual(self.executor.pending, [])
        self.assertEqual(self.jobs.list_jobs(), [])
        with internal_pipeline("flow"), patch("backend.quality_checklist.freeze_options", return_value=self.options) as freeze:
            job = self.jobs.submit(batch_id=self.batch, task_ids=["A"])
        freeze.assert_called_once_with(self.root)
        self.assertEqual(job["pipeline_id"], "flow")

    def test_legacy_alias_uses_actual_batch_for_preflight_write_authority(self):
        self.manager()
        self.jobs.records.put("batch_run_aliases", "old-tree", {"batch_id": self.batch})
        self.jobs.records.put("pipeline_owners", self.batch, {"pipeline_id": "flow"})
        self.jobs.records.put("pipelines", "flow", {"pipeline_id": "flow", "batch_id": self.batch, "status": "running"})
        with patch("backend.batch_results.resolve_current_batch_id", return_value=self.batch), \
             patch("backend.quality_checklist.freeze_options", side_effect=AssertionError("unauthorized remote request")) as freeze:
            with self.assertRaises(PipelineManagedError):
                self.jobs.submit(run_id="old-tree", task_ids=["A"])
            freeze.assert_not_called()
        self.assertEqual(self.executor.pending, [])

    def test_submit_rechecks_ownership_after_remote_freezing(self):
        self.manager()
        def ownership_changes(_root):
            self.jobs.records.put("pipeline_owners", self.batch, {"pipeline_id": "other-flow"})
            self.jobs.records.put("pipelines", "other-flow", {"pipeline_id": "other-flow", "batch_id": self.batch, "status": "running"})
            return self.options
        with patch("backend.quality_checklist.freeze_options", side_effect=ownership_changes):
            with self.assertRaises(PipelineManagedError):
                self.jobs.submit(batch_id=self.batch, task_ids=["A"])
        self.assertEqual(self.executor.pending, [])
        self.assertEqual(self.jobs.list_jobs(), [])

