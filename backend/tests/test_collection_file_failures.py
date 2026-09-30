"""Local file diagnoses remain useful across jobs without leaking provider data."""
from __future__ import annotations

import json
import unittest
from unittest.mock import Mock

from backend.collection_runs import CollectionRunError
from backend.pipeline_retry_errors import (
    failure_from_exception, failure_from_payload, file_failure_message,
    normalize_file_failure,
)
from backend.pipelines import PipelineManager, PipelineStepFailure


class CollectionFileFailureTests(unittest.TestCase):
    def detail(self):
        return {"code": "manifest_mismatch", "trajectory": "任务001/轨迹001-1",
                "missing_count": 2, "unexpected_count": 1,
                "paths": ["任务001/轨迹001-1/step001_vla_input.jpg",
                          "任务001/轨迹001-1/step002_vla_input.jpg"]}

    def error(self, detail=None):
        # This is the locally trusted type; setting the field also verifies that
        # the classifier reconstructs a safe message rather than copying str().
        error = CollectionRunError("local wrapper contains sk-test-secret")
        error.file_failure = detail or self.detail()
        return error

    def test_constructor_formats_existing_detail_error_strings(self):
        error = CollectionRunError("do not expose sk-test-secret", file_failure=self.detail())
        self.assertEqual(str(error), file_failure_message(self.detail()))
        self.assertEqual(error.status, 409)
        self.assertNotIn("sk-test-secret", str(error))
        invalid = CollectionRunError("do not expose sk-test-secret", file_failure={"code": "invalid"})
        self.assertEqual(str(invalid), "处理输入无效")
        self.assertIsNone(invalid.file_failure)

    def test_typed_failure_and_wrapped_cause_preserve_specific_safe_message(self):
        try:
            raise RuntimeError("provider body sk-test-secret") from self.error()
        except RuntimeError as wrapper:
            failure = failure_from_exception(wrapper)
        self.assertEqual(failure["category"], "input")
        self.assertFalse(failure["retryable"])
        self.assertIn("缺失 2 个文件", failure["message"])
        self.assertIn("新增未登记 1 个文件", failure["message"])
        self.assertIn("step001_vla_input.jpg", failure["message"])
        self.assertEqual(failure["file_failure"], self.detail())
        self.assertNotIn("sk-test-secret", json.dumps(failure))
        self.assertEqual(failure_from_payload(json.loads(json.dumps(failure))), failure)

    def test_arbitrary_exception_attributes_and_name_are_not_trusted(self):
        spoof_type = type("CollectionRunError", (ValueError,), {})
        for exception in (ValueError("sk-test-secret"), RuntimeError("sk-test-secret"),
                          spoof_type("sk-test-secret")):
            exception.file_failure = self.detail()
            failure = failure_from_exception(exception)
            self.assertNotIn("file_failure", failure)
            self.assertNotIn("step001", failure["message"])
            self.assertNotIn("sk-test-secret", json.dumps(failure))

    def test_model_error_with_fake_file_detail_keeps_model_classification(self):
        error = RuntimeError("secret prompt and response sk-test-secret")
        error.status_code = 500
        error.body = {"error": {"code": "bad_response_body", "message": "sk-test-secret"}}
        error.file_failure = self.detail()
        failure = failure_from_exception(error)
        self.assertEqual(failure["category"], "server_error")
        self.assertTrue(failure["retryable"])
        self.assertNotIn("file_failure", failure)
        self.assertNotIn("sk-test-secret", json.dumps(failure))
        forged_payload = {**failure, "file_failure": self.detail(), "message": "sk-test-secret"}
        self.assertEqual(failure_from_payload(forged_payload), failure)

    def test_relative_samples_and_counts_are_bounded_and_credentials_are_removed(self):
        detail = self.detail()
        detail.update(trajectory=r"C:\private\sk-test-secret", missing_count=-1,
                      unexpected_count=True, message="sk-test-secret", raw_response="sk-test-secret",
                      paths=[r"C:\private\data.json", "/root/private.json", "../outside.json",
                             "task_001/sk-test-secret.json", "task_001/Bearer secret.txt",
                             "task_001/API_KEY=secret.txt", "task_001/line\nsecret.txt",
                             "task_001/safe-file.json"])
        normalized = normalize_file_failure(detail)
        self.assertEqual(normalized, {"code": "manifest_mismatch", "paths": ["task_001/safe-file.json"]})
        self.assertNotIn("sk-test-secret", file_failure_message(detail))
        huge = self.detail()
        huge["trajectory"] = "任务/" + "轨" * 1000
        huge["paths"] = ["任务/" + "轨" * 800 + f"/step{index}.json" for index in range(1000)]
        huge["missing_count"] = 1_000_000_001
        normalized = normalize_file_failure(huge)
        self.assertEqual(len(normalized["paths"]), 20)
        self.assertTrue(all(len(path) <= 320 for path in normalized["paths"]))
        self.assertLessEqual(len(normalized["trajectory"]), 320)
        self.assertNotIn("missing_count", normalized)
        self.assertLessEqual(len(file_failure_message(huge)), 600)

    def test_unknown_codes_and_legacy_payloads_remain_generic(self):
        for payload in (None, [], {"code": []}, {"code": "provider_reply"}, {"message": "secret"}):
            self.assertIsNone(normalize_file_failure(payload))
            self.assertEqual(file_failure_message(payload), "处理输入无效")
        expected = {"category": "input", "retryable": False, "http_status": None,
                    "code": None, "request_id": None, "message": "处理输入无效"}
        self.assertEqual(failure_from_payload({"category": "input", "message": "sk-test-secret"}), expected)
        self.assertEqual(failure_from_exception(ValueError("sk-test-secret")), expected)
        self.assertEqual(failure_from_exception(CollectionRunError("sk-test-secret")), expected)

    def test_pipeline_old_frontend_fields_show_detail_and_do_not_retry(self):
        manager = object.__new__(PipelineManager)
        manager._log = Mock()
        step = {"id": "collection", "label": "采集与回传", "status": "running",
                "retry_info": {"attempts": 1, "max_attempts": 3, "next_retry_at": None}}
        pipeline = {"status": "running", "retry_policy": {"max_attempts": 3, "delays_seconds": [30, 120]}}
        manager._schedule_or_fail(pipeline, step, self.error())
        self.assertEqual(pipeline["status"], "failed")
        self.assertEqual(step["status"], "failed")
        self.assertIn("step001_vla_input.jpg", pipeline["error"])
        self.assertEqual(step["error"], step["retry_info"]["last_error"])
        self.assertIsNone(step["retry_info"]["next_retry_at"])
        self.assertNotIn("sk-test-secret", json.dumps(pipeline))
        child = PipelineStepFailure("unsanitized sk-test-secret", failure_from_exception(self.error()))
        manager._schedule_or_fail(pipeline, step, child)
        self.assertIn("缺失 2 个文件", pipeline["error"])
        self.assertNotIn("sk-test-secret", pipeline["error"])


if __name__ == "__main__":
    unittest.main()
