"""Pipeline audit storage never writes business data or changes execution."""
from __future__ import annotations

import base64
import hashlib
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from backend import pipeline_diagnostics as diag
from backend.file_io import io_path


class PipelineDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def events(self, pipeline="pl_test"):
        base = self.root / "logs" / "pipelines" / pipeline / "events"
        return [json.loads(line) for file in base.glob("*.jsonl") for line in file.read_text(encoding="utf-8").splitlines()]

    def test_no_context_does_not_write_or_create_database(self):
        diag.log_event("outside")
        self.assertFalse(diag.write_json("test.json", {"hello": "world"}))
        with diag.job_context({"job_id": "manual"}, "quality", self.root):
            diag.log_event("manual")
        self.assertEqual(list(self.root.iterdir()), [])

    def test_request_content_credentials_images_and_exception_are_scrubbed(self):
        secret = "private-key-123-unique"
        diag.register_secrets(secret)
        picture = b"not a real picture"
        encoded = base64.b64encode(picture).decode()
        value = {"messages": [{"content": "full task business text\nline two"},
                              {"image_url": {"url": "data:image/png;base64," + encoded}}],
                 "headers": [["Authorization", "Bearer " + secret], ["Content-Type", "application/json"]],
                 "api_key": secret, "model": "model-test", "completion_tokens": 89,
                 "error": f"failed with {secret}; https://user:pwd@host/v1?api_key=other&model=test",
                 "source": {"type": "base64", "media_type": "image/png", "data": encoded}}
        clean = diag.sanitize(value)
        serialized = json.dumps(clean)
        self.assertNotIn(secret, serialized)
        self.assertNotIn(encoded, serialized)
        self.assertNotIn("user:pwd", serialized)
        self.assertNotIn("api_key=other", serialized)
        self.assertEqual(clean["messages"][0]["content"], value["messages"][0]["content"])
        self.assertEqual(clean["messages"][1]["image_url"]["url"]["sha256"], hashlib.sha256(picture).hexdigest())
        self.assertEqual(clean["completion_tokens"], 89)
        try:
            try:
                raise ValueError("api_key='" + secret + "'")
            except ValueError as exc:
                raise RuntimeError("outer") from exc
        except RuntimeError as exc:
            record = diag.format_exception(exc)
        self.assertEqual([entry["type"] for entry in record["chain"]], ["RuntimeError", "ValueError"])
        self.assertNotIn(secret, json.dumps(record))

    def test_concurrent_pipeline_contexts_are_separate_and_jsonl_is_valid(self):
        def produce(number):
            with diag.trace_context(root=self.root, pipeline_id=f"pl_{number % 2}", job_id=f"job_{number}"):
                for i in range(12):
                    diag.log_event("progress", count=i, detail='quoted "api_key=abc" business text')
        with ThreadPoolExecutor(max_workers=5) as executor:
            list(executor.map(produce, range(10)))
        for number in range(2):
            events = self.events(f"pl_{number}")
            self.assertEqual(len(events), 60)
            self.assertTrue(all(item["pipeline_id"] == f"pl_{number}" for item in events))
            self.assertEqual(len({(item["writer_id"], item["sequence"]) for item in events}), 60)

    def test_retry_keeps_old_exception_and_new_execution(self):
        payload = {"job_id": "job_test", "pipeline_id": "pl_test", "batch_id": "batch", "status": "running"}
        try:
            with diag.job_context(payload, "quality", self.root):
                raise TimeoutError("test timeout")
        except TimeoutError:
            pass
        with diag.job_context(payload, "quality", self.root):
            diag.record_job_snapshot({**payload, "status": "succeeded"}, "quality", self.root)
        starts = [item for item in self.events() if item["event"] == "execution_started"]
        self.assertEqual(len({item["execution_id"] for item in starts}), 2)
        failures = list((self.root / "logs/pipelines/pl_test/jobs/job_test").glob("*/exception.json"))
        self.assertEqual(len(failures), 1)
        self.assertIn("test timeout", failures[0].read_text(encoding="utf-8"))
        self.assertFalse((self.root / "system/app.sqlite").exists())

    def test_disk_failure_preserves_success_and_original_exception(self):
        payload = {"job_id": "job_test", "pipeline_id": "pl_test", "batch_id": "batch"}
        original = RuntimeError("business failure")
        with patch.object(diag, "_atomic", side_effect=OSError("disk failure")), \
             patch.object(diag, "_append_bytes", side_effect=OSError("disk failure")):
            with diag.job_context(payload, "quality", self.root):
                self.assertFalse(diag.write_text("ok.txt", "anything"))
            with self.assertRaises(RuntimeError) as caught:
                with diag.job_context(payload, "quality", self.root):
                    raise original
            self.assertIs(caught.exception, original)

    def test_path_escape_and_link_rejected_without_mutating_outside(self):
        with diag.trace_context(root=self.root, pipeline_id="pl_test"):
            for path in ("../outside.json", "C:/outside.json", "/outside.json", "requests/../../outside.json"):
                self.assertFalse(diag.write_json(path, {}))
            with patch.object(diag, "is_link_or_junction", side_effect=lambda p: Path(p).name == "requests"):
                self.assertFalse(diag.write_json("requests/request.json", {}))
        self.assertFalse((self.root / "outside.json").exists())

    def test_long_unicode_paths_index_and_nested_context_restore(self):
        root = self.root / ("目录" * 30) / ("long" * 35)
        payload = {"pipeline_id": "pl_test", "batch_id": "batch", "status": "running", "steps": [
            {"id": "quality", "job_ids": ["new"], "previous_job_ids": ["old"]}]}
        diag.record_pipeline(payload, root)
        index = io_path(root / "logs/pipelines/pl_test/index.json")
        self.assertEqual(json.loads(index.read_text(encoding="utf-8"))["pipeline"]["steps"], payload["steps"])
        with diag.trace_context(root=self.root, pipeline_id="pl_test", task_id="A"):
            with diag.trace_context(task_id="B"):
                self.assertEqual(diag.current_context()["task_id"], "B")
            self.assertEqual(diag.current_context()["task_id"], "A")
        self.assertIsNone(diag.current_context())

    def test_partial_credentials_and_unrelated_job_context_do_not_leak(self):
        diag.register_secrets("unique-partial-credential-42")
        self.assertNotIn("unique-partial", diag.sanitize_partial('body unique-partial'))
        self.assertNotIn("user:pass", diag.sanitize_partial('body https://user:pass'))
        with diag.trace_context(root=self.root, pipeline_id="pl_test", job_id="parent", execution_id="old", task_id="wrong"):
            diag.record_job_snapshot({"pipeline_id": "pl_test", "job_id": "new"}, "quality", self.root)
            with diag.job_context({"pipeline_id": "pl_test", "job_id": "new"}, "quality", self.root):
                self.assertNotIn("task_id", diag.current_context())
                self.assertNotEqual(diag.current_context()["execution_id"], "old")
        events = [e for e in self.events() if e.get("job_id") == "new"]
        self.assertTrue(events)
        self.assertTrue(all(e.get("execution_id") != "old" for e in events))

    def test_embedded_json_and_partial_image_credentials_are_removed(self):
        examples = [
            'payload={"image_base64":"YWJjYWJj"}',
            'ERROR response={"source":{"type":"base64","media_type":"image/png","data":"YWJjYWJj"}}',
            'ERROR response={"url":"http:\\/\\/username:password@host/v1"}',
            'ERROR response={"source":{"type":"base64","data":"YWJjYWJj',
        ]
        for example in examples:
            for clean in (diag.sanitize(example), diag.sanitize_partial(example)):
                self.assertNotIn("YWJjYWJj", clean)
                self.assertNotIn("username:password", clean)

    def test_tree_nested_pool_retains_correct_pipeline_task_and_step(self):
        from backend.trajectories_tree.tree_builder import classify_trajectories
        from backend.tests.test_trajectory_tree import step, classification
        from backend.quality_input_builder import build_quality_workbook
        from backend.pipeline_access import execution_pipeline_id
        seen, summaries = [], []

        class Classifier:
            def classify(self, **kwargs):
                seen.append((diag.current_context(), execution_pipeline_id()))
                return classification()

        class Summarizer:
            model = "mock"
            def summarize_trajectory(self, task, trajectory_id, records):
                summaries.append(diag.current_context())
                return "mock summary"

        trajectories = []
        for number in (1, 2):
            item = step("tr_" + str(number), 1, {"action": "wait"})
            item.task_id = "task_" + str(number)
            item.observation = "mock observation"
            target = self.root / item.image
            target.parent.mkdir(parents=True)
            target.write_bytes(b"mock image; classifier never decodes")
            trajectories.append((item.trajectory, [item]))
        with diag.trace_context(root=self.root, pipeline_id="pl_test", job_id="tree"):
            classify_trajectories(trajectories, Classifier(), self.root, .8, max_concurrent=2)
            # Classifier fixture intentionally lacks Observation; supply test input.
            for _, items in trajectories:
                items[0].observation = "mock observation"
            build_quality_workbook(grouped={items[0].task_id: [(tr, items)] for tr, items in trajectories},
                task_goals={}, trajectory_root=self.root, output=self.root / "quality.xlsx",
                env_path=self.root / "unused.env", summarizer=Summarizer(), max_concurrent=2)
        self.assertEqual({c[0]["task_id"] for c in seen}, {"task_1", "task_2"})
        self.assertTrue(all(c[0]["pipeline_id"] == "pl_test" and c[0]["step"] == 1 for c in seen))
        self.assertEqual({c["trajectory_id"] for c in summaries}, {"tr_1", "tr_2"})
        self.assertTrue(all(c["pipeline_id"] == "pl_test" for c in summaries))


if __name__ == "__main__":
    unittest.main()
