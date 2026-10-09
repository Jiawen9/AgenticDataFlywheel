"""Quality clients use explicit HTTP routing without contacting model services."""
from __future__ import annotations

import asyncio
import importlib
import json
import os
import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
from openai import DefaultAsyncHttpxClient
from openai._constants import DEFAULT_CONNECTION_LIMITS, DEFAULT_MAX_RETRIES, DEFAULT_TIMEOUT

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "DevelopRubrics"))
from adarubric.llm.http_options import resolve_http_options
from trajectory_tools import settings

CLIENTS = (
    ("adarubric.llm.openai_client", "OpenAIClient"),
    ("adarubric.llm.vllm_client", "VLLMClient"),
)
# Windows TLS initialization requires the OS root; model/proxy settings stay isolated.
OS_ENV = {name: os.environ[name] for name in ("SYSTEMROOT", "WINDIR") if name in os.environ}
MODEL_VALUES = {"YUNAI_API_KEY": "test-only-key", "MODEL_URL": "https://model.invalid/v1", "MODEL_NAME": "test-model"}


class QualityHTTPOptionsTests(unittest.TestCase):
    def test_direct_connection_is_default_even_with_standard_proxy_variables(self):
        with patch.dict(os.environ, {"HTTP_PROXY": "http://unused.invalid:1", "HTTPS_PROXY": "http://unused.invalid:2",
                                     "ALL_PROXY": "socks5://unused.invalid:3"}, clear=True):
            self.assertEqual(resolve_http_options(), {"proxy": None, "trust_env": False})

    def test_module_environment_overrides_shared_and_keywords_override_both(self):
        env = {"HTTP_PROXY_URL": "http://shared.invalid:81", "HTTP_TRUST_ENV": "true",
               "ADARUBRIC_HTTP_PROXY_URL": "http://module.invalid:82", "ADARUBRIC_HTTP_TRUST_ENV": "false"}
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(resolve_http_options(), {"proxy": env["ADARUBRIC_HTTP_PROXY_URL"], "trust_env": False})
            self.assertEqual(resolve_http_options(http_proxy_url="http://explicit.invalid:83", http_trust_env=True),
                             {"proxy": "http://explicit.invalid:83", "trust_env": True})
            self.assertEqual(resolve_http_options(http_proxy_url="", http_trust_env=False), {"proxy": None, "trust_env": False})
        with patch.dict(os.environ, {key: value for key, value in env.items() if not key.startswith("ADARUBRIC_")}, clear=True):
            self.assertEqual(resolve_http_options(), {"proxy": env["HTTP_PROXY_URL"], "trust_env": True})

    def test_empty_module_settings_explicitly_clear_shared_settings(self):
        with patch.dict(os.environ, {"HTTP_PROXY_URL": "http://shared.invalid", "HTTP_TRUST_ENV": "yes",
                                     "ADARUBRIC_HTTP_PROXY_URL": "  ", "ADARUBRIC_HTTP_TRUST_ENV": ""}, clear=True):
            self.assertEqual(resolve_http_options(), {"proxy": None, "trust_env": False})

    def test_boolean_spellings_are_strict_case_insensitive_and_whitespace_tolerant(self):
        with patch.dict(os.environ, OS_ENV, clear=True):
            for value in ("true", "1", "yes", "on", " TRUE ", "Yes", True):
                with self.subTest(value=value):
                    self.assertTrue(resolve_http_options(http_trust_env=value)["trust_env"])
            for value in ("false", "0", "no", "off", "", "  ", " FALSE ", False):
                with self.subTest(value=value):
                    self.assertFalse(resolve_http_options(http_trust_env=value)["trust_env"])

    def test_invalid_boolean_only_exposes_field_name(self):
        secret = "invalid-private-credential"
        for env, field in (({"ADARUBRIC_HTTP_TRUST_ENV": secret}, "ADARUBRIC_HTTP_TRUST_ENV"),
                           ({"HTTP_TRUST_ENV": secret}, "HTTP_TRUST_ENV")):
            with patch.dict(os.environ, env, clear=True), self.assertRaises(ValueError) as caught:
                resolve_http_options()
            self.assertEqual(str(caught.exception), field + " must be a boolean")
            self.assertNotIn(secret, str(caught.exception))
        with patch.dict(os.environ, OS_ENV, clear=True):
            for value in (secret, "2", 2, [], {}):
                with self.subTest(value=type(value).__name__), self.assertRaises(ValueError) as caught:
                    resolve_http_options(http_trust_env=value)
                self.assertEqual(str(caught.exception), "http_trust_env must be a boolean")

    def test_invalid_network_option_does_not_allocate_transport(self):
        for module_name, class_name in CLIENTS:
            module = importlib.import_module(module_name)
            with patch.dict(os.environ, OS_ENV, clear=True), patch.object(module, "DefaultAsyncHttpxClient") as transport:
                with self.assertRaises(ValueError):
                    getattr(module, class_name)(model="test", api_key="test", http_trust_env="invalid")
                transport.assert_not_called()

    def test_constructor_failure_closes_transport_in_synchronous_code(self):
        for module_name, class_name in CLIENTS:
            module = importlib.import_module(module_name)
            for kwargs in ({"api_key": None}, {"api_key": "test", "base_url": object()}):
                transports = []
                def make_transport(**options):
                    transport = DefaultAsyncHttpxClient(**options)
                    transports.append(transport)
                    return transport
                with self.subTest(client=class_name, invalid=list(kwargs)), patch.dict(os.environ, OS_ENV, clear=True), \
                     patch.object(module, "DefaultAsyncHttpxClient", side_effect=make_transport):
                    with self.assertRaises(Exception):
                        getattr(module, class_name)(model="test", **kwargs)
                    self.assertEqual(len(transports), 1)
                    self.assertTrue(transports[0].is_closed)

    def test_platform_maps_resolved_network_values_without_overriding_model_behavior(self):
        with patch.dict(os.environ, {"ADARUBRIC_HTTP_PROXY_URL": "stale", "ADARUBRIC_HTTP_TRUST_ENV": "true",
                                     "ADARUBRIC_MODEL": "existing-model"}, clear=True), \
             patch.object(settings, "load_repository_env", return_value=MODEL_VALUES), \
             patch("backend.model_http.model_http_options", return_value={"proxy": None, "trust_env": False}) as options:
            result = settings.configure_model_environment(Path("not-read.env"))
            options.assert_called_once_with(MODEL_VALUES, prefix="ADARUBRIC")
            self.assertEqual(os.environ["ADARUBRIC_HTTP_PROXY_URL"], "")
            self.assertEqual(os.environ["ADARUBRIC_HTTP_TRUST_ENV"], "false")
            self.assertEqual(os.environ["ADARUBRIC_MODEL"], "existing-model")
            self.assertEqual(os.environ["TRAJECTORY_MODEL"], "test-model")
            self.assertEqual(result, {"model": "test-model", "base_url": MODEL_VALUES["MODEL_URL"]})

    def test_platform_uses_shared_file_settings_and_module_specific_precedence(self):
        values = {**MODEL_VALUES, "HTTP_PROXY_URL": "http://file-shared.invalid:81", "HTTP_TRUST_ENV": "no",
                  "ADARUBRIC_HTTP_PROXY_URL": "http://file-module.invalid:82", "ADARUBRIC_HTTP_TRUST_ENV": "on"}
        with patch.dict(os.environ, {"HTTP_PROXY_URL": "http://process-shared.invalid:83",
                                     "HTTP_TRUST_ENV": "off"}, clear=True), \
             patch.object(settings, "load_repository_env", return_value=values):
            settings.configure_model_environment(Path("not-read.env"))
            self.assertEqual(resolve_http_options(), {"proxy": values["ADARUBRIC_HTTP_PROXY_URL"], "trust_env": True})

    def test_platform_rejects_invalid_boolean_without_value_disclosure(self):
        values = {**MODEL_VALUES, "ADARUBRIC_HTTP_TRUST_ENV": "invalid-secret"}
        with patch.dict(os.environ, OS_ENV, clear=True), patch.object(settings, "load_repository_env", return_value=values):
            with self.assertRaises(ValueError) as caught:
                settings.configure_model_environment(Path("not-read.env"))
            self.assertEqual(str(caught.exception), "ADARUBRIC_HTTP_TRUST_ENV must be a boolean")
            self.assertNotIn("ADARUBRIC_HTTP_PROXY_URL", os.environ)


class QualityHTTPClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_default_transport_keeps_sdk_timeouts_pool_redirects_and_retry_settings(self):
        for module_name, class_name in CLIENTS:
            module = importlib.import_module(module_name)
            with self.subTest(client=class_name), patch.dict(os.environ, {**OS_ENV,
                "HTTP_PROXY": "http://unused.invalid:1", "HTTPS_PROXY": "http://unused.invalid:2",
                "ALL_PROXY": "invalid://ignored-system-proxy",
            }, clear=True):
                client = getattr(module, class_name)(model="test", api_key="test", base_url="https://model.invalid/v1")
                try:
                    transport = client._client._client
                    self.assertIsInstance(transport, DefaultAsyncHttpxClient)
                    self.assertFalse(transport.trust_env)
                    self.assertEqual(transport.timeout, DEFAULT_TIMEOUT)
                    self.assertEqual(transport.timeout.read, 600)
                    self.assertTrue(transport.follow_redirects)
                    self.assertEqual(transport._transport._pool._max_connections, DEFAULT_CONNECTION_LIMITS.max_connections)
                    self.assertEqual(transport._transport._pool._max_keepalive_connections, DEFAULT_CONNECTION_LIMITS.max_keepalive_connections)
                    self.assertEqual(client._client.max_retries, DEFAULT_MAX_RETRIES)
                    self.assertEqual(client._max_retries, 3)
                finally:
                    await client.close()
                self.assertTrue(transport.is_closed)

    async def test_http_settings_are_forwarded_and_pipeline_single_attempt_is_unchanged(self):
        for module_name, class_name in CLIENTS:
            module = importlib.import_module(module_name)
            transport = MagicMock()
            transport.aclose = AsyncMock()
            with self.subTest(client=class_name), patch.dict(os.environ, {
                "PIPELINE_MODEL_SINGLE_ATTEMPT": "1", "HTTP_PROXY_URL": "http://shared.invalid",
            }, clear=True), patch.object(module, "DefaultAsyncHttpxClient", return_value=transport) as factory, \
                 patch.object(module, "AsyncOpenAI") as sdk:
                client = getattr(module, class_name)(model="test", api_key="test", base_url="https://model.invalid/v1",
                    max_retries=7, http_proxy_url="http://explicit.invalid:81", http_trust_env=True)
                factory.assert_called_once_with(proxy="http://explicit.invalid:81", trust_env=True)
                self.assertEqual(client._max_retries, 1)
                self.assertEqual(sdk.call_args.kwargs["max_retries"], 0)
                self.assertIs(sdk.call_args.kwargs["http_client"], transport)

    async def test_constructor_failure_closes_transport_on_running_event_loop(self):
        for module_name, class_name in CLIENTS:
            module = importlib.import_module(module_name)
            for kwargs in ({"api_key": None}, {"api_key": "test", "base_url": object()}):
                transports = []
                def make_transport(**options):
                    transport = DefaultAsyncHttpxClient(**options)
                    transports.append(transport)
                    return transport
                with self.subTest(client=class_name, invalid=list(kwargs)), patch.dict(os.environ, OS_ENV, clear=True), \
                     patch.object(module, "DefaultAsyncHttpxClient", side_effect=make_transport):
                    with self.assertRaises(Exception):
                        getattr(module, class_name)(model="test", **kwargs)
                    await asyncio.sleep(0)
                    self.assertEqual(len(transports), 1)
                    self.assertTrue(transports[0].is_closed)

    async def test_cleanup_failure_does_not_replace_original_sdk_initialization_error(self):
        for module_name, class_name in CLIENTS:
            module = importlib.import_module(module_name)
            transport = MagicMock()
            transport.aclose = AsyncMock(side_effect=RuntimeError("secondary close failure"))
            with patch.dict(os.environ, OS_ENV, clear=True), patch.object(module, "DefaultAsyncHttpxClient", return_value=transport), \
                 patch.object(module, "AsyncOpenAI", side_effect=ValueError("original SDK error")):
                with self.assertRaisesRegex(ValueError, "original SDK error"):
                    getattr(module, class_name)(model="test", api_key="test")
                await asyncio.sleep(0)
                transport.aclose.assert_awaited_once()

    async def test_chat_payload_uses_mock_transport_without_changing_request_body(self):
        for module_name, class_name in CLIENTS:
            module = importlib.import_module(module_name)
            captured = []
            async def respond(request):
                captured.append(json.loads(request.content))
                return httpx.Response(200, json={"id": "mock", "object": "chat.completion", "created": 0, "model": "test",
                    "choices": [{"index": 0, "message": {"role": "assistant", "content": "mock result"}, "finish_reason": "stop"}]})
            def make_transport(**options):
                return DefaultAsyncHttpxClient(**options, transport=httpx.MockTransport(respond))
            with self.subTest(client=class_name), patch.dict(os.environ, OS_ENV, clear=True), \
                 patch.object(module, "DefaultAsyncHttpxClient", side_effect=make_transport):
                client = getattr(module, class_name)(model="test", api_key="test", base_url="https://model.invalid/v1")
                try:
                    messages = [{"role": "user", "content": "sample"}]
                    self.assertEqual(await client.generate_text(messages, temperature=0.2, max_tokens=17), "mock result")
                    self.assertEqual(captured, [{"model": "test", "messages": messages, "temperature": 0.2, "max_tokens": 17}])
                finally:
                    await client.close()


class QualityEvaluationCleanupTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        from backend.DevelopRubrics import quality_job_runner
        cls.worker = quality_job_runner
        cls.evaluator = quality_job_runner.EVAL

    async def test_managed_pipeline_shares_one_client_and_closes_once(self):
        E = self.evaluator
        client = SimpleNamespace(close=AsyncMock())
        with patch.dict(os.environ, OS_ENV, clear=True), patch.object(E, "OpenAIClient", return_value=client) as factory:
            async with E.evaluation_pipeline({}) as pipeline:
                self.assertIs(pipeline._generator._client, client)
                self.assertIs(pipeline._evaluator._client, client)
                client.close.assert_not_awaited()
            factory.assert_called_once()
            client.close.assert_awaited_once()

    async def test_build_failure_closes_client_and_preserves_original_error(self):
        E = self.evaluator
        for step in ("_build_aggregator", "_build_filter", "AdaRubricPipeline"):
            original = ValueError("invalid evaluation construction")
            client = SimpleNamespace(close=AsyncMock(side_effect=RuntimeError("cleanup failure")))
            with self.subTest(step=step), patch.dict(os.environ, OS_ENV, clear=True), \
                 patch.object(E, "OpenAIClient", return_value=client), patch.object(E, step, side_effect=original):
                with self.assertRaises(ValueError) as caught:
                    async with E.evaluation_pipeline({}):
                        self.fail("invalid pipeline must not be entered")
                self.assertIs(caught.exception, original)
                client.close.assert_awaited_once()

    async def test_invalid_config_after_client_creation_still_closes(self):
        E = self.evaluator
        for config in ({"evaluation_max_concurrent": 0}, {"evaluation_runs": 0}):
            client = SimpleNamespace(close=AsyncMock())
            with self.subTest(config=config), patch.dict(os.environ, OS_ENV, clear=True), \
                 patch.object(E, "OpenAIClient", return_value=client), \
                 patch.object(E, "_load_rubric", return_value=object()), \
                 patch.object(E, "evaluate_run_incrementally", new_callable=AsyncMock) as model:
                with self.assertRaises(ValueError):
                    await E.evaluate_task(task=SimpleNamespace(task_id="A"), trajectories=[],
                        rubric_path=Path("not-read.json"), config=config,
                        evaluations_path=Path("not-written.jsonl"), existing_evaluations={})
                client.close.assert_awaited_once()
                model.assert_not_awaited()

    async def test_cli_multiple_runs_reuse_client_and_close_on_success(self):
        E = self.evaluator
        client = SimpleNamespace(close=AsyncMock())
        pipelines = []
        async def evaluate(**kwargs):
            client.close.assert_not_awaited()
            pipelines.append(kwargs["pipeline"])
            return SimpleNamespace(all_evaluations=[])
        with patch.dict(os.environ, OS_ENV, clear=True), patch.object(E, "OpenAIClient", return_value=client), \
             patch.object(E, "_load_rubric", return_value=object()), \
             patch.object(E, "evaluate_run_incrementally", side_effect=evaluate):
            result = await E.evaluate_task(task=SimpleNamespace(task_id="A"), trajectories=[],
                rubric_path=Path("not-read.json"), config={"evaluation_runs": 2},
                evaluations_path=Path("not-written.jsonl"), existing_evaluations={})
        self.assertEqual(len(result.results), 2)
        self.assertIs(pipelines[0], pipelines[1])
        client.close.assert_awaited_once()

    async def test_successful_cli_result_is_kept_when_close_raises(self):
        E = self.evaluator
        client = SimpleNamespace(close=AsyncMock(side_effect=OSError("cleanup failure")))
        evaluated = SimpleNamespace(all_evaluations=[])
        with patch.dict(os.environ, OS_ENV, clear=True), patch.object(E, "OpenAIClient", return_value=client), \
             patch.object(E, "_load_rubric", return_value=object()), \
             patch.object(E, "evaluate_run_incrementally", return_value=evaluated):
            result = await E.evaluate_task(task=SimpleNamespace(task_id="A"), trajectories=[],
                rubric_path=Path("not-read.json"), config={},
                evaluations_path=Path("not-written.jsonl"), existing_evaluations={})
        self.assertEqual(result.results, [evaluated])
        client.close.assert_awaited_once()

    async def test_cli_failure_and_cancellation_keep_original_error_when_close_fails(self):
        E = self.evaluator
        for original in (RuntimeError("model failure"), asyncio.CancelledError()):
            client = SimpleNamespace(close=AsyncMock(side_effect=RuntimeError("cleanup failure")))
            with self.subTest(error=type(original).__name__), patch.dict(os.environ, OS_ENV, clear=True), \
                 patch.object(E, "OpenAIClient", return_value=client), \
                 patch.object(E, "_load_rubric", return_value=object()), \
                 patch.object(E, "evaluate_run_incrementally", side_effect=original):
                with self.assertRaises(type(original)) as caught:
                    await E.evaluate_task(task=SimpleNamespace(task_id="A"), trajectories=[],
                        rubric_path=Path("not-read.json"), config={},
                        evaluations_path=Path("not-written.jsonl"), existing_evaluations={})
                self.assertIs(caught.exception, original)
                client.close.assert_awaited_once()

    async def test_concurrent_work_is_drained_before_close_on_failure_and_cancellation(self):
        E = self.evaluator
        for cancel in (False, True):
            active = set()
            events = []
            both_started = asyncio.Event()
            block = asyncio.Event()
            original = RuntimeError("one trajectory failed")

            async def evaluate(**kwargs):
                identity = kwargs["trajectory"].trajectory_id
                active.add(identity)
                if len(active) == 2:
                    both_started.set()
                try:
                    await both_started.wait()
                    if not cancel and identity == "first":
                        raise original
                    await block.wait()
                finally:
                    # Cleanup yields to prove the owner awaits worker termination.
                    await asyncio.sleep(0)
                    active.remove(identity)
                    events.append(identity)

            async def close():
                self.assertEqual(active, set())
                self.assertCountEqual(events, ["first", "second"])
                events.append("closed")

            client = SimpleNamespace(close=AsyncMock(side_effect=close))
            async def run():
                async with E.evaluation_pipeline({}) as pipeline:
                    return await E.evaluate_run_incrementally(
                        pipeline=pipeline, task=SimpleNamespace(task_id="A"), rubric=object(),
                        trajectories=[SimpleNamespace(trajectory_id=name) for name in ("first", "second")],
                        rubric_path=Path("not-read.json"), run_number=1, temperature=0,
                        eval_max_tokens=17, max_concurrent=2, evaluations_path=Path("not-written.jsonl"),
                        config={}, existing_evaluations={})

            with self.subTest(cancel=cancel), patch.dict(os.environ, OS_ENV, clear=True), \
                 patch.object(E, "OpenAIClient", return_value=client), \
                 patch.object(E, "evaluate_trajectory", side_effect=evaluate), \
                 patch.object(E, "append_evaluation_jsonl") as checkpoint:
                running = asyncio.create_task(run())
                await asyncio.wait_for(both_started.wait(), timeout=1)
                if cancel:
                    running.cancel()
                with self.assertRaises(asyncio.CancelledError if cancel else RuntimeError) as caught:
                    await running
                if not cancel:
                    self.assertIs(caught.exception, original)
                client.close.assert_awaited_once()
                checkpoint.assert_not_called()
                self.assertEqual(events[-1], "closed")

    async def _run_worker_with_client(self, *, failure=None, checkpoint_failure=None):
        W, E = self.worker, self.evaluator
        clients = []
        events = []
        identities = ["A", "B"] if failure is None and checkpoint_failure is None else ["A"]
        tasks = {identity: SimpleNamespace(task_id=identity) for identity in identities}
        trajectories = [SimpleNamespace(task_id=identity, trajectory_id="trajectory-" + identity,
            final_answer="done", steps=[SimpleNamespace(observation="recorded")]) for identity in identities]
        current = {
            "tasks": [{"task_id": identity, "trajectory_count": 1} for identity in identities],
            "trees": {identity: {"terminal_trajectories": ["trajectory-" + identity]} for identity in identities},
            "tree_hashes": {identity: "hash-" + identity for identity in identities},
        }
        rubric = SimpleNamespace(model_dump_json=lambda: '{"dimensions": []}')

        def create_client(**_):
            identity = identities[len(clients)]
            async def close():
                events.append("closed-" + identity)
                if failure is not None:
                    raise RuntimeError("cleanup failure")
            client = SimpleNamespace(close=AsyncMock(side_effect=close))
            clients.append(client)
            return client

        async def evaluate(**kwargs):
            identity = kwargs["task"].task_id
            clients[-1].close.assert_not_awaited()
            events.append("evaluated-" + identity)
            if failure is not None:
                raise failure
            data = {"trajectory_id": "trajectory-" + identity, "global_score": 4, "passed_threshold": True}
            evaluation = SimpleNamespace(**data, model_dump_json=lambda **_: json.dumps(data))
            kwargs["on_trajectory_complete"](evaluation)
            return SimpleNamespace(all_evaluations=[evaluation])

        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            root = Path(directory)
            for owner, name, value in (
                (W, "DATA_ROOT", root), (W, "CHECKPOINT_ROOT", root / "checkpoints"),
            ):
                stack.enter_context(patch.object(owner, name, value))
            stack.enter_context(patch.dict(os.environ, OS_ENV, clear=True))
            stack.enter_context(patch.object(W, "configure_model_environment"))
            stack.enter_context(patch.object(W, "progress"))
            records = stack.enter_context(patch.object(W, "RecordStore"))
            records.return_value.get.return_value = None
            stack.enter_context(patch.object(W, "load_quality_objects", return_value=(tasks, trajectories)))
            stack.enter_context(patch.object(W, "quality_task_fingerprints", return_value=current["tree_hashes"]))
            stack.enter_context(patch.object(W, "_matching_rubric", return_value=root / "rubric.json"))
            stack.enter_context(patch.object(W.GEN, "load_config", return_value={}))
            stack.enter_context(patch.object(E, "_load_rubric", return_value=rubric))
            stack.enter_context(patch.object(E, "OpenAIClient", side_effect=create_client))
            stack.enter_context(patch.object(E, "load_existing_evaluations_jsonl", return_value={}))
            stack.enter_context(patch.object(E, "initialize_evaluations_jsonl", side_effect=checkpoint_failure))
            model = stack.enter_context(patch.object(E, "evaluate_run_incrementally", side_effect=evaluate))
            merge = stack.enter_context(patch.object(W, "merge_quality_results", return_value={"saved": True}))
            original = failure if failure is not None else checkpoint_failure
            if original is not None:
                with self.assertRaises(type(original)) as caught:
                    await W._run_frozen("batch", identities, "job", current=current, work=root)
                self.assertIs(caught.exception, original)
                merge.assert_not_called()
                if checkpoint_failure is not None:
                    model.assert_not_awaited()
            else:
                result = await W._run_frozen("batch", identities, "job", current=current, work=root)
                self.assertEqual(result, {"saved": True})
                self.assertEqual(events, ["evaluated-A", "closed-A", "evaluated-B", "closed-B"])
            for client in clients:
                client.close.assert_awaited_once()

    async def test_worker_closes_each_task_before_starting_next(self):
        await self._run_worker_with_client()

    async def test_worker_model_failure_and_cancellation_close_once(self):
        for original in (RuntimeError("model failure"), asyncio.CancelledError()):
            with self.subTest(error=type(original).__name__):
                await self._run_worker_with_client(failure=original)

    async def test_worker_checkpoint_failure_also_closes_client(self):
        await self._run_worker_with_client(checkpoint_failure=OSError("checkpoint failure"))


if __name__ == "__main__":
    unittest.main()
