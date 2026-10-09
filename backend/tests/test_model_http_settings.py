"""Model calls must not accidentally inherit a Windows or shell proxy."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import httpx
import openai

from backend.model_http import model_http_client, model_http_options
from backend.DevelopRubrics.trajectory_tools.gui_trajectory_excel import QwenSummarizer
from backend.quality_input_builder import build_quality_workbook
from backend.trajectory_correction.cot_generator import QwenCotGenerator

WINDOWS_ENV = {key: os.environ[key] for key in ("SYSTEMROOT", "WINDIR") if key in os.environ}


class ModelHttpSettingsTests(unittest.TestCase):
    def test_only_explicit_project_proxy_settings_are_read(self):
        with patch.dict(os.environ, WINDOWS_ENV | {"HTTP_PROXY": "http://127.0.0.1:9",
                                     "HTTPS_PROXY": "http://127.0.0.1:9"}, clear=True):
            self.assertEqual(model_http_options(), {"proxy": None, "trust_env": False})
            self.assertEqual(model_http_options({"HTTP_PROXY_URL": "http://proxy.invalid:3128"}),
                             {"proxy": "http://proxy.invalid:3128", "trust_env": False})

    def test_file_settings_and_module_overrides(self):
        values = {"HTTP_PROXY_URL": "http://shared.invalid:80", "HTTP_TRUST_ENV": "true",
                  "COT_HTTP_TRUST_ENV": "false", "COT_HTTP_PROXY_URL": ""}
        with patch.dict(os.environ, WINDOWS_ENV, clear=True):
            self.assertEqual(model_http_options(values, prefix="COT"), {"proxy": None, "trust_env": False})
        with patch.dict(os.environ, WINDOWS_ENV | {"HTTP_TRUST_ENV": "true", "COT_HTTP_PROXY_URL": "http://module.invalid:81"}, clear=True):
            # Module file configuration takes priority over shared environment.
            self.assertEqual(model_http_options(values, prefix="COT"),
                             {"proxy": "http://module.invalid:81", "trust_env": False})
        with patch.dict(os.environ, WINDOWS_ENV | {"COT_HTTP_TRUST_ENV": "true"}, clear=True):
            self.assertTrue(model_http_options(values, prefix="COT")["trust_env"])

    def test_boolean_configuration_is_explicit_and_invalid_values_are_redacted(self):
        with patch.dict(os.environ, WINDOWS_ENV, clear=True):
            for value in ("true", "1", "yes", "ON"):
                self.assertTrue(model_http_options({"HTTP_TRUST_ENV": value})["trust_env"])
            for value in ("false", "0", "no", "OFF", ""):
                self.assertFalse(model_http_options({"HTTP_TRUST_ENV": value})["trust_env"])
            with self.assertRaises(ValueError) as error:
                model_http_options({"HTTP_TRUST_ENV": "private-config-value"})
            self.assertIn("HTTP_TRUST_ENV", str(error.exception))
            self.assertNotIn("private-config-value", str(error.exception))

    def test_default_client_keeps_sdk_defaults_and_never_queries_system_proxy(self):
        with patch.dict(os.environ, WINDOWS_ENV | {"HTTPS_PROXY": "http://127.0.0.1:9",
                                     "SSL_CERT_FILE": "does-not-exist.pem"}, clear=True), \
             patch("httpx._utils.getproxies", side_effect=AssertionError("system proxy must not be read")):
            client = model_http_client()
            try:
                self.assertEqual(client.timeout, openai.DEFAULT_TIMEOUT)
                self.assertTrue(client.follow_redirects)
                self.assertIs(client._transport_for_url(httpx.URL("https://model.invalid")), client._transport)
            finally:
                client.close()

    def test_explicit_proxy_and_system_proxy_opt_in_still_route_requests(self):
        with patch.dict(os.environ, WINDOWS_ENV, clear=True):
            client = model_http_client(model_http_options({"HTTP_PROXY_URL": "http://proxy.invalid:3128"}))
            try:
                self.assertIsNot(client._transport_for_url(httpx.URL("https://model.invalid")), client._transport)
            finally:
                client.close()
        with patch.dict(os.environ, WINDOWS_ENV, clear=True), \
             patch("httpx._utils.getproxies", return_value={"https": "http://proxy.invalid:3128"}) as proxies:
            client = model_http_client(model_http_options({"HTTP_TRUST_ENV": "true"}))
            try:
                proxies.assert_called_once()
                self.assertIsNot(client._transport_for_url(httpx.URL("https://model.invalid")), client._transport)
            finally:
                client.close()

    def test_empty_or_partial_client_options_still_default_to_direct(self):
        with patch.dict(os.environ, WINDOWS_ENV, clear=True), \
             patch("httpx._utils.getproxies", side_effect=AssertionError("system proxy must not be read")):
            for options in ({}, {"proxy": None}):
                client = model_http_client(options)
                try:
                    self.assertFalse(client.trust_env)
                finally:
                    client.close()

    def test_network_settings_do_not_invalidate_tree_or_quality_content(self):
        from backend.batch_results import quality_task_fingerprints
        from backend.tree_build_service import tree_build_config

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env = root / ".env"
            base = "YUNAI_API_KEY=test-key\nMODEL_URL=https://model.invalid/v1\nMODEL_NAME=mock\n"
            env.write_text(base, encoding="utf-8")
            trees = {"tree_hashes": {"task": "content-digest"}, "quality_input": {}}
            before_tree = tree_build_config(env)
            before_quality = quality_task_fingerprints(trees, config_path=root / "config.json", env_path=env)
            env.write_text(base + "HTTP_TRUST_ENV=false\nHTTP_PROXY_URL=http://proxy.invalid:3128\n", encoding="utf-8")
            self.assertEqual(tree_build_config(env), before_tree)
            self.assertEqual(quality_task_fingerprints(trees, config_path=root / "config.json", env_path=env), before_quality)


class ModelClientRoutingTests(unittest.TestCase):
    def test_summary_and_cot_succeed_with_unusable_system_proxy_and_preserve_cache(self):
        requests = []
        transports = []
        default_client = openai.DefaultHttpxClient

        def respond(request):
            requests.append(request)
            text = "可见页面摘要" if len(requests) == 1 else "<thought>页面保持稳定。</thought><summary>等待</summary>"
            return httpx.Response(200, json={"id": "mock", "object": "chat.completion", "created": 0,
                "model": "mock", "choices": [{"index": 0, "finish_reason": "stop",
                    "message": {"role": "assistant", "content": text}}]})

        def http_client(**options):
            self.assertEqual(options, {"proxy": None, "trust_env": False})
            client = default_client(transport=httpx.MockTransport(respond), **options)
            transports.append(client)
            return client

        with tempfile.TemporaryDirectory() as directory, \
             patch.dict(os.environ, WINDOWS_ENV | {"HTTP_PROXY": "http://127.0.0.1:9", "HTTPS_PROXY": "http://127.0.0.1:9"}, clear=True), \
             patch("openai.DefaultHttpxClient", side_effect=http_client):
            root = Path(directory)
            summary = QwenSummarizer("mock", "https://model.invalid/v1", "test-key", root / "summary.json")
            try:
                self.assertEqual(summary._complete("test", [], 10), "可见页面摘要")
                self.assertEqual(summary._complete("test", [], 10), "可见页面摘要")
                self.assertEqual(summary.client.max_retries, 2)
                self.assertEqual(summary.client.timeout, openai.DEFAULT_TIMEOUT)
            finally:
                summary.close()
            env = root / ".env"
            env.write_text("YUNAI_API_KEY=test-key\nMODEL_URL=https://model.invalid/v1\nMODEL_NAME=mock\n", encoding="utf-8")
            image = root / "step.jpg"
            image.write_bytes(b"mock-image")
            cot = QwenCotGenerator(env_file=env, cache_dir=root / "cot")
            try:
                arguments = dict(task="等待", trajectory_id="tr-test", step=1, history="",
                                 action={"action": "wait"}, image=image)
                self.assertEqual(cot.generate(**arguments)["thought"], "页面保持稳定。")
                self.assertTrue(cot.generate(**arguments)["cached"])
                self.assertEqual(cot.client.max_retries, 2)
            finally:
                cot.close()
        self.assertEqual(len(requests), 2)
        self.assertTrue(all(client.is_closed for client in transports))

    def test_sdk_constructor_failure_closes_summary_transport(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, WINDOWS_ENV, clear=True):
            transport = model_http_client()
            with patch("backend.DevelopRubrics.trajectory_tools.gui_trajectory_excel.model_http_client", return_value=transport), \
                 patch("openai.OpenAI", side_effect=RuntimeError("mock constructor failure")):
                with self.assertRaisesRegex(RuntimeError, "mock constructor failure"):
                    QwenSummarizer("mock", "https://model.invalid/v1", "test-key", Path(directory) / "cache.json")
            self.assertTrue(transport.is_closed)

    def test_workbook_builder_passes_file_network_config_and_closes_owned_summary(self):
        for fail, close_fail in ((False, False), (True, False), (False, True), (True, True)):
            with self.subTest(fail=fail, close_fail=close_fail), tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, WINDOWS_ENV, clear=True):
                root = Path(directory)
                env = root / ".env"
                env.write_text("YUNAI_API_KEY=test-key\nMODEL_URL=https://model.invalid/v1\nMODEL_NAME=mock\n"
                               "HTTP_PROXY_URL=http://proxy.invalid:3128\nHTTP_TRUST_ENV=false\n", encoding="utf-8")
                summary = Mock(model="mock")
                summary.summarize_trajectory.return_value = "页面摘要"
                if close_fail:
                    summary.close.side_effect = RuntimeError("mock close failure")
                if fail:
                    summary.summarize_trajectory.side_effect = RuntimeError("mock summary failure")
                step = SimpleNamespace(counted_in_tree=True, observation="页面", action={"action": "wait"},
                                       image=str(root / "step.jpg"), summary="等待", step_index=1)
                with patch("backend.quality_input_builder.QwenSummarizer", return_value=summary) as factory:
                    arguments = dict(grouped={"task": [("trajectory", [step])]}, task_goals={"task": "等待"},
                                     trajectory_root=root, output=root / "result.xlsx", env_path=env)
                    if fail:
                        with self.assertRaisesRegex(RuntimeError, "mock summary failure"):
                            build_quality_workbook(**arguments)
                    else:
                        self.assertEqual(build_quality_workbook(**arguments), (1, 1, 1))
                    self.assertEqual(factory.call_args.kwargs["http_options"],
                                     {"proxy": "http://proxy.invalid:3128", "trust_env": False})
                    summary.close.assert_called_once()
                if not fail:
                    summary.reset_mock()
                    build_quality_workbook(**arguments, summarizer=summary)
                    summary.close.assert_not_called()
