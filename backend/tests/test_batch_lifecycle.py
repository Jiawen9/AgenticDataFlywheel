"""Publication is the final, atomic state transition of every selected batch."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import sqlite3
import threading
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend import batch_api, data_registry_api
from backend.batch_lifecycle import BatchNotReadyError, BatchPublishedError, lifecycle, install_lifecycle_handlers
from backend.data_store import ArtifactStore, RecordStore
from backend.data_store import migrate_batch_lifecycle as migration
from backend.tests import test_data_publishing as publishing_fixtures
from contextlib import closing
from backend.trajectory_correction.session_state import session_fingerprint


class BatchLifecycleTests(unittest.TestCase):
    setUp = publishing_fixtures.DatasetPublishingTests.setUp
    tearDown = publishing_fixtures.DatasetPublishingTests.tearDown
    add_session = publishing_fixtures.DatasetPublishingTests.add_session
    registry = publishing_fixtures.DatasetPublishingTests.registry

    def test_publish_requires_all_collected_tasks_and_reviewed_drafts(self):
        session = self.add_session()
        root = self.root / "data"
        store = ArtifactStore(root)
        store.publish(session["batch_id"], "00_collection", payload={"snapshot": {"tasks": [
            {"task_id": "TASK-A"}, {"task_id": "TASK-B"}]}})
        with self.assertRaises(BatchNotReadyError) as caught:
            self.registry().create("incomplete", [session["session_id"]])
        self.assertIn("TASK-B", str(caught.exception))
        self.assertEqual(lifecycle(session["batch_id"], root)["status"], "active")
        self.assertEqual(self.registry().list_releases(), [])
        store.publish(session["batch_id"], "00_collection", payload={"snapshot": {"tasks": [{"task_id": "TASK-A"}]}})
        session["pending_review"] = {"step": {"thought": "expert"}}
        with self.assertRaisesRegex(BatchNotReadyError, "人工修改"):
            self.registry().create("review", [session["session_id"]])
        self.assertFalse(session.get("published"))

    def test_new_completed_task_requires_refreshing_correction_before_publish(self):
        from backend.tests.lifecycle_fixtures import seed_publishable_batch
        session = self.add_session()
        expanded = deepcopy(session)
        expanded["selection"]["tasks"].append({"task_id": "TASK-B", "trajectory_count": 1})
        expanded.pop("task_fingerprints")
        seed_publishable_batch(self.root / "data", session["batch_id"], expanded)
        with self.assertRaisesRegex(BatchNotReadyError, "修正会话"):
            self.registry().create("stale selection", [session["session_id"]])
        self.assertEqual(lifecycle(session["batch_id"], self.root / "data")["status"], "active")

    def test_pending_dependency_recovery_blocks_publish(self):
        session = self.add_session()
        RecordStore(self.root / "data").put("batch_invalidations", session["batch_id"],
            {"batch_id": session["batch_id"], "quality": ["TASK-A"]})
        with self.assertRaisesRegex(BatchNotReadyError, "同步"):
            self.registry().create("not recovered", [session["session_id"]])

    def test_conversion_replaced_without_annotation_cannot_publish_old_results(self):
        session = self.add_session()
        store = ArtifactStore(self.root / "data")
        conversion = store.read_payload(store.get(session["batch_id"], "01_conversion"))
        conversion["sheets"]["VLA trajectories"][0]["action"] = "new conversion"
        store.publish(session["batch_id"], "01_conversion", payload=conversion)
        with self.assertRaisesRegex(BatchNotReadyError, "当前转换结果"):
            self.registry().create("old annotation", [session["session_id"]])
        self.assertEqual(lifecycle(session["batch_id"], self.root / "data")["status"], "active")

    def test_stale_or_partial_quality_cannot_close_batch(self):
        session = self.add_session()
        root = self.root / "data"
        store = ArtifactStore(root)
        ref = store.get(session["batch_id"], "05_quality")
        quality = store.read_payload(ref)
        quality["tasks"][0]["evaluations"].pop()
        store.publish(session["batch_id"], "05_quality", payload=quality)
        with self.assertRaisesRegex(BatchNotReadyError, "质检"):
            self.registry().create("partial", [session["session_id"]])
        annotated = store.read_payload(store.get(session["batch_id"], "02_annotation"))
        annotated["sheets"]["VLA trajectories"][0]["actions_box"] = "changed"
        store.publish(session["batch_id"], "02_annotation", payload=annotated)
        with self.assertRaisesRegex(BatchNotReadyError, "建树"):
            self.registry().create("stale", [session["session_id"]])
        self.assertFalse(session.get("published"))

    def test_running_operations_block_publish_but_failed_jobs_do_not(self):
        session = self.add_session()
        registry = self.registry()
        records = RecordStore(self.root / "data")
        for namespace in ("collection_runs", "preprocessing_jobs", "tree_jobs", "quality_jobs",
                          "correction_cot_jobs", "task_generation.jobs", "batch_operations"):
            with self.subTest(namespace=namespace):
                key = "active-job"
                job = {"job_id": key, "batch_id": session["batch_id"], "status": "running"}
                records.put(namespace, key, job)
                with self.assertRaisesRegex(BatchNotReadyError, "正在执行"):
                    registry.create("busy", [session["session_id"]])
                self.assertFalse(session.get("published"))
                records.put(namespace, key, {**job, "status": "failed"})
        registry.create("complete without mandatory COT", [session["session_id"]])
        self.assertTrue(session["published"])

    def test_multibatch_database_failure_rolls_back_every_batch_and_file(self):
        one, two = self.add_session("a"*16), self.add_session("b"*16)
        registry = self.registry()
        root = self.root / "data"
        originals = deepcopy(self.sessions)
        real_save = RecordStore._save
        calls = []
        def fail_last(connection, namespace, key, payload, revision):
            calls.append(namespace)
            if namespace == "batch_lifecycle" and key == two["batch_id"]:
                raise sqlite3.OperationalError("injected commit failure")
            return real_save(connection, namespace, key, payload, revision)
        with patch.object(RecordStore, "_save", side_effect=fail_last):
            with self.assertRaisesRegex(sqlite3.OperationalError, "injected"):
                registry.create("atomic", [one["session_id"], two["session_id"]])
        self.assertIn("dataset_releases", calls)
        self.assertIn("batch_lifecycle", calls)
        self.assertEqual(self.sessions, originals)
        self.assertEqual(registry.list_releases(), [])
        self.assertEqual(RecordStore(root).list("correction_sessions"), [])
        self.assertEqual(list((root / "releases").iterdir()), [])
        for item in (one, two):
            self.assertEqual(lifecycle(item["batch_id"], root)["status"], "active")
        release = registry.create("retry", [one["session_id"], two["session_id"]])
        self.assertEqual(set(release["batch_ids"]), {one["batch_id"], two["batch_id"]})
        for item in (one, two):
            self.assertEqual(lifecycle(item["batch_id"], root)["release_id"], release["release_id"])

    def test_concurrent_publish_and_late_artifact_write_cannot_reopen(self):
        session = self.add_session()
        root = self.root / "data"
        registry = self.registry()
        # Each publisher has an independent process-local registry lock.
        other = self.registry()
        barrier = threading.Barrier(2)
        def publish(instance):
            barrier.wait(timeout=5)
            try:
                return instance.create("once", [session["session_id"]])
            except BatchPublishedError:
                return None
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(publish, (registry, other)))
        self.assertEqual(sum(value is not None for value in results), 1)
        store = ArtifactStore(root)
        previous = store.get(session["batch_id"], "04_tree")
        payload = store.read_payload(previous)
        with self.assertRaises(BatchPublishedError):
            store.publish(session["batch_id"], "04_tree", payload=payload)
        self.assertEqual(store.get(session["batch_id"], "04_tree"), previous)
        self.assertEqual(len(self.registry().list_releases()), 1)

    def test_closed_workspaces_return_release_identity_but_artifacts_download(self):
        session = self.add_session()
        other = self.add_session("b"*16)
        registry = self.registry()
        release = registry.create("closed", [session["session_id"]])
        store = ArtifactStore(self.root / "data")
        app = FastAPI()
        install_lifecycle_handlers(app)
        app.include_router(data_registry_api.router)
        app.include_router(batch_api.router)
        with patch.object(data_registry_api, "store", store), patch.object(batch_api, "store", store), TestClient(app) as client:
            self.assertEqual([x["batch_id"] for x in client.get("/api/data-batches").json()["batches"]], [other["batch_id"]])
            prefix = "/api/data-batches/" + session["batch_id"]
            state = client.get(prefix + "/lifecycle")
            self.assertEqual(state.headers["cache-control"], "no-store")
            self.assertEqual(state.json()["release_id"], release["release_id"])
            for suffix in ("/tree", "/quality", "/tasks/TASK-A/tree", "/tasks/TASK-A/quality"):
                response = client.get(prefix + suffix)
                self.assertEqual(response.status_code, 409)
                self.assertEqual(response.json()["detail"]["code"], "batch_published")
                self.assertEqual(response.json()["detail"]["release_id"], release["release_id"])
            detail = client.get(prefix + "/artifacts/04_tree")
            self.assertEqual(detail.status_code, 200)
            # The canonical JSON remains accessible via the retained manifest.
            manifest = store.get(session["batch_id"], "04_tree")
            for item in manifest["files"]:
                name = item["path"].split("/")[-1] if "path" in item else item["name"]
                self.assertEqual(client.get(prefix + "/artifacts/04_tree/files/" + name).status_code, 200)


class LifecycleBackfillTests(unittest.TestCase):
    setUp = publishing_fixtures.DatasetPublishingTests.setUp
    tearDown = publishing_fixtures.DatasetPublishingTests.tearDown
    add_session = publishing_fixtures.DatasetPublishingTests.add_session
    registry = publishing_fixtures.DatasetPublishingTests.registry

    def legacy_release(self):
        session = self.add_session()
        registry = self.registry()
        release = registry.create("legacy", [session["session_id"]])
        records = RecordStore(self.root / "data")
        records.delete("batch_lifecycle", session["batch_id"])
        return session, release, records

    def test_backfill_backs_up_database_and_preserves_all_retained_files(self):
        session, release, records = self.legacy_release()
        root = self.root / "data"
        before = migration.inventory(root)
        release_before = records.get("dataset_releases", release["release_id"])
        self.assertEqual(len(migration.check(root)["candidates"]), 1)
        result = migration.apply(root)
        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["closed_batch_ids"], [session["batch_id"]])
        from pathlib import Path
        backup = Path(result["backup"])
        self.assertEqual(migration.sha(backup), result["backup_sha256"])
        with closing(sqlite3.connect(backup)) as connection:
            self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(connection.execute("SELECT count(*) FROM records WHERE namespace = 'batch_lifecycle'").fetchone()[0], 0)
        self.assertEqual(migration.inventory(root), before)
        self.assertEqual(records.get("dataset_releases", release["release_id"]), release_before)
        self.assertEqual(migration.apply(root)["status"], "no_changes")

    def test_backfill_preserves_modified_incomplete_or_busy_legacy_work(self):
        session, release, records = self.legacy_release()
        root = self.root / "data"
        saved = records.get("correction_sessions", session["session_id"])
        saved["manual_revision"] = 2
        saved["row_edits"] = {"2": {"thought": "unpublished work"}}
        records.put("correction_sessions", session["session_id"], saved)
        report = migration.apply(root)
        self.assertEqual(report["status"], "no_changes")
        self.assertIn("旧发布后有修改", " ".join(report["skipped"][0]["reasons"]))
        self.assertEqual(lifecycle(session["batch_id"], root)["status"], "active")
        saved["published_content_fingerprint"] = session_fingerprint(saved)
        records.put("correction_sessions", session["session_id"], saved)
        ArtifactStore(root).publish(session["batch_id"], "00_collection", payload={"snapshot": {"tasks": [{"task_id": "unfinished"}]}})
        self.assertIn("unfinished", str(migration.check(root)["skipped"]))
        self.assertEqual(lifecycle(session["batch_id"], root)["status"], "active")


    def test_file_verification_failure_rolls_back_lifecycle_with_backup_retained(self):
        session, release, records = self.legacy_release()
        root = self.root / "data"
        before = migration.inventory(root)
        with patch.object(migration, "inventory", side_effect=[before, before, {**before, "unexpected": {}}]):
            with self.assertRaisesRegex(ValueError, "发生变化"):
                migration.apply(root)
        self.assertEqual(lifecycle(session["batch_id"], root)["status"], "active")
        reports = list((root.parent / ".batch-publication-migration").rglob("report.json"))
        import json
        self.assertEqual(json.loads(reports[0].read_text(encoding="utf-8"))["status"], "rolled_back")
