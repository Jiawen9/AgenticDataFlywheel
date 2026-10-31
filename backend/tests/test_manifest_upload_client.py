from __future__ import annotations

import contextlib
import io
import json
import os
import socket
import tempfile
import unittest
import urllib.error
from email import policy
from email.parser import BytesParser
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from backend import phonefactory_client as client


def capability(**limits):
    return {"task_manifest_file_version": 1, "start_run_limits": {
        "task_manifest_bytes": 32 * 1024**2, "request_bytes": 128 * 1024**2, **limits}}


class ManifestUploadClientTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.task, self.apps, self.manifest = root / "tasks.xlsx", root / "apps.json", root / "manifest.json"
        self.task.write_bytes(b"workbook")
        self.apps.write_text('[{"phone_id":"phone-a","app":"App"}]', encoding="utf-8")
        self.manifest.write_text('{"tasks":[]}', encoding="utf-8")
        self.requests = []
        self.response = capability()
        self.cap_raw = None
        self.cap_error = None
        self.post_error = None
        self.addCleanup(patch.stopall)
        patch.object(client, "SERVER", "http://collector.invalid:9011").start()
        patch.object(client.uuid, "uuid4", return_value=SimpleNamespace(hex="a" * 32)).start()
        patch.object(client.urllib.request, "urlopen", side_effect=self.urlopen).start()

    def urlopen(self, request, **kwargs):
        self.requests.append(request)
        self.assertEqual(kwargs["timeout"], client.TIMEOUT)
        if request.full_url == client.SERVER + "/capabilities":
            self.assertEqual(request.get_method(), "GET")
            if self.cap_error is not None:
                raise self.cap_error
            if self.cap_raw is not None:
                return io.BytesIO(self.cap_raw)
            return io.BytesIO(json.dumps(self.response).encode())
        self.assertEqual(request.full_url, client.SERVER + "/start_run")
        self.assertEqual(request.get_method(), "POST")
        if self.post_error is not None:
            raise self.post_error
        return io.BytesIO(b'{"ok":true}')

    def args(self, *, manifest=True, extra=()):
        command = ["start-run", str(self.task), str(self.apps)]
        if manifest:
            command += ["--task-manifest", str(self.manifest)]
        return client.build_parser().parse_args(command + list(extra))

    @staticmethod
    def http_error(status, body=b"<html>error</html>"):
        return urllib.error.HTTPError(client.SERVER + "/test", status, "error", {}, io.BytesIO(body))

    def posts(self):
        return [request for request in self.requests if request.get_method() == "POST"]

    @staticmethod
    def parts(request):
        raw = ("MIME-Version: 1.0\r\nContent-Type: " + request.get_header("Content-type") +
               "\r\n\r\n").encode() + request.data
        return {part.get_param("name", header="content-disposition"): part
                for part in BytesParser(policy=policy.default).parsebytes(raw).iter_parts()}

    def failure(self, args=None):
        stderr = io.StringIO()
        with patch.dict(os.environ, {"PHONE_FACTORY_CLIENT_ERROR_METADATA": "1"}), contextlib.redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as caught:
                client.cmd_start_run(args or self.args())
        lines = [line for line in stderr.getvalue().splitlines() if line.startswith(client.CLIENT_ERROR_PREFIX)]
        self.assertEqual(len(lines), 1, stderr.getvalue())
        return str(caught.exception), json.loads(lines[0][len(client.CLIENT_ERROR_PREFIX):])

    def test_large_unicode_manifest_uses_only_json_file_for_both_engines_and_modes(self):
        logical = json.dumps({"tasks": [{"task": "中文任务" * 60000}]}, ensure_ascii=False)
        self.assertGreater(len(logical.encode()), 500000)
        self.manifest.write_text(logical, encoding="utf-8")
        for engine in ("newagent", "pipelineagent"):
            for mode in ("generate", "modeliter"):
                with self.subTest(engine=engine, mode=mode):
                    self.requests.clear()
                    self.assertEqual(client.cmd_start_run(self.args(extra=("--agent-type", engine, "--runmode", mode))), {"ok": True})
                    self.assertEqual(len(self.requests), 2)
                    parts = self.parts(self.posts()[0])
                    self.assertNotIn("task_manifest", parts)
                    self.assertEqual(parts["task_manifest_file"].get_filename(), "tasks.json")
                    self.assertEqual(parts["task_manifest_file"].get_content_type(), "application/json")
                    self.assertEqual(parts["task_manifest_file"].get_payload(decode=True), logical.encode())
                    self.assertEqual(parts["task_file"].get_payload(decode=True), b"workbook")
                    self.assertEqual(parts["task_file"].get_content_type(), "application/octet-stream")

    def test_no_manifest_preserves_old_request_without_capability_probe(self):
        self.assertEqual(client.cmd_start_run(self.args(manifest=False)), {"ok": True})
        self.assertEqual(len(self.requests), 1)
        self.assertNotIn("task_manifest_file", self.parts(self.posts()[0]))

    def test_newline_handling_is_unchanged_and_repeats_do_not_expand_manifest(self):
        original = b'{\r\n  "tasks": [{"task": "one"}]\r\n}\r\n'
        self.manifest.write_bytes(original)
        for repeats in (1, 100):
            client.cmd_start_run(self.args(extra=("--repeat-times", str(repeats))))
            parts = self.parts(self.posts()[-1])
            self.assertEqual(parts["task_manifest_file"].get_payload(decode=True), original.replace(b"\r\n", b"\n"))
            self.assertEqual(parts["repeat_times"].get_payload(decode=True), str(repeats).encode())

    def test_missing_file_flag_and_only_404_or_405_allow_small_text_fallback(self):
        for status in (None, 404, 405):
            with self.subTest(status=status):
                self.response = {"protocol_version": 1}
                self.cap_error = self.http_error(status) if status else None
                self.requests.clear()
                client.cmd_start_run(self.args())
                parts = self.parts(self.posts()[0])
                self.assertIn("task_manifest", parts)
                self.assertIsNone(parts["task_manifest"].get_filename())
                self.assertNotIn("task_manifest_file", parts)

    def test_old_collector_large_manifest_is_not_submitted_and_reports_upgrade(self):
        self.response = {}
        self.manifest.write_text("x" * (client.LEGACY_MANIFEST_MAX_BYTES + 1), encoding="utf-8")
        message, metadata = self.failure()
        self.assertIn("升级采集端", message)
        self.assertEqual(self.posts(), [])
        self.assertIs(metadata["dispatch_not_sent"], True)
        self.assertNotIn("remote_status", metadata)

    def test_legacy_limit_is_bytes_and_exact_boundary_is_accepted(self):
        self.response = {}
        boundary = "中" * (client.LEGACY_MANIFEST_MAX_BYTES // 3) + "x" * (client.LEGACY_MANIFEST_MAX_BYTES % 3)
        self.manifest.write_text(boundary, encoding="utf-8")
        client.cmd_start_run(self.args())
        self.assertEqual(len(self.posts()), 1)
        self.requests.clear()
        self.manifest.write_text(boundary + "中", encoding="utf-8")
        self.failure()
        self.assertEqual(self.posts(), [])

    def test_capability_uncertainty_or_invalid_shape_never_falls_back(self):
        for error in (socket.timeout(), urllib.error.URLError("offline"), self.http_error(500)):
            with self.subTest(error=type(error).__name__):
                self.cap_error = error
                self.requests.clear()
                _, metadata = self.failure()
                self.assertEqual(self.posts(), [])
                self.assertIs(metadata["dispatch_not_sent"], True)
        self.cap_error = None
        for response in (None, [], {"task_manifest_file_version": True},
                         {"task_manifest_file_version": 2}, capability(task_manifest_bytes=0),
                         capability(request_bytes=True), capability(request_bytes="128")):
            with self.subTest(response=response):
                self.response = response
                self.requests.clear()
                _, metadata = self.failure()
                self.assertEqual(self.posts(), [])
                self.assertEqual(metadata["remote_kind"], "protocol")

    def test_manifest_limit_checks_utf8_bytes_and_allows_exact_boundary(self):
        logical = '{"tasks":["中文"]}'
        size = len(logical.encode())
        self.manifest.write_text(logical, encoding="utf-8")
        self.response = capability(task_manifest_bytes=size)
        client.cmd_start_run(self.args())
        self.requests.clear()
        self.response = capability(task_manifest_bytes=size - 1)
        message, metadata = self.failure()
        self.assertIn(str(size), message)
        self.assertEqual(self.posts(), [])
        self.assertIs(metadata["dispatch_not_sent"], True)

    def test_empty_or_non_json_capability_response_does_not_downgrade(self):
        for raw in (b"", b"<html>gateway page</html>"):
            with self.subTest(raw=raw):
                self.cap_raw = raw
                self.requests.clear()
                _, metadata = self.failure()
                self.assertEqual(self.posts(), [])
                self.assertEqual(metadata["remote_kind"], "protocol")
                self.assertIs(metadata["dispatch_not_sent"], True)

    def test_complete_multipart_body_is_checked_before_post(self):
        client.cmd_start_run(self.args())
        body_size = len(self.posts()[0].data)
        self.requests.clear()
        self.response = capability(request_bytes=body_size)
        client.cmd_start_run(self.args())
        self.assertEqual(len(self.posts()[0].data), body_size)
        self.requests.clear()
        self.response = capability(request_bytes=body_size - 1)
        message, metadata = self.failure()
        self.assertIn("启动请求", message)
        self.assertIn(str(body_size), message)
        self.assertEqual(self.posts(), [])
        self.assertIs(metadata["dispatch_not_sent"], True)

    def test_empty_manifest_keeps_content_and_invalid_utf8_is_not_submitted(self):
        self.manifest.write_bytes(b"")
        client.cmd_start_run(self.args())
        self.assertEqual(self.parts(self.posts()[0])["task_manifest_file"].get_payload(decode=True), b"")
        self.requests.clear()
        self.manifest.write_bytes(b"\xff")
        message, metadata = self.failure()
        self.assertIn("UTF-8", message)
        self.assertEqual(self.requests, [])
        self.assertIs(metadata["dispatch_not_sent"], True)

    def test_html_and_json_413_retain_remote_status_without_text_retry(self):
        for body, expected in ((b"<html>too large</html>", "请求超过采集服务接收限制"),
                               (json.dumps({"error": "任务清单超过上限"}, ensure_ascii=False).encode(), "任务清单超过上限")):
            with self.subTest(body=body):
                self.post_error = self.http_error(413, body)
                self.requests.clear()
                message, metadata = self.failure()
                self.assertIn(expected, message)
                self.assertIn("HTTP 413", message)
                self.assertNotIn(client.UNREACHABLE_MSG, message)
                self.assertEqual(metadata, {"remote_kind": "http", "remote_status": 413})
                self.assertEqual(len(self.requests), 2)
                self.assertEqual(len(self.posts()), 1)

    def test_capability_413_is_explicitly_not_a_submitted_start_request(self):
        self.cap_error = self.http_error(413)
        _, metadata = self.failure()
        self.assertEqual(metadata, {"remote_kind": "http", "remote_status": 413, "dispatch_not_sent": True})
        self.assertEqual(self.posts(), [])


if __name__ == "__main__":
    unittest.main()
