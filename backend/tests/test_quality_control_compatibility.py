"""Runtime controls preserve grading identity and old safe error interfaces."""
from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from backend.batch_results import quality_task_fingerprints
from backend.pipeline_retry_errors import failure_from_exception, failure_from_payload

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "DevelopRubrics"))
from trajectory_tools.settings import configure_model_environment


class QualityControlCompatibilityTests(unittest.TestCase):
    def test_execution_failures_keep_their_specific_safe_diagnosis(self):
        from backend.quality_execution import QualityExecutionBlocked, QualityExecutionRevoked
        for error, category, code in (
            (QualityExecutionBlocked("secret"), "unknown", "quality_worker_cleanup_failed"),
            (QualityExecutionRevoked("secret"), "stale", "quality_execution_revoked"),
        ):
            result = failure_from_exception(error)
            self.assertEqual(result["category"], category)
            self.assertEqual(result["code"], code)
            self.assertFalse(result["retryable"])
            self.assertNotIn("secret", str(result))

    def test_env_file_deadline_is_used_but_process_override_wins(self):
        settings = {"MODEL_NAME": "mock", "MODEL_URL": "https://model.invalid/v1",
                    "YUNAI_API_KEY": "test-only", "ADARUBRIC_REQUEST_TIMEOUT_SECONDS": "300"}
        for process, expected in (({}, "300"), ({"ADARUBRIC_REQUEST_TIMEOUT_SECONDS": "45"}, "45")):
            with self.subTest(process=process), patch.dict(os.environ, process, clear=True), \
                 patch("trajectory_tools.settings.load_repository_env", return_value=settings):
                configure_model_environment(Path("unused.env"))
                self.assertEqual(os.environ["ADARUBRIC_REQUEST_TIMEOUT_SECONDS"], expected)

    def test_deadline_change_does_not_invalidate_quality_content(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config.json"
            config.write_text('{"num_dimensions": 5}', encoding="utf-8")
            env = root / "model.env"
            trees = {"tree_hashes": {"task": "unchanged"}, "quality_input": {}}
            signatures = []
            for timeout in ("300", "600"):
                env.write_text("MODEL_NAME=mock\nMODEL_URL=https://model.invalid/v1\n"
                               f"ADARUBRIC_REQUEST_TIMEOUT_SECONDS={timeout}\n", encoding="utf-8")
                signatures.append(quality_task_fingerprints(trees, config_path=config, env_path=env))
            self.assertEqual(signatures[0], signatures[1])

    def test_worker_diagnostics_are_safe_and_do_not_retry_unknown_hangs(self):
        for code, expected in (("quality_worker_unresponsive", "质检子进程无响应"),
                               ("quality_worker_cleanup_failed", "质检子进程退出未确认")):
            with self.subTest(code=code):
                result = failure_from_payload({"category": "unknown", "code": code,
                                               "message": "secret-request-body"})
                self.assertFalse(result["retryable"])
                self.assertIn(expected, result["message"])
                self.assertNotIn("secret", str(result))
        timeout = failure_from_payload({"category": "timeout", "code": "quality_request_deadline"})
        self.assertTrue(timeout["retryable"])
        self.assertIn("超过等待上限", timeout["message"])
        # A provider error using the same code keeps its HTTP-based diagnosis.
        rejected = failure_from_payload({"category": "bad_request", "http_status": 407,
                                         "code": "quality_request_deadline"})
        self.assertFalse(rejected["retryable"])
        self.assertIn("模型请求被拒绝", rejected["message"])


if __name__ == "__main__":
    unittest.main()
