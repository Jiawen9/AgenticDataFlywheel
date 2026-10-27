from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from backend.collection_runs import CollectionRunError, CollectionRunStore
from backend.collection_transfer import CollectionTransferManager
from backend.data_store import RecordStore
from backend.remote_sync import RemoteSyncPolicy
from backend.tests.test_collection_runs import seed_batch
from backend.tests.test_remote_sync import RemoteFailure


class CollectionSyncPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        seed_batch(self.root)
        self.runs = CollectionRunStore(self.root)
        self.run, _ = self.runs.create("batch-one", dispatch_key="request", metadata={
            "run_mode": "generate", "result_transport": "http", "phone_ids": ["phone"]})
        self.run_id = self.run["collection_run_id"]
        self.records = RecordStore(self.root)
        self.records.update("collection_runs", self.run_id, lambda run: run.update(created_at=9000))
        self.now = 10000.0
        self.policy = RemoteSyncPolicy(self.records, clock=lambda: self.now)
        self.remote = Mock()
        self.remote.get_run.side_effect = RemoteFailure()
        self.manager = CollectionTransferManager(self.root, self.runs, self.remote, sync_policy=self.policy)
        self.addCleanup(self.manager.close)

    def test_recover_missing_run_pauses_without_business_status_mutation(self):
        for _ in range(3):
            self.assertEqual(self.manager.recover(), [])
            self.now += 301
        self.assertEqual(self.remote.get_run.call_count, 3)
        self.assertEqual(self.policy.view("collection", self.run_id)["state"], "paused")
        self.assertEqual(self.runs.get(self.run_id)["status"], "dispatching")
        self.assertEqual(self.manager.recover(), [])
        restarted = CollectionTransferManager(self.root, self.runs, self.remote,
            sync_policy=RemoteSyncPolicy(self.records, clock=lambda: self.now))
        self.addCleanup(restarted.close)
        restarted.recover()
        self.assertEqual(self.remote.get_run.call_count, 3)

    def test_manual_retry_and_page_calls_obey_same_policy(self):
        self.manager.recover()
        for _ in range(4):
            with self.assertRaises(CollectionRunError):
                self.manager.sync(self.run_id, force=False)
        self.assertEqual(self.remote.get_run.call_count, 1)
        with self.assertRaises(RemoteFailure):
            self.manager.sync(self.run_id)
        self.assertEqual(self.remote.get_run.call_count, 2)
        self.assertEqual(self.policy.view("collection", self.run_id)["failure_count"], 1)

    def test_success_terminal_missing_manifest_is_limited_final_sync_failure(self):
        self.remote.get_run.side_effect = None
        self.remote.get_run.return_value = {"collection_run_id": self.run_id, "batch_id": "batch-one",
            "run_mode": "generate", "status": "completed"}
        for _ in range(8):
            self.manager.recover()
            self.now += 301
        self.assertEqual(self.remote.get_run.call_count, 8)
        self.assertEqual(self.policy.view("collection", self.run_id)["pause_reason"], "final_sync_exhausted")
        self.assertEqual(self.runs.get(self.run_id)["status"], "dispatching")
        self.manager.recover()
        self.assertEqual(self.remote.get_run.call_count, 8)

    def test_pre_dispatch_failure_is_not_polled(self):
        self.records.update("collection_runs", self.run_id,
            lambda run: run.update(status="failed", dispatch_attempted=False))
        self.assertEqual(self.manager.recover(), [])
        self.remote.get_run.assert_not_called()

    def test_remote_identity_mismatch_does_not_confirm_terminal_state(self):
        self.remote.get_run.side_effect = None
        self.remote.get_run.return_value = {"collection_run_id": "wrong", "batch_id": "batch-one",
            "run_mode": "generate", "status": "completed"}
        self.manager.recover()
        state = self.policy.view("collection", self.run_id)
        self.assertIsNone(state["remote_status"])
        self.assertEqual(state["final_failure_count"], 0)


if __name__ == "__main__":
    unittest.main()
