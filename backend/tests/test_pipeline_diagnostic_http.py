"""Passive Pipeline model diagnostics, entirely with mock HTTP transports."""
from __future__ import annotations

import asyncio
import gzip
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from openai import AsyncOpenAI, OpenAI
from pydantic import BaseModel

from backend import pipeline_diagnostics as trace
from backend.pipeline_diagnostic_http import instrument_http_client, model_operation, install_box_reviewer_logging


REPLY = {"id": "fixture", "object": "chat.completion", "created": 1, "model": "mock",
         "choices": [{"index": 0, "message": {"role": "assistant", "content": '{"ok":true}'},
                      "finish_reason": "stop"}], "usage": {"prompt_tokens": 9, "completion_tokens": 4, "total_tokens": 13}}


class SyncChunks(httpx.SyncByteStream):
    def __init__(self, chunks, error=None):
        self.chunks, self.error = chunks, error

    def __iter__(self):
        yield from self.chunks
        if self.error:
            raise self.error


class AsyncChunks(httpx.AsyncByteStream):
    def __init__(self, chunks, *, delay=0, error=None):
        self.chunks, self.delay, self.error = chunks, delay, error

    async def __aiter__(self):
        for chunk in self.chunks:
            await asyncio.sleep(self.delay)
            yield chunk
        if self.error:
            raise self.error


class SyncModel:
    model = "mock"

    def __init__(self, transport, *, retries=0):
        self.http = instrument_http_client(httpx.Client(transport=transport, trust_env=False),
                                           api_key="fixture-secret-never-log", settings={"trust_env": False, "proxy": None})
        self.sdk = OpenAI(api_key="fixture-secret-never-log", base_url="https://mock.invalid/v1", http_client=self.http,
                          max_retries=retries)

    @model_operation("fixture_score")
    def generate(self, *, trajectory_id="tr_1", step=3, invalid=False):
        result = self.sdk.chat.completions.create(model=self.model,
            messages=[{"role": "user", "content": "same input"}], max_tokens=17, temperature=0)
        if invalid:
            raise ValueError("invalid JSON body fixture-secret-never-log")
        return json.loads(result.choices[0].message.content)


class AsyncModel:
    model = "mock"

    def __init__(self, transport):
        self.http = instrument_http_client(httpx.AsyncClient(transport=transport, trust_env=False),
            api_key="fixture-secret-never-log", settings={"trust_env": False, "proxy": None})
        self.sdk = AsyncOpenAI(api_key="fixture-secret-never-log", base_url="https://mock.invalid/v1", http_client=self.http, max_retries=0)

    @model_operation("fixture_score")
    async def generate(self, *, trajectory_id="tr_1", timeout=None):
        pending = self.sdk.chat.completions.create(model=self.model, messages=[{"role": "user", "content": "same input"}], max_tokens=17)
        response = await asyncio.wait_for(pending, timeout) if timeout else await pending
        return json.loads(response.choices[0].message.content)


class PipelineDiagnosticHttpTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.log = self.root / "logs" / "pipelines" / "pl_fixture"
        self.addCleanup(patch.stopall)
        patch("socket.socket.connect", side_effect=AssertionError("network forbidden")).start()

    def context(self, **kwargs):
        return trace.trace_context(root=self.root, pipeline_id="pl_fixture", batch_id="batch_fixture",
                                   step_id="quality", job_id="job_fixture", execution_id="exec_fixture", **kwargs)

    def read(self, pattern):
        return [json.loads(path.read_text(encoding="utf-8")) for path in self.log.glob(pattern)]

    def test_sync_success_identical_body_and_metadata(self):
        requests = []
        def handle(request):
            requests.append((request.url, request.method, request.content, request.extensions["timeout"]))
            return httpx.Response(200, stream=SyncChunks([json.dumps(REPLY).encode()]), headers={"x-request-id": "server_1"})
        model = SyncModel(httpx.MockTransport(handle))
        try:
            baseline = model.generate()
            self.assertFalse(self.log.exists())
            with self.context():
                self.assertEqual(model.generate(), baseline)
        finally:
            model.sdk.close()
        self.assertEqual(requests[0], requests[1])
        call = self.read("requests/*/call.json")[0]
        attempt = self.read("requests/*/attempts/*/attempt.json")[0]
        self.assertEqual(call["job_id"], "job_fixture")
        self.assertEqual(call["trajectory_id"], "tr_1")
        self.assertEqual(call["step"], 3)
        self.assertEqual(call["http_attempt_count"], 1)
        self.assertEqual(call["status"], "success")
        self.assertEqual(attempt["finish_reasons"], ["stop"])
        self.assertEqual(attempt["usage"]["total_tokens"], 13)
        self.assertTrue(attempt["response_body_complete"])
        self.assertLessEqual(attempt["headers_elapsed_ms"], attempt["first_body_elapsed_ms"])
        all_text = "\n".join(p.read_text(encoding="utf-8") for p in self.log.rglob("*") if p.is_file())
        self.assertNotIn("fixture-secret-never-log", all_text)
        self.assertNotIn("Authorization", all_text)

    def test_http_200_parse_error_separate_from_http_success(self):
        model = SyncModel(httpx.MockTransport(lambda request: httpx.Response(200, json=REPLY)))
        try:
            with self.context(), self.assertRaises(ValueError):
                model.generate(invalid=True)
        finally:
            model.sdk.close()
        call = self.read("requests/*/call.json")[0]
        attempt = self.read("requests/*/attempts/*/attempt.json")[0]
        self.assertEqual(call["status"], "failed")
        self.assertEqual(attempt["status_code"], 200)
        self.assertTrue(attempt["response_body_complete"])
        self.assertIn("ValueError", json.dumps(call["exception"]))
        self.assertNotIn("fixture-secret-never-log", json.dumps(call))

    async def test_no_response_headers_timeout(self):
        async def handle(request):
            await asyncio.sleep(60)
        model = AsyncModel(httpx.MockTransport(handle))
        try:
            with self.context(), self.assertRaises(TimeoutError):
                await model.generate(timeout=0.3)
        finally:
            await model.sdk.close()
        attempt = self.read("requests/*/attempts/*/attempt.json")[0]
        self.assertIsNone(attempt["status_code"])
        self.assertFalse(attempt["response_headers_received"])
        self.assertEqual(attempt["waiting_at"], "response_headers")
        self.assertEqual(len(self.read("requests/*/call.json")), 1)

    async def test_partial_body_disconnect(self):
        def handle(request):
            return httpx.Response(200, stream=AsyncChunks([b'{"partial":"received"'], error=httpx.ReadError("connection lost")))
        model = AsyncModel(httpx.MockTransport(handle))
        try:
            with self.context(), self.assertRaises(Exception):
                await model.generate()
        finally:
            await model.sdk.close()
        attempt = self.read("requests/*/attempts/*/attempt.json")[0]
        self.assertEqual(attempt["status_code"], 200)
        self.assertFalse(attempt["response_body_complete"])
        self.assertEqual(attempt["waiting_at"], "response_body")
        body = next(self.log.glob("requests/*/attempts/*/response_body.txt")).read_text()
        self.assertIn("received", body)

    async def test_concurrent_calls_keep_distinct_identity(self):
        model = AsyncModel(httpx.MockTransport(lambda request: httpx.Response(200,
            stream=AsyncChunks([json.dumps(REPLY).encode()], delay=0.01))))
        try:
            with self.context():
                await asyncio.gather(model.generate(trajectory_id="tr_a"), model.generate(trajectory_id="tr_b"))
        finally:
            await model.sdk.close()
        calls = self.read("requests/*/call.json")
        self.assertEqual({c["trajectory_id"] for c in calls}, {"tr_a", "tr_b"})
        self.assertTrue(all(c["http_attempt_count"] == 1 for c in calls))

    def test_sdk_retry_records_two_attempts_one_logical_call(self):
        seen = []
        def handle(request):
            seen.append(request)
            if len(seen) == 1:
                return httpx.Response(503, json={"error": "busy"}, headers={"retry-after-ms": "1"})
            return httpx.Response(200, json=REPLY)
        model = SyncModel(httpx.MockTransport(handle), retries=1)
        try:
            with self.context():
                self.assertEqual(model.generate(), {"ok": True})
        finally:
            model.sdk.close()
        calls = self.read("requests/*/call.json")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["http_attempt_count"], 2)
        attempts = self.read("requests/*/attempts/*/attempt.json")
        self.assertEqual({a["status_code"] for a in attempts}, {200, 503})

    def test_gzip_response_decoded_for_log_without_altering_sdk(self):
        packed = gzip.compress(json.dumps(REPLY).encode())
        model = SyncModel(httpx.MockTransport(lambda request: httpx.Response(200,
            stream=SyncChunks([packed[:16], packed[16:]]), headers={"content-encoding": "gzip"})))
        try:
            with self.context():
                self.assertEqual(model.generate(), {"ok": True})
        finally:
            model.sdk.close()
        attempt = self.read("requests/*/attempts/*/attempt.json")[0]
        self.assertEqual(attempt["usage"]["total_tokens"], 13)
        self.assertEqual(attempt["received_bytes"], len(packed))

    def test_log_failure_never_changes_result_or_network_count(self):
        seen = []
        def handle(request):
            seen.append(request)
            return httpx.Response(200, json=REPLY)
        model = SyncModel(httpx.MockTransport(handle))
        try:
            with self.context(), patch.object(trace, "write_json", side_effect=OSError("disk full")), \
                    patch("backend.pipeline_diagnostic_http._LOG.warning", side_effect=RuntimeError("bad logger")):
                self.assertEqual(model.generate(), {"ok": True})
        finally:
            model.sdk.close()
        self.assertEqual(len(seen), 1)

    def test_images_credentials_not_persisted(self):
        client = instrument_http_client(httpx.Client(transport=httpx.MockTransport(lambda request:
            httpx.Response(200, json={"ok": True}))), api_key="fixture-secret-never-log")
        @model_operation("image_test")
        def run():
            return client.post("https://u:password@mock.invalid/v1?api_key=fixture-secret-never-log", json={
                "messages": [{"content": [{"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,aGVsbG8="}}]}],
                "api_key": "fixture-secret-never-log"}, headers={"Cookie": "private-cookie", "X-Custom-Secret": "hidden"})
        try:
            with self.context():
                self.assertEqual(run().status_code, 200)
        finally:
            client.close()
        text = "\n".join(p.read_text(encoding="utf-8") for p in self.log.rglob("*") if p.is_file())
        for secret in ("fixture-secret-never-log", "private-cookie", "aGVsbG8=", "u:password", "hidden"):
            self.assertNotIn(secret, text)
        self.assertIn("sha256", text)

    async def test_quality_real_client_rubric_repair_and_parse_failure(self):
        project = Path(__file__).resolve().parents[2]
        if str(project / "backend" / "DevelopRubrics") not in sys.path:
            sys.path.insert(0, str(project / "backend" / "DevelopRubrics"))
        from adarubric.llm.openai_client import OpenAIClient
        from adarubric.llm.request_control import request_context
        class Answer(BaseModel):
            ok: bool
        requests = []
        def handle(request):
            requests.append(json.loads(request.content))
            reply = json.loads(json.dumps(REPLY))
            if len(requests) == 1:
                reply["choices"][0]["message"]["content"] = "not structured JSON"
            return httpx.Response(200, json=reply)
        http = httpx.AsyncClient(transport=httpx.MockTransport(handle), trust_env=False)
        with patch.dict(os.environ, {"PIPELINE_MODEL_SINGLE_ATTEMPT": "1", "ADARUBRIC_REQUEST_TIMEOUT_SECONDS": "300"}), \
                patch("adarubric.llm.openai_client.DefaultAsyncHttpxClient", return_value=http):
            client = OpenAIClient(model="mock", api_key="fixture-secret-never-log", base_url="https://mock.invalid/v1")
        try:
            with self.context(), request_context("rubric_generate", task_id="task_1"):
                with self.assertRaises(Exception):
                    await client.generate_structured([{"role": "user", "content": "evaluate"}], Answer)
            with self.context(), request_context("rubric_repair", task_id="task_1"):
                self.assertTrue((await client.generate_structured([{"role": "user", "content": "repair"}], Answer)).ok)
        finally:
            await client.close()
        calls = self.read("requests/*/call.json")
        self.assertEqual({c["operation"] for c in calls}, {"rubric_generate", "rubric_repair"})
        self.assertEqual({c["status"] for c in calls}, {"failed", "success"})
        self.assertTrue(all(c["task_id"] == "task_1" and c["http_attempt_count"] == 1 for c in calls))
        self.assertEqual(len(requests), 2)
        self.assertTrue(all(r["response_format"] == {"type": "json_object"} for r in requests))

    async def test_partial_secret_prefix_never_saved(self):
        secret = "fixture-secret-never-log"
        prefix = secret[:-4]
        observed = []
        outer = self
        class SecretChunks(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield ('{"content":"' + prefix).encode()
                path = next(outer.log.glob("requests/*/attempts/*/response_body.txt"))
                observed.append(path.read_text(encoding="utf-8"))
                raise httpx.ReadError("disconnected")
        model = AsyncModel(httpx.MockTransport(lambda request: httpx.Response(200, stream=SecretChunks())))
        try:
            with self.context(), self.assertRaises(Exception):
                await model.generate()
        finally:
            await model.sdk.close()
        self.assertTrue(observed)
        self.assertNotIn(prefix, observed[0])
        self.assertNotIn(prefix, next(self.log.glob("requests/*/attempts/*/response_body.txt")).read_text())

    async def test_partial_json_images_and_escaped_url_never_saved(self):
        examples = [b'{"source":{"type":"base64","media_type":"image/png","data":"YWJjYWJj',
                    b'{"url":"http:\\/\\/username:password@host/v1']
        for payload in examples:
            with self.subTest(payload=payload):
                model = AsyncModel(httpx.MockTransport(lambda request: httpx.Response(200,
                    stream=AsyncChunks([payload], error=httpx.ReadError("disconnected")))))
                try:
                    with self.context(), self.assertRaises(Exception):
                        await model.generate()
                finally:
                    await model.sdk.close()
        text = "\n".join(p.read_text(encoding="utf-8") for p in self.log.rglob("*") if p.is_file())
        for secret in ("YWJjYWJj", "username:password"):
            self.assertNotIn(secret, text)

    def test_malformed_choices_still_finishes_http_record(self):
        model = SyncModel(httpx.MockTransport(lambda request: httpx.Response(200, json={"choices": None})))
        try:
            with self.context(), self.assertRaises(Exception):
                model.generate()
        finally:
            model.sdk.close()
        attempt = self.read("requests/*/attempts/*/attempt.json")[0]
        self.assertTrue(attempt["response_body_complete"])
        self.assertEqual(attempt["status_code"], 200)
        self.assertEqual(attempt["finish_reasons"], [])
        self.assertEqual(self.read("requests/*/call.json")[0]["status"], "failed")

    def test_box_wrapper_is_idempotent_and_source_bytes_unchanged(self):
        from backend.bounding_box.qwen_reviewer import QwenBoxReviewer
        source = Path(__file__).resolve().parents[1] / "bounding_box" / "qwen_reviewer.py"
        before = source.read_bytes()
        original_init, original_review = QwenBoxReviewer.__init__, QwenBoxReviewer.review
        try:
            install_box_reviewer_logging()
            installed_init, installed_review = QwenBoxReviewer.__init__, QwenBoxReviewer.review
            install_box_reviewer_logging()
            self.assertIs(QwenBoxReviewer.__init__, installed_init)
            self.assertIs(QwenBoxReviewer.review, installed_review)
            self.assertEqual(source.read_bytes(), before)
        finally:
            QwenBoxReviewer.__init__, QwenBoxReviewer.review = original_init, original_review

    def test_image_source_refs_are_metadata_only_and_concurrency_is_explicit(self):
        seen = []
        def handle(request):
            seen.append(request.content)
            return httpx.Response(200, json={"ok": True})
        client = instrument_http_client(httpx.Client(transport=httpx.MockTransport(handle), trust_env=False))
        @model_operation("fixture_images")
        def run(*, current_image_path, candidate_image_paths):
            return client.post("https://mock.invalid/v1", json={"messages": ["unchanged"]}).json()
        missing_image = self.root / "does-not-exist" / "截图.jpg"
        paths = [missing_image, "another-missing-image.jpg"]
        try:
            with self.context():
                self.assertEqual(run(current_image_path=missing_image, candidate_image_paths=paths), {"ok": True})
            with self.context(concurrency=2):
                self.assertEqual(run(current_image_path=missing_image, candidate_image_paths=paths), {"ok": True})
        finally:
            client.close()
        self.assertFalse(missing_image.exists())
        self.assertEqual(seen[0], seen[1])
        calls = self.read("requests/*/call.json")
        self.assertEqual({call["concurrency"] for call in calls}, {"unknown", 2})
        for call in calls:
            self.assertEqual(call["image_source_refs"], {
                "current_image_path": str(missing_image),
                "candidate_image_paths": [str(item) for item in paths],
            })


if __name__ == "__main__":
    unittest.main()
