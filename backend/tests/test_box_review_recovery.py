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
MALFORMED = json.dumps({"decision": "replace", "bbox": [200, 300]})


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

    def reviewer(self, client, **options):
        with patch("backend.bounding_box.qwen_reviewer.httpx.Client"), \
             patch("backend.bounding_box.qwen_reviewer.OpenAI", return_value=client):
            return QwenBoxReviewer("test-model", self.cache, client_settings=self.settings, **options)

    def test_checkpointed_worker_ignores_legacy_reply_file(self):
        client = MagicMock()
        client.chat.completions.create.return_value = reply(GOOD)
        self.reviewer(client).review(**self.args)
        old = self.cache.read_bytes()
        client.chat.completions.create.return_value = reply(ACCEPT)
        result = self.reviewer(client, use_response_cache=False).review(**self.args)
        self.assertFalse(result.cached)
        self.assertEqual(result.decision, "accept")
        self.assertEqual(client.chat.completions.create.call_count, 2)
        self.assertEqual(self.cache.read_bytes(), old)

    def diagnostic(self):
        paths = list((self.root / "box_failures").glob("*.json"))
        self.assertEqual(len(paths), 1)
        return paths[0], json.loads(paths[0].read_text(encoding="utf-8"))

    def test_incomplete_box_is_recorded_and_corrected_with_feedback(self):
        client = MagicMock()
        client.chat.completions.create.side_effect = [reply(MALFORMED), reply(GOOD)]
        result = self.reviewer(client).review(**self.args)
        self.assertEqual(result.bbox, (120, 533, 360, 1066))
        self.assertEqual(client.chat.completions.create.call_count, 2)
        second = client.chat.completions.create.call_args.kwargs["messages"]
        self.assertIn("四个", second[-1]["content"])
        self.assertIn("截图像素", second[1]["content"][0]["text"])
        _, saved = self.diagnostic()
        self.assertTrue(saved["resolved"])
        detail = saved["attempts"][0]["details"]
        self.assertEqual(saved["attempts"][0]["failure_code"], "invalid_bbox")
        self.assertEqual(detail["model_bbox"], [200, 300])
        self.assertEqual(list(json.loads(self.cache.read_text()).values()), [GOOD])

    def test_accepting_one_pixel_candidate_uses_it_without_a_second_call(self):
        client = MagicMock()
        client.chat.completions.create.return_value = reply(ACCEPT)
        args = {**self.args, "candidate_bbox": (1198, 2482, 1199, 2639)}
        self.assertEqual(self.reviewer(client).review(**args).bbox, args["candidate_bbox"])
        client.chat.completions.create.assert_called_once()

    def test_three_invalid_responses_stop_with_safe_classification_and_evidence(self):
        client = MagicMock()
        client.chat.completions.create.return_value = reply(MALFORMED)
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
        first.chat.completions.create.return_value = reply(MALFORMED)
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
        self.assertIn("四个", second.chat.completions.create.call_args.kwargs["messages"][-1]["content"])
        self.assertTrue(self.diagnostic()[1]["resolved"])
        self.assertTrue(resumed.review(**self.args).cached)
        second.chat.completions.create.assert_called_once()

    def test_repeat_failure_overwrites_one_bounded_file(self):
        client = MagicMock()
        client.chat.completions.create.return_value = reply(MALFORMED)
        reviewer = self.reviewer(client)
        for _ in range(5):
            with self.assertRaises(BoxReviewOutputError):
                reviewer.review(**self.args)
        path, saved = self.diagnostic()
        self.assertLessEqual(len(saved["attempts"]), 8)
        self.assertLess(path.stat().st_size, 128 * 1024)

    def test_invalid_json_and_secrets_are_bounded_and_not_saved_as_cache(self):
        client = MagicMock()
        body = json.dumps({"decision": "replace", "bbox": [],
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
                client.chat.completions.create.return_value = reply(MALFORMED)
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

    def test_parser_rejects_incomplete_or_nonnumeric_bbox(self):
        values = [[True, 2, 500, 600], [float("nan"), 2, 500, 600],
                  [0, 2, float("inf"), 600], [0, 2, 10**400, 600],
                  [0, 2, "500", 600], [1, 2]]
        for bbox in values:
            with self.subTest(bbox=bbox):
                with self.assertRaises(BoxReviewOutputError):
                    _parse_response(json.dumps({"decision": "replace", "bbox": bbox}),
                                    self.args["candidate_bbox"], self.args["image_size"])

    def test_accept_keeps_execution_pixel_candidate_unchanged(self):
        result = _parse_response(ACCEPT, (48, 1558, 240, 1742), (1200, 2664))
        self.assertEqual(result.bbox, (48, 1558, 240, 1742))

    def test_normalized_edges_reversed_and_zero_area_boxes(self):
        for box, expected in [
                ([-10, 10, 1100, 1e308], (0, 27, 1199, 2663)),
                ([800, 900, 200, 100], (240, 266, 960, 2398)),
                ([0, 0, 0, 0], (0, 0, 0, 0))]:
            with self.subTest(box=box):
                raw = json.dumps({"decision": "replace", "bbox": box})
                result = _parse_response(raw, self.args["candidate_bbox"], self.args["image_size"])
                self.assertEqual(result.bbox, expected)

    def test_explicit_box_survives_missing_decision_and_unusable_confidence(self):
        raw = json.dumps({"bbox": [100, 200, 300, 400], "confidence": "unknown"})
        result = _parse_response(raw, self.args["candidate_bbox"], self.args["image_size"])
        self.assertEqual(result.decision, "replace")
        self.assertEqual(result.bbox, (120, 533, 360, 1066))
        self.assertEqual(result.confidence, .5)

    def test_field_step_ten_keeps_model_replacement_below_click(self):
        Image.new("RGB", (1148, 2492), "white").save(self.image)
        raw = json.dumps({"decision": "replace", "bbox": [780, 280, 970, 340]})
        client = MagicMock()
        client.chat.completions.create.return_value = reply(raw)
        args = {**self.args, "action": {"action": "click", "coordinate": [1003, 309]},
                "image_size": (1148, 2492), "candidate_bbox": (24, 24, 1124, 2303)}
        result = self.reviewer(client).review(**args)
        self.assertEqual(result.bbox, (895, 698, 1114, 847))
        client.chat.completions.create.assert_called_once()
        prompt = client.chat.completions.create.call_args.kwargs["messages"][1]["content"][0]["text"]
        self.assertIn('[874, 124]', prompt)

    def test_long_press_and_swipe_keep_box_that_excludes_points(self):
        for action in ({"action": "long_press", "coordinate": [1100, 100]},
                       {"action": "swipe", "start_coordinate": [200, 700], "end_coordinate": [1100, 100]}):
            with self.subTest(action=action):
                result = _parse_response(GOOD, self.args["candidate_bbox"], self.args["image_size"], action=action)
                self.assertEqual(result.bbox, (120, 533, 360, 1066))

    def test_old_cache_box_excluding_click_is_reused(self):
        client = MagicMock()
        client.chat.completions.create.return_value = reply(GOOD)
        reviewer = self.reviewer(client)
        reviewer.review(**self.args)
        key = next(iter(reviewer.cache))
        reviewer.cache[key] = json.dumps({"decision": "replace", "bbox": [800, 800, 1000, 1000]})
        self.cache.write_text(json.dumps(reviewer.cache), encoding="utf-8")
        fresh = MagicMock()
        fresh.chat.completions.create.return_value = reply(GOOD)
        result = self.reviewer(fresh).review(**self.args)
        self.assertTrue(result.cached)
        self.assertEqual(result.bbox, (960, 2131, 1199, 2663))
        fresh.chat.completions.create.assert_not_called()

    def test_failure_file_remains_readable_after_json_escaping(self):
        client = MagicMock()
        client.chat.completions.create.return_value = reply(MALFORMED)
        reviewer = self.reviewer(client)
        error = BoxReviewOutputError("invalid_bbox", {"decision": "replace"})
        item = reviewer._failure_record(error, "\x01" * 12000, source="model", attempt=1)
        key = hashlib.sha256(b"fixed").hexdigest()
        path = reviewer._write_failures(key, {}, self.image, [item] * 8)
        self.assertLessEqual(path.stat().st_size, 128 * 1024)
        feedback, previous = reviewer._previous_failure(key)
        self.assertIn("四个", feedback)
        self.assertGreaterEqual(len(previous), 1)
        self.assertEqual(previous[-1]["failure_code"], "invalid_bbox")

    def test_long_summary_does_not_remove_geometry_from_diagnostic(self):
        client = MagicMock()
        client.chat.completions.create.return_value = reply(MALFORMED)
        with self.assertRaises(BoxReviewOutputError):
            self.reviewer(client).review(**{**self.args, "action_summary": "很长的中文摘要" * 4000})
        request = self.diagnostic()[1]["request"]
        self.assertEqual(request["candidate_bbox"], list(self.args["candidate_bbox"]))
        self.assertEqual(request["image_size"], [1200, 2664])
        self.assertEqual(request["round"], 1)
        self.assertEqual(request["action"]["coordinate"], [200, 700])

    def test_historical_containment_failure_does_not_request_a_correction(self):
        client = MagicMock()
        client.chat.completions.create.return_value = reply(GOOD)
        reviewer = self.reviewer(client)
        reviewer.review(**self.args)
        key = next(iter(reviewer.cache))
        reviewer.cache.clear()
        old_entry = {"failure_code": "bbox_excludes_action", "raw_response": GOOD}
        reviewer._write_failures(key, {}, self.image, [old_entry])
        client.reset_mock()
        self.assertEqual(reviewer.review(**self.args).bbox, (120, 533, 360, 1066))
        for call in client.chat.completions.create.call_args_list:
            self.assertEqual([m["role"] for m in call.kwargs["messages"]], ["system", "user"])

    def test_real_v3_valid_cache_is_reused_and_malformed_cache_is_rejected(self):
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
        self.cache.write_text(json.dumps({key: MALFORMED}), encoding="utf-8")
        client.chat.completions.create.return_value = reply(GOOD)
        self.assertFalse(self.reviewer(client).review(**self.args).cached)
        client.chat.completions.create.assert_called_once()


if __name__ == "__main__":
    unittest.main()
