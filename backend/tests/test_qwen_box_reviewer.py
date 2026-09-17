from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from PIL import Image

from backend.bounding_box.qwen_reviewer import QwenBoxReviewer


VALID = json.dumps({"decision": "accept", "bbox": [167, 150, 833, 850], "confidence": .95, "reason": "完整目标控件"})


def response(content=VALID, *, finish="stop", reasoning=None):
    return SimpleNamespace(choices=[SimpleNamespace(finish_reason=finish,
        message=SimpleNamespace(content=content, reasoning_content=reasoning, reasoning=reasoning))])


class QwenBoxReviewerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.image = self.root / "image.jpg"
        Image.new("RGB", (120, 200), "white").save(self.image)
        self.cache = self.root / "cache.json"
        self.arguments = {"image_path": self.image, "action": {"action": "click", "coordinate": [50, 60]},
                          "action_summary": "点击按钮", "candidate_bbox": (20, 30, 100, 170),
                          "image_size": (120, 200), "rule_context": {"source": "xml"}, "round_index": 1}
        self.settings = {"proxy": "", "verify": True, "timeout": 2, "trust_env": False,
                         "api_key": "test-key", "base_url": "https://example.invalid/v1"}

    def reviewer(self, client, model="qwen3.8-max"):
        with patch("backend.bounding_box.qwen_reviewer.qwen_settings", return_value=self.settings), \
             patch("backend.bounding_box.qwen_reviewer.httpx.Client"), \
             patch("backend.bounding_box.qwen_reviewer.OpenAI", return_value=client):
            return QwenBoxReviewer(model, self.cache)

    def test_qwen38_requests_json_and_larger_budget_then_reuses_valid_cache(self):
        client = MagicMock()
        client.chat.completions.create.return_value = response(reasoning="not the result")
        result = self.reviewer(client).review(**self.arguments)
        self.assertFalse(result.cached)
        self.assertEqual(result.bbox, (20, 30, 100, 170))
        request = client.chat.completions.create.call_args.kwargs
        self.assertEqual(request["model"], "qwen3.8-max")
        self.assertEqual(request["max_tokens"], 2048)
        self.assertEqual(request["response_format"], {"type": "json_object"})
        stored = json.loads(self.cache.read_text(encoding="utf-8"))
        self.assertEqual(list(stored.values()), [VALID])
        resumed_client = MagicMock()
        resumed = self.reviewer(resumed_client).review(**self.arguments)
        self.assertTrue(resumed.cached)
        resumed_client.chat.completions.create.assert_not_called()

    def test_other_model_keeps_previous_request_parameters(self):
        client = MagicMock()
        client.chat.completions.create.return_value = response()
        self.reviewer(client, "another-model").review(**self.arguments)
        request = client.chat.completions.create.call_args.kwargs
        self.assertEqual(request["max_tokens"], 500)
        self.assertNotIn("response_format", request)

    def test_empty_reasoning_only_truncated_and_invalid_responses_are_not_cached(self):
        cases = [response(None, reasoning=VALID), response("", reasoning=VALID),
                 response(VALID, finish="length"), response("正在分析候选框，尚未输出结果"),
                 response('{"decision":'), response('{"decision":"invalid"}'),
                 response('{"decision":"replace","bbox":[1,2]}')]
        for reply in cases:
            with self.subTest(reply=reply):
                client = MagicMock()
                client.chat.completions.create.return_value = reply
                reviewer = self.reviewer(client)
                with self.assertRaises((ValueError, TypeError)):
                    reviewer.review(**self.arguments)
                self.assertEqual(reviewer.cache, {})
                self.assertFalse(self.cache.exists())

    def test_invalid_existing_entry_retries_without_losing_completed_entries(self):
        client = MagicMock()
        client.chat.completions.create.return_value = response()
        reviewer = self.reviewer(client)
        reviewer.review(**self.arguments)
        first_key = next(iter(reviewer.cache))
        second_arguments = {**self.arguments, "round_index": 2}
        reviewer.review(**second_arguments)
        second_key = next(key for key in reviewer.cache if key != first_key)
        reviewer.cache[second_key] = "previous explanation without JSON"
        self.cache.write_text(json.dumps(reviewer.cache), encoding="utf-8")
        old_bytes = self.cache.read_bytes()

        failing_client = MagicMock()
        failing_client.chat.completions.create.return_value = response(None, reasoning=VALID)
        failing = self.reviewer(failing_client)
        self.assertTrue(failing.review(**self.arguments).cached)
        failing_client.chat.completions.create.assert_not_called()
        with self.assertRaisesRegex(ValueError, "empty"):
            failing.review(**second_arguments)
        self.assertEqual(self.cache.read_bytes(), old_bytes)

        resumed_client = MagicMock()
        resumed_client.chat.completions.create.return_value = response()
        resumed = self.reviewer(resumed_client)
        self.assertFalse(resumed.review(**second_arguments).cached)
        resumed_client.chat.completions.create.assert_called_once()
        values = json.loads(self.cache.read_text(encoding="utf-8"))
        self.assertEqual(values[first_key], VALID)
        self.assertEqual(values[second_key], VALID)
        self.assertTrue(resumed.review(**second_arguments).cached)
        resumed_client.chat.completions.create.assert_called_once()


if __name__ == "__main__":
    unittest.main()
