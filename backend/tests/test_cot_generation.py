import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from backend.trajectory_correction.cot_generator import QwenCotGenerator, parse_cot_response
from backend.trajectory_correction.cot_jobs import _bbox_reviewer


class CotResponseTests(unittest.TestCase):
    def test_parses_strict_response_and_keeps_expected_action(self):
        action = {"action": "click", "coordinate": [848, 125]}
        raw = (
            "<thought>当前截图显示桌面上的应用图标。</thought>"
            "<tool_call>{\"action\": \"click\", \"coordinate\": [848, 125]}</tool_call>"
            "<summary>点击目标应用图标以打开应用</summary>"
        )
        self.assertEqual(
            parse_cot_response(raw, action),
            {"thought": "当前截图显示桌面上的应用图标。", "summary": "点击目标应用图标以打开应用"},
        )

    def test_ignores_tool_call_content(self):
        raw = "<thought>当前页面保持不变。</thought><tool_call>not parsed</tool_call><summary>等待</summary>"
        self.assertEqual(
            parse_cot_response(raw, {"action": "click", "coordinate": [1, 2]}),
            {"thought": "当前页面保持不变。", "summary": "等待"},
        )

    def test_accepts_response_without_tool_call(self):
        raw = "<thought>当前页面保持不变。</thought><summary>等待</summary>"
        self.assertEqual(
            parse_cot_response(raw),
            {"thought": "当前页面保持不变。", "summary": "等待"},
        )

    def test_rejects_missing_summary(self):
        raw = "<thought>当前页面保持不变。</thought><tool_call>{\"action\":\"wait\"}</tool_call><summary></summary>"
        with self.assertRaises(ValueError):
            parse_cot_response(raw, {"action": "wait"})

    def test_rejects_observe_tag(self):
        raw = "<observe>旧格式</observe><tool_call>{\"action\":\"wait\"}</tool_call><summary>等待</summary>"
        with self.assertRaisesRegex(ValueError, "thought"):
            parse_cot_response(raw, {"action": "wait"})

    def test_rejects_empty_thought(self):
        raw = "<thought></thought><tool_call>{\"action\":\"wait\"}</tool_call><summary>等待</summary>"
        with self.assertRaises(ValueError):
            parse_cot_response(raw, {"action": "wait"})

    def test_accepts_all_new_tags_when_gateway_reorders_sections(self):
        raw = (
            '<summary>等待</summary><tool_call>{"action":"wait"}</tool_call>'
            '<thought>当前页面保持不变。</thought>'
        )
        self.assertEqual(
            parse_cot_response(raw, {"action": "wait"}),
            {"thought": "当前页面保持不变。", "summary": "等待"},
        )

    def test_uses_cot_specific_model_configuration(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            env_file = Path(temp_dir) / ".env"
            env_file.write_text(
                "YUNAI_API_KEY=test-key\n"
                "MODEL_URL=https://example.invalid/v1\n"
                "MODEL_NAME=bbox-model\n"
                "COT_MODEL_NAME=qwen3-vl-32b-instruct\n",
                encoding="utf-8",
            )
            with patch("openai.OpenAI"):
                generator = QwenCotGenerator(env_file=env_file, cache_dir=Path(temp_dir) / "cache")
            self.assertEqual(generator.model, "qwen3-vl-32b-instruct")

    def test_generate_sends_thought_prompt_and_caches_thought_summary(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            image = root / "step.jpg"
            image.write_bytes(b"fake-jpeg")
            create = Mock(
                return_value=SimpleNamespace(
                    choices=[SimpleNamespace(message=SimpleNamespace(content=(
                        "<thought>当前桌面显示爱奇艺图标，应点击该图标打开应用。</thought>"
                        "<tool_call>{\"action\":\"click\",\"coordinate\":[848,125]}</tool_call>"
                        "<summary>点击爱奇艺图标以打开应用</summary>"
                    )))]
                )
            )
            generator = QwenCotGenerator.__new__(QwenCotGenerator)
            generator.model = "qwen3-vl-32b-instruct"
            generator.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
            generator.cache_dir = root / "cache"

            result = generator.generate(
                task="打开爱奇艺",
                trajectory_id="trajectory-1",
                step=1,
                history="",
                action={"action": "click", "coordinate": [848, 125]},
                image=image,
                reference_answer="click(bbox=<bbox>[793,67,899,174]</bbox>)",
            )

            self.assertEqual(result["thought"], "当前桌面显示爱奇艺图标，应点击该图标打开应用。")
            self.assertEqual(create.call_args.kwargs["model"], "qwen3-vl-32b-instruct")
            self.assertEqual(create.call_args.kwargs["extra_body"]["top_k"], 0)
            self.assertIn("<thought>", create.call_args.kwargs["messages"][0]["content"])
            user_prompt = create.call_args.kwargs["messages"][1]["content"][0]["text"]
            self.assertIn('## Reference Answer\n{"action": "click", "coordinate": [848, 125]}', user_prompt)
            self.assertNotIn("<bbox>", user_prompt)
            self.assertNotIn("## Current Action", user_prompt)
            cached = json.loads(next(generator.cache_dir.glob("*.json")).read_text(encoding="utf-8"))
            self.assertEqual(cached["content_tag"], "thought_summary")

            reused = generator.generate(
                task="打开爱奇艺",
                trajectory_id="trajectory-1",
                step=1,
                history="",
                action={"action": "click", "coordinate": [848, 125]},
                image=image,
                reference_answer="click(bbox=<bbox>[1,2,3,4]</bbox>)",
            )
            self.assertTrue(reused["cached"])
            create.assert_called_once()

    def test_reference_answer_uses_corrected_action_without_bbox(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            image = root / "step.jpg"
            image.write_bytes(b"fake-jpeg")
            create = Mock(
                return_value=SimpleNamespace(
                    choices=[SimpleNamespace(message=SimpleNamespace(content=(
                        "<thought>当前页面需要等待。</thought><summary>等待页面加载</summary>"
                    )))]
                )
            )
            generator = QwenCotGenerator.__new__(QwenCotGenerator)
            generator.model = "qwen3-vl-32b-instruct"
            generator.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
            generator.cache_dir = root / "cache"

            generator.generate(
                task="等待页面加载",
                trajectory_id="trajectory-1",
                step=2,
                history="Step 1: 打开应用",
                action={"action": "wait"},
                image=image,
            )

            user_prompt = create.call_args.kwargs["messages"][1]["content"][0]["text"]
            self.assertIn('## Reference Answer\n{"action": "wait"}', user_prompt)
            self.assertNotIn("## Current Action", user_prompt)

    def test_bbox_reviewer_keeps_the_general_model(self):
        settings = {
            "YUNAI_API_KEY": "test-key",
            "MODEL_URL": "https://example.invalid/v1",
            "MODEL_NAME": "bbox-model",
            "COT_MODEL_NAME": "qwen3-vl-32b-instruct",
        }
        reviewer = object()
        with (
            patch.dict(os.environ, {}, clear=False),
            patch("backend.trajectory_correction.cot_jobs.read_env", return_value=settings),
            patch("backend.trajectory_correction.cot_jobs.QwenBoxReviewer", return_value=reviewer) as reviewer_class,
        ):
            self.assertIs(_bbox_reviewer(), reviewer)
            self.assertEqual(os.environ["TRAJECTORY_MODEL"], "bbox-model")
        reviewer_class.assert_called_once_with(
            model="bbox-model",
            cache_path=reviewer_class.call_args.kwargs["cache_path"],
        )

if __name__ == "__main__":
    unittest.main()
