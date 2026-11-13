from __future__ import annotations

import json
import hashlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from PIL import Image

from backend.bounding_box.qwen_reviewer import BoxReviewOutputError, QwenBoxReviewer, _parse_response
from backend.pipeline_retry_errors import failure_from_exception, failure_from_payload


def reply(body):
    return SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content=body))])


ACCEPT = json.dumps({"decision": "accept", "bbox": [1, 2, 3, 4], "confidence": .9})
GOOD = json.dumps({"decision": "replace", "bbox": [100, 200, 300, 400], "confidence": .9})
OUTSIDE = json.dumps({"decision": "replace", "bbox": [1100, 930, 1150, 990], "confidence": .9})


class BoxReviewRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.image = self.root / "trajectory-one" / "step002_vla_input_stability.jpg"
        self.image.parent.mkdir()
        Image.new("RGB", (1200, 2664), "white").save(self.image)
        self.cache = self.root / "cache.json"
        self.settings = {"proxy": "", "verify": True, "timeout": 2, "trust_env": False,
                         "api_key": "test-private-key", "base_url": "https://example.invalid/v1"}
        self.args = dict(image_path=self.image, action={"action": "click", "coordinate": [200, 700]},
                         action_summary="点击按钮", candidate_bbox=(100, 500, 400, 1000),
                         image_size=(1200, 2664), rule_context={"source": "xml"}, round_index=1)

    def reviewer(self, client):
        with patch("backend.bounding_box.qwen_reviewer.httpx.Client"), \
             patch("backend.bounding_box.qwen_reviewer.OpenAI", return_value=client):
            return QwenBoxReviewer("test-model", self.cache, client_settings=self.settings)

    def diagnostic(self):
        paths = list((self.root / "box_failures").glob("*.json"))
        self.assertEqual(len(paths), 1)
        return paths[0], json.loads(paths[0].read_text(encoding="utf-8"))

    def test_outside_frame_is_recorded_before_clamp_and_corrected_with_feedback(self):
        client = MagicMock()
        client.chat.completions.create.side_effect = [reply(OUTSIDE), reply(GOOD)]
        result = self.reviewer(client).review(**self.args)
        self.assertEqual(result.bbox, (120, 533, 360, 1066))
        self.assertEqual(client.chat.completions.create.call_count, 2)
        second = client.chat.completions.create.call_args.kwargs["messages"]
        self.assertIn("不能使用像素坐标", second[-1]["content"])
        self.assertIn("截图像素", second[1]["content"][0]["text"])
        _, saved = self.diagnostic()
        self.assertTrue(saved["resolved"])
        detail = saved["attempts"][0]["details"]
        self.assertEqual(detail["model_bbox"], [1100, 930, 1150, 990])
        self.assertEqual(detail["pixel_bbox_before_clamp"][:1], [1320])
        self.assertEqual([detail["pixel_bbox_after_clamp"][i] for i in (0, 2)], [1198, 1199])
        self.assertEqual(list(json.loads(self.cache.read_text()).values()), [GOOD])

    def test_accepting_same_one_pixel_candidate_is_corrected_by_replace(self):
        client = MagicMock()
        client.chat.completions.create.side_effect = [reply(ACCEPT), reply(GOOD)]
        args = {**self.args, "candidate_bbox": (1198, 2482, 1199, 2639)}
        self.reviewer(client).review(**args)
        _, saved = self.diagnostic()
        self.assertEqual(saved["attempts"][0]["failure_code"], "bbox_too_small")
        self.assertEqual(saved["attempts"][0]["details"]["decision"], "accept")
        self.assertIn("必须 replace", client.chat.completions.create.call_args.kwargs["messages"][-1]["content"])

    def test_three_invalid_responses_stop_with_safe_classification_and_evidence(self):
        client = MagicMock()
        client.chat.completions.create.return_value = reply(OUTSIDE)
        with self.assertRaises(BoxReviewOutputError) as caught:
            self.reviewer(client).review(**self.args)
        self.assertEqual(client.chat.completions.create.call_count, 3)
        self.assertFalse(self.cache.exists())
        path, saved = self.diagnostic()
        self.assertIn(str(path), str(caught.exception))
        self.assertEqual(len(saved["attempts"]), 3)
        self.assertFalse(saved["resolved"])
        wrapped = RuntimeError("failed to annotate trajectory/step002")
        wrapped.__cause__ = caught.exception
        failure = failure_from_exception(wrapped)
        self.assertEqual(failure["category"], "model_output")
        self.assertEqual(failure["code"], "bbox_invalid")
        self.assertTrue(failure["retryable"])
        self.assertEqual(failure_from_payload(failure), failure)
        self.assertNotIn("details", failure)
        self.assertNotIn("raw_response", failure)

    def test_managed_single_attempt_next_job_uses_saved_safe_feedback(self):
        first = MagicMock()
        first.chat.completions.create.return_value = reply(OUTSIDE)
        reviewer = self.reviewer(first)
        reviewer.max_output_attempts = 1
        with self.assertRaises(BoxReviewOutputError):
            reviewer.review(**self.args)
        first.chat.completions.create.assert_called_once()
        second = MagicMock()
        second.chat.completions.create.return_value = reply(GOOD)
        resumed = self.reviewer(second)
        resumed.max_output_attempts = 1
        resumed.review(**self.args)
        second.chat.completions.create.assert_called_once()
        self.assertIn("不能使用像素坐标", second.chat.completions.create.call_args.kwargs["messages"][-1]["content"])
        self.assertTrue(self.diagnostic()[1]["resolved"])
        self.assertTrue(resumed.review(**self.args).cached)
        second.chat.completions.create.assert_called_once()

    def test_repeat_failure_overwrites_one_bounded_file(self):
        client = MagicMock()
        client.chat.completions.create.return_value = reply(OUTSIDE)
        reviewer = self.reviewer(client)
        for _ in range(5):
            with self.assertRaises(BoxReviewOutputError):
                reviewer.review(**self.args)
        path, saved = self.diagnostic()
        self.assertLessEqual(len(saved["attempts"]), 8)
        self.assertLess(path.stat().st_size, 128 * 1024)

    def test_invalid_json_and_secrets_are_bounded_and_not_saved_as_cache(self):
        client = MagicMock()
        body = json.dumps({"decision": "replace", "bbox": [1100, 930, 1150, 990],
                           "api_key": "test-private-key", "reason": "test-private-key " + "中文" * 20000})
        client.chat.completions.create.return_value = reply(body)
        with self.assertRaises(BoxReviewOutputError):
            self.reviewer(client).review(**self.args)
        path, saved = self.diagnostic()
        text = path.read_text(encoding="utf-8")
        self.assertNotIn("test-private-key", text)
        self.assertTrue(saved["attempts"][0]["raw_response_truncated"])
        self.assertLess(path.stat().st_size, 128 * 1024)
        self.assertFalse(self.cache.exists())

    def test_transport_failure_is_not_corrected_or_classified_as_bad_output(self):
        client = MagicMock()
        client.chat.completions.create.side_effect = TimeoutError("transport")
        with self.assertRaises(TimeoutError) as caught:
            self.reviewer(client).review(**self.args)
        client.chat.completions.create.assert_called_once()
        self.assertNotEqual(failure_from_exception(caught.exception)["category"], "model_output")

    def test_diagnostic_disk_failure_does_not_inherit_output_error_context(self):
        for error in (PermissionError("denied"), OSError(28, "disk full")):
            with self.subTest(error=type(error).__name__):
                client = MagicMock()
                client.chat.completions.create.return_value = reply(OUTSIDE)
                reviewer = self.reviewer(client)
                with patch.object(reviewer, "_write_failures", side_effect=error):
                    with self.assertRaises(OSError) as caught:
                        reviewer.review(**self.args)
                client.chat.completions.create.assert_called_once()
                self.assertIsNone(caught.exception.__context__)
                self.assertNotEqual(failure_from_exception(caught.exception)["category"], "model_output")

    def test_valid_cache_disk_failure_is_not_retried(self):
        client = MagicMock()
        client.chat.completions.create.return_value = reply(GOOD)
        reviewer = self.reviewer(client)
        with patch.object(reviewer, "_save_cache", side_effect=PermissionError("denied")):
            with self.assertRaises(PermissionError):
                reviewer.review(**self.args)
        client.chat.completions.create.assert_called_once()

    def test_missing_image_does_not_call_model(self):
        client = MagicMock()
        self.image.unlink()
        with self.assertRaises(FileNotFoundError):
            self.reviewer(client).review(**self.args)
        client.chat.completions.create.assert_not_called()

    def test_parser_rejects_bad_geometry_without_guessing_pixel_units(self):
        values = [[True, 2, 500, 600], [float("nan"), 2, 500, 600],
                  [0, 2, float("inf"), 600], [0, 2, 10**400, 600],
                  [0, 2, 1e308, 1e308], [0, 2, "500", 600], [1, 2],
                  [1100, 930, 1150, 990], [-1, 2, 500, 600], [500, 2, 100, 600],
                  [100, 100, 101, 500], [100, 100, 500, 101]]
        for bbox in values:
            with self.subTest(bbox=bbox):
                with self.assertRaises(BoxReviewOutputError):
                    _parse_response(json.dumps({"decision": "replace", "bbox": bbox}),
                                    self.args["candidate_bbox"], self.args["image_size"])

    def test_accept_keeps_execution_pixel_candidate_unchanged(self):
        result = _parse_response(ACCEPT, (48, 1558, 240, 1742), (1200, 2664))
        self.assertEqual(result.bbox, (48, 1558, 240, 1742))

    def test_reported_newagent_point_cannot_be_replaced_with_bottom_right_box(self):
        Image.new("RGB", (1200, 2640), "white").save(self.image)
        correct = json.dumps({"decision": "replace", "bbox": [850, 50, 950, 150]})
        bottom = json.dumps({"decision": "replace", "bbox": [850, 900, 1000, 1000]})
        client = MagicMock()
        client.chat.completions.create.side_effect = [reply(bottom), reply(correct)]
        args = {**self.args, "action": {"action": "click", "coordinate": [1080, 238]},
                "image_size": (1200, 2640), "candidate_bbox": (1000, 180, 1150, 300)}
        result = self.reviewer(client).review(**args)
        self.assertEqual(result.bbox, (1020, 132, 1140, 396))
        saved = self.diagnostic()[1]
        self.assertEqual(saved["attempts"][0]["failure_code"], "bbox_excludes_action")
        self.assertEqual(saved["attempts"][0]["details"]["executed_points_pixels"], [[1080, 238]])
        prompt = client.chat.completions.create.call_args.kwargs["messages"][1]["content"][0]["text"]
        self.assertIn('[900, 90]', prompt)

    def test_long_press_and_swipe_require_actual_points_in_box(self):
        for action in ({"action": "long_press", "coordinate": [1100, 100]},
                       {"action": "swipe", "start_coordinate": [200, 700], "end_coordinate": [1100, 100]}):
            with self.subTest(action=action):
                with self.assertRaises(BoxReviewOutputError) as caught:
                    _parse_response(GOOD, self.args["candidate_bbox"], self.args["image_size"], action=action)
                self.assertEqual(caught.exception.failure_code, "bbox_excludes_action")

    def test_old_cache_box_excluding_click_does_not_bypass_validation(self):
        client = MagicMock()
        client.chat.completions.create.return_value = reply(GOOD)
        reviewer = self.reviewer(client)
        reviewer.review(**self.args)
        key = next(iter(reviewer.cache))
        reviewer.cache[key] = json.dumps({"decision": "replace", "bbox": [800, 800, 1000, 1000]})
        self.cache.write_text(json.dumps(reviewer.cache), encoding="utf-8")
        fresh = MagicMock()
        fresh.chat.completions.create.return_value = reply(GOOD)
        self.assertFalse(self.reviewer(fresh).review(**self.args).cached)
        fresh.chat.completions.create.assert_called_once()
        self.assertEqual(self.diagnostic()[1]["attempts"][0]["source"], "cache")

    def test_failure_file_remains_readable_after_json_escaping(self):
        client = MagicMock()
        client.chat.completions.create.return_value = reply(OUTSIDE)
        reviewer = self.reviewer(client)
        error = BoxReviewOutputError("bbox_out_of_range", {"decision": "replace"})
        item = reviewer._failure_record(error, "\x01" * 12000, source="model", attempt=1)
        key = hashlib.sha256(b"fixed").hexdigest()
        path = reviewer._write_failures(key, {}, self.image, [item] * 8)
        self.assertLessEqual(path.stat().st_size, 128 * 1024)
        feedback, previous = reviewer._previous_failure(key)
        self.assertIn("不能使用像素坐标", feedback)
        self.assertGreaterEqual(len(previous), 1)
        self.assertEqual(previous[-1]["failure_code"], "bbox_out_of_range")

    def test_long_summary_does_not_remove_geometry_from_diagnostic(self):
        client = MagicMock()
        client.chat.completions.create.return_value = reply(OUTSIDE)
        with self.assertRaises(BoxReviewOutputError):
            self.reviewer(client).review(**{**self.args, "action_summary": "很长的中文摘要" * 4000})
        request = self.diagnostic()[1]["request"]
        self.assertEqual(request["candidate_bbox"], list(self.args["candidate_bbox"]))
        self.assertEqual(request["image_size"], [1200, 2664])
        self.assertEqual(request["round"], 1)
        self.assertEqual(request["action"]["coordinate"], [200, 700])

    def test_real_v3_valid_cache_is_reused_and_bad_geometry_is_rejected(self):
        payload = {"version": "single-action-box-review-v3-action-marker", "model": "test-model",
            "image_digest": hashlib.sha256(self.image.read_bytes()).hexdigest(),
            "image_size": self.args["image_size"], "action": self.args["action"],
            "action_summary": self.args["action_summary"], "candidate_bbox": self.args["candidate_bbox"],
            "rule_context": self.args["rule_context"], "round": 1}
        key = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        self.cache.write_text(json.dumps({key: GOOD}), encoding="utf-8")
        client = MagicMock()
        self.assertTrue(self.reviewer(client).review(**self.args).cached)
        client.chat.completions.create.assert_not_called()
        self.cache.write_text(json.dumps({key: OUTSIDE}), encoding="utf-8")
        client.chat.completions.create.return_value = reply(GOOD)
        self.assertFalse(self.reviewer(client).review(**self.args).cached)
        client.chat.completions.create.assert_called_once()


if __name__ == "__main__":
    unittest.main()
