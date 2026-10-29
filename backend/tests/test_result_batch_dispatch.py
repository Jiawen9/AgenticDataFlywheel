"""Production dispatch freezes one independent result without real phones."""
from __future__ import annotations

import base64
import json
from pathlib import Path
import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient
from openpyxl import load_workbook

from backend.batch_lifecycle import BatchPublishedError
from backend.phone_factory import create_router
from backend.tests import test_phone_factory_runtime as fixtures
from backend.tests.test_manual_collection import workbook_bytes


class ResultDispatchTests(unittest.TestCase):
    setUp = fixtures.FactoryRuntimeTests.setUp
    request = fixtures.FactoryRuntimeTests.request

    def client(self, args):
        result = fixtures.FactoryRuntimeTests.client(self, args)
        if not result["ok"]:
            return result
        value = json.loads(result.get("output", "{}"))
        if args[0] == "capabilities":
            value["agent_types"] = {name: {"available": True, "collection": True,
                "capture_logprobs": True, "model_iteration_evaluation": 1}
                for name in ("newagent", "pipelineagent")}
        if args[0] == "start-run":
            value["agent_type"] = args[args.index("--agent-type") + 1]
            self.remote[value["run_id"]]["agent_type"] = value["agent_type"]
        return {**result, "output": json.dumps(value)}

    def source(self):
        return self.store.add_task(self.upload)["imported_task"]["source_batch_id"]

    def publish_marker(self, batch_id):
        self.store.records.put("batch_lifecycle", batch_id,
            {"batch_id": batch_id, "status": "published", "release_id": "frozen-release"})

    def test_two_engines_and_recollection_have_independent_batches_with_common_source(self):
        source = self.source()
        first = self.store.remote_start(self.request())
        second = self.store.remote_start(self.request(request_id="pipeline", agent_type="pipelineagent"))
        third = self.store.remote_start(self.request(request_id="newagent-again"))
        self.assertEqual(len({item["batch_id"] for item in (first, second, third)}), 3)
        self.assertEqual(len({item["collection_run_id"] for item in (first, second, third)}), 3)
        for run in (first, second, third):
            self.assertEqual(run["task_source_batch_id"], source)
            self.assertEqual(run["batch_role"], "collection_result")
            self.assertNotEqual(run["batch_id"], source)
            self.assertIn(run["batch_id"], Path(run["output_dir"]).parts)
        starts = [call for call in self.calls if call[0] == "start-run"]
        self.assertEqual([call[call.index("--batch-id") + 1] for call in starts],
                         [run["batch_id"] for run in (first, second, third)])
        self.assertEqual([run["task_source_batch_id"] for run in self.store.runtime.run_views("generate")], [source] * 3)
        self.assertEqual([item["batch_id"] for item in self.store.runtime.batches()], [source])

    def test_frozen_dispatch_replay_survives_removed_source_files(self):
        source = self.source()
        first = self.store.remote_start(self.request())
        source_batch = self.store.collection_runs.batch(source, require_workbook=True)
        Path(source_batch["workbook_path"]).unlink()
        task = self.store._current()["tasks"][0]
        self.store.task_path(task["filename"]).unlink()
        replay = self.store.remote_start(self.request())
        self.assertTrue(replay["reused"])
        self.assertEqual(replay["batch_id"], first["batch_id"])
        self.assertEqual(len([call for call in self.calls if call[0] == "start-run"]), 1)
        self.assertTrue(Path(self.store.collection_runs.batch(first["batch_id"], require_workbook=True)["workbook_path"]).is_file())
        self.store.task_path(task["filename"]).write_bytes(b"changed redundant upload")
        self.assertEqual(self.store.remote_start(self.request())["collection_run_id"], first["collection_run_id"])

    def test_published_source_is_read_only_reusable_and_result_publication_is_independent(self):
        source = self.source()
        self.publish_marker(source)
        before = self.store.records.get("batch_lifecycle", source)
        first = self.store.remote_start(self.request())
        self.publish_marker(first["batch_id"])
        second = self.store.remote_start(self.request(request_id="after-publication"))
        self.assertNotEqual(first["batch_id"], second["batch_id"])
        self.assertEqual(self.store.records.get("batch_lifecycle", source), before)
        self.assertEqual(len(self.store.state()["tasks"]), 1)
        self.assertEqual(len(self.store.runtime.batches(include_published=True)), 1)
        with self.assertRaises(BatchPublishedError):
            self.store.remove_task(self.store._current()["tasks"][0]["filename"])
        app = FastAPI()
        app.include_router(create_router(self.store))
        with TestClient(app) as client:
            listing = client.get("/api/phone-factory/batches?batch_role=collection_result&include_published=true")
            self.assertEqual(listing.status_code, 200, listing.text)
            self.assertEqual(len(listing.json()["batches"]), 2)
            self.assertEqual(client.get(f"/api/phone-factory/batches/{source}").status_code, 200)
            runs = client.get(f"/api/phone-factory/runs?task_source_batch_id={source}&include_published=true").json()["runs"]
            self.assertEqual({run["batch_id"] for run in runs}, {first["batch_id"], second["batch_id"]})
            self.assertEqual({run["batch_id"]: run["batch_status"] for run in runs},
                             {first["batch_id"]: "published", second["batch_id"]: "active"})
            self.assertEqual(next(run for run in runs if run["batch_id"] == first["batch_id"])["release_id"], "frozen-release")

    def test_phone_app_subset_keeps_all_seventeen_values_and_original_task_ids(self):
        rows = [["a", "single", "App", "scene", "level2", "level3", "Do A", "hard", "element", 7,
                 "source", "author", "end", "complex", "key-action", "full SOP", "variables"],
                ["b", "single", "Other", "scene", "level2", "level3", "Do B"]]
        self.upload["content_base64"] = base64.b64encode(workbook_bytes(rows)).decode()
        source = self.source()
        source_task = self.store.collection_runs.batch(source)["snapshot"]["tasks"][0]
        run = self.store.remote_start(self.request(phone_id="phone-1", app="App"))
        batch = self.store.collection_runs.batch(run["batch_id"], require_workbook=True)
        self.assertEqual([task["task_id"] for task in batch["snapshot"]["tasks"]], [source_task["task_id"]])
        book = load_workbook(batch["workbook_path"], read_only=True)
        try:
            values = list(book.active.values)
            self.assertEqual(len(values[0]), 17)
            self.assertEqual(list(values[1]), rows[0])
            self.assertEqual(len(values), 2)
        finally:
            book.close()

    def test_failed_result_is_retained_and_new_request_does_not_append_to_it(self):
        self.source()
        first = self.store.remote_start(self.request())
        failed = self.store.records.update("collection_runs", first["collection_run_id"],
            lambda item: item.update(status="failed", dispatch_error=None, errors=[{"error": "device failed"}]))
        retry = self.store.remote_start(self.request())
        self.assertEqual(retry["collection_run_id"], first["collection_run_id"])
        fresh = self.store.remote_start(self.request(request_id="genuine-new-attempt"))
        self.assertNotEqual(fresh["batch_id"], failed["batch_id"])
        self.assertEqual(self.store.records.get("collection_runs", first["collection_run_id"]), failed)

    def test_accepted_historical_request_replays_without_parent_workbook(self):
        source = self.source()
        first = self.store.runtime.remote_start(self.request(), "generate", legacy_batch=True)
        frozen = self.store.records.get("collection_runs", first["collection_run_id"])
        batch = self.store.collection_runs.batch(source, require_workbook=True)
        Path(batch["workbook_path"]).unlink()
        self.store.task_path(self.store._current()["tasks"][0]["filename"]).unlink()
        replay = self.store.remote_start(self.request())
        self.assertTrue(replay["reused"])
        self.assertEqual(replay["batch_id"], source)
        self.assertEqual(replay["collection_run_id"], first["collection_run_id"])
        self.assertEqual(self.store.records.get("collection_runs", first["collection_run_id"]), frozen)
        self.assertEqual(self.store.records.list("collection_result_batches"), [])
        self.assertEqual(len([call for call in self.calls if call[0] == "start-run"]), 1)

    def test_historical_lost_ack_queries_remote_before_reading_parent_workbook(self):
        source = self.source()
        first = self.store.runtime.remote_start(self.request(), "generate", legacy_batch=True)
        run_id = first["collection_run_id"]
        self.store.records.update("collection_runs", run_id,
            lambda item: item.update(status="failed", dispatch_error="lost acknowledgement"))
        batch = self.store.collection_runs.batch(source, require_workbook=True)
        Path(batch["workbook_path"]).unlink()
        self.store.task_path(self.store._current()["tasks"][0]["filename"]).unlink()
        replay = self.store.remote_start(self.request())
        self.assertTrue(replay["reused"])
        self.assertEqual(replay["collection_run_id"], run_id)
        self.assertEqual(self.store.records.list("collection_result_batches"), [])
        self.assertEqual(len([call for call in self.calls if call[0] == "start-run"]), 1)

    def test_new_results_never_fall_back_to_dispatch_without_run_identity(self):
        self.source()
        real = self.store.run_client
        self.store.runtime._protocol_cache = "legacy"
        self.store.run_client = lambda args: {"ok": False, "output": "temporary capability outage"}
        with self.assertRaisesRegex(ValueError, "capability outage"):
            self.store.remote_start(self.request())
        frozen = self.store.collection_runs.list_runs()[0]
        self.assertEqual(frozen["request"]["metadata"]["result_transport"], "http")
        self.assertFalse(any(call[0] == "start-run" for call in self.calls))
        self.store.run_client = lambda args: {"ok": True, "output": '{"ok":true}'}
        with self.assertRaisesRegex(ValueError, "升级"):
            self.store.remote_start(self.request())
        self.store.run_client = real
        recovered = self.store.remote_start(self.request())
        self.assertEqual(recovered["batch_id"], frozen["batch_id"])
        starts = [call for call in self.calls if call[0] == "start-run"]
        self.assertEqual(len(starts), 1)
        self.assertIn("--collection-run-id", starts[0])
        self.assertIn("--batch-id", starts[0])
        self.assertIn("--output-dir", starts[0])


if __name__ == "__main__":
    unittest.main()
