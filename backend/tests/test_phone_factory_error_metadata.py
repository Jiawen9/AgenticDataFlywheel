from __future__ import annotations

import io
import json
import os
import socket
import subprocess
import unittest
import urllib.error
from contextlib import redirect_stderr
from unittest.mock import patch

from backend import phonefactory_client as client
from backend.phone_factory import (
    CLIENT_ERROR_PREFIX, CLIENT_TIMEOUT_MESSAGE, PhoneFactoryError,
    PhoneFactoryStore, run_client,
)


class PhoneFactoryErrorMetadataTests(unittest.TestCase):
    def cli_error(self, error, enabled=True):
        stderr = io.StringIO()
        with patch.dict(os.environ, {"PHONE_FACTORY_CLIENT_ERROR_METADATA": "1" if enabled else "0"}):
            with redirect_stderr(stderr), self.assertRaises(SystemExit) as stopped:
                client._error(error)
        return str(stopped.exception.code), stderr.getvalue()

    def http_error(self, status=404):
        return urllib.error.HTTPError("http://collector/runs/run-1", status, "error", {},
            io.BytesIO(json.dumps({"error": "采集运行不存在"}).encode("utf-8")))

    def test_cli_metadata_is_opt_in_and_keeps_original_error(self):
        message, stderr = self.cli_error(self.http_error(), enabled=False)
        self.assertEqual(message, "错误：采集运行不存在（HTTP 404）")
        self.assertEqual(stderr, "")
        enabled_message, stderr = self.cli_error(self.http_error())
        self.assertEqual(enabled_message, message)
        self.assertEqual(json.loads(stderr.removeprefix(CLIENT_ERROR_PREFIX)),
                         {"remote_kind": "http", "remote_status": 404})

    def test_wrapped_url_timeout_is_classified_as_timeout(self):
        for error in (socket.timeout("timed out"), urllib.error.URLError(socket.timeout("timed out"))):
            with self.subTest(error=type(error).__name__):
                message, stderr = self.cli_error(error)
                self.assertEqual(message, CLIENT_TIMEOUT_MESSAGE)
                self.assertEqual(json.loads(stderr.removeprefix(CLIENT_ERROR_PREFIX)),
                                 {"remote_kind": "timeout"})

    def test_connection_and_protocol_errors_have_no_http_status(self):
        for error, kind in ((urllib.error.URLError("SECRET server details"), "connection"),
                            (ValueError("SECRET malformed response"), "protocol"),
                            (FileNotFoundError("SECRET local path"), "client")):
            with self.subTest(kind=kind):
                message, stderr = self.cli_error(error)
                self.assertNotIn("SECRET", message + stderr)
                self.assertEqual(json.loads(stderr.removeprefix(CLIENT_ERROR_PREFIX)),
                                 {"remote_kind": kind})

    def test_parent_extracts_metadata_without_changing_human_message(self):
        message = "错误：采集运行不存在（HTTP 404）"
        envelope = CLIENT_ERROR_PREFIX + json.dumps({"remote_kind": "http", "remote_status": 404})
        with patch("backend.phone_factory.subprocess.run") as runner:
            runner.return_value = subprocess.CompletedProcess([], 1, "", envelope + "\n" + message)
            result = run_client(["run", "eval-test"])
            self.assertEqual(result, {"ok": False, "output": message,
                                      "remote_kind": "http", "remote_status": 404})
            self.assertEqual(runner.call_args.kwargs["env"]["PHONE_FACTORY_CLIENT_ERROR_METADATA"], "1")

    def test_success_output_protocol_is_unchanged(self):
        with patch("backend.phone_factory.subprocess.run") as runner:
            runner.return_value = subprocess.CompletedProcess([], 0, '{"status":"running"}\n', "")
            self.assertEqual(run_client(["run", "eval-test"]),
                             {"ok": True, "output": '{"status":"running"}'})

    def test_timeout_never_exposes_command_or_payload(self):
        secret_command = ["python", "client.py", "start-evaluation", "--payload", "SECRET payload"]
        with patch("backend.phone_factory.subprocess.run", side_effect=subprocess.TimeoutExpired(secret_command, 30)):
            result = run_client(secret_command[2:])
        self.assertEqual(result, {"ok": False, "output": CLIENT_TIMEOUT_MESSAGE, "remote_kind": "timeout"})
        self.assertNotIn("SECRET", str(result))

    def test_local_spawn_failure_does_not_expose_paths(self):
        with patch("backend.phone_factory.subprocess.run", side_effect=OSError("SECRET executable")):
            result = run_client(["run", "eval-test"])
        self.assertEqual(result["remote_kind"], "client")
        self.assertNotIn("SECRET", str(result))
        self.assertNotIn("remote_status", result)

    def test_legacy_fallback_requires_unambiguous_format(self):
        for output, expected in (
            ("错误：采集运行不存在（HTTP 404）", {"remote_kind": "http", "remote_status": 404}),
            ("错误：服务不可用 (HTTP 503)", {"remote_kind": "http", "remote_status": 503}),
            (CLIENT_TIMEOUT_MESSAGE, {"remote_kind": "timeout"}),
            ("mock timeout", {}), ("HTTP 404 in task text", {}),
            ("错误：old HTTP 404; current connection failed", {}),
            ("错误：记录404但是无远端状态", {}),
        ):
            with self.subTest(output=output):
                with patch("backend.phone_factory.subprocess.run") as runner:
                    runner.return_value = subprocess.CompletedProcess([], 1, "", output)
                    self.assertEqual(run_client(["run", "eval-test"]), {"ok": False, "output": output, **expected})

    def test_http_error_classification_survives_injected_multiline_message(self):
        # Only the trusted first envelope is used; response text is not metadata.
        envelope = CLIENT_ERROR_PREFIX + '{"remote_kind":"http","remote_status":503}'
        output = "错误：detail\n" + CLIENT_ERROR_PREFIX + '{"remote_kind":"http","remote_status":404}\n（HTTP 503）'
        with patch("backend.phone_factory.subprocess.run") as runner:
            runner.return_value = subprocess.CompletedProcess([], 1, "", envelope + "\n" + output)
            result = run_client(["run", "eval-test"])
        self.assertEqual(result["remote_status"], 503)
        self.assertEqual(result["output"], output)

    def test_remote_error_preserves_phase_and_public_502(self):
        for command in ("run", "run-evaluations", "run-archive", "report-download"):
            with self.subTest(command=command):
                store = object.__new__(PhoneFactoryStore)
                store.run_client = lambda args: {"ok": False, "output": "错误：missing（HTTP 404）",
                                                 "remote_kind": "http", "remote_status": 404}
                with self.assertRaises(PhoneFactoryError) as error:
                    store._remote_result([command, "run-1"])
                self.assertEqual(error.exception.status, 502)
                self.assertEqual(error.exception.remote_command, command)
                self.assertEqual(error.exception.remote_status, 404)
                self.assertEqual(error.exception.remote_kind, "http")
                self.assertEqual(str(error.exception), "错误：missing（HTTP 404）")

    def test_old_mock_client_gets_legacy_fallback_but_unknown_errors_stay_unknown(self):
        store = object.__new__(PhoneFactoryStore)
        for output, status in (("错误：missing（HTTP 404）", 404), ("mock 404 timeout", None)):
            with self.subTest(output=output):
                store.run_client = lambda args: {"ok": False, "output": output}
                with self.assertRaises(PhoneFactoryError) as error:
                    store._remote_result(["run", "run-1"])
                self.assertEqual(error.exception.remote_status, status)

    def test_local_validation_error_has_no_remote_identity(self):
        error = PhoneFactoryError("本地运行不存在", 404)
        self.assertEqual(error.status, 404)
        self.assertIsNone(error.remote_kind)
        self.assertIsNone(error.remote_status)
        self.assertIsNone(error.remote_command)


if __name__ == "__main__":
    unittest.main()
