"""Exercise the actual CLI multipart construction without a network service."""
import io
import json
import tempfile
import unittest
from email.parser import BytesParser
from email.policy import default
from pathlib import Path
from unittest.mock import patch

from backend import phonefactory_client as client


class ChecklistCliTests(unittest.TestCase):
    def test_parser_and_actual_multipart_preserve_manifest_bytes(self):
        requests = []
        def respond(request, **kwargs):
            requests.append(request)
            body = {"quality_checklist_jobs": {"version": 1}} if request.full_url.endswith("/capabilities") else {"job_id": "qc_1", "status": "preparing"}
            return io.BytesIO(json.dumps(body).encode())
        with tempfile.TemporaryDirectory() as temporary:
            manifest = Path(temporary) / "中文清单.json"
            raw = '{"schema_version":1,"tasks":[],"note":"中文\\n路径"}\r\n'.encode("utf-8")
            manifest.write_bytes(raw)
            args = client.build_parser().parse_args(["quality-checklist-start", "--payload", '{"request_id":"same-id"}', "--manifest", str(manifest)])
            with patch.object(client.urllib.request, "urlopen", side_effect=respond):
                result = client.cmd_quality_checklist(args)
        self.assertEqual(result["job_id"], "qc_1")
        self.assertEqual(len(requests), 2)
        request = requests[-1]
        message = BytesParser(policy=default).parsebytes(("Content-Type: " + request.get_header("Content-type") + "\r\n\r\n").encode() + request.data)
        parts = {part.get_param("name", header="content-disposition"): part for part in message.iter_parts()}
        self.assertEqual(set(parts), {"request", "manifest_file"})
        self.assertEqual(parts["manifest_file"].get_payload(decode=True), raw)
        self.assertEqual(json.loads(parts["request"].get_payload(decode=True))["request_id"], "same-id")

    def test_old_collector_is_rejected_without_submitting(self):
        with tempfile.TemporaryDirectory() as temporary:
            manifest = Path(temporary) / "manifest.json"
            manifest.write_text('{"tasks":[]}', encoding="utf-8")
            args = client.build_parser().parse_args(["quality-checklist-start", "--payload", '{}', "--manifest", str(manifest)])
            with patch.object(client, "get_json", return_value={}), patch.object(client, "post_multipart") as post:
                with self.assertRaisesRegex(ValueError, "升级"):
                    client.cmd_quality_checklist(args)
            post.assert_not_called()

    def test_control_and_page_endpoints_do_not_touch_phone_runs(self):
        for argv, expected in [
            (["quality-checklist-results", "a/b", "--offset", "20", "--limit", "10"], "/quality-checklist-jobs/a%2Fb/results?offset=20&limit=10"),
            (["quality-checklist-status", "qc_1"], "/quality-checklist-jobs/qc_1"),
        ]:
            with patch.object(client, "get_json", return_value={}) as get:
                client.cmd_quality_checklist(client.build_parser().parse_args(argv))
            get.assert_called_once_with(expected)
        with patch.object(client, "post_json", return_value={}) as post:
            client.cmd_quality_checklist(client.build_parser().parse_args(["quality-checklist-cancel", "qc_1", "--payload", '{"request_id":"stop1"}']))
        post.assert_called_once_with("/quality-checklist-jobs/qc_1/cancel", {"request_id": "stop1"})


if __name__ == "__main__":
    unittest.main()
