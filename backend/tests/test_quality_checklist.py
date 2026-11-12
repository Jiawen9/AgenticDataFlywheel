import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from backend.data_store import RecordStore
from backend.quality_checklist import build_task, evaluate_task, settings, freeze_options, cancel_remote_jobs


class Remote:
    def __init__(self, root):
        self.records = RecordStore(root)
        self.calls = []
        self.fail_first = False
    def _remote_result(self, args):
        self.calls.append(args)
        if args[0] == "capabilities":
            return {"quality_checklist_jobs": {"version": 1}}
        if args[0] == "quality-checklist-start":
            request = json.loads(args[args.index("--payload")+1])
            return {"job_id": request["request_id"]}
        if args[0] == "quality-checklist-status":
            return {"status": "completed"}
        if args[0] == "quality-checklist-results":
            status = "error" if self.fail_first else "complete"
            return {"items": [{"task_id": "t", "trajectory_id": "a", "checklist": {"status": status, "definition_digest": "d"}}], "has_more": False}
        return {"ok": True}


class QualityChecklistTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.remote = Remote(self.root)
        self.task = {"task_id": "t", "case_id": "original", "task": "test", "key_elements": "", "trajectories": [{"trajectory_id": "a", "source_directory": str(self.root)}]}
        self.job = {"job_id": "job", "checklist_options": {"collector_server": "http://localhost:9011",
                    "evaluator_config": {}, "evaluation_profile_id": "rules", "evaluation_profile_digest": "digest"}}

    def test_successful_remote_result_is_reused_without_repeat_evaluation(self):
        first = evaluate_task(self.root, self.job, self.task, "input", store=self.remote, poll_seconds=0)
        count = len(self.remote.calls)
        self.assertEqual(evaluate_task(self.root, {**self.job, "job_id": "newjob"}, self.task, "input", store=self.remote), first)
        self.assertEqual(len(self.remote.calls), count)
        path = next((self.root / "system" / "quality_checklist").glob("*/manifest.json"))
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["tasks"], [self.task])
        self.assertEqual(list(self.root.glob("**/*.jpg")), [])

    def test_failed_checklist_retry_keeps_request_and_uses_retry_endpoint(self):
        self.remote.fail_first = True
        evaluate_task(self.root, self.job, self.task, "input", store=self.remote, poll_seconds=0)
        self.remote.fail_first = False
        result = evaluate_task(self.root, {**self.job, "job_id": "retryjob"}, self.task, "input", store=self.remote, poll_seconds=0)
        self.assertEqual(result["a"]["status"], "complete")
        requests = [json.loads(c[c.index("--payload")+1])["request_id"] for c in self.remote.calls if c[0] == "quality-checklist-start"]
        self.assertEqual(len(set(requests)), 1)
        self.assertTrue(any(c[0] == "quality-checklist-retry" for c in self.remote.calls))

    def test_source_mapping_uses_identity_and_does_not_change_original(self):
        original = self.root / "_trajectory_for_evaluate.json"
        original.write_text('{"key_elements":"priority_1: target"}', encoding="utf-8")
        before = original.read_bytes()
        trajectory = SimpleNamespace(trajectory_id="visible", metadata={"source_directory": str(self.root), "source_identity": {"task_id": "original"}})
        result = build_task("t", SimpleNamespace(instruction="task", context={}), [trajectory])
        self.assertEqual(result["case_id"], "original")
        self.assertEqual(result["key_elements"], "priority_1: target")
        self.assertEqual(original.read_bytes(), before)

    def test_unique_profile_default_and_multiple_profiles_require_selection(self):
        with patch("backend.model_iteration.get_profiles", return_value={"profiles": [{"id": "r", "name": "r", "digest": "d"}]}):
            self.assertEqual(settings(self.root, store=self.remote)["evaluation_profile_id"], "r")
        with patch("backend.model_iteration.get_profiles", return_value={"profiles": [{"id": "r", "name": "r", "digest": "d"}, {"id": "r2", "name": "r2", "digest": "d2"}]}):
            self.assertEqual(settings(self.root, store=self.remote)["evaluation_profile_id"], "")

    def test_structured_key_elements_remain_json_not_python_repr(self):
        trajectory = SimpleNamespace(trajectory_id="visible", metadata={"source_directory": str(self.root),
            "key_elements": {"priority_1": ["到达目标", "保留登录"]}})
        result = build_task("t", SimpleNamespace(instruction="task", context={}), [trajectory])
        self.assertEqual(json.loads(result["key_elements"]), trajectory.metadata["key_elements"])

    def test_frozen_config_is_independent_of_later_settings(self):
        from backend.model_iteration import default_config
        config = default_config()
        for role in config:
            config[role].update(base_url="http://models/v1", model="actual-model")
        profile = {"id": "rules", "name": "Rules", "digest": "a" * 64}
        with patch("backend.model_iteration.get_profiles", return_value={"profiles": [profile]}), \
             patch("backend.model_iteration.get_config", return_value=config):
            frozen = freeze_options(self.root, store=self.remote)
        config["text"]["model"] = "new-default"
        self.assertEqual(frozen["evaluator_config"]["text"]["model"], "actual-model")
        self.assertEqual(frozen["evaluation_profile_digest"], profile["digest"])

    def test_parent_cleanup_is_generation_fenced_and_does_not_touch_new_owner(self):
        self.remote.records.put("quality_checklist_by_job", "old", {"keys": ["owned", "reused"]})
        common = {"remote_job_id": "remote", "collector_server": "http://localhost:9011", "remote_generation": 4}
        self.remote.records.put("quality_checklist_jobs", "owned", {**common, "last_quality_job_id": "old"})
        self.remote.records.put("quality_checklist_jobs", "reused", {**common, "last_quality_job_id": "new"})
        cancel_remote_jobs(self.root, "old", store=self.remote)
        self.assertEqual(len(self.remote.calls), 1)
        command = self.remote.calls[0]
        self.assertEqual(command[0], "quality-checklist-cancel")
        self.assertEqual(json.loads(command[command.index("--payload") + 1])["expected_generation"], 4)
        self.assertTrue(self.remote.records.get("quality_checklist_jobs", "owned")["retryable"])
        self.assertNotIn("retryable", self.remote.records.get("quality_checklist_jobs", "reused"))

    def test_lost_acceptance_without_id_is_not_recreated_by_cleanup(self):
        self.remote.records.put("quality_checklist_by_job", "old", {"keys": ["key"]})
        self.remote.records.put("quality_checklist_jobs", "key", {"last_quality_job_id": "old", "request_id": "stable"})
        cancel_remote_jobs(self.root, "old", store=self.remote)
        self.assertEqual(self.remote.calls, [])


if __name__ == "__main__":
    unittest.main()
