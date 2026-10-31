from __future__ import annotations

import json
import os
import subprocess
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from backend.collection_runs import CollectionRunError
from backend.data_store.locking import batch_lock
from backend.phone_factory import PhoneFactoryError, _client_failure, run_client
from backend.remote_sync import RemoteSyncPolicy, SyncDeferred
from backend.tests import test_phone_factory_runtime as runtime_fixtures


class Dispatch413Tests(unittest.TestCase):
    """Use the real platform stores and dispatcher with a device-free CLI double."""

    request = runtime_fixtures.FactoryRuntimeTests.request

    def setUp(self):
        self.failure = {"ok": False, "output": "上传数据超过限制（HTTP 413）",
                        "remote_kind": "http", "remote_status": 413}
        runtime_fixtures.FactoryRuntimeTests.setUp(self)
        self.addCleanup(self.store.runtime.close)

    def client(self, args):
        if args[0] == "start-run" and self.failure is not None:
            self.calls.append(args)
            return dict(self.failure)
        result = runtime_fixtures.FactoryRuntimeTests.client(self, args)
        if args[0] == "capabilities":
            value = json.loads(result["output"])
            value["agent_types"]["pipelineagent"] = dict(value["agent_types"]["newagent"])
            result["output"] = json.dumps(value)
        return result

    def start_failed(self, mode="generate", request_id="request-1"):
        self.store.add_task(self.upload, mode=mode)
        with self.assertRaises(PhoneFactoryError) as caught:
            self.store.remote_start(self.request(request_id=request_id), mode=mode)
        namespace = "collection_runs" if mode == "generate" else "model_iter_runs"
        run = next(run for run in self.store.records.list(namespace)
                   if run.get("dispatch_error") == str(caught.exception))
        return run, caught.exception

    def test_only_start_run_remote_413_maps_to_413(self):
        original = self.store.run_client
        self.addCleanup(setattr, self.store, "run_client", original)
        for command, kind, status, not_sent, expected in (
                ("start-run", "http", 413, False, 413),
                ("run", "http", 413, False, 502),
                ("capabilities", "http", 413, False, 502),
                ("start-run", "http", 413, True, 502),
                ("start-run", "http", 409, False, 502),
                ("start-run", "http", 502, False, 502),
                ("start-run", "timeout", None, False, 502)):
            with self.subTest(command=command, status=status, not_sent=not_sent):
                self.store.run_client = lambda args: {"ok": False, "output": "failure",
                    "remote_kind": kind, "remote_status": status, "dispatch_not_sent": not_sent}
                with self.assertRaises(PhoneFactoryError) as caught:
                    self.store._remote_result([command])
                error = caught.exception
                self.assertEqual((error.status, error.remote_status, error.remote_command),
                                 (expected, status, command))
                self.assertEqual(error.dispatch_not_sent, not_sent)

    def test_not_sent_metadata_requires_literal_true(self):
        for flag, expected in ((True, True), ("true", False), (1, False), (False, False)):
            metadata = {"remote_kind": "client", "dispatch_not_sent": flag}
            result = _client_failure("__PHONE_FACTORY_ERROR__=" + json.dumps(metadata) + "\nfailed")
            self.assertEqual(result.get("dispatch_not_sent", False), expected)

    def test_cli_parent_budget_allows_capability_get_and_start_post(self):
        with patch.dict(os.environ, {"PHONE_FACTORY_TIMEOUT": "7", "PHONE_FACTORY_TRANSFER_TIMEOUT": "50"}), \
                patch("backend.phone_factory.subprocess.run") as process:
            process.return_value = subprocess.CompletedProcess([], 0, "{}", "")
            for command, expected in (("start-run", 19), ("capabilities", 12),
                                      ("run", 12), ("run-archive", 55), ("report-download", 55)):
                with self.subTest(command=command):
                    self.assertTrue(run_client([command])["ok"])
                    self.assertEqual(process.call_args.kwargs["timeout"], expected)

    def test_collection_rejection_survives_restart_and_blocks_manual_transfer(self):
        run, error = self.start_failed()
        run_id = run["collection_run_id"]
        self.assertEqual(error.status, 413)
        self.assertIs(run["dispatch_attempted"], True)
        self.assertIs(run["dispatch_rejected"], True)
        self.assertNotIn("dispatch_rejected", run["request"]["metadata"])
        restarted = RemoteSyncPolicy(self.store.records)
        self.assertFalse(restarted.due("collection", run))
        self.assertEqual(restarted.view("collection", run_id)["pause_reason"], "dispatch_rejected")
        self.assertEqual(self.store.runtime.transfer._candidates(), [])
        before = len(self.calls)
        with self.assertRaises(CollectionRunError) as caught:
            self.store.runtime.transfer.sync(run_id, force=True)
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(len(self.calls), before)
        self.assertIsNone(self.store.collection_runs.get(run_id).get("transfer_error"))

    def test_modeliter_rejects_both_consumers_and_retries_same_identity(self):
        run, _ = self.start_failed("modeliter")
        run_id, digest = run["run_id"], run["request_digest"]
        collection = self.store.collection_runs.get(run_id)
        self.assertIs(collection["dispatch_rejected"], True)
        for kind, current in (("evaluation", run), ("collection", collection)):
            self.assertFalse(self.store.runtime.sync_policy.due(kind, current))
        before = len(self.calls)
        with self.assertRaises(PhoneFactoryError) as caught:
            self.store.runtime.sync_evaluation(run_id, force=True)
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(len(self.calls), before)
        self.failure = None
        retried = self.store.remote_start(self.request(), mode="modeliter")
        self.assertEqual((retried["run_id"], retried["request_digest"]), (run_id, digest))
        for namespace in ("collection_runs", "model_iter_runs"):
            saved = self.store.records.get(namespace, run_id)
            self.assertIs(saved["dispatch_rejected"], False)
            self.assertNotIn("dispatch_rejection", saved)
        for kind in ("collection", "evaluation"):
            self.assertEqual(self.store.runtime.sync_policy.view(kind, run_id)["state"], "active")

    def test_repeated_413_then_acceptance_does_not_change_frozen_request(self):
        run, _ = self.start_failed()
        run_id, digest = run["collection_run_id"], run["request_digest"]
        with self.assertRaises(PhoneFactoryError):
            self.store.remote_start(self.request())
        self.assertIs(self.store.collection_runs.get(run_id)["dispatch_rejected"], True)
        self.failure = None
        result = self.store.remote_start(self.request())
        self.assertEqual((result["collection_run_id"], result["request_digest"]), (run_id, digest))
        self.assertIs(result["dispatch_rejected"], False)
        self.assertNotIn("dispatch_rejection", result)

    def test_pipeline_uses_the_same_rejection_contract(self):
        self.store.add_task(self.upload)
        with self.assertRaises(PhoneFactoryError) as caught:
            self.store.remote_start(self.request(agent_type="pipelineagent"))
        self.assertEqual(caught.exception.status, 413)
        run = self.store.records.list("collection_runs")[0]
        self.assertIs(run["dispatch_rejected"], True)
        self.assertEqual(run["request"]["metadata"]["agent_type"], "pipelineagent")

    def test_uncertain_prior_attempt_cannot_be_reclassified_by_later_413(self):
        self.failure = {"ok": False, "output": "timeout", "remote_kind": "timeout"}
        run, _ = self.start_failed()
        run_id = run["collection_run_id"]
        self.failure = {"ok": False, "output": "too large", "remote_kind": "http", "remote_status": 413}
        with self.assertRaises(PhoneFactoryError):
            self.store.remote_start(self.request())
        current = self.store.collection_runs.get(run_id)
        self.assertIsNot(current.get("dispatch_rejected"), True)
        self.assertTrue(self.store.runtime.sync_policy.due("collection", current))

    def test_network_server_and_conflict_errors_remain_recoverable(self):
        for index, (kind, status) in enumerate((("timeout", None), ("connection", None),
                                             ("http", 500), ("http", 502), ("http", 409))):
            with self.subTest(kind=kind, status=status):
                self.failure = {"ok": False, "output": f"failure-{index}", "remote_kind": kind,
                                "remote_status": status}
                run, _ = self.start_failed(request_id=f"request-{index}")
                self.assertIsNot(run.get("dispatch_rejected"), True)
                self.assertTrue(self.store.runtime.sync_policy.due("collection", run))

    def test_confirmed_not_sent_preflight_failure_does_not_poll(self):
        self.failure = {"ok": False, "output": "旧采集端不支持大清单文件，请升级",
                        "remote_kind": "client", "dispatch_not_sent": True}
        run, error = self.start_failed()
        self.assertEqual(error.status, 502)
        self.assertIs(run["dispatch_rejected"], True)
        self.assertIs(run["dispatch_rejection"]["dispatch_not_sent"], True)
        self.assertIsNone(run["dispatch_rejection"]["remote_status"])

    def test_historical_missing_attempt_evidence_is_not_rejected(self):
        self.failure = {"ok": False, "output": "uncertain", "remote_kind": "timeout"}
        run, _ = self.start_failed()
        run_id = run["collection_run_id"]
        def remove_attempt(item):
            item.pop("dispatch_attempted", None)
        self.store.records.update("collection_runs", run_id, remove_attempt)
        self.failure = {"ok": False, "output": "too large", "remote_kind": "http", "remote_status": 413}
        with self.assertRaises(PhoneFactoryError):
            self.store.remote_start(self.request())
        self.assertIsNot(self.store.collection_runs.get(run_id).get("dispatch_rejected"), True)

    def test_acceptance_evidence_overrides_inconsistent_rejected_marker(self):
        run, _ = self.start_failed()
        run_id = run["collection_run_id"]
        self.store.records.update("collection_runs", run_id, lambda item: item.update(
            dispatch_response={"collection_run_id": run_id, "status": "running"}))
        with self.assertRaises(PhoneFactoryError):
            self.store.remote_start(self.request())
        self.assertIsNot(self.store.collection_runs.get(run_id).get("dispatch_rejected"), True)

    def test_late_404_cannot_overwrite_rejection_or_resume_policy(self):
        self.failure = {"ok": False, "output": "uncertain", "remote_kind": "timeout"}
        run, _ = self.start_failed()
        run_id = run["collection_run_id"]
        entered, release = threading.Event(), threading.Event()
        policy = self.store.runtime.sync_policy

        def late_failure():
            entered.set()
            self.assertTrue(release.wait(5))
            self.store.runtime.transfer._update(run_id, transfer_status="failed", transfer_error="stale 404")
            raise PhoneFactoryError("stale 404", 502, remote_kind="http", remote_status=404, remote_command="run")

        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(policy.execute, "collection", run, late_failure, True)
            try:
                self.assertTrue(entered.wait(5))
                with batch_lock(self.root, "dispatch_" + run_id):
                    self.store.records.update("collection_runs", run_id, lambda item: item.update(
                        dispatch_rejected=True, dispatch_error="root 413", transfer_error=None))
                    policy.reject_dispatch("collection", run_id, "root 413")
            finally:
                release.set()
            with self.assertRaises(SyncDeferred):
                future.result(timeout=5)
        saved = self.store.collection_runs.get(run_id)
        self.assertEqual(saved["dispatch_error"], "root 413")
        self.assertIsNone(saved.get("transfer_error"))
        state = policy.view("collection", run_id)
        self.assertEqual((state["state"], state["last_error"], state["failure_count"]), ("paused", "root 413", 0))


if __name__ == "__main__":
    unittest.main()
