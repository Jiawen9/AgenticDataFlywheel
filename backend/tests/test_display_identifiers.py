from __future__ import annotations

import copy
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.data_store import ArtifactStore, RecordStore, RevisionConflict
from backend.display_identifiers import DISPLAY_BATCHES, DISPLAY_IDS, DisplayIdentifierStore
from backend.identifiers import allocate_id, batch_uses_short_ids
from backend import display_identifiers_router
from backend.collection_runs import CollectionRunStore
from backend.manual_collection import ManualCollectionStore
from backend.preprocessing_jobs import PreprocessingJobManager
from backend.preprocessing_service import SHEET
from backend.tests.test_collection_runs import raw_trajectory, seed_batch
from backend.tests.test_manual_collection import workbook_bytes
from backend.tests.test_preprocessing_jobs import CONFIG, ManualQueue
from backend.tests.test_trajectories_preprocessing import AcceptingReviewer


DAY = datetime(2026, 10, 25, 16, tzinfo=timezone.utc)
LATER = datetime(2026, 10, 27, tzinfo=timezone.utc)


class DisplayIdentifierTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "data"
        self.aliases = DisplayIdentifierStore(self.root)
        self.records = RecordStore(self.root)

    def resolve(self, kind, identifier, scope=""):
        return self.aliases.resolve([{"kind": kind, "id": identifier, "scope": scope}])[0]

    def test_bulk_lookup_is_read_only_for_missing_and_legacy_records(self):
        identifiers = [{"kind": "batch", "id": "manual_" + "a" * 64},
                       {"kind": "pipeline", "id": "p261026-001"},
                       {"kind": "task", "id": "legacy", "scope": "old-batch"}]
        self.assertTrue(all(not row["mapped"] for row in self.aliases.resolve(identifiers)))
        self.assertFalse(self.root.exists())
        self.records.put("collection_runs", "cr_old", {"run_id": "cr_old"})
        before = self.records.database_path.read_bytes()
        self.assertEqual([row["display_id"] for row in self.aliases.resolve(identifiers)],
                         [row["id"] for row in identifiers])
        self.assertEqual(before, self.records.database_path.read_bytes())
        self.assertEqual(self.records.list(DISPLAY_IDS), [])

    def test_reserved_alias_retries_across_days_and_never_enables_short_paths(self):
        item = {"kind": "batch", "id": "manual_original"}
        pending = self.aliases.prepare_entries([item], now=DAY)
        self.assertEqual(pending[0]["payload"]["display_id"], "b261026-001")
        self.assertFalse(self.resolve("batch", item["id"])["mapped"])
        retried = DisplayIdentifierStore(self.root).prepare_entries([item], now=LATER)
        self.assertEqual(retried[0]["payload"]["display_id"], "b261026-001")
        self.records.put_many(retried)
        self.assertEqual(self.resolve("batch", item["id"])["display_id"], "b261026-001")
        self.assertFalse(batch_uses_short_ids(item["id"], self.root))
        self.assertFalse(batch_uses_short_ids("b261026-001", self.root))
        native = allocate_id("b", self.root, now=DAY)
        self.assertEqual(native, "b261026-002")
        self.assertTrue(batch_uses_short_ids(native, self.root))

    def test_native_short_ids_skip_aliases_and_display_purpose_cannot_replay_identity(self):
        items = [{"kind": "task", "scope": "batch", "id": "t261026-001"}]
        self.assertEqual(self.aliases.prepare_entries(items, now=DAY), [])
        self.assertEqual(self.aliases.resolve(items)[0]["display_id"], "t261026-001")
        real = allocate_id("b", self.root, key=["same"], now=DAY)
        alias = allocate_id("b", self.root, key=["same"], now=DAY, purpose="display")
        self.assertNotEqual(real, alias)
        self.assertFalse(batch_uses_short_ids(alias, self.root))

    def test_sequence_above_999_and_scoped_identity_are_unambiguous(self):
        self.records.put("short_id_counters", "tr:261026", {"sequence": 999})
        items = [{"kind": "trajectory", "scope": scope, "id": "same-trajectory"} for scope in ("batch-a", "batch-b")]
        self.records.put_many(self.aliases.prepare_entries(items, now=DAY))
        result = self.aliases.resolve(items)
        self.assertEqual([row["display_id"] for row in result], ["tr261026-1000", "tr261026-1001"])
        self.assertFalse(self.resolve("trajectory", "same-trajectory", "batch-c")["mapped"])
        self.assertEqual(DisplayIdentifierStore(self.root).resolve(items), result)

    def test_concurrent_run_creation_is_atomic_and_duplicates_never_backfill(self):
        def create(index):
            key = "eval_" + str(index)
            try:
                return DisplayIdentifierStore(self.root).create_run("model_iter_runs", key, {"run_id": key}, now=DAY)
            except RevisionConflict:
                return self.records.get("model_iter_runs", key)
        with ThreadPoolExecutor(max_workers=6) as pool:
            values = list(pool.map(create, list(range(12)) * 2))
        self.assertEqual(len(values), 24)
        aliases = self.records.list(DISPLAY_IDS)
        self.assertEqual(len(aliases), 12)
        self.assertEqual(len({row["display_id"] for row in aliases}), 12)
        self.records.put("model_iter_runs", "eval_historical", {"run_id": "eval_historical"})
        with self.assertRaises(RevisionConflict):
            self.aliases.create_run("model_iter_runs", "eval_historical", {"run_id": "eval_historical"}, now=LATER)
        self.assertFalse(self.resolve("run", "eval_historical")["mapped"])

    def test_run_transaction_failure_does_not_commit_alias_or_consume_number(self):
        original = self.aliases.records._save
        def fail_run(connection, namespace, key, payload, revision):
            if namespace == "collection_runs":
                raise OSError("simulated database interruption")
            return original(connection, namespace, key, payload, revision)
        with patch.object(self.aliases.records, "_save", side_effect=fail_run), self.assertRaises(OSError):
            self.aliases.create_run("collection_runs", "cr_failure", {"run_id": "cr_failure"}, now=DAY)
        self.assertEqual(self.records.list(DISPLAY_IDS), [])
        self.assertEqual(self.records.list("short_id_counters"), [])
        self.aliases.create_run("collection_runs", "cr_failure", {"run_id": "cr_failure"}, now=DAY)
        self.assertEqual(self.resolve("run", "cr_failure")["display_id"], "cr261026-001")

    def test_manual_batch_atomic_publish_preserves_content_identity_and_replay(self):
        store, content = ManualCollectionStore(self.root), workbook_bytes()
        with patch.object(store.artifacts.records, "put_many", side_effect=OSError("publish failed")):
            with self.assertRaises(OSError):
                store.register("original.xlsx", content, request_id="same-request")
        self.assertEqual(self.records.list(DISPLAY_IDS), [])
        self.assertEqual(self.records.list(DISPLAY_BATCHES), [])
        batch = store.register("original.xlsx", content, request_id="same-request")
        before = self.records.list(DISPLAY_IDS)
        self.assertEqual(store.register("original.xlsx", content, request_id="same-request"), batch)
        self.assertEqual(self.records.list(DISPLAY_IDS), before)
        self.assertEqual(Path(batch["workbook_path"]).read_bytes(), content)
        self.assertTrue(batch["batch_id"].startswith("manual_"))
        self.assertIn(batch["batch_id"], batch["filename"])
        self.assertEqual(batch["original_filename"], "original.xlsx")
        self.assertFalse(batch_uses_short_ids(batch["batch_id"], self.root))
        self.assertTrue(self.resolve("batch", batch["batch_id"])["mapped"])
        task = batch["snapshot"]["tasks"][0]
        self.assertEqual(task["collection_case_id"], "CASE-01")
        self.assertTrue(self.resolve("task", task["task_id"], batch["batch_id"])["mapped"])

    def test_historical_batch_reprocessing_does_not_backfill_but_new_run_gets_alias(self):
        batch, _ = seed_batch(self.root)
        run, _ = CollectionRunStore(self.root).create(batch["batch_id"], dispatch_key="new")
        self.assertTrue(self.resolve("run", run["collection_run_id"])["mapped"])
        self.assertFalse(self.resolve("batch", batch["batch_id"])["mapped"])
        payload = {"sheets": {SHEET: [{"task_id": "legacy-task", "trajectory_id": "tr_old"}]}}
        self.assertEqual(self.aliases.conversion_entries(batch["batch_id"], payload), [])
        self.assertFalse(self.resolve("task", "legacy-task", batch["batch_id"])["mapped"])

    def test_historical_manual_upload_replay_does_not_enroll_batch_or_tasks(self):
        store, content = ManualCollectionStore(self.root), workbook_bytes()
        with patch.object(DisplayIdentifierStore, "new_batch_entries", return_value=[]):
            batch = store.register("old.xlsx", content, request_id="legacy-upload")
        replay = store.register("old.xlsx", content, request_id="legacy-upload")
        self.assertEqual(replay, batch)
        self.assertEqual(self.records.list(DISPLAY_BATCHES), [])
        self.assertEqual(self.records.list(DISPLAY_IDS), [])
        run, _ = CollectionRunStore(self.root).create(batch["batch_id"], dispatch_key="new-run")
        self.assertTrue(self.resolve("run", run["collection_run_id"])["mapped"])
        self.assertFalse(self.resolve("batch", batch["batch_id"])["mapped"])

    def test_preprocessing_preserves_paths_and_distinguishes_same_source_in_two_runs(self):
        batch = ManualCollectionStore(self.root).register("original.xlsx", workbook_bytes(), request_id="manual")
        runs = CollectionRunStore(self.root)
        for request in ("first", "second"):
            run, _ = runs.create(batch["batch_id"], dispatch_key=request)
            entry = raw_trajectory(run, "CASE-01", "same-source-name")
            runs.complete(run["collection_run_id"], {"batch_id": batch["batch_id"], "trajectories": [entry]})
        original = {str(p): p.read_bytes() for p in (self.root / "raw").rglob("*") if p.is_file()}
        queue = ManualQueue()
        manager = PreprocessingJobManager(self.root, executor=queue, config_loader=lambda: copy.deepcopy(CONFIG),
                                          reviewer_factory=lambda **_: AcceptingReviewer())
        self.addCleanup(manager.shutdown)
        job = manager.submit(batch["batch_id"])
        queue.drain()
        done = manager.get(job["job_id"])
        self.assertEqual(done["status"], "succeeded", done.get("error"))
        aliases_by_stage = []
        for artifact in done["artifacts"]:
            rows = ArtifactStore(self.root).read_payload(artifact)["sheets"][SHEET]
            values = self.aliases.resolve([{"kind": "trajectory", "id": row["trajectory_id"], "scope": batch["batch_id"]} for row in rows])
            aliases_by_stage.append(values)
            self.assertTrue(all(row["mapped"] for row in values))
            self.assertEqual(len({row["display_id"] for row in values}), 2)
            self.assertTrue(all(row["trajectory_id"].startswith("tr_") for row in rows))
            self.assertTrue(all(row["collection_run_id"] in row["image"] for row in rows))
            self.assertTrue(all(row["logprobs_path"] == "" for row in rows))
        self.assertEqual(aliases_by_stage[0], aliases_by_stage[1])
        self.assertEqual(original, {str(p): p.read_bytes() for p in (self.root / "raw").rglob("*") if p.is_file()})
        before = self.records.list(DISPLAY_IDS)
        self.assertEqual(manager.submit(batch["batch_id"])["job_id"], job["job_id"])
        self.assertEqual(before, self.records.list(DISPLAY_IDS))

    def test_online_and_offline_evaluation_creation_register_alias_without_changing_dispatch(self):
        from backend.tests.test_model_iteration import ModelIterationTests
        from backend.tests.test_model_iteration_offline import OfflineEvaluationTests
        for fixture_type in (ModelIterationTests, OfflineEvaluationTests):
            fixture = fixture_type()
            fixture.setUp()
            self.addCleanup(fixture.doCleanups)
            if hasattr(fixture, "make_preview"):
                fixture.make_preview()
            run = fixture.start()
            store = DisplayIdentifierStore(fixture.store.root)
            resolved = store.resolve([{"kind": "run", "id": run["run_id"]}])[0]
            self.assertRegex(resolved["display_id"], r"^eval\d{6}-\d{3,}$")
            self.assertTrue(run["run_id"].startswith("eval_"))
            self.assertNotIn("display_id", run["request"])
            again = fixture.start()
            self.assertEqual(run["run_id"], again["run_id"])
            self.assertEqual(store.resolve([{"kind": "run", "id": run["run_id"]}])[0], resolved)

    def test_http_bulk_contract_order_validation_and_no_allocation(self):
        self.aliases.create_run("collection_runs", "cr_long", {"run_id": "cr_long"}, now=DAY)
        app = FastAPI()
        app.include_router(display_identifiers_router.router)
        items = [{"kind": "run", "id": "cr_long"}, {"kind": "task", "id": "old", "scope": "batch"},
                 {"kind": "run", "id": "cr_long"}]
        before = self.records.database_path.read_bytes()
        with patch.object(display_identifiers_router, "DisplayIdentifierStore", return_value=self.aliases), TestClient(app) as client:
            response = client.post("/api/display-identifiers/resolve", json={"items": items})
            self.assertEqual(response.status_code, 200)
            self.assertEqual([row["id"] for row in response.json()["items"]], [item["id"] for item in items])
            self.assertEqual(response.json()["items"][0]["scope"], "")
            self.assertEqual(response.json()["items"][0]["display_id"], "cr261026-001")
            self.assertEqual(client.post("/api/display-identifiers/resolve", json={"items": [{"kind": "phone", "id": "x"}]}).status_code, 422)
            self.assertEqual(client.post("/api/display-identifiers/resolve", json={"items": [{"kind": "run", "id": "x", "scope": "batch"}]}).status_code, 422)
            self.assertEqual(client.post("/api/display-identifiers/resolve", json={"items": items * 334}).status_code, 422)
        self.assertEqual(before, self.records.database_path.read_bytes())


if __name__ == "__main__":
    unittest.main()
