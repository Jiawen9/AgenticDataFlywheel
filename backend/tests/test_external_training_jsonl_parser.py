import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from backend.training_data_overview.external_jsonl import parse_external_jsonl


def line(task, history, image, *, assistant=None, solution=None, **metadata):
    messages = [{"role": "user", "content": f"## Task\n{task}\n\n## Execution History\n{history}\n\n## Current Screenshot\n<image>"}]
    if assistant is not None:
        messages.append({"role": "assistant", "content": assistant})
    sample = {"messages": messages, "images": [image], **metadata}
    if solution is not None:
        sample["solution"] = solution
    return json.dumps(sample, ensure_ascii=False).encode()


class ExternalTrainingJsonlParserTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name, "样本.jsonl")

    def parse(self, source_format, data, **kwargs):
        self.path.write_bytes(data)
        return parse_external_jsonl(self.path, source_format=source_format, data_source="历史训练",
                                    data_date="2026-11-18", **kwargs)

    def test_flywheel_counts_sparse_image_steps_without_opening_images(self):
        click = '<tool_call>click(bbox=<bbox>[[1,2,3,4]]</bbox>)</tool_call>'
        open_app = '<tool_call>open_app(app_name="测试应用")</tool_call>'
        data = b'\n'.join([
            line("甲", "Step 1: Empty", "does/not/exist/traj01/task_1_step_6.jpg", solution=click),
            line("甲", "Step 1: Empty", "does/not/exist/traj01/task_1_step_9.jpg", solution=click),
            line("乙", "Step 1: Empty", "does/not/exist/traj01/task_2_step_1.jpg", solution=open_app),
        ])
        result = self.parse("data_flywheel", data)
        self.assertTrue(result["valid"], result["errors"])
        self.assertEqual([2, 1], [row["step数量"] for row in result["rows"]])
        self.assertEqual({"click": 2, "open_app": 1}, result["summary"]["action_counts"])
        self.assertEqual(0, result["summary"]["single_step_fallback_count"])

    def test_factory_history_extension_requires_task_parent_and_exact_prefix(self):
        click = '<tool_call>{"action":"click","coordinate":[10,20]}</tool_call>'
        legacy = '<tool_call>{"action":"terminate","status":"success"}}</tool_call>'
        data = b'\n'.join([
            line("同一个任务", "Empty", "some/run/1.jpg", assistant=click),
            line("同一个任务", "Step 1: 打开", "some/run/2.jpg", assistant=legacy),
            line("同一个任务", "Empty", "some/run/2.jpg", assistant=click),
            line("同一个任务", "Step 1: 下一项", "some/run/3.jpg", assistant=click),
            line("同一个任务", "Step 1: 其他\nStep 2: 继续", "some/run/4.jpg", assistant=click),
            line("同一个任务", "Step 1: 其他\nStep 2: 继续\nStep 3: 点击", "some/other-run/5.jpg", assistant=legacy),
        ])
        for source_format in ("factory", "health"):
            with self.subTest(source_format=source_format):
                result = self.parse(source_format, data)
                self.assertTrue(result["valid"], result["errors"])
                self.assertEqual([2, 2, 1, 1], [row["step数量"] for row in result["rows"]])
                self.assertEqual({"click": 4, "terminate": 2}, result["summary"]["action_counts"])
                self.assertEqual(2, result["summary"]["single_step_fallback_count"])
                self.assertEqual([2, 6], [warning["row"] for warning in result["warnings"]])

    def test_middle_shared_image_does_not_define_trajectory(self):
        data = b'\n'.join([
            line("甲", "Empty", "/training/same.png", solution=json.dumps({"sftcall": {"action": "click"}, "gt": {"action": "wait"}})),
            line("乙", "Empty", "/training/same.png", solution={"gt": {"action": "type"}}),
            line("丙", "Empty", "/training/same.png", solution={"sftcall": "not an action", "gt": {"action": "wait"}}),
        ])
        result = self.parse("middle", data)
        self.assertEqual(3, result["summary"]["trajectory_count"])
        self.assertEqual(3, result["summary"]["single_step_fallback_count"])
        self.assertEqual({"click": 1, "type": 1, "unknown": 1}, result["summary"]["action_counts"])
        self.assertEqual([3], [warning["row"] for warning in result["warnings"]])

    def test_preserves_byte_hash_bad_line_positions_and_explicit_metadata(self):
        rows = [line("选项", "Empty", "local/a.jpg", solution={"gt": {"action": "click"}},
                     metadata={"trajectory_id": "explicit", "APP": "地图"}, is_refined=True),
                b'not a record', b'',
                line("选项", "Empty", "local/b.jpg", solution={"gt": {"action": "click"}},
                     trajectory_id="explicit", is_refined=False),
                line("选项", "Empty", "local/c.jpg", solution={"gt": {"action": "wait"}},
                     trajectory_id="explicit", type=4)]
        data = b'\xef\xbb\xbf' + b'\r\n'.join(rows)
        result = self.parse("middle", data, app="缺省应用", level1="出行")
        self.assertTrue(result["valid"], result["errors"])
        self.assertEqual(hashlib.sha256(data).hexdigest(), result["source_sha256"])
        self.assertEqual(len(data), result["source_size"])
        self.assertEqual(5, result["summary"]["line_count"])
        self.assertEqual(1, result["summary"]["excluded_rows"])
        self.assertEqual([2], [warning["row"] for warning in result["warnings"]])
        self.assertEqual(1, result["summary"]["trajectory_count"])
        row = result["rows"][0]
        self.assertEqual(("explicit", "地图", "出行", "未分类"),
                         (row["trajectory_grouping"], row["APP"], row["一级场景"], row["二级场景"]))
        self.assertEqual((1, 2, 1, [1]), (row["人工精修步骤数量"], row["manual_known_steps"],
                                          row["manual_unknown_steps"], row["manual_refined_jsonl_rows"]))

    def test_no_valid_sample_rejects_import_with_issue_location(self):
        result = self.parse("middle", b'{}\n\xff\n')
        self.assertFalse(result["valid"])
        self.assertEqual(2, result["summary"]["excluded_rows"])
        self.assertEqual([1, 2], [warning["row"] for warning in result["warnings"]])
        self.assertEqual("file", result["errors"][0]["field"])


if __name__ == "__main__":
    unittest.main()
