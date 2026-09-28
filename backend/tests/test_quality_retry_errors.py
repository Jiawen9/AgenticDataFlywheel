"""Safe quality subprocess failures and Pipeline retry evidence."""

from __future__ import annotations

import asyncio
from contextlib import redirect_stdout
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from backend.data_store import RecordStore
from backend.pipeline_retry_errors import failure_from_exception, failure_from_payload
from backend import quality_jobs


class ProviderStatusError(Exception):
    def __init__(self, status: int, body: dict, request_id: str = "req-123") -> None:
        self.status_code = status
        self.body = body
        self.request_id = request_id
        super().__init__("do not persist this secret sk-test-123")


class QualityFailureTests(unittest.TestCase):
    def test_server_error_keeps_safe_status_and_request_id_through_wrapper(self):
        provider = ProviderStatusError(
            500,
            {"error": {"code": "bad_response_body", "message": "unexpected EOF; secret sk-test-123"}},
        )
        try:
            raise RuntimeError("trajectory=tr_abc, step=42, response includes sk-test-123") from provider
        except RuntimeError as exc:
            failure = failure_from_exception(exc)
        self.assertEqual(failure["category"], "server_error")
        self.assertTrue(failure["retryable"])
        self.assertEqual(failure["http_status"], 500)
        self.assertEqual(failure["request_id"], "req-123")
        self.assertEqual(failure["trajectory_id"], "tr_abc")
        self.assertEqual(failure["step"], 42)
        self.assertNotIn("sk-test-123", json.dumps(failure))

    def test_quota_auth_input_and_unknown_are_not_retryable(self):
        quota = failure_from_exception(ProviderStatusError(
            403, {"error": {"code": "local:pre_consume_token_quota_failed",
                            "message": "token quota is not enough"}},
        ))
        self.assertEqual(quota["category"], "quota")
        self.assertFalse(quota["retryable"])
        self.assertEqual(failure_from_exception(ValueError("secret sk-test-123"))["category"], "input")
        self.assertFalse(failure_from_exception(RuntimeError("exit 1"))["retryable"])
        self.assertNotIn("secret", json.dumps(failure_from_payload({"category": "server_error",
            "http_status": 500, "message": "secret sk-test-123"})))

    def test_subprocess_error_protocol_and_unstructured_exit(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            process = Mock(stdout=io.StringIO(
                'noise which might be sensitive\n'
                'PROGRESS {"stage":"evaluating","percent":30,"raw_response":"secret sk-test-123"}\n'
                'ERROR {"category":"server_error","http_status":500,"request_id":"req-500","code":"bad_response_body","message":"secret sk-test-123"}\n'
            ))
            process.wait.return_value = 1
            progress_frames = []
            with patch.object(quality_jobs, "DATA_ROOT", Path(temp_dir)), \
                 patch.object(quality_jobs, "_env_values", return_value={}), \
                 patch.object(quality_jobs.subprocess, "Popen", return_value=process):
                with self.assertRaises(quality_jobs.QualitySubprocessError) as captured:
                    quality_jobs.run_quality_subprocess("run", ["task"], job_id="j",
                                                        progress=progress_frames.append)
            self.assertEqual(progress_frames, [{"stage": "evaluating", "percent": 30}])
            self.assertTrue(captured.exception.failure["retryable"])
            self.assertEqual(captured.exception.failure["request_id"], "req-500")
            self.assertNotIn("secret", str(captured.exception))
            self.assertEqual(process.wait.call_count, 1)

            process = Mock(stdout=io.StringIO("Traceback contains secret sk-test-123\n"))
            process.wait.return_value = 1
            with patch.object(quality_jobs, "DATA_ROOT", Path(temp_dir)), \
                 patch.object(quality_jobs, "_env_values", return_value={}), \
                 patch.object(quality_jobs.subprocess, "Popen", return_value=process):
                with self.assertRaises(quality_jobs.QualitySubprocessError) as captured:
                    quality_jobs.run_quality_subprocess("run", ["task"], job_id="j", progress=lambda _: None)
            self.assertEqual(captured.exception.failure["category"], "unknown")
            self.assertFalse(captured.exception.failure["retryable"])
            self.assertNotIn("secret", str(captured.exception))

    def test_failed_and_restart_jobs_persist_only_sanitized_diagnostics(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            records = RecordStore(root)
            records.put("quality_jobs", "old", {
                "job_id": "old", "run_id": "run", "status": "running", "created_at": "2026-09-23"
            })
            def runner(*args, **kwargs):
                raise ProviderStatusError(502, {"error": {"code": "bad_response_body",
                                                          "message": "secret sk-test-123"}})
            manager = quality_jobs.QualityJobManager(root / "quality-jobs", runner=runner, data_root=root)
            interrupted = manager.get("old")
            self.assertEqual(interrupted["failure"]["category"], "service_restart")
            self.assertTrue(interrupted["failure"]["retryable"])
            records.put("quality_jobs", "new", {
                "job_id": "new", "run_id": "run", "status": "queued", "created_at": "2026-09-23"
            })
            manager._run("new", "run", ["task"])
            failed = manager.get("new")
            manager.shutdown()
            self.assertEqual(failed["failure"]["category"], "server_error")
            self.assertEqual(failed["failure"]["http_status"], 502)
            self.assertNotIn("sk-test-123", json.dumps(failed))
            self.assertTrue(failed["diagnostic_log"])

    def test_child_entry_sets_single_attempt_only_for_new_pipeline(self):
        rubric_module = Path(__file__).resolve().parents[1] / "DevelopRubrics"
        with patch.object(sys, "path", [str(rubric_module), *sys.path]):
            from backend.DevelopRubrics import quality_job_runner

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            records = RecordStore(root)
            records.put("pipelines", "new", {"pipeline_id": "new", "retry_policy": {"version": 1}})
            records.put("pipelines", "old", {"pipeline_id": "old"})
            observed = []
            async def fail(*args):
                observed.append(os.environ.get("PIPELINE_MODEL_SINGLE_ATTEMPT"))
                raise ProviderStatusError(503, {"error": {"code": "bad_response_body",
                                                          "message": "secret sk-test-123"}})
            for owner, expected in (("new", "1"), ("old", "0")):
                records.put("quality_jobs", "job", {"job_id": "job", "pipeline_id": owner})
                output = io.StringIO()
                with patch.object(quality_job_runner, "DATA_ROOT", root), \
                     patch.object(quality_job_runner, "run", side_effect=fail), \
                     patch.object(sys, "argv", ["quality_job_runner.py", "--run-id", "run",
                                                "--task-id", "task", "--job-id", "job"]), \
                     redirect_stdout(output):
                    self.assertEqual(quality_job_runner.main(), 1)
                protocol = [line for line in output.getvalue().splitlines() if line.startswith("ERROR ")]
                self.assertEqual(len(protocol), 1)
                failure = json.loads(protocol[0][6:])
                self.assertEqual(failure["category"], "server_error")
                self.assertEqual(failure["http_status"], 503)
                self.assertNotIn("secret", output.getvalue())
                self.assertEqual(observed[-1], expected)

    def test_new_pipeline_rubric_generation_makes_one_request(self):
        rubric_module = Path(__file__).resolve().parents[1] / "DevelopRubrics"
        with patch.object(sys, "path", [str(rubric_module), *sys.path]):
            from backend.DevelopRubrics import quality_job_runner

        gen = quality_job_runner.GEN
        client = Mock()
        async def close():
            return None
        client.close = close
        error = ProviderStatusError(500, {"error": {"code": "bad_response_body",
                                                    "message": "unexpected EOF"}})
        async def fail(*args, **kwargs):
            raise error
        task = SimpleNamespace(task_id="task")
        with patch.dict(os.environ, {"PIPELINE_MODEL_SINGLE_ATTEMPT": "1"}), \
             patch.object(gen, "OpenAIClient", return_value=client), \
             patch.object(gen, "build_validator", return_value=None), \
             patch.object(gen, "_generate_rubric_json", side_effect=fail) as model:
            with self.assertRaises(ProviderStatusError):
                asyncio.run(gen.generate_rubric(task=task, trajectories=[], messages=[],
                                                config={}, num_dimensions=5))
            self.assertEqual(model.call_count, 1)


if __name__ == "__main__":
    unittest.main()
