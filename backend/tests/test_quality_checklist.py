from copy import deepcopy
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

    def _evaluate_result_rows(self, rows, *, error=None):
        self.task["trajectories"] = [{"trajectory_id": row["trajectory_id"], "source_directory": str(self.root)} for row in rows]
        state = {"status": "partial", "error": error}
        page = {"items": rows, "has_more": False}
        before = deepcopy((state, page))
        original = self.remote._remote_result

        def respond(args):
            if args[0] == "quality-checklist-status":
                return state
            if args[0] == "quality-checklist-results":
                return page
            return original(args)

        with patch.object(self.remote, "_remote_result", side_effect=respond):
            result = evaluate_task(self.root, self.job, self.task, "input", store=self.remote, poll_seconds=0)
        self.assertEqual((state, page), before, "reading results must not modify the collector response")
        saved = self.remote.records.list("quality_checklist_jobs")
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0]["results"], result)
        return result

    def test_failed_nonempty_checklist_preserves_redacted_bounded_row_error(self):
        checklist = {"status": "error", "definition_digest": "d", "source": "gold",
                     "total": 5, "passed": 0, "failed": 0, "unchecked": 5}
        error = ("ValueError: source_directory is outside configured evaluation input roots or is missing; "
                 "api_key=not-for-display Authorization: Bearer private-bearer " + "x" * 2500)
        result = self._evaluate_result_rows([{"task_id": "t", "trajectory_id": "a", "status": "failed",
                                             "checklist": checklist, "error": error}])["a"]
        self.assertEqual({key: value for key, value in result.items() if key != "error"}, checklist)
        self.assertIn("source_directory is outside configured evaluation input roots", result["error"])
        self.assertNotIn("not-for-display", result["error"])
        self.assertNotIn("private-bearer", result["error"])
        self.assertLessEqual(len(result["error"]), 2000)

    def test_nested_error_precedes_row_and_job_errors(self):
        result = self._evaluate_result_rows([{"task_id": "t", "trajectory_id": "a", "status": "failed",
            "checklist": {"status": "incomplete", "definition_digest": "d", "error": "specific image unreadable; api_key=nested-secret"},
            "error": "less specific row error"}], error="less specific job error")["a"]
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(result["definition_digest"], "d")
        self.assertIn("specific image unreadable", result["error"])
        self.assertNotIn("nested-secret", result["error"])
        self.assertNotIn("less specific", result["error"])

    def test_job_error_only_fills_unfinished_results(self):
        complete = {"status": "complete", "definition_digest": "d", "source": "gold", "verdict": "success"}
        result = self._evaluate_result_rows([
            {"task_id": "t", "trajectory_id": "a", "status": "completed", "checklist": complete},
            {"task_id": "t", "trajectory_id": "b", "status": "failed", "checklist": {"status": "error"}},
            {"task_id": "t", "trajectory_id": "c", "status": "failed", "checklist": {"status": "error"}, "error": "individual failure"},
        ], error="job storage unavailable; password=job-secret")
        self.assertEqual(result["a"], complete)
        self.assertIn("job storage unavailable", result["b"]["error"])
        self.assertNotIn("job-secret", result["b"]["error"])
        self.assertEqual(result["c"]["error"], "individual failure")

    def test_failed_result_without_any_error_gets_explanation(self):
        result = self._evaluate_result_rows([{"task_id": "t", "trajectory_id": "a", "status": "failed",
                                             "checklist": {"status": "error"}}])["a"]
        self.assertEqual(result["status"], "error")
        self.assertTrue(result["error"])
        self.assertRegex(result["error"], "[\u4e00-\u9fff]")

    def test_source_mapping_uses_identity_and_does_not_change_original(self):
        original = self.root / "_trajectory_for_evaluate.json"
        original.write_text('{"key_elements":"priority_1: target"}', encoding="utf-8")
        before = original.read_bytes()
        trajectory = SimpleNamespace(trajectory_id="visible", metadata={"source_directory": str(self.root), "source_identity": {"task_id": "original"}})
        result = build_task("t", SimpleNamespace(instruction="task", context={}), [trajectory])
        self.assertEqual(result["case_id"], "original")
        self.assertEqual(result["key_elements"], "priority_1: target")
        self.assertEqual(original.read_bytes(), before)

    def test_checklist_uses_frozen_plain_task_without_changing_rubric_input(self):
        from backend.DevelopRubrics.trajectory_tools.gui_trajectory_excel import SYSTEM_PROMPT
        text = "打开爱奇艺，进入我的页面\n确认登录状态。"
        task = SimpleNamespace(instruction=f"{SYSTEM_PROMPT}\n\nTask:\n{text}",
                               context={"task_description": text})
        before = deepcopy(vars(task))
        trajectory = SimpleNamespace(trajectory_id="a", metadata={"source_directory": str(self.root)})
        result = build_task("t", task, [trajectory])
        self.assertEqual(result["task"], text)
        self.assertEqual(vars(task), before)

    def test_exact_legacy_gui_wrapper_is_removed_without_context(self):
        from backend.DevelopRubrics.trajectory_tools.gui_trajectory_excel import SYSTEM_PROMPT
        trajectory = SimpleNamespace(trajectory_id="a", metadata={"source_directory": str(self.root)})
        for newline in ("\n", "\r\n"):
            with self.subTest(newline=repr(newline)):
                text = "搜索 Task: 文本，再返回主页"
                instruction = SYSTEM_PROMPT + newline * 2 + "Task:" + newline + text
                task = SimpleNamespace(instruction=instruction, context={})
                self.assertEqual(build_task("t", task, [trajectory])["task"], text)
                self.assertEqual(task.instruction, instruction)

    def test_plain_instruction_and_unknown_wrappers_are_not_guessed(self):
        trajectory = SimpleNamespace(trajectory_id="a", metadata={"source_directory": str(self.root)})
        for text in ("打开搜索，输入 Task: 示例", "Unknown instructions\n\nTask:\n打开页面"):
            with self.subTest(text=text):
                task = SimpleNamespace(instruction=text, context={})
                self.assertEqual(build_task("t", task, [trajectory])["task"], text)

    def test_frozen_task_is_not_replaced_with_another_original_task(self):
        source = self.root / "_trajectory_for_evaluate.json"
        source.write_text(json.dumps({"task": "另一项任务", "key_elements": "打开页面"}), encoding="utf-8")
        before = source.read_bytes()
        trajectory = SimpleNamespace(trajectory_id="a", metadata={"source_directory": str(self.root)})
        task = SimpleNamespace(instruction="Rubric instruction", context={"task_description": "本次冻结任务"})
        self.assertEqual(build_task("t", task, [trajectory])["task"], "本次冻结任务")
        self.assertEqual(source.read_bytes(), before)

    def test_unique_profile_default_and_multiple_profiles_require_selection(self):
        with patch("backend.quality_checklist_config._profiles", return_value={"profiles": [{"id": "r", "name": "r", "digest": "d"}]}):
            self.assertEqual(settings(self.root, store=self.remote)["evaluation_profile_id"], "r")
        with patch("backend.quality_checklist_config._profiles", return_value={"profiles": [{"id": "r", "name": "r", "digest": "d"}, {"id": "r2", "name": "r2", "digest": "d2"}]}):
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
        from backend.quality_checklist_config import _profiles
        profile = _profiles()["profiles"][0]
        self.remote.records.put('model_iter_settings', 'evaluator', {'config': config})
        with patch("backend.quality_checklist_config.preflight"):
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
