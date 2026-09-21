from __future__ import annotations

import copy
import hashlib
import io
import json
import shutil
import stat
import tempfile
import unittest
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from backend.batch_lifecycle import BatchPublishedError
from backend.collection_runs import CollectionRunError, CollectionRunStore
from backend.collection_transfer import CollectionTransferManager
from backend.data_store import RecordStore
from backend.manual_collection import ManualCollectionStore
from backend.preprocessing_service import convert_input
from backend.tests.test_collection_runs import raw_trajectory, seed_batch
from backend.tests.test_manual_collection import workbook_bytes


class MockRemote:
    def __init__(self, root, run, entries=None, errors=None):
        self.root, self.run = root, run
        self.get_calls = self.download_calls = 0
        self.manifest = {"schema_version": 1, "batch_id": run["batch_id"], "collection_run_id": run["collection_run_id"],
            "run_mode": "generate", "trajectories": entries or [], "errors": errors or [], "reports": []}
        self.status = "completed"
        self.build_archive()

    def build_archive(self, extra=None):
        target = io.BytesIO()
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
            for entry in self.manifest["trajectories"]:
                for file in entry["files"]:
                    archive.write(self.root / file["path"], file["path"])
            for name, value in (extra or {}).items():
                archive.writestr(name, value)
        self.archive = target.getvalue()
        self.manifest["archive"] = {"size": len(self.archive), "sha256": hashlib.sha256(self.archive).hexdigest()}

    def get_run(self, run_id):
        self.get_calls += 1
        return {"run_id": run_id, "collection_run_id": run_id, "batch_id": self.run["batch_id"], "run_mode": "generate",
                "status": self.status, "errors": self.manifest.get("errors", []), "manifest": copy.deepcopy(self.manifest)}

    def download_archive(self, run_id, destination):
        self.download_calls += 1
        destination.write_bytes(self.archive)


class CollectionTransferTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "platform"
        self.remote_root = Path(self.temp.name) / "collector"
        seed_batch(self.root)
        self.runs = CollectionRunStore(self.root)
        self.run, _ = self.runs.create("batch-one", dispatch_key="request", metadata={"run_mode": "generate", "result_transport": "http", "phone_ids": ["192.0.2.1:5555", "phone-two"]})
        self.run_id = self.run["collection_run_id"]
        remote_run = {**self.run, "output_dir": str(self.remote_root)}
        first = raw_trajectory(remote_run, name="device1__same")
        first.update(phone_id="192.0.2.1:5555", source_trajectory_id="same")
        second = raw_trajectory(remote_run, name="device2__same")
        second.update(phone_id="phone-two", source_trajectory_id="same")
        self.remote = MockRemote(self.remote_root, self.run, [first, second])
        self.manager = CollectionTransferManager(self.root, self.runs, self.remote)
        self.addCleanup(self.manager.close)

    def test_complete_two_devices_same_original_trajectory_and_replay_without_download(self):
        result = self.manager.sync(self.run_id)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["transfer_status"], "completed")
        self.assertEqual(len(result["trajectories"]), 2)
        self.assertEqual({entry["source_trajectory_id"] for entry in result["trajectories"]}, {"same"})
        snapshot = self.runs.ready_input("batch-one")
        payload, counts = convert_input(snapshot, lambda _: None, data_root=self.root)
        self.assertEqual(counts["trajectory_count"], 2)
        self.assertEqual(len({row["trajectory_id"] for row in payload["sheets"]["VLA trajectories"]}), 2)
        self.assertEqual(self.manager.sync(self.run_id), result)
        self.assertEqual(self.remote.download_calls, 1)
        self.assertFalse(self.manager._temporary(self.run_id).exists())

    def test_concurrent_sync_only_downloads_once(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            values = list(pool.map(lambda _: self.manager.sync(self.run_id), range(4)))
        self.assertEqual({value["status"] for value in values}, {"completed"})
        self.assertEqual(self.remote.download_calls, 1)

    def test_running_idle_does_not_mark_complete_and_missing_manifest_is_failure(self):
        self.remote.status = "running"
        result = self.manager.sync(self.run_id)
        self.assertEqual(result["transfer_status"], "waiting")
        self.assertNotEqual(result["status"], "completed")
        self.assertEqual(self.remote.download_calls, 0)
        with patch.object(self.remote, "get_run", return_value={"collection_run_id": self.run_id, "batch_id": "batch-one", "run_mode": "generate", "status": "failed"}):
            result = self.manager.sync(self.run_id)
        self.assertEqual(result["transfer_status"], "remote_failed")
        with self.assertRaises(CollectionRunError):
            self.runs.ready_input("batch-one")

    def test_empty_terminal_manifest_is_remote_failure_not_completed_collection(self):
        for state in ("failed", "partial", "succeeded"):
            with self.subTest(state=state):
                self.remote.status = state
                self.remote.manifest["trajectories"] = []
                self.remote.manifest["errors"] = [{"error": "No usable final trajectory"}]
                self.remote.build_archive()
                result = self.manager.sync(self.run_id)
                self.assertEqual(result["status"], "failed")
                self.assertEqual(result["transfer_status"], "remote_failed")
                self.assertEqual(result["remote_status"], state)
                self.assertEqual(result["remote_errors"], self.remote.manifest["errors"])
                self.assertEqual(self.remote.download_calls, 0)
                self.assertEqual(list(Path(self.run["output_dir"]).iterdir()), [])
                with self.assertRaises(CollectionRunError):
                    self.runs.ready_input("batch-one")

    def test_partial_results_preserve_errors_and_valid_trajectories(self):
        self.remote.status = "partial"
        self.remote.manifest["errors"] = [{"collection_case_id": "task-two", "error": "device disconnected"}]
        result = self.manager.sync(self.run_id)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["remote_status"], "partial")
        self.assertEqual(result["errors"][0]["collection_case_id"], "task-two")

    def test_hash_mismatch_and_unknown_case_phone_or_identity_rejected(self):
        original = copy.deepcopy(self.remote.manifest)
        changes = [lambda m: m["archive"].update(sha256="0" * 64),
                   lambda m: m["trajectories"][0].update(collection_case_id="forged"),
                   lambda m: m["trajectories"][0].update(phone_id="forged"),
                   lambda m: m.update(batch_id="wrong"),
                   lambda m: m["trajectories"][0]["files"][0].update(sha256="0" * 64)]
        for change in changes:
            self.remote.manifest = copy.deepcopy(original)
            change(self.remote.manifest)
            with self.assertRaises(CollectionRunError):
                self.manager.sync(self.run_id)
            self.assertNotEqual(self.runs.get(self.run_id)["status"], "completed")
            self.assertEqual(list(Path(self.run["output_dir"]).iterdir()), [])

    def test_zip_traversal_extra_duplicate_symlink_and_expansion_limit_rejected(self):
        for name in ("../outside.txt", "extra.txt", self.remote.manifest["trajectories"][0]["files"][0]["path"]):
            self.remote.build_archive({name: b"bad"})
            with self.assertRaises(CollectionRunError):
                self.manager.sync(self.run_id)
        self.remote.build_archive()
        target = io.BytesIO(self.remote.archive)
        with zipfile.ZipFile(target, "a") as archive:
            info = zipfile.ZipInfo("link")
            info.create_system = 3
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(info, "outside")
        self.remote.archive = target.getvalue()
        self.remote.manifest["archive"] = {"size": len(self.remote.archive), "sha256": hashlib.sha256(self.remote.archive).hexdigest()}
        with self.assertRaises(CollectionRunError):
            self.manager.sync(self.run_id)
        self.remote.build_archive()
        self.manager.max_expanded_bytes = 1
        with self.assertRaises(CollectionRunError):
            self.manager.sync(self.run_id)
        self.assertFalse((Path(self.temp.name) / "outside.txt").exists())

    def test_rename_then_database_failure_recovers_without_redownload_or_overwrite(self):
        original_complete = self.runs.complete
        with patch.object(self.runs, "complete", side_effect=OSError("database unavailable")):
            with self.assertRaises(OSError):
                self.manager.sync(self.run_id)
        self.assertTrue(any(Path(self.run["output_dir"]).iterdir()))
        journal = self.manager.records.get("collection_transfers", self.run_id)
        self.assertEqual(journal["status"], "verified")
        result = CollectionTransferManager(self.root, CollectionRunStore(self.root), self.remote).sync(self.run_id)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(self.remote.download_calls, 1)

    def test_uncertain_database_acknowledgement_preserves_results_and_finishes_journal(self):
        original_complete = self.runs.complete
        def committed(run_id, manifest):
            original_complete(run_id, manifest)
            raise OSError("lost acknowledgement")
        with patch.object(self.runs, "complete", side_effect=committed):
            with self.assertRaises(OSError):
                self.manager.sync(self.run_id)
        self.assertEqual(self.runs.get(self.run_id)["status"], "completed")
        result = self.manager.sync(self.run_id)
        self.assertEqual(result["transfer_status"], "completed")
        self.assertEqual(self.remote.download_calls, 1)
        self.assertFalse(self.manager._temporary(self.run_id).exists())

    def test_published_batches_reject_late_return_without_network_or_mutation(self):
        RecordStore(self.root).put("batch_lifecycle", "batch-one", {"batch_id": "batch-one", "status": "published", "release_id": "release"})
        before = self.runs.get(self.run_id)
        with self.assertRaises(BatchPublishedError):
            self.manager.sync(self.run_id)
        self.assertEqual(self.runs.get(self.run_id), before)
        self.assertEqual(self.remote.get_calls, 0)
        self.assertEqual(self.manager.recover(), [])

    def test_existing_unregistered_files_are_never_overwritten(self):
        path = Path(self.run["output_dir"]) / "keep.txt"
        path.write_text("keep")
        with self.assertRaises(CollectionRunError):
            self.manager.sync(self.run_id)
        self.assertEqual(path.read_text(), "keep")
        self.assertNotEqual(self.runs.get(self.run_id)["status"], "completed")

    def test_manual_collection_round_trip_preserves_classification_and_source(self):
        batch = ManualCollectionStore(self.root).register("manual.xlsx", workbook_bytes(), "", "manual")
        run, _ = self.runs.create(batch["batch_id"], dispatch_key="manual", metadata={"result_transport": "http", "phone_ids": ["phone"]})
        remote_root = self.remote_root / "manual"
        entry = raw_trajectory({**run, "output_dir": str(remote_root)}, "CASE-01", "phone__roll")
        entry["phone_id"] = "phone"
        remote = MockRemote(remote_root, run, [entry])
        result = CollectionTransferManager(self.root, self.runs, remote).sync(run["collection_run_id"])
        self.assertEqual(result["trajectories"][0]["source_kind"], "manual_collection")
        self.assertIsNone(result["trajectories"][0]["source_result_id"])
        self.assertEqual(self.runs.batch(batch["batch_id"])["snapshot"]["tasks"][0]["scene"], "工具")

    def test_local_interruption_and_late_dispatch_never_restore_the_run(self):
        self.manager.records.update("collection_runs", self.run_id, lambda run: run.update(status="interrupted"))
        before = self.runs.get(self.run_id)
        self.assertEqual(self.manager.sync(self.run_id), before)
        self.assertEqual(self.manager.recover(), [])
        self.assertEqual(self.remote.get_calls, 0)
        self.assertEqual(self.runs.dispatched(self.run_id, {"ok": True})["status"], "interrupted")
        self.assertEqual(self.runs.dispatch_failed(self.run_id, "late timeout")["status"], "interrupted")
        with self.assertRaises(CollectionRunError):
            self.runs.complete(self.run_id, self.remote.manifest)
        self.manager.records.put("batch_lifecycle", "batch-one", {"batch_id": "batch-one", "status": "published"})
        with self.assertRaises(BatchPublishedError):
            self.runs.dispatch_failed(self.run_id, "late failure")
        with self.assertRaises(BatchPublishedError):
            self.runs.dispatched(self.run_id, {"ok": True})

    def test_process_exit_before_file_rename_recovers_verified_staging(self):
        class ProcessExit(BaseException):
            pass
        with patch.object(Path, "rename", side_effect=ProcessExit()):
            with self.assertRaises(ProcessExit):
                self.manager.sync(self.run_id)
        self.assertEqual(self.manager.records.get("collection_transfers", self.run_id)["status"], "verified")
        self.assertTrue((self.manager._temporary(self.run_id) / "files").is_dir())
        result = self.manager.sync(self.run_id)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(self.remote.download_calls, 1)

    def test_recovery_only_polls_http_production_runs(self):
        local, _ = self.runs.create("batch-one", dispatch_key="local")
        results = self.manager.recover()
        self.assertEqual(len(results), 1)
        self.assertEqual(self.runs.get(local["collection_run_id"])["status"], "dispatching")
        self.assertEqual(self.remote.get_calls, 1)


if __name__ == "__main__":
    unittest.main()
