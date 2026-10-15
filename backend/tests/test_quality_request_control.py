"""Offline coverage of per-request deadlines and isolated safe diagnostics."""
from __future__ import annotations

import asyncio
import json
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from openai import APITimeoutError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "DevelopRubrics"))
from adarubric.llm import openai_client as client_module
from adarubric.llm.request_control import (
    QualityClientCleanupTimeout, QualityRequestTimeout, RequestConfigurationError, controlled_request,
    observe_requests, request_context, request_timeout_seconds, safe_service_url,
)

METADATA = {
    "timeout_seconds": 1.0, "messages": [{"role": "user", "content": "private prompt"}],
    "max_tokens": 123, "model": "test-model", "base_url": "https://user:secret@model.invalid/v1?api_key=secret#secret",
    "http_proxy_enabled": False, "http_trust_env": False,
}


class RequestConfigurationTests(unittest.TestCase):
    def test_defaults_and_positive_finite_configuration(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(request_timeout_seconds(), 300)
        for raw, expected in (("12.5", 12.5), (" 1 ", 1.0), ("0.01", .01)):
            with patch.dict(os.environ, {"ADARUBRIC_REQUEST_TIMEOUT_SECONDS": raw}, clear=True):
                self.assertEqual(request_timeout_seconds(), expected)

    def test_invalid_values_are_safe_configuration_errors(self):
        for value in ("", "0", "-1", "nan", "inf", "-Infinity", "private-secret"):
            with self.subTest(value=value), patch.dict(os.environ, {"ADARUBRIC_REQUEST_TIMEOUT_SECONDS": value}, clear=True):
                with self.assertRaises(RequestConfigurationError) as caught:
                    request_timeout_seconds()
                self.assertEqual(str(caught.exception), "ADARUBRIC_REQUEST_TIMEOUT_SECONDS must be a finite positive number")

    def test_addresses_redact_credentials_and_unknown_paths(self):
        self.assertEqual(safe_service_url(METADATA["base_url"]), "https://model.invalid/v1")
        self.assertEqual(safe_service_url("http://user:pass@[::1]:9011/api/private-key/v1"),
                         "http://[::1]:9011/api/[redacted]/v1")
        for url in ("relative/secret", "ftp://model.invalid", "https://bad\nhost.invalid/v1", "https://x.invalid:bad/v1", None):
            self.assertIsNone(safe_service_url(url))


class RequestDeadlineTests(unittest.IsolatedAsyncioTestCase):
    async def test_success_metadata_contains_no_prompt_response_or_credentials(self):
        frames = []
        with observe_requests(frames.append), request_context("rubric_generate", task_id="task-A"):
            result = await controlled_request(AsyncMock(return_value="private response"), **METADATA)
        self.assertEqual(result, "private response")
        self.assertEqual([frame["event"] for frame in frames], ["started", "finished"])
        self.assertEqual(frames[0]["local_request_id"], frames[1]["local_request_id"])
        self.assertEqual(frames[0]["task_id"], "task-A")
        self.assertEqual(frames[0]["operation"], "rubric_generate")
        self.assertEqual(frames[0]["input_characters"], len("private prompt"))
        serialized = json.dumps(frames)
        for private in ("private prompt", "private response", "secret", "api_key", "user:"):
            self.assertNotIn(private, serialized)

    async def test_never_returning_request_is_cancelled_at_deadline(self):
        cancelled = asyncio.Event()
        async def never_returns():
            try:
                await asyncio.Future()
            finally:
                cancelled.set()
        frames = []
        with observe_requests(frames.append), self.assertRaises(QualityRequestTimeout) as caught:
            await controlled_request(never_returns, **{**METADATA, "timeout_seconds": .01})
        self.assertTrue(cancelled.is_set())
        self.assertEqual(caught.exception.body["code"], "quality_request_deadline")
        self.assertEqual([frame["event"] for frame in frames], ["started", "timeout"])

    async def test_intermittent_response_activity_does_not_extend_total_deadline(self):
        ticks = []
        async def trickles_indefinitely():
            while True:
                ticks.append(1)
                await asyncio.sleep(.001)
        with self.assertRaises(QualityRequestTimeout):
            await controlled_request(trickles_indefinitely, **{**METADATA, "timeout_seconds": .04})
        self.assertGreater(len(ticks), 1)

    async def test_deadline_has_safe_retryable_failure_code(self):
        from backend.pipeline_retry_errors import failure_from_exception
        failure = failure_from_exception(QualityRequestTimeout())
        self.assertEqual(failure["category"], "timeout")
        self.assertEqual(failure["code"], "quality_request_deadline")
        self.assertTrue(failure["retryable"])

    async def test_timeout_is_reported_before_slow_cancellation_and_late_result_is_discarded(self):
        cancelling = asyncio.Event()
        allow_exit = asyncio.Event()
        async def delayed_cancellation():
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                cancelling.set()
                await allow_exit.wait()
                return "late response must not be used"
        frames = []
        with observe_requests(frames.append):
            running = asyncio.create_task(controlled_request(delayed_cancellation, **{**METADATA, "timeout_seconds": .01}))
            await asyncio.wait_for(cancelling.wait(), timeout=1)
            self.assertEqual([frame["event"] for frame in frames], ["started", "timeout"])
            self.assertFalse(running.done(), "a retry cannot begin until the old HTTP coroutine exits")
            allow_exit.set()
            with self.assertRaises(QualityRequestTimeout):
                await running
        self.assertEqual(len(frames), 2)

    async def test_concurrent_requests_keep_context_and_independent_ids(self):
        barrier = asyncio.Event()
        frames = []
        async def call(trajectory):
            with request_context("trajectory_score", trajectory_id=trajectory):
                return await controlled_request(lambda: barrier.wait(), **METADATA)
        with observe_requests(frames.append), request_context("trajectory_score", task_id="task-shared"):
            tasks = [asyncio.create_task(call(identity)) for identity in ("trajectory-A", "trajectory-B")]
            while len(frames) < 2:
                await asyncio.sleep(0)
            barrier.set()
            await asyncio.gather(*tasks)
        starts = [frame for frame in frames if frame["event"] == "started"]
        self.assertEqual({frame["trajectory_id"] for frame in starts}, {"trajectory-A", "trajectory-B"})
        self.assertEqual({frame["task_id"] for frame in starts}, {"task-shared"})
        self.assertEqual(len({frame["local_request_id"] for frame in starts}), 2)

    async def test_revoked_start_never_constructs_or_sends_request(self):
        request = AsyncMock()
        def revoked(_):
            raise RuntimeError("revoked")
        with observe_requests(revoked), self.assertRaisesRegex(RuntimeError, "revoked"):
            await controlled_request(request, **METADATA)
        request.assert_not_called()

    async def test_cancellation_closes_active_request_and_preserves_cancellation(self):
        started = asyncio.Event()
        cancelled = asyncio.Event()
        async def pending():
            started.set()
            try:
                await asyncio.Future()
            finally:
                cancelled.set()
        frames = []
        with observe_requests(frames.append):
            task = asyncio.create_task(controlled_request(pending, **METADATA))
            await started.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(cancelled.is_set())
        self.assertEqual([frame["event"] for frame in frames], ["started", "failed"])

    async def test_provider_error_metadata_does_not_include_exception_body(self):
        frames = []
        with observe_requests(frames.append), self.assertRaisesRegex(ValueError, "secret"):
            await controlled_request(AsyncMock(side_effect=ValueError("secret")), **METADATA)
        self.assertEqual([frame["event"] for frame in frames], ["started", "failed"])
        self.assertNotIn("secret", json.dumps(frames))

    async def test_sdk_timeout_remains_observable_during_client_cleanup(self):
        frames = []
        error = APITimeoutError(request=httpx.Request("POST", "https://model.invalid/v1"))
        with observe_requests(frames.append), self.assertRaises(APITimeoutError):
            await controlled_request(AsyncMock(side_effect=error), **METADATA)
        self.assertEqual([frame["event"] for frame in frames], ["started", "timeout"])

    async def test_exited_timeout_is_settled_only_when_same_client_and_context_retries(self):
        frames = []
        owner = object()
        timeout = APITimeoutError(request=httpx.Request("POST", "https://model.invalid/v1"))
        with observe_requests(frames.append):
            with request_context("trajectory_score", task_id="task", trajectory_id="one"):
                with self.assertRaises(APITimeoutError):
                    await controlled_request(AsyncMock(side_effect=timeout), request_owner=owner, **METADATA)
                failed_id = frames[0]["local_request_id"]
            with request_context("trajectory_score", task_id="task", trajectory_id="two"):
                await controlled_request(AsyncMock(return_value="ok"), request_owner=owner, **METADATA)
            with request_context("trajectory_score", task_id="task", trajectory_id="one"):
                await controlled_request(AsyncMock(return_value="other client"), request_owner=object(), **METADATA)
                self.assertNotIn("settled", [frame["event"] for frame in frames])
                await controlled_request(AsyncMock(return_value="retried"), request_owner=owner, **METADATA)
        self.assertEqual(frames[-3]["event"], "settled")
        self.assertEqual(frames[-3]["local_request_id"], failed_id)
        self.assertEqual([frame["event"] for frame in frames[-2:]], ["started", "finished"])

    async def test_pipeline_sdk_is_called_only_once_on_timeout(self):
        async def pending(**_):
            await asyncio.Future()
        sdk = SimpleNamespace(base_url="https://model.invalid/v1", close=AsyncMock(),
                              chat=SimpleNamespace(completions=SimpleNamespace(create=AsyncMock(side_effect=pending))))
        with patch.dict(os.environ, {"PIPELINE_MODEL_SINGLE_ATTEMPT": "1", "ADARUBRIC_REQUEST_TIMEOUT_SECONDS": ".01"}, clear=True), \
             patch.object(client_module, "DefaultAsyncHttpxClient"), \
             patch.object(client_module, "AsyncOpenAI", return_value=sdk) as factory:
            client = client_module.OpenAIClient(api_key="test")
            with self.assertRaises(QualityRequestTimeout):
                await client.generate_text([{"role": "user", "content": "x"}])
            self.assertEqual(factory.call_args.kwargs["max_retries"], 0)
            sdk.chat.completions.create.assert_awaited_once()
            await client.close()


class ClientCleanupTests(unittest.IsolatedAsyncioTestCase):
    def _client(self, close):
        sdk = SimpleNamespace(base_url="https://model.invalid/v1", close=close,
                              chat=SimpleNamespace(completions=SimpleNamespace(create=AsyncMock(
                                  return_value=SimpleNamespace(choices=[SimpleNamespace(
                                      message=SimpleNamespace(content="ok"))])))))
        with patch.dict(os.environ, {"PIPELINE_MODEL_SINGLE_ATTEMPT": "1"}, clear=True), \
             patch.object(client_module, "DefaultAsyncHttpxClient"), \
             patch.object(client_module, "AsyncOpenAI", return_value=sdk):
            return client_module.OpenAIClient(api_key="test"), sdk

    async def test_normal_client_cleanup_has_its_own_non_model_operation(self):
        client, sdk = self._client(AsyncMock())
        frames = []
        with observe_requests(frames.append):
            await client.close()
        sdk.close.assert_awaited_once()
        sdk.chat.completions.create.assert_not_called()
        self.assertEqual([frame["event"] for frame in frames], ["started", "finished"])
        self.assertTrue(all(frame["operation"] == "client_cleanup" for frame in frames))
        self.assertEqual(frames[0]["timeout_seconds"], 15)
        self.assertEqual(frames[0]["input_characters"], 0)

    async def test_completed_model_call_cannot_hide_a_stuck_client_close(self):
        async def pending_close():
            await asyncio.Future()
        client, sdk = self._client(AsyncMock(side_effect=pending_close))
        frames = []
        with observe_requests(frames.append), patch.object(client_module, "_CLIENT_CLEANUP_TIMEOUT_SECONDS", .01):
            self.assertEqual(await client.generate_text([{"role": "user", "content": "x"}]), "ok")
            with self.assertRaises(QualityClientCleanupTimeout) as caught:
                await client.close()
        self.assertEqual(caught.exception.body["code"], "quality_client_cleanup_timeout")
        self.assertEqual([frame["event"] for frame in frames], ["started", "finished", "started", "timeout"])
        self.assertEqual(frames[-1]["operation"], "client_cleanup")
        sdk.chat.completions.create.assert_awaited_once()

    async def test_cleanup_ignoring_cancellation_remains_visible_until_parent_can_kill(self):
        cancelled, release = asyncio.Event(), asyncio.Event()
        async def delayed_close():
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                cancelled.set()
                await release.wait()
        client, _ = self._client(AsyncMock(side_effect=delayed_close))
        frames = []
        with observe_requests(frames.append), patch.object(client_module, "_CLIENT_CLEANUP_TIMEOUT_SECONDS", .01):
            task = asyncio.create_task(client.close())
            await asyncio.wait_for(cancelled.wait(), 1)
            self.assertFalse(task.done())
            self.assertEqual(frames[-1]["operation"], "client_cleanup")
            self.assertEqual(frames[-1]["event"], "timeout")
            release.set()
            with self.assertRaises(QualityClientCleanupTimeout):
                await task

    async def test_cleanup_timeout_retains_original_model_error(self):
        async def pending_close():
            await asyncio.Future()
        client, _ = self._client(AsyncMock(side_effect=pending_close))
        original = APITimeoutError(request=httpx.Request("POST", "https://model.invalid/v1"))
        with patch.object(client_module, "_CLIENT_CLEANUP_TIMEOUT_SECONDS", .01):
            with self.assertRaises(QualityClientCleanupTimeout) as caught:
                try:
                    raise original
                finally:
                    await client.close()
        self.assertIs(caught.exception.__cause__, original)


if __name__ == "__main__":
    unittest.main()
