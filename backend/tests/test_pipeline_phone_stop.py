import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from backend.data_store import RecordStore
from backend.phone_factory import PhoneFactoryError, PhoneFactoryStore
from backend.pipeline_job_stop import PipelineJobCancelled
from backend.pipeline_phone_stop import request_phone_stops


class PipelinePhoneStopTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.records = RecordStore(self.root)
        self.pipeline = self.records.put("pipelines", "p-one", {
            "pipeline_id": "p-one", "batch_id": "b-one", "start_mode": "collect",
            "collection_dispatch_key": "p-one", "status": "terminating",
        })
        self.records.put("pipeline_owners", "b-one", {"pipeline_id": "p-one"})
        self.runtime = Mock(records=self.records)
        self.runtime.resume_service.diagnostics.return_value = {}
        self.adapter = SimpleNamespace(factory=SimpleNamespace(runtime=self.runtime), collection_runs=self.runs)

    def runs(self, batch_id):
        return [run for run in self.records.list("collection_runs") if run["batch_id"] == batch_id]

    def add_collection(self, run_id="cr_one", **changes):
        return self.records.put("collection_runs", run_id, {
            "collection_run_id": run_id, "batch_id": "b-one", "dispatch_key": "p-one",
            "status": "running", "dispatch_attempted": True, "transfer_status": "downloading",
            "request": {"metadata": {"collection_artifacts_version": 2}},
            **changes,
        })

    def reconcile(self, **changes):
        return request_phone_stops(self.adapter, {**self.pipeline, **changes})

    def test_owned_devices_stop_independently_of_transfer(self):
        self.add_collection(phone_states={
            "phone-one": {"status": "completed", "stop_confirmed": True, "claimed": False},
            "phone-two": {"status": "needs_attention", "stop_confirmed": False,
                          "claimed": True, "reason": "设备断连"},
        })
        self.add_collection("cr_other", dispatch_key="other-request")
        self.add_collection("cr_other_batch", batch_id="b-other")
        pending = self.reconcile()
        self.runtime.cancel_collection.assert_called_once_with("cr_one", pipeline_stop=True, pipeline_retry_id=None)
        self.assertEqual([(item["phone_id"], item["message"]) for item in pending], [("phone-two", "设备断连")])
        self.records.update("collection_runs", "cr_one", lambda run: run["phone_states"]["phone-two"].update(
            status="cancelled", stop_confirmed=True, claimed=False))
        self.assertEqual(self.reconcile(), [])
        self.assertEqual(self.records.get("collection_runs", "cr_one")["transfer_status"], "downloading")

    def test_unlaunched_cancel_and_uncertain_dispatch_are_distinct(self):
        self.add_collection(dispatch_attempted=False, status="dispatching")
        self.add_collection("cr_uncertain", status="failed", dispatch_error="超时")
        self.assertEqual([item["job_id"] for item in self.reconcile()], ["cr_uncertain"])
        self.runtime.cancel_collection.assert_called_once_with(
            "cr_uncertain", pipeline_stop=True, pipeline_retry_id=None)
        self.assertTrue(self.records.get("collection_runs", "cr_one")["collection_dispatch_cancelled"])

    def test_late_status_cannot_prove_device_release_without_phone_confirmation(self):
        self.add_collection(status="completed", control_terminal=True,
                            phone_states={"phone-one": {"status": "cancelled", "stop_confirmed": True, "claimed": True}})
        self.assertTrue(self.reconcile())
        self.runtime.cancel_collection.assert_called_once()

    def test_existing_or_independent_evaluation_is_left_running(self):
        self.add_collection()
        self.assertEqual(self.reconcile(start_mode="existing"), [])
        self.records.put("pipeline_evaluations", "p-one", {"run_id": "eval_shared"})
        self.assertEqual(self.reconcile(start_mode="evaluation", batch_id=None), [])
        self.runtime.cancel_collection.assert_not_called()
        self.runtime.cancel_evaluation.assert_not_called()

    def test_resume_waits_for_confirmation_at_same_local_revision(self):
        self.add_collection()
        head = {"run_id": "cr_one", "active": True, "revision": 2, "remote_revision": 1}
        self.records.put("collection_resume_heads", "cr_one", head)
        self.assertTrue(self.reconcile())
        self.runtime.resume_service.cancel.assert_called_once()
        self.records.put("collection_resume_heads", "cr_one", {**head, "cancel_intent": {
            "pipeline_id": "p-one", "epoch": 1, "confirmed": True,
        }})
        self.assertTrue(self.reconcile())
        self.records.update("collection_resume_heads", "cr_one",
                            lambda row: row["cancel_intent"].update(epoch=2))
        self.assertEqual(self.reconcile(), [])

    def test_resume_finishes_stop_when_only_receipts_are_pending(self):
        phones_stopped = {"phone-one": {"status": "cancelled", "stop_confirmed": True, "claimed": False}}
        run = self.add_collection(phone_states=phones_stopped)
        self.records.put("collection_resume_heads", "cr_one", {
            "run_id": "cr_one", "active": True, "revision": 2, "remote_revision": 1,
        })
        self.records.put("collection_resume_operations", "resume_one", {
            "operation_id": "resume_one", "run_id": "cr_one", "epoch": 2,
            "execution_id": "cr_resume", "status": "started",
        })
        self.records.put("collection_executions", "cr_resume", {
            **run, "root_run_id": "cr_one", "collection_run_id": "cr_resume",
        })
        self.assertEqual(self.reconcile(), [])
        self.runtime.resume_service.cancel.assert_not_called()
        self.assertTrue(self.records.get("collection_resume_heads", "cr_one")["active"])

    def test_terminating_pipeline_rejects_collection_dispatch_before_network(self):
        store = PhoneFactoryStore(self.root, run_client_fn=Mock())
        self.addCleanup(store.runtime.close)
        from backend.tests.test_collection_runs import seed_batch
        seed_batch(self.root)
        self.records.update("pipelines", "p-one", lambda row: row.update(batch_id="batch-one"))
        run = self.add_collection(batch_id="batch-one", status="dispatching", dispatch_attempted=False)
        with self.assertRaises(PipelineJobCancelled):
            store.runtime._admit_pipeline_dispatch(run, run["collection_run_id"], "generate", True)
        self.assertFalse(self.records.get("collection_runs", "cr_one")["dispatch_attempted"])

    def test_remote_404_then_acceptance_stops_the_original_collection_only(self):
        store = PhoneFactoryStore(self.root, run_client_fn=Mock())
        runtime = store.runtime
        self.addCleanup(runtime.close)
        from backend.tests.test_collection_runs import seed_batch
        seed_batch(self.root)
        pipeline = {**self.pipeline, "batch_id": "batch-one"}
        self.records.put("pipelines", "p-one", pipeline)
        run, _ = store.collection_runs.create("batch-one", dispatch_key="p-one", metadata={
            "result_transport": "http", "run_mode": "generate", "collection_artifacts_version": 2,
        })
        run_id = run["collection_run_id"]
        self.records.update("collection_runs", run_id, lambda row: row.update(
            status="failed", dispatch_attempted=True, dispatch_error="lost", transfer_status="downloading"))
        adapter = SimpleNamespace(factory=store, collection_runs=self.runs)
        running = {"run_id": run_id, "batch_id": "batch-one", "run_mode": "generate", "status": "running"}
        stopped = {**running, "status": "cancelled", "state_revision": 3, "phone_states": {
            "phone-one": {"status": "cancelled", "stop_confirmed": True, "claimed": False},
        }}
        with patch.object(runtime._control_pool, "submit"), \
                patch.object(runtime, "get_run", side_effect=[PhoneFactoryError("404", 404), running]), \
                patch.object(store, "_remote_result", return_value=stopped) as command:
            self.assertTrue(request_phone_stops(adapter, pipeline))
            runtime._cancel_collection_remote(run_id)
            command.assert_not_called()
            self.assertTrue(request_phone_stops(adapter, pipeline))
            runtime._cancel_collection_remote(run_id)
            command.assert_called_once_with(["cancel-run", run_id])
        self.assertEqual(request_phone_stops(adapter, pipeline), [])
        self.assertEqual(self.records.get("collection_runs", run_id)["transfer_status"], "downloading")

    def test_uncertain_managed_evaluation_stop_never_reposts_start(self):
        store = PhoneFactoryStore(self.root, run_client_fn=Mock())
        self.addCleanup(store.runtime.close)
        run = {"run_id": "eval_own", "pipeline_id": "p-one", "status": "stopping",
               "dispatch_attempted": True, "dispatch_confirmed": False, "stop_requested_at": "now",
               "pipeline_stop_requested": True, "request": {"execution_mode": "evaluate_only",
                                                             "offline_evaluation_jobs_version": 1}}
        self.records.put("model_iter_runs", "eval_own", run)
        runtime = store.runtime
        with patch.object(runtime, "get_run", side_effect=PhoneFactoryError("尚未查到远端登记", 404)), \
                patch("backend.offline_evaluation_dispatch.dispatch") as dispatch:
            runtime._cancel_evaluation_remote("eval_own")
        dispatch.assert_not_called()
        self.assertFalse(self.records.get("model_iter_runs", "eval_own").get("pipeline_stop_confirmed"))
