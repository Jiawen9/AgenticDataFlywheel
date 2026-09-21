from __future__ import annotations

import io
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from openpyxl import Workbook

from backend.batch_lifecycle import BatchPublishedError
from backend.collection_runs import CollectionRunError, CollectionRunStore
from backend.data_store import ArtifactStore, RecordStore
from backend.manual_collection import ManualCollectionError, ManualCollectionStore
from backend.preprocessing_service import convert_input
from backend.task_generation.collection_batches import COLLECTION_COLUMNS
from backend.tests.test_collection_runs import raw_trajectory, seed_batch


def workbook_bytes(rows=None, headers=None):
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(headers or COLLECTION_COLUMNS)
    for row in rows if rows is not None else [["CASE-01", "单APP", "Demo", "工具", "设置", "账户", "Open settings", "easy", "button", 4, "manual", "tester", "done", "simple", "tap", "hello", "none"]]:
        sheet.append(row)
    target = io.BytesIO()
    workbook.save(target)
    workbook.close()
    return target.getvalue()


class ManualCollectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "data"
        self.store = ManualCollectionStore(self.root)

    def register(self, **kwargs):
        return self.store.register("manual.xlsx", workbook_bytes(), "manual tasks", **kwargs)

    def test_register_preserves_original_bytes_and_all_values_without_generation_job(self):
        original = workbook_bytes()
        batch = self.store.register("manual.xlsx", original, "description", "req-one")
        self.assertEqual(Path(batch["workbook_path"]).read_bytes(), original)
        self.assertEqual(batch["kind"], "manual_collection")
        self.assertIsNone(batch["source_job_id"])
        self.assertIsNone(batch["snapshot"]["job_id"])
        task = batch["snapshot"]["tasks"][0]
        self.assertEqual(list(task["source_values"]), COLLECTION_COLUMNS)
        self.assertEqual(task["source_values"]["SOP"], "hello")
        self.assertEqual(task["source_values"]["预期步数"], 4)
        self.assertEqual(task["scene"], "工具")
        self.assertIsNone(task["source_result_id"])
        self.assertEqual(RecordStore(self.root).list("task_generation.jobs"), [])
        self.assertEqual(self.store.get(batch["batch_id"]), batch)
        self.assertEqual(self.store.list(), [batch])

    def test_request_identity_concurrent_replay_and_conflict(self):
        original = workbook_bytes()
        with ThreadPoolExecutor(max_workers=4) as pool:
            values = list(pool.map(lambda _: ManualCollectionStore(self.root).register("tasks.xlsx", original, "", "one"), range(4)))
        self.assertEqual(len({value["batch_id"] for value in values}), 1)
        self.assertEqual(len(self.store.list()), 1)
        with self.assertRaises(ManualCollectionError):
            self.store.register("different.xlsx", original, "", "one")
        self.assertNotEqual(self.store.register("tasks.xlsx", original, "", "two")["batch_id"], values[0]["batch_id"])

    def test_invalid_headers_rows_ids_and_apps_rejected(self):
        bad = [workbook_bytes([]), workbook_bytes(headers=["task"]),
               workbook_bytes([["../bad", "single", "App", None, None, None, "Task"]]),
               workbook_bytes([["case", "single", "App", None, None, None, "Task"], ["CASE", "single", "App", None, None, None, "Task"]]),
               workbook_bytes([["case", "single", None, None, None, None, "Task"]]),
               workbook_bytes([["case", "single", "App", None, None, None, "=A1"]])]
        for data in bad:
            with self.assertRaises(CollectionRunError):
                self.store.register("tasks.xlsx", data)
        self.assertEqual(self.store.list(), [])
        with self.assertRaises(ManualCollectionError):
            self.store.register("tasks.xls", b"bad")

    def test_missing_classification_warns_and_published_batch_is_not_restored(self):
        original = workbook_bytes([["1", "single", "Demo", None, None, None, "Task"]])
        batch = self.store.register("tasks.xlsx", original, "", "replay")
        self.assertEqual(len(batch["warnings"]), 1)
        self.assertIsNone(batch["snapshot"]["tasks"][0]["scene"])
        RecordStore(self.root).put("batch_lifecycle", batch["batch_id"], {"batch_id": batch["batch_id"], "status": "published", "release_id": "release"})
        self.assertEqual(self.store.list(), [])
        self.assertEqual(self.store.get(batch["batch_id"])["batch_id"], batch["batch_id"])
        with self.assertRaises(BatchPublishedError):
            self.store.register("tasks.xlsx", original, "", "replay")
        with self.assertRaises(BatchPublishedError):
            CollectionRunStore(self.root).create(batch["batch_id"])

    def test_manual_task_survives_collection_and_preprocessing_identity_mapping(self):
        batch = self.register()
        runs = CollectionRunStore(self.root)
        run, _ = runs.create(batch["batch_id"], dispatch_key="request")
        entry = raw_trajectory(run, "CASE-01")
        result, _ = runs.complete(run["collection_run_id"], {"batch_id": batch["batch_id"], "trajectories": [entry]})
        task = batch["snapshot"]["tasks"][0]
        self.assertEqual(result["trajectories"][0]["source_row_id"], task["source_row_id"])
        payload, _ = convert_input(runs.ready_input(batch["batch_id"]), lambda _: None, data_root=self.root)
        row = payload["sheets"]["VLA trajectories"][0]
        self.assertEqual(row["task_id"], task["task_id"])
        self.assertEqual(row["source_row_id"], task["source_row_id"])
        self.assertIsNone(row["source_result_id"])
        from backend.preprocessing_jobs import PreprocessingJobManager
        manager = PreprocessingJobManager(self.root)
        try:
            listed = manager.batches()[0]
            self.assertEqual(listed["kind"], "manual_collection")
            self.assertTrue(listed["can_start"])
            self.assertEqual(listed["ready_trajectory_count"], 1)
        finally:
            manager.shutdown()

    def test_failed_atomic_registration_leaves_no_batch_and_same_request_can_retry(self):
        original = workbook_bytes()
        with patch.object(self.store.artifacts.records, "put_many", side_effect=OSError("database unavailable")):
            with self.assertRaises(OSError):
                self.store.register("tasks.xlsx", original, "", "atomic")
        self.assertEqual(self.store.list(), [])
        self.assertEqual(self.store.artifacts.list(), [])
        self.assertFalse(any((self.root / "batches").glob("*/00_collection/result.json")))
        recovered = self.store.register("tasks.xlsx", original, "", "atomic")
        self.assertEqual(Path(recovered["workbook_path"]).read_bytes(), original)

    def test_tampered_manual_workbook_rejected_but_existing_generation_validation_remains(self):
        batch = self.register()
        Path(batch["workbook_path"]).write_bytes(b"tampered")
        with self.assertRaises((ValueError, OSError)):
            CollectionRunStore(self.root).create(batch["batch_id"])
        generated, _ = seed_batch(self.root)
        runs = CollectionRunStore(self.root)
        self.assertEqual(runs.create(generated["batch_id"])[0]["batch_tasks"]["task-one"]["source_result_id"], "result-one")
        generated["snapshot"]["tasks"][0]["source_result_id"] = None
        with self.assertRaises(CollectionRunError):
            CollectionRunStore(self.root, batch_loader=lambda _: generated).create(generated["batch_id"])


if __name__ == "__main__":
    unittest.main()
