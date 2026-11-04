from __future__ import annotations

import errno
import sqlite3
import threading
import unittest
from unittest.mock import patch

from backend.collection_artifact_transfer import ArtifactTransfer
from backend.collection_receipt_storage import storage_failure
from backend.phone_factory import PhoneFactoryError
from backend.tests import test_collection_receipts as fixtures


class ReceiptStorageTests(unittest.TestCase):
    setUp = fixtures.ReceiptTests.setUp
    rewrite = fixtures.ReceiptTests.rewrite

    def attach_control(self):
        self.controls = []
        def control(run_id, paused):
            self.controls.append((run_id, paused))
            return {"ok": True, "receipt_storage": {"paused": paused}}
        self.remote.set_receipt_storage = control

    def test_local_disk_failure_pauses_then_successful_receipt_resumes_exact_run(self):
        self.attach_control()
        key = self.remote.add()
        with patch.object(self.manager, "_extract", side_effect=OSError(errno.ENOSPC, "disk full")):
            saved = self.manager.sync(self.run_id)
        self.assertEqual(self.controls, [(self.run_id, True)])
        self.assertEqual(saved["transfer_status"], "retry_wait")
        self.assertEqual(self.manager.artifact_rows(self.run_id)[0]["receipt_status"], "retry_wait")
        self.manager.sync(self.run_id)
        self.assertEqual(self.controls, [(self.run_id, True), (self.run_id, False)])
        state = self.manager.records.get("collection_receipt_storage", self.run_id)
        self.assertEqual((state["paused"], state["pending"]), (False, False))
        self.assertEqual(self.remote.downloads.count(key), 2)

    def test_typed_cli_storage_error_is_recoverable_not_quarantined(self):
        self.attach_control()
        self.remote.add()
        with patch.object(self.remote, "download_collection_artifact", side_effect=PhoneFactoryError(
                "local write failed", 502, remote_kind="storage")):
            self.manager.sync(self.run_id)
        self.assertEqual(self.controls, [(self.run_id, True)])
        self.assertEqual(self.manager.artifact_rows(self.run_id)[0]["receipt_status"], "retry_wait")

    def test_network_failure_never_pauses_or_quarantines(self):
        self.attach_control()
        self.remote.add()
        with patch.object(self.remote, "download_collection_artifact", side_effect=PhoneFactoryError(
                "connection failed", 502, remote_kind="connection")):
            self.manager.sync(self.run_id)
        self.assertEqual(self.controls, [])
        self.assertEqual(self.manager.artifact_rows(self.run_id)[0]["receipt_status"], "retry_wait")

    def test_database_full_still_attempts_remote_pause(self):
        self.attach_control()
        # The exception escapes the batch lock before the pause call. Even the
        # dedicated pause receipt cannot be persisted in this failure scenario.
        with patch.object(self.manager.records, "put", side_effect=sqlite3.OperationalError("database or disk is full")):
            with self.assertRaises(sqlite3.OperationalError):
                ArtifactTransfer(self.manager).sync(self.run)
        self.assertEqual(self.controls, [(self.run_id, True)])
        self.assertTrue(self.manager._receipt_storage_states[self.run_id]["paused"])

    def test_failed_pause_control_remains_pending_and_next_round_recovers(self):
        self.attach_control()
        self.remote.add()
        with patch.object(self.remote, "set_receipt_storage", side_effect=TimeoutError("pause timeout")), patch.object(
                self.manager, "_extract", side_effect=OSError(errno.ENOSPC, "disk full")):
            self.manager.sync(self.run_id)
        state = self.manager.records.get("collection_receipt_storage", self.run_id)
        self.assertEqual((state["paused"], state["pending"]), (True, True))
        self.assertIn("disk full", state["error"])
        self.assertIn("timeout", state["notification_error"])
        self.manager.sync(self.run_id)
        self.assertEqual(self.controls, [(self.run_id, False)])
        self.assertFalse(self.manager.records.get("collection_receipt_storage", self.run_id)["pending"])

    def test_restart_recovers_remote_pause_if_local_receipt_was_unwritable(self):
        self.attach_control()
        self.remote.add()
        self.remote.get_run = lambda run_id: {"run_id": run_id, "run_mode": "generate", "agent_type": "newagent",
            "status": "running", "receipt_storage": {"paused": True, "reason": "platform_storage_unavailable"}}
        self.manager.sync(self.run_id)
        self.assertEqual(self.controls, [(self.run_id, False)])
        self.assertTrue(self.manager.records.get("collection_receipt_storage", self.run_id)["recovered_from_remote"])

    def test_storage_classification_is_narrow_and_legacy_runs_never_pause(self):
        for exc in (OSError(errno.ENOSPC, "full"), PermissionError(errno.EACCES, "denied"),
                    sqlite3.OperationalError("disk I/O error")):
            self.assertTrue(storage_failure(exc))
        for exc in (TimeoutError(), ConnectionResetError(), sqlite3.OperationalError("database is locked")):
            self.assertFalse(storage_failure(exc))
        self.attach_control()
        self.run["request"]["metadata"]["collection_artifacts_version"] = 1
        from backend.collection_receipt_storage import ReceiptStorage
        self.assertFalse(ReceiptStorage(self.manager, self.run).on_error(OSError(errno.ENOSPC, "full")))
        self.assertEqual(self.controls, [])

    def test_slow_pause_request_does_not_block_other_runs(self):
        from backend.collection_receipt_storage import ReceiptStorage
        other, _ = self.runs.create("batch-one", dispatch_key="another-storage-run",
                                    metadata=self.run["request"]["metadata"])
        entered, release, second = threading.Event(), threading.Event(), threading.Event()
        def control(run_id, paused):
            if run_id == self.run_id:
                entered.set()
                release.wait(3)
            else:
                second.set()
            return {"ok": True}
        self.remote.set_receipt_storage = control
        first = threading.Thread(target=lambda: ReceiptStorage(self.manager, self.run).on_error(
            OSError(errno.ENOSPC, "full")))
        peer = threading.Thread(target=lambda: ReceiptStorage(self.manager, other).on_error(
            OSError(errno.ENOSPC, "full")))
        first.start()
        try:
            self.assertTrue(entered.wait(2))
            peer.start()
            self.assertTrue(second.wait(1))
        finally:
            release.set()
            first.join(3)
            if peer.ident is not None:
                peer.join(3)
        self.assertFalse(first.is_alive())
        self.assertFalse(peer.is_alive())


if __name__ == "__main__":
    unittest.main()
