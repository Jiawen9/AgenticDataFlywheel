from __future__ import annotations

import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from contextlib import closing, redirect_stdout
from unittest import mock
import zipfile

from backend.file_io import io_path

SPEC = importlib.util.spec_from_file_location("diagnose_pipeline_cli", Path(__file__).resolve().parents[2] / "scripts" / "diagnose_pipeline.py")
CLI = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CLI)


class PipelineDiagnosticCliTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.pipeline = {"pipeline_id": "pl_one", "batch_id": "batch_one", "status": "running", "current_step": "quality",
                         "collection_run_ids": ["cr_one"], "session_id": "sess_one", "release_id": "release_one",
                         "steps": [{"id": "quality", "status": "running", "job_ids": ["job_now"], "previous_job_ids": ["job_old"],
                                    "retry_info": {"attempts": 2, "max_attempts": 3}}]}

    def write(self, path, value):
        destination = self.root / "logs" / "pipelines" / "pl_one" / path
        io_path(destination.parent).mkdir(parents=True, exist_ok=True)
        raw = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False).encode()
        io_path(destination).write_bytes(raw)
        return destination

    def database(self):
        directory = self.root / "system"
        directory.mkdir(exist_ok=True)
        db = directory / "app.sqlite"
        rows = [("pipelines", "pl_one", self.pipeline),
                ("quality_jobs", "job_old", {"job_id": "job_old", "status": "failed", "failure": {"category": "timeout"}}),
                ("quality_jobs", "job_now", {"job_id": "job_now", "status": "running"}),
                ("quality_jobs", "job_other_latest", {"job_id": "job_other_latest", "batch_id": "batch_one", "status": "succeeded"}),
                ("quality_executions", "job_now", {"token": "secret-execution", "state": "active"}),
                ("collection_runs", "cr_one", {"collection_run_id": "cr_one"}),
                ("collection_transfers", "cr_one", {"status": "verified"}),
                ("correction_sessions", "sess_one", {"session_id": "sess_one"}),
                ("dataset_releases", "release_one", {"release_id": "release_one", "path": "releases/real.xlsx"}),
                ("training_overview_conversions", "release_one", {"status": "running"}),
                ("release_intents", "pipeline:pl_one", {"pipeline_id": "pl_one", "release_id": "release_one", "status": "committed"})]
        with closing(sqlite3.connect(db)) as connection:
            connection.execute("CREATE TABLE records(namespace TEXT, record_key TEXT, payload TEXT)")
            connection.executemany("INSERT INTO records VALUES(?,?,?)", [(ns, key, json.dumps(value)) for ns, key, value in rows])
            connection.commit()
        return db

    def logs(self):
        self.write("index.json", {"schema_version": 1, "pipeline": self.pipeline, "runtime": {"git_revision": "test"}, "coverage": "recorded_from_logging_deployment"})
        events = [{"event": "model_request_started", "at": "2026-10-14T01:00:00Z", "writer_id": "a", "sequence": 1,
                   "pipeline_id": "pl_one", "step_id": "quality", "job_id": "job_now", "request_id": "req_one"},
                  {"event": "model_request_failed", "at": "2026-10-14T00:00:00Z", "writer_id": "b", "sequence": 1,
                   "pipeline_id": "pl_one", "step_id": "quality", "job_id": "job_old", "request_id": "req_old", "error": "timeout"}]
        self.write("events/a.jsonl", b"\n".join(json.dumps(item).encode() for item in events) + b'\n{"event":')
        self.write("jobs/job_old/state.json", {"context": {"step_id": "quality", "job_id": "job_old"}, "job": {"status": "failed"}})
        self.write("requests/req_one/call.json", {"context": {"step_id": "quality", "job_id": "job_now"}, "request_id": "req_one", "status": "running"})
        self.write("requests/req_one/attempts/http_one/attempt.json", {"request_id": "req_one", "attempt_id": "http_one", "elapsed_ms": 300001,
                                                                     "response_headers_received": False, "response_body_complete": False, "failure": {"type": "TimeoutError"}})
        self.write("requests/req_one/attempts/http_one/request.json", {"headers": {"Authorization": "Bearer top-secret", "Cookie": "session=secret"},
                                                                     "url": "http://name:password@example.com/v1?api_key=secret"})
        self.write("requests/req_one/attempts/http_one/response_body.txt", b'{"image":"data:image/png;base64,YWJj", "api_key":"another-secret"}')

    def test_exact_related_records_and_no_database_writes(self):
        db = self.database()
        before = db.read_bytes()
        report = CLI.collect(self.root, "pl_one")
        keys = {item["record_key"] for item in report["records"]}
        self.assertTrue({"job_old", "job_now", "cr_one", "sess_one", "release_one"}.issubset(keys))
        self.assertNotIn("job_other_latest", keys)
        namespaces = {item["namespace"] for item in report["records"]}
        self.assertIn("collection_transfers", namespaces)
        self.assertIn("release_intents", namespaces)
        self.assertNotIn("secret-execution", json.dumps(report["records"]))
        self.assertEqual(before, db.read_bytes())
        self.assertEqual(report["coverage"], "snapshot_only")
        self.assertFalse((self.root / "logs").exists())
        self.assertIn("历史明细缺失", CLI.summary(report))
        self.assertIn("job_old", CLI.summary(report))

    def test_events_timeline_partial_line_and_running_snapshot(self):
        self.database()
        self.logs()
        report = CLI.collect(self.root, "pl_one")
        self.assertEqual([event["job_id"] for event in report["events"]], ["job_old", "job_now"])
        self.assertTrue(report["active"])
        self.assertTrue(next(value for value in report["file_observations"] if value["path"] == "events/a.jsonl")["partial"])
        text = CLI.summary(report)
        self.assertIn("未收到响应头", text)
        self.assertIn("慢请求", text)
        self.assertIn("不是跨文件全局一致快照", text)
        self.assertIn("响应头耗时：未知", text)

    def test_bundle_sanitized_and_manifest_matches_exported_bytes(self):
        db = self.database()
        self.logs()
        self.write(".env", b"API_KEY=never")
        self.write("requests/picture.png", b"PNG_BYTES")
        old = self.root / "logs" / "quality_diagnostics"
        old.mkdir(parents=True)
        (old / "private.txt").write_text("RAW_SECRET")
        before = db.read_bytes()
        report = CLI.collect(self.root, "pl_one")
        bundle = CLI.bundle(self.root, report)
        with zipfile.ZipFile(bundle) as archive:
            all_raw = b"\n".join(archive.read(name) for name in archive.namelist())
            for secret in (b"top-secret", b"another-secret", b"session=secret", b"name:password", b"YWJj", b"secret-execution", b"RAW_SECRET", b"PNG_BYTES"):
                self.assertNotIn(secret, all_raw)
            self.assertIn("诊断摘要.txt", archive.namelist())
            manifest = json.loads(archive.read("manifest.json"))
            for item in manifest["files"]:
                raw = archive.read(item["path"])
                self.assertEqual(hashlib.sha256(raw).hexdigest(), item["sha256"])
                self.assertEqual(len(raw), item["size_bytes"])
            self.assertFalse(any(name.endswith(".sqlite") or ".env" in name for name in archive.namelist()))
        self.assertEqual(before, db.read_bytes())

    def test_job_stage_and_attempt_filters(self):
        self.database()
        self.logs()
        report = CLI.collect(self.root, "pl_one", stage="quality", job_id="job_now")
        self.assertEqual([event["job_id"] for event in report["events"]], ["job_now"])
        self.assertNotIn("jobs/job_old/state.json", report["files"])
        self.assertIn("requests/req_one/attempts/http_one/request.json", report["files"])
        attempt = CLI.collect(self.root, "pl_one", request_id="http_one")
        self.assertIn("requests/req_one/attempts/http_one/request.json", attempt["files"])
        self.assertEqual(len(attempt["requests"]), 1)
        tree = CLI.collect(self.root, "pl_one", stage="tree")
        self.assertFalse(tree["events"])
        self.assertFalse(tree["requests"])

    def test_logs_without_database_readable(self):
        self.logs()
        report = CLI.collect(self.root, "pl_one")
        self.assertEqual(report["pipeline"]["batch_id"], "batch_one")
        self.assertEqual(report["runtime"]["git_revision"], "test")
        self.assertFalse((self.root / "system").exists())

    def test_failed_finished_event_summary_is_brief_and_preserves_full_file(self):
        self.logs()
        self.write("events/c.jsonl", (json.dumps({"event": "model_request_finished", "at": "2026-10-14T03:00:00Z", "status": "failed",
                                               "job_id": "job_now", "exception": {"type": "ValueError", "message": "JSON 无效", "traceback": "LONG_STACK_TRACE"}}) + "\n").encode())
        report = CLI.collect(self.root, "pl_one")
        text = CLI.summary(report)
        self.assertIn("ValueError", text)
        self.assertIn("JSON 无效", text)
        self.assertNotIn("LONG_STACK_TRACE", text)
        self.assertIn(b"LONG_STACK_TRACE", report["files"]["events/c.jsonl"])
        report["requests"][0]["body_capture_gap"] = "Unsupported response encoding: br"
        self.assertIn("正文证据缺口：Unsupported response encoding: br", CLI.summary(report))

    def test_retry_only_job_maps_to_stage_and_corrupt_index_tolerated(self):
        self.pipeline["steps"][0]["retry_job_id"] = "job_retry_only"
        self.database()
        self.logs()
        self.write("jobs/job_retry_only/state.json", {"job": {"status": "failed"}})
        self.write("index.json", [])
        report = CLI.collect(self.root, "pl_one", stage="quality", job_id="job_retry_only")
        self.assertIn("jobs/job_retry_only/state.json", report["files"])
        self.assertIn("job_retry_only", CLI.summary(report))
        self.assertTrue(any("索引不是对象" in item for item in report["issues"]))

    def test_directory_link_rejected_without_symlink_privilege(self):
        self.logs()
        original = CLI.is_link_or_junction

        def linked(value):
            return Path(value).name == "requests" or original(value)

        with mock.patch.object(CLI, "is_link_or_junction", linked):
            report = CLI.collect(self.root, "pl_one")
        self.assertFalse(any(name.startswith("requests/") for name in report["files"]))
        self.assertTrue(any("未读取文件：requests" in issue for issue in report["issues"]))

    def test_missing_pipeline_and_bad_identifiers_create_nothing(self):
        for identity in ("../outside", "C:drive", "pl_one/../pl_two", "NUL", "missing"):
            with self.assertRaises(ValueError):
                CLI.collect(self.root, identity)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_symlink_file_excluded(self):
        self.logs()
        outside = self.root / "private.json"
        outside.write_text('{"secret":"do-not-export"}')
        link = self.root / "logs" / "pipelines" / "pl_one" / "jobs" / "leak.json"
        try:
            link.symlink_to(outside)
        except OSError:
            self.skipTest("Symlink unavailable on this Windows account")
        report = CLI.collect(self.root, "pl_one")
        self.assertNotIn("jobs/leak.json", report["files"])
        self.assertTrue(any("未读取文件" in issue for issue in report["issues"]))

    def test_broken_json_is_marked_without_exporting_secret(self):
        self.logs()
        self.write("requests/req_one/partial.json", b'{"api_key":"secret-not-closed')
        report = CLI.collect(self.root, "pl_one")
        self.assertNotIn(b"secret-not-closed", report["files"]["requests/req_one/partial.json"])
        self.assertTrue(json.loads(report["files"]["requests/req_one/partial.json"])["incomplete_or_invalid_json"])

    def test_file_changes_during_snapshot_are_flagged(self):
        self.logs()
        real_fstat = CLI.os.fstat
        call = [0]

        def changing_fstat(fd):
            from types import SimpleNamespace
            stat = real_fstat(fd)
            call[0] += 1
            return SimpleNamespace(st_size=stat.st_size, st_mtime_ns=stat.st_mtime_ns + (call[0] % 2))

        with mock.patch.object(CLI.os, "fstat", changing_fstat):
            report = CLI.collect(self.root, "pl_one")
        self.assertTrue(all(item["source_changed_during_read"] for item in report["file_observations"]))

    def test_long_chinese_path_and_cli_readonly(self):
        longroot = self.root.joinpath(*(["长路径测试" * 5] * 8))
        self.root = longroot
        self.logs()
        before = {p.as_posix() for p in io_path(self.root).rglob("*")}
        output = io.StringIO()
        with redirect_stdout(output):
            result = CLI.main(["--pipeline-id", "pl_one", "--data-root", str(self.root), "--timeline"])
        self.assertEqual(result, 0)
        self.assertIn("Pipeline：pl_one", output.getvalue())
        self.assertEqual(before, {p.as_posix() for p in io_path(self.root).rglob("*")})


if __name__ == "__main__":
    unittest.main()
