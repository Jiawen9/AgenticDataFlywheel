from __future__ import annotations

import base64
import copy
import hashlib
import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

from backend.collection_runs import CollectionRunError, CollectionRunStore
from backend.phone_factory import PhoneFactoryError, PhoneFactoryStore, create_router
from backend.phonefactory_client import build_parser, cmd_start_run
from backend.task_generation.collection_batches import collection_batch_payload, payload_digest, workbook_digest, write_collection_workbook
from backend.task_generation.collection_input import build_collection_input


def seed_batch(root: Path, batch_id="batch-one", kind="task_generation"):
    rows = [{"result_id": "result-one", "task_uuid": "task-one", "task": "Open settings", "app": "Demo", "用例编号": "CASE-01"},
            {"result_id": "result-two", "task_uuid": "task-two", "task": "Open help", "app": "Demo", "用例编号": "CASE-02"}]
    value = collection_batch_payload(build_collection_input({
        "job_id": batch_id, "kind": kind, "status": "succeeded", "knowledge_base_version": "kb-one",
    }, rows), "2026-09-15T12:00:00+08:00")
    directory = root / "system/task_generation/collection_batches" / batch_id
    directory.mkdir(parents=True)
    workbook = directory / value["filename"]
    write_collection_workbook(value["snapshot"], workbook)
    stored = {**value, "_integrity": {"payload_sha256": payload_digest(value), "workbook_sha256": workbook_digest(workbook)}}
    (directory / "batch.json").write_text(json.dumps(stored), encoding="utf-8")
    return value, workbook


def raw_trajectory(run: dict, case_id="task-one", name="original-run"):
    directory = Path(run["output_dir"]) / case_id / name
    directory.mkdir(parents=True, exist_ok=True)
    action = {"action": "click", "coordinate": [10, 10]}
    (directory / "_trajectory_for_evaluate.json").write_text(json.dumps({"actions_flat": [{"global_step": 1, "action": action}]}), encoding="utf-8")
    (directory / "step001_vla_model_response.json").write_text(json.dumps({"content": '<tool_call>' + json.dumps(action) + '</tool_call><summary>Tap</summary>'}), encoding="utf-8")
    (directory / "step001_vla_input_ui.xml").write_text('<hierarchy/>', encoding="utf-8")
    for name in ("step001_vla_input.jpg", "step001_vla_input_stability.jpg"):
        Image.new("RGB", (20, 30), "white").save(directory / name)
    return {
        "collection_case_id": case_id, "source_trajectory_id": directory.name,
        "relative_dir": directory.relative_to(Path(run["output_dir"])).as_posix(),
        "collected_at": "2026-09-15T13:00:00+08:00",
        "files": [{"path": file.relative_to(Path(run["output_dir"])).as_posix(),
                   "sha256": workbook_digest(file), "size": file.stat().st_size}
                  for file in sorted(directory.iterdir())],
    }


class CollectionRunTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "outside-project-data"
        self.batch, self.workbook = seed_batch(self.root)
        self.store = CollectionRunStore(self.root)

    def create(self, key=None):
        return self.store.create("batch-one", dispatch_key=key)[0]

    def complete(self, run, entry=None, **kwargs):
        value = {"batch_id": "batch-one", "trajectories": [entry or raw_trajectory(run)], **kwargs}
        return self.store.complete(run["collection_run_id"], value)

    def test_request_key_is_stable_across_restart_and_parallel_creation(self):
        with ThreadPoolExecutor(max_workers=6) as pool:
            values = list(pool.map(lambda _: CollectionRunStore(self.root).create("batch-one", dispatch_key="request-one"), range(6)))
        self.assertEqual(sum(created for _, created in values), 1)
        self.assertEqual(len({run["collection_run_id"] for run, _ in values}), 1)
        self.assertEqual(len(self.store.list_runs("batch-one")), 1)
        existing = values[0][0]
        self.assertEqual(CollectionRunStore(self.root).create("batch-one", dispatch_key="request-one")[0], existing)
        with self.assertRaises(CollectionRunError):
            self.store.create("batch-one", dispatch_key="request-one", metadata={"phone": "changed"})
        self.assertNotEqual(self.create()["collection_run_id"], self.create()["collection_run_id"])

    def test_complete_maps_case_identity_and_freezes_full_input_without_writes_on_reads(self):
        run = self.create()
        entry = raw_trajectory(run)
        entry["task_id"] = "forged"
        completed, created = self.complete(run, entry, errors=[{"collection_case_id": "task-two", "error": "device unavailable"}])
        self.assertTrue(created)
        self.assertEqual(completed["status"], "completed")
        trajectory = completed["trajectories"][0]
        self.assertEqual((trajectory["task_id"], trajectory["source_result_id"]), ("task-one", "result-one"))
        before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.root.rglob("*") if p.is_file()}
        result = CollectionRunStore(self.root).ready_input("batch-one")
        self.assertEqual(result["raw_root"], str(self.root / "raw/collection_batches/batch-one"))
        self.assertEqual(result, self.store.freeze_ready_input("batch-one"))
        self.assertEqual(result["errors"][0]["collection_run_id"], run["collection_run_id"])
        self.assertEqual(before, {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.root.rglob("*") if p.is_file()})

    def test_augmentation_case_maps_back_to_original_generated_task(self):
        seed_batch(self.root, "augmentation-batch", "augmentation")
        run, _ = self.store.create("augmentation-batch")
        entry = raw_trajectory(run, "CASE-01")
        result, _ = self.store.complete(run["collection_run_id"], {"batch_id": "augmentation-batch", "trajectories": [entry]})
        self.assertEqual(result["trajectories"][0]["task_id"], "task-one")
        self.assertEqual(result["trajectories"][0]["source_result_id"], "result-one")

    def test_duplicate_completion_is_idempotent_and_different_manifest_conflicts(self):
        run = self.create()
        entry = raw_trajectory(run)
        manifest = {"batch_id": "batch-one", "trajectories": [entry]}
        with ThreadPoolExecutor(max_workers=4) as pool:
            values = list(pool.map(lambda _: CollectionRunStore(self.root).complete(run["collection_run_id"], manifest), range(4)))
        self.assertEqual(sum(created for _, created in values), 1)
        current = self.store.get(run["collection_run_id"])
        self.assertEqual(self.store.complete(run["collection_run_id"], manifest), (current, False))
        with self.assertRaises(CollectionRunError):
            self.store.complete(run["collection_run_id"], {**manifest, "errors": [{"error": "changed"}]})
        self.assertEqual(self.store.get(run["collection_run_id"]), current)

    def test_multiple_runs_same_original_name_stay_distinct_and_new_run_changes_digest(self):
        first = self.create()
        self.complete(first)
        before = self.store.ready_input("batch-one")
        second = self.create()
        self.assertEqual(self.store.ready_input("batch-one")["input_digest"], before["input_digest"])
        self.complete(second)
        after = self.store.ready_input("batch-one")
        self.assertEqual(len(after["trajectories"]), 2)
        self.assertNotEqual(after["input_digest"], before["input_digest"])
        self.assertEqual(len({item["collection_run_id"] for item in after["trajectories"]}), 2)

    def test_zero_success_is_completed_but_cannot_start_preprocessing(self):
        run = self.create()
        saved, _ = self.store.complete(run["collection_run_id"], {"batch_id": "batch-one", "trajectories": [], "errors": [{"collection_case_id": "task-one", "error": "failed"}]})
        self.assertEqual(saved["trajectory_count"], 0)
        with self.assertRaises(CollectionRunError):
            self.store.ready_input("batch-one")

    def test_missing_tampered_unlisted_and_cross_case_files_rejected_without_completing(self):
        run = self.create()
        entry = raw_trajectory(run)
        invalid = []
        wrong_hash = copy.deepcopy(entry)
        wrong_hash["files"][0]["sha256"] = "0" * 64
        invalid.append(wrong_hash)
        wrong_case = copy.deepcopy(entry)
        wrong_case["collection_case_id"] = "not-in-batch"
        invalid.append(wrong_case)
        traversal = copy.deepcopy(entry)
        traversal["files"][0]["path"] = "../outside"
        invalid.append(traversal)
        other_case = copy.deepcopy(entry)
        other_case["files"][0]["path"] = "task-two/original-run/file"
        invalid.append(other_case)
        omitted = copy.deepcopy(entry)
        omitted["files"].pop()
        invalid.append(omitted)
        for value in invalid:
            with self.subTest(value=value):
                with self.assertRaises(CollectionRunError):
                    self.complete(run, value)
                self.assertEqual(self.store.get(run["collection_run_id"])["status"], "dispatching")
        file = Path(run["output_dir"]) / entry["files"][0]["path"]
        file.unlink()
        with self.assertRaises(CollectionRunError):
            self.complete(run, entry)

    def test_source_changes_after_completion_fail_read_without_replacing_manifest(self):
        run = self.create()
        entry = raw_trajectory(run)
        original, _ = self.complete(run, entry)
        source = Path(run["output_dir"]) / entry["files"][0]["path"]
        source.write_bytes(b"changed")
        with self.assertRaises(CollectionRunError):
            self.store.ready_input("batch-one")
        with self.assertRaises(CollectionRunError):
            self.complete(run, entry)
        self.assertEqual(self.store.get(run["collection_run_id"]), original)

    def test_json_is_required_but_export_not_required_after_dispatch(self):
        run = self.create()
        self.complete(run)
        self.workbook.unlink()
        self.assertEqual(len(self.store.ready_input("batch-one")["trajectories"]), 1)
        with self.assertRaises(CollectionRunError):
            self.create()
        self.workbook.with_name("batch.json").unlink()
        with self.assertRaises(CollectionRunError):
            self.store.ready_input("batch-one")

    def test_old_directories_are_invisible_and_reads_of_missing_root_do_not_write(self):
        old = self.root / "old/raw/collection_batches/batch-one/runs/run-one"
        old.mkdir(parents=True)
        (old / "manifest.json").write_text('{"status":"completed"}')
        self.assertEqual(self.store.list_runs(), [])
        with self.assertRaises(CollectionRunError):
            self.store.ready_input("batch-one")
        empty = CollectionRunStore(self.root / "empty")
        self.assertIsNone(empty.get("missing"))
        self.assertEqual(empty.list_runs(), [])
        self.assertFalse(empty.root.exists())

    def test_router_real_batch_dispatch_idempotency_completion_and_client_fields(self):
        calls = []
        def remote(args):
            if args[0] == "run":
                return {"ok": False, "output": "unknown remote run"}
            if args[0] == "capabilities":
                return {"ok": True, "output": '{"protocol_version":1,"batch_results":true}'}
            calls.append(args)
            return {"ok": True, "output": '{"ok":true}'}
        factory = PhoneFactoryStore(self.root, run_client_fn=remote)
        factory.dispatch("phone-apps", "POST", {"phone_id": "phone", "app": "Demo"})
        factory.dispatch("vla", "POST", {"value": "http://model.invalid"})
        factory.add_task({"filename": self.batch["filename"], "description": "batch", "source_batch_id": "batch-one", "content_base64": base64.b64encode(self.workbook.read_bytes()).decode()})
        app = FastAPI()
        app.include_router(create_router(factory))
        with TestClient(app) as client:
            request = {"filename": self.batch["filename"], "request_id": "request-one"}
            first = client.post("/api/phone-factory/remote/start-run", json=request)
            self.assertEqual(first.status_code, 200, first.text)
            run_id = first.json()["collection_run_id"]
            second = client.post("/api/phone-factory/remote/start-run", json=request)
            self.assertEqual(second.json()["collection_run_id"], run_id)
            self.assertEqual(len(calls), 1)
            self.assertIn("--batch-id", calls[0])
            self.assertEqual(calls[0][calls[0].index("--collection-run-id") + 1], run_id)
            conflict = client.post("/api/phone-factory/remote/start-run", json={**request, "vla": "http://different.invalid"})
            self.assertEqual(conflict.status_code, 409)
            run = factory.collection_runs.get(run_id)
            entry = raw_trajectory(run)
            done = client.post(f"/api/phone-factory/collection-runs/{run_id}/complete", json={"batch_id": "batch-one", "trajectories": [entry]})
            self.assertEqual(done.status_code, 200, done.text)
            self.assertEqual(done.json()["status"], "completed")
            self.assertEqual(client.get("/api/phone-factory/collection-runs?batch_id=batch-one").json()["runs"][0]["collection_run_id"], run_id)
            self.assertEqual(client.post("/api/phone-factory/collection-runs/missing/complete", json={}).status_code, 404)
            self.assertEqual(client.post(f"/api/phone-factory/collection-runs/{run_id}/complete", json={"batch_id": "other", "trajectories": [entry]}).status_code, 409)

    def test_dispatch_failure_retry_uses_same_run_and_completed_callback_is_not_lost(self):
        calls = []
        def remote(args):
            if args[0] == "run":
                return {"ok": False, "output": "unknown remote run"}
            if args[0] == "capabilities":
                return {"ok": True, "output": '{"protocol_version":1,"batch_results":true}'}
            calls.append(args)
            if len(calls) == 1:
                return {"ok": False, "output": "mock timeout"}
            run_id = args[args.index("--collection-run-id") + 1]
            run = self.store.get(run_id)
            self.complete(run)
            return {"ok": True, "output": '{"ok":true}'}
        factory = PhoneFactoryStore(self.root, run_client_fn=remote)
        factory.dispatch("phone-apps", "POST", {"phone_id": "phone", "app": "Demo"})
        factory.dispatch("vla", "POST", {"value": "http://model.invalid"})
        factory.add_task({"filename": self.batch["filename"], "description": "batch", "source_batch_id": "batch-one", "content_base64": base64.b64encode(self.workbook.read_bytes()).decode()})
        request = {"filename": self.batch["filename"], "request_id": "retry-one"}
        with self.assertRaises(PhoneFactoryError):
            factory.remote_start(request)
        result = factory.remote_start(request)
        self.assertEqual(calls[0][calls[0].index("--collection-run-id") + 1], result["collection_run_id"])
        self.assertEqual(self.store.get(result["collection_run_id"])["status"], "completed")

    def test_python_forwarder_adds_optional_fields_and_keeps_manual_protocol(self):
        apps = self.root / "apps.json"
        apps.write_text("[]")
        base = ["start-run", str(self.workbook), str(apps)]
        with patch("backend.phonefactory_client.post_multipart", return_value={"ok": True}) as post:
            cmd_start_run(build_parser().parse_args(base))
            self.assertNotIn("collection_run_id", post.call_args.args[1])
            cmd_start_run(build_parser().parse_args(base + ["--batch-id", "batch-one", "--collection-run-id", "run-one", "--output-dir", "D:/raw/output"]))
            self.assertEqual({key: post.call_args.args[1][key] for key in ("batch_id", "collection_run_id", "output_dir")}, {"batch_id": "batch-one", "collection_run_id": "run-one", "output_dir": "D:/raw/output"})
            self.assertEqual(post.call_args.args[2][0][2], self.workbook.read_bytes())
            with self.assertRaises(SystemExit):
                cmd_start_run(build_parser().parse_args(base + ["--batch-id", "batch-one"]))

    def test_parallel_dispatch_claims_remote_once_and_setup_failure_is_retryable(self):
        entered, release = threading.Event(), threading.Event()
        calls = []
        def remote(args):
            if args[0] == "run":
                return {"ok": False, "output": "unknown remote run"}
            if args[0] == "capabilities":
                return {"ok": True, "output": '{"protocol_version":1,"batch_results":true}'}
            calls.append(args)
            entered.set()
            release.wait(5)
            return {"ok": True, "output": '{"ok":true}'}
        factory = PhoneFactoryStore(self.root, run_client_fn=remote)
        factory.dispatch("phone-apps", "POST", {"phone_id": "phone", "app": "Demo"})
        factory.dispatch("vla", "POST", {"value": "http://model.invalid"})
        factory.add_task({"filename": self.batch["filename"], "description": "batch", "source_batch_id": "batch-one", "content_base64": base64.b64encode(self.workbook.read_bytes()).decode()})
        request = {"filename": self.batch["filename"], "request_id": "parallel"}
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(factory.remote_start, request)
            self.assertTrue(entered.wait(5))
            try:
                second = pool.submit(factory.remote_start, request)
                self.assertEqual(len(calls), 1)
            finally:
                release.set()
            second_result = second.result(timeout=5)
            self.assertTrue(second_result["reused"])
            self.assertEqual(first.result(timeout=5)["collection_run_id"], second_result["collection_run_id"])
            self.assertEqual(len(calls), 1)
        with patch("backend.phone_factory_runtime.tempfile.TemporaryDirectory", side_effect=OSError("disk error")):
            with self.assertRaises(OSError):
                factory.remote_start({**request, "request_id": "disk-failure"})
        failed = next(run for run in self.store.list_runs() if run["dispatch_key"] == "disk-failure")
        self.assertEqual(failed["status"], "failed")
        self.assertIn("disk error", failed["dispatch_error"])


if __name__ == "__main__":
    unittest.main()
