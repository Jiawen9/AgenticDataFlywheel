"""Only explicit, locally validated model-output failures gain bounded retries."""
from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import httpx
from openai import APIStatusError

from backend.pipeline_retry_errors import (
    ModelConfigurationError, ModelOutputError, failure_from_exception, failure_from_payload,
)
from backend.pipelines import PipelineManager
from backend.tests.test_pipelines import OfflineRuntime


class ModelOutputFailureTests(unittest.TestCase):
    CODES = ("empty_choices", "empty_content", "invalid_content", "truncated", "invalid_json",
             "invalid_schema", "inconsistent_fields", "missing_observation")
    SECRET = "sk-test-private-provider-data"

    def api_error(self, status, *, code="provider_error", message=None):
        response = httpx.Response(status, request=httpx.Request("POST", "https://model.invalid/v1/chat/completions"),
                                  headers={"x-request-id": "req-safe"})
        return APIStatusError(self.SECRET, response=response, body={"error": {
            "message": message or self.SECRET, "code": code,
        }})

    def test_all_known_codes_are_retryable_and_survive_persistence(self):
        for code in self.CODES:
            with self.subTest(code=code):
                error = ModelOutputError(code)
                self.assertIsInstance(error, ValueError)
                failure = failure_from_exception(error)
                self.assertEqual(failure["category"], "model_output")
                self.assertEqual(failure["code"], code)
                self.assertTrue(failure["retryable"])
                self.assertIsNone(failure["http_status"])
                self.assertEqual(failure_from_payload(json.loads(json.dumps(failure))), failure)

    def test_unknown_constructor_codes_fail_without_echoing_input(self):
        for code in (self.SECRET, "arbitrary_code", None, [], {"message": self.SECRET}):
            with self.subTest(code_type=type(code).__name__):
                with self.assertRaises(ValueError) as caught:
                    ModelOutputError(code)
                self.assertNotIsInstance(caught.exception, ModelOutputError)
                self.assertNotIn(self.SECRET, str(caught.exception))

    def test_wrapped_output_error_retains_location_without_response_text(self):
        error = RuntimeError("中间状态分类失败: trajectory=tr-safe, step=15, error=" + self.SECRET)
        error.__cause__ = ModelOutputError("inconsistent_fields")
        error.__cause__.__cause__ = ValueError("untrusted-json=" + self.SECRET)
        failure = failure_from_exception(error)
        self.assertEqual(failure["category"], "model_output")
        self.assertTrue(failure["retryable"])
        self.assertEqual(failure["trajectory_id"], "tr-safe")
        self.assertEqual(failure["step"], 15)
        self.assertIn("字段相互矛盾", failure["message"])
        self.assertNotIn(self.SECRET, json.dumps(failure))

    def test_context_chain_is_supported_without_explicit_cause(self):
        error = RuntimeError(self.SECRET)
        error.__context__ = ModelOutputError("empty_content")
        failure = failure_from_exception(error)
        self.assertEqual(failure["category"], "model_output")
        self.assertEqual(failure["code"], "empty_content")

    def test_payload_ignores_raw_messages_and_caller_retryable(self):
        failure = failure_from_payload({"category": "model_output", "code": "invalid_json",
                                        "message": self.SECRET, "retryable": False,
                                        "raw_response": self.SECRET, "body": self.SECRET})
        self.assertTrue(failure["retryable"])
        self.assertIn("JSON", failure["message"])
        self.assertNotIn(self.SECRET, json.dumps(failure))
        self.assertNotIn("raw_response", failure)

    def test_payload_requires_known_code_and_no_http_status(self):
        for code, status in ((None, None), ("arbitrary_code", None), (self.SECRET, None),
                             ("invalid_json", 400), ("invalid_json", 401), ("invalid_json", 403),
                             ("invalid_json", 429), ("invalid_json", 200)):
            with self.subTest(code=code, status=status):
                failure = failure_from_payload({"category": "model_output", "code": code,
                                                "http_status": status, "retryable": True})
                self.assertEqual(failure["category"], "unknown")
                self.assertFalse(failure["retryable"])
                self.assertIsNone(failure["code"])

    def test_generic_value_errors_remain_input_without_model_retry(self):
        for error in (ValueError("invalid JSON " + self.SECRET), TypeError(self.SECRET), FileNotFoundError(self.SECRET)):
            with self.subTest(type=type(error).__name__):
                error.code = "invalid_json"
                error.category = "model_output"
                error.retryable = True
                failure = failure_from_exception(error)
                self.assertEqual(failure["category"], "input")
                self.assertFalse(failure["retryable"])
                self.assertNotIn(self.SECRET, json.dumps(failure))

    def test_wrapped_generic_value_error_keeps_previous_unknown_behavior(self):
        error = RuntimeError(self.SECRET)
        error.__cause__ = ValueError("invalid JSON " + self.SECRET)
        failure = failure_from_exception(error)
        self.assertEqual(failure["category"], "unknown")
        self.assertFalse(failure["retryable"])

    def test_lookalike_exception_name_is_not_trusted(self):
        fake_type = type("ModelOutputError", (ValueError,), {})
        error = fake_type(self.SECRET)
        error.code = "invalid_json"
        failure = failure_from_exception(error)
        self.assertEqual(failure["category"], "input")
        self.assertFalse(failure["retryable"])

    def test_api_rejections_remain_non_retryable_even_if_wrapped_as_output(self):
        for status, expected in ((400, "bad_request"), (401, "auth"), (403, "auth")):
            with self.subTest(status=status):
                error = ModelOutputError("invalid_json")
                error.__cause__ = self.api_error(status)
                failure = failure_from_exception(error)
                self.assertEqual(failure["category"], expected)
                self.assertEqual(failure["http_status"], status)
                self.assertFalse(failure["retryable"])
                self.assertNotIn(self.SECRET, json.dumps(failure))

    def test_quota_is_not_relabelled_as_retryable_model_output(self):
        for status in (403, 429):
            with self.subTest(status=status):
                error = ModelOutputError("invalid_schema")
                error.__cause__ = self.api_error(status, code="insufficient_quota", message="quota is not enough")
                failure = failure_from_exception(error)
                self.assertEqual(failure["category"], "quota")
                self.assertFalse(failure["retryable"])

    def test_actual_config_and_file_errors_retain_non_retryable_priority(self):
        from backend.collection_runs import CollectionRunError

        for cause, category in ((ModelConfigurationError(self.SECRET), "config"),
                                (CollectionRunError("missing", file_failure={"code": "source_missing"}), "input")):
            with self.subTest(category=category):
                error = ModelOutputError("invalid_schema")
                error.__cause__ = cause
                failure = failure_from_exception(error)
                self.assertEqual(failure["category"], category)
                self.assertFalse(failure["retryable"])

    def test_unknown_mutated_code_cannot_become_retryable(self):
        error = ModelOutputError("invalid_json")
        error.code = self.SECRET
        failure = failure_from_exception(error)
        self.assertEqual(failure["category"], "unknown")
        self.assertFalse(failure["retryable"])
        self.assertNotIn(self.SECRET, json.dumps(failure))


class ModelOutputPipelineRetryTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.runtime = OfflineRuntime(self.root)
        self.manager = PipelineManager(self.runtime, self.root)
        self.pipeline = self.manager.create({"request_id": "case-output", "name": "case-output",
            "batch_id": "batch-output", "mode": "automatic", "start_mode": "existing", "threshold": 4})
        self.manager.tick(self.pipeline["pipeline_id"])
        self.manager.tick(self.pipeline["pipeline_id"])

    def tick(self):
        self.manager.tick(self.pipeline["pipeline_id"])
        return self.manager.get(self.pipeline["pipeline_id"])

    def test_model_output_uses_existing_three_attempts_and_30_120_delays(self):
        failure = failure_from_exception(ModelOutputError("inconsistent_fields"))
        for attempt, delay in ((1, 30), (2, 120), (3, None)):
            self.runtime.complete("preprocessing", fail=True, failure=failure)
            now = datetime.now(timezone.utc)
            current = self.tick()
            info = current["steps"][1]["retry_info"]
            self.assertEqual(info["attempts"], attempt)
            self.assertEqual(info["max_attempts"], 3)
            self.assertEqual(len(self.runtime.calls), attempt)
            if delay is None:
                self.assertEqual(current["status"], "failed")
                self.assertIsNone(info["next_retry_at"])
            else:
                self.assertEqual(current["status"], "retry_waiting")
                due = datetime.fromisoformat(info["next_retry_at"].replace("Z", "+00:00"))
                self.assertLess(abs((due - now).total_seconds() - delay), 3)
                with patch("backend.pipelines._retry_due", return_value=True):
                    self.tick()
        self.assertEqual(self.tick()["status"], "failed")
        self.assertEqual(len(self.runtime.calls), 3)

    def test_old_pipeline_without_policy_does_not_gain_automatic_retry(self):
        stored = self.manager.get(self.pipeline["pipeline_id"], public=False)
        stored.pop("retry_policy")
        for step in stored["steps"]:
            step.pop("retry_info", None)
        self.manager._save(stored)
        self.runtime.complete("preprocessing", fail=True,
                              failure=failure_from_exception(ModelOutputError("invalid_json")))
        self.assertEqual(self.tick()["status"], "failed")
        self.assertEqual(len(self.runtime.calls), 1)

    def test_already_failed_pipeline_stays_failed_after_classifier_update(self):
        self.runtime.complete("preprocessing", fail=True, failure={"category": "unknown"})
        self.assertEqual(self.tick()["status"], "failed")
        self.runtime.complete("preprocessing", fail=True,
                              failure=failure_from_exception(ModelOutputError("invalid_json")))
        self.manager = PipelineManager(self.runtime, self.root)
        self.assertEqual(self.tick()["status"], "failed")
        self.assertEqual(len(self.runtime.calls), 1)


if __name__ == "__main__":
    unittest.main()
