"""External quality probes use one mock request and preserve opt-in raw evidence."""
from __future__ import annotations

import asyncio
import importlib
import contextlib
import hashlib
import io
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx


class QualityJobDiagnosticTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.output = Path(self.temp.name) / "diagnostic"
        self.output.mkdir()
        self.module = importlib.import_module("scripts.diagnose_quality_job")
        self.requests = []
        self.options = {
            "model": "fixture-model",
            "base_url": "https://model.invalid/v1",
            "api_key": "sk-fixture-not-a-real-key",
            "messages": [{"role": "system", "content": "fixture system"},
                         {"role": "user", "content": "原始诊断输入"}],
            "temperature": 0.0, "max_tokens": 41,
            "extra_body": {"enable_thinking": False},
            "http_options": {"proxy": None, "trust_env": False},
        }
        isolated = {name: os.environ[name] for name in ("SYSTEMROOT", "WINDIR") if name in os.environ}
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, isolated, clear=True).start()
        patch("socket.create_connection", side_effect=AssertionError("real network is forbidden")).start()
        patch("socket.socket.connect", side_effect=AssertionError("real network is forbidden")).start()

    async def _run(self, respond, **kwargs):
        async def capture(request):
            self.requests.append(request)
            return await respond(request)
        return await self.module.perform_request(self.options, self.output,
                                                 transport=httpx.MockTransport(capture), **kwargs)

    def _assert_raw_request(self):
        self.assertEqual(len(self.requests), 1)
        request = self.requests[0]
        self.assertEqual((self.output / "request_body.bin").read_bytes(), request.content)
        saved = json.loads((self.output / "request.json").read_text(encoding="utf-8"))
        serialized = json.dumps(saved, ensure_ascii=False)
        # The raw probe was explicitly requested: fixture credentials must remain
        # in its private output file, never silently replaced with redactions.
        self.assertIn("Bearer " + self.options["api_key"], serialized)
        self.assertIn(str(request.url), serialized)
        expected = {
            "model": self.options["model"], "messages": self.options["messages"],
            "temperature": self.options["temperature"], "max_tokens": self.options["max_tokens"], "enable_thinking": False,
        }
        if self.options.get("response_format"):
            expected["response_format"] = self.options["response_format"]
        self.assertEqual(json.loads(request.content), expected)

    async def test_success_preserves_exact_request_and_response_bytes(self):
        raw = json.dumps({"id": "fixture", "object": "chat.completion", "created": 0,
            "model": "fixture-model", "choices": [{"index": 0,
                "message": {"role": "assistant", "content": "原始模型回复"}, "finish_reason": "stop"}]},
            ensure_ascii=False, indent=1).encode("utf-8") + b"\r\n"
        async def respond(_):
            return httpx.Response(200, content=raw, headers={"content-type": "application/json",
                                                         "x-request-id": "fixture-success-id"})
        report = await self._run(respond)
        self.assertIsInstance(report, dict)
        self.assertEqual(report["outcome"], "success")
        self.assertEqual(report["request_count"], 1)
        self.assertEqual(report["status_code"], 200)
        self._assert_raw_request()
        self.assertEqual((self.output / "response_body.bin").read_bytes(), raw)
        self.assertEqual((self.output / "response_body.txt").read_text(encoding="utf-8"), raw.decode("utf-8").replace("\r\n", "\n"))
        self.assertTrue((self.output / "report.json").is_file())

    async def test_http_400_keeps_original_provider_error_without_retry(self):
        raw = b'{"error": {"message": "fixture unredacted error sk-provider-fixture", "code": "fixture_bad_input"}}\r\n'
        async def respond(_):
            return httpx.Response(400, content=raw, headers={"content-type": "application/json",
                                                         "x-request-id": "fixture-failure-id"})
        report = await self._run(respond)
        self.assertEqual(report["outcome"], "failed")
        self.assertEqual(report["status_code"], 400)
        self._assert_raw_request()
        self.assertEqual((self.output / "response_body.bin").read_bytes(), raw)
        self.assertIn("fixture unredacted error sk-provider-fixture", (self.output / "response_body.txt").read_text(encoding="utf-8"))
        self.assertIn("fixture_bad_input", (self.output / "exception.txt").read_text(encoding="utf-8"))

    async def test_connect_error_records_original_exception_and_does_not_retry(self):
        async def respond(request):
            raise httpx.ConnectError("fixture offline connection failure", request=request)
        report = await self._run(respond)
        self.assertIsInstance(report, dict)
        self.assertEqual(report["outcome"], "failed")
        self.assertEqual(report["request_count"], 1)
        self.assertIsNone(report["status_code"])
        self._assert_raw_request()
        self.assertIn("fixture offline connection failure", (self.output / "exception.txt").read_text(encoding="utf-8"))

    async def test_slow_response_fragments_do_not_extend_total_deadline(self):
        stream_closed = asyncio.Event()
        class SlowStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                while True:
                    yield b" "
                    await asyncio.sleep(.003)
            async def aclose(self):
                stream_closed.set()
        async def respond(_):
            return httpx.Response(200, stream=SlowStream(), headers={"content-type": "application/json"})
        started = time.monotonic()
        report = await self._run(respond, timeout_seconds=.05)
        self.assertLess(time.monotonic() - started, 2)
        self.assertIsInstance(report, dict)
        self._assert_raw_request()
        self.assertTrue(stream_closed.is_set())
        self.assertTrue((self.output / "exception.txt").is_file())

    async def test_redirect_does_not_submit_a_second_http_request(self):
        async def respond(_):
            return httpx.Response(307, headers={"location": "https://other.invalid/v1/chat/completions"},
                                  content=b"fixture redirect")
        report = await self._run(respond)
        self.assertEqual(report["request_count"], 1)
        self.assertEqual(report["status_code"], 307)
        self.assertEqual(report["outcome"], "failed")
        self._assert_raw_request()

    async def test_cooperative_stalled_sdk_cleanup_is_bounded_and_recorded(self):
        from openai import AsyncOpenAI
        original_close = AsyncOpenAI.close
        original_wait_for = asyncio.wait_for
        closed = asyncio.Event()
        async def stalled_close(client):
            await original_close(client)
            closed.set()
            await asyncio.Future()
        async def short_cleanup(awaitable, timeout):
            return await original_wait_for(awaitable, timeout=.02 if timeout == 5 else timeout)
        async def respond(_):
            return httpx.Response(400, json={"error": {"message": "fixture rejected"}})
        with patch.object(AsyncOpenAI, "close", new=stalled_close), \
             patch.object(self.module.asyncio, "wait_for", side_effect=short_cleanup):
            report = await self._run(respond)
        self.assertTrue(closed.is_set())
        self.assertIn("cleanup_error", report)
        self.assertEqual(report["status_code"], 400)
        self.assertEqual(report["request_count"], 1)

    def _score_context(self):
        return {
            "rubric": {"task_id": "task-score", "dimensions": [{"name": "quality",
                "description": "Evaluate the quality of this action", "weight": 1.0,
                "scoring_criteria": {str(level): "criterion " + str(level) for level in range(1, 6)}}]},
            "trajectory": {"trajectory_id": "trajectory-score", "task_id": "task-score",
                "steps": [{"step_id": identity, "action": "click", "action_input": {"summary": "page summary"},
                           "observation": "observed page " + str(identity)} for identity in (0, 3, 7, 11)],
                "final_answer": "fixture answer"},
            "target_step_ids": [3], "config": {},
        }

    async def test_http_200_score_schema_failure_is_reported_without_second_request(self):
        content = json.dumps({"step_evaluations": [{"step_id": 3, "dimension_scores": [
            {"dimension_name": "quality", "score": 99, "rationale": "fixture raw failed rationale"}]}]})
        self.options.update(response_format={"type": "json_object"}, validation_context=self._score_context())
        async def respond(_):
            return httpx.Response(200, json={"id": "fixture", "object": "chat.completion", "created": 0,
                "model": "fixture-model", "choices": [{"index": 0,
                    "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}]})
        with self.assertLogs("adarubric.llm.openai_client", level="WARNING"):
            report = await self._run(respond)
        self._assert_raw_request()
        self.assertEqual(report["status_code"], 200)
        self.assertEqual(report["outcome"], "failed")
        self.assertEqual(report["response_validation"]["status"], "failed")
        self.assertEqual(report["request_count"], 1)
        detail = (self.output / "validation_error.json").read_text(encoding="utf-8")
        self.assertIn("fixture raw failed rationale", detail)
        self.assertIn("99", detail)
        self.assertTrue((self.output / "exception.txt").is_file())

    async def test_correct_score_uses_production_conversion_and_json_response_format(self):
        content = json.dumps({"step_evaluations": [{"step_id": 3, "dimension_scores": [
            {"dimension_name": "quality", "score": 4, "confidence": .9, "rationale": "fixture rationale"}]}]})
        self.options.update(response_format={"type": "json_object"}, validation_context=self._score_context())
        async def respond(_):
            return httpx.Response(200, json={"id": "fixture", "object": "chat.completion", "created": 0,
                "model": "fixture-model", "choices": [{"index": 0,
                    "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}]})
        report = await self._run(respond)
        self._assert_raw_request()
        self.assertEqual(report["outcome"], "success")
        self.assertEqual(report["request_count"], 1)
        self.assertEqual(report["response_validation"]["status"], "passed")
        self.assertEqual(report["response_validation"]["expected_step_ids"], [3])
        self.assertEqual(report["response_validation"]["returned_step_ids"], [3])
        self.assertEqual(report["response_validation"]["missing_step_ids"], [])
        self.assertFalse((self.output / "validation_error.json").exists())

    async def test_prepared_score_messages_match_real_structured_evaluator_request(self):
        from adarubric.core.models import TaskDescription, Trajectory, DynamicRubric
        from adarubric.evaluator.trajectory_evaluator import LLMTrajectoryEvaluator
        from adarubric.llm.openai_client import OpenAIClient
        generator = self.module.load_example("generate-jiawen-rubrics")
        evaluation = self.module.load_example("evaluate-jiawen-rubrics")
        fixture = self._score_context()
        task = TaskDescription(task_id="task-score", instruction="Perform this fixture task")
        trajectory = Trajectory.model_validate(fixture["trajectory"])
        rubric = DynamicRubric.model_validate(fixture["rubric"])
        root = Path(self.temp.name)
        cached = root / "system" / "rubric_outputs" / "rubrics" / "jiawen_gui_initial_rubric__task-score.json"
        cached.parent.mkdir(parents=True)
        cached.write_text(rubric.model_dump_json(), encoding="utf-8")
        original_hash = hashlib.sha256(cached.read_bytes()).hexdigest()
        for chunked in (False, True):
            config = {"evaluation_chunk_enabled": chunked, "evaluation_chunk_threshold": 1,
                      "evaluation_step_lookahead": 1, "evaluation_include_action": False,
                      "evaluation_include_action_input": True, "evaluation_temperature": .25,
                      "evaluation_max_tokens": 301}
            with self.subTest(chunked=chunked), patch.dict(os.environ, {"ADARUBRIC_EVAL_MAX_TOKENS": "999"}):
                prepared, metadata = self.module.prepare_score(root, generator, config, task, [trajectory],
                    {"failure": {"trajectory_id": trajectory.trajectory_id, "step": 3}}, self.output, None, None)
                effective = evaluation._trajectory_for_evaluation(trajectory, include_action=False, include_action_input=True)
                targets = None
                if chunked:
                    chunk = next(item for item in evaluation._trajectory_chunks(effective, lookahead_steps=1)
                                 if 3 in item.core_step_ids)
                    effective, targets = chunk.trajectory, chunk.core_step_ids
                    self.assertEqual(metadata["context_step_ids"], [0, 3, 7])
                    self.assertEqual(metadata["target_step_ids"], [3])
                seen = []
                class RecordedProductionClient:
                    generate_structured = OpenAIClient.generate_structured
                    async def _chat(self, messages, **kwargs):
                        seen.append((messages, kwargs))
                        return json.dumps({"step_evaluations": [{"step_id": identity,
                            "dimension_scores": [{"dimension_name": "quality", "score": 4}]} for identity in
                            (targets if targets is not None else [item.step_id for item in effective.steps])]})
                production = LLMTrajectoryEvaluator(RecordedProductionClient(), aggregator=evaluation._build_aggregator(config))
                await production.evaluate(effective, rubric, task_instruction=task.instruction,
                                          temperature=.25, max_tokens=301, target_step_ids=targets)
                self.assertEqual(len(seen), 1)
                self.assertEqual(prepared["messages"], seen[0][0])
                self.assertEqual(prepared["max_tokens"], seen[0][1]["max_tokens"])
                self.assertEqual(prepared["temperature"], seen[0][1]["temperature"])
                self.assertTrue(seen[0][1]["json_mode"])
                self.assertEqual(prepared["response_format"], {"type": "json_object"})
        self.assertEqual(original_hash, hashlib.sha256(cached.read_bytes()).hexdigest())


class QualityJobDiagnosticCLITests(unittest.TestCase):
    def test_default_cli_only_exports_existing_records_without_model_or_business_writes(self):
        from scripts import diagnose_quality_job as diagnostic
        from backend.data_store import RecordStore
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            records = RecordStore(root)
            records.put("quality_jobs", "job-fixture", {
                "job_id": "job-fixture", "batch_id": "fixture-batch", "status": "failed",
                "stage": "generating_rubric", "current_task": "fixture-task", "created_at": "2026-10-14T00:00:00Z",
                "failure": {"category": "bad_request", "http_status": 400, "code": "fixture-code"},
            })
            artifact = root / "batches" / "fixture-batch" / "05_quality" / "result.json"
            artifact.parent.mkdir(parents=True)
            artifact.write_bytes(b'{"fixture":true}')
            watched = [records.database_path, artifact]
            before = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in watched}
            with patch.object(diagnostic, "prepare_request", side_effect=AssertionError("no model preparation without --probe")), \
                 patch.object(diagnostic, "perform_request", side_effect=AssertionError("no model call without --probe")), \
                 patch("socket.socket.connect", side_effect=AssertionError("real network is forbidden")), \
                 contextlib.redirect_stdout(io.StringIO()) as output:
                result = diagnostic.main(["--data-root", str(root), "--job-id", "job-fixture"])
            self.assertEqual(result, 0)
            self.assertIn("只导出了已有记录", output.getvalue())
            self.assertEqual(before, {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in watched})
            folders = list((root / "logs" / "quality_diagnostics").iterdir())
            self.assertEqual(len(folders), 1)
            self.assertEqual(json.loads((folders[0] / "job.json").read_text(encoding="utf-8"))["job_id"], "job-fixture")
            self.assertFalse((folders[0] / "request.json").exists())


if __name__ == "__main__":
    unittest.main()
