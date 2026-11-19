"""Durable generation entry, review and existing orchestration compatibility."""
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor, Future

from backend.data_store import RevisionConflict
from backend.pipelines import PipelineError, PipelineManager, PipelineRuntime
from backend.pipeline_router import CreatePipelineRequest, ConfirmGenerationRequest
from backend.tests.test_pipelines import OfflineRuntime


class GenerationFixture:
    def __init__(self, records):
        self.records = records
        self.sources = {}

    def get(self, job_id):
        return self.records.get("task_generation.jobs", job_id)

    def submit_collection_batch(self, job_id):
        created = job_id not in self.sources
        self.sources.setdefault(job_id, {"batch_id": job_id})
        return deepcopy(self.sources[job_id]), created

    def retry_generation(self, job_id, *, request_id, pipeline_id, on_created):
        old = self.get(job_id)
        retry_id = job_id + "-retry"
        job = self.get(retry_id)
        if job is None:
            job = self.records.put("task_generation.jobs", retry_id, {**old, "job_id": retry_id,
                "status": "queued", "request_id": request_id, "retry_of_job_id": job_id})
        on_created(job)
        return job


class GenerationPipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.runtime = OfflineRuntime(self.root)
        self.runtime.generation = GenerationFixture(self.runtime.records)
        self.manager = PipelineManager(self.runtime, self.root)

    def create(self, **extra):
        return self.manager.create({"request_id": "generation-entry", "name": "生成全链路", "mode": "automatic",
            "start_mode": "generate", "generation_kind": "initial", "threshold": 3, **extra})

    def job(self, p, status="queued", *, identity="generation-job", bind=True, **extra):
        job = self.manager.records.put("task_generation.jobs", identity, {"job_id": identity,
            "kind": "task_generation", "pipeline_id": p["pipeline_id"], "request_id": "generate-request-" + identity,
            "status": status, "percent": 20 if status == "running" else 100 if status in {"succeeded", "partial"} else 0,
            "result_count": 2, **extra})
        if bind:
            self.manager.bind_generation(p["pipeline_id"], job)
        return job

    def tick(self, p, count=1):
        for _ in range(count):
            self.manager.tick(p["pipeline_id"])
        return self.manager.get(p["pipeline_id"])

    def ready(self):
        p = self.create()
        self.job(p, "partial")
        return self.tick(p)

    def confirm(self, p, **extra):
        return self.manager.confirm_generation(p["pipeline_id"], {"request_id": "confirm-one",
            "expected_revision": p["storage_revision"], "collection_config": {"repeat_times": 2}, **extra})

    def control(self, p, action):
        p = self.manager.get(p["pipeline_id"])
        return self.manager.control(p["pipeline_id"], action, p["storage_revision"])

    def test_unconfigured_has_no_batch_owner_and_read_is_safe(self):
        p = self.create()
        self.assertIsNone(p["batch_id"])
        self.assertEqual(p["status"], "waiting_for_generation_config")
        self.assertEqual(self.manager.records.list("pipeline_owners"), [])
        self.assertEqual(self.tick(p)["storage_revision"], p["storage_revision"])
        self.assertEqual(self.create()["pipeline_id"], p["pipeline_id"])
        with self.assertRaises(PipelineError):
            self.create(generation_kind="augmentation")
        page = self.manager.summary_page(limit=10)
        self.assertIsNone(page["items"][0]["batch_id"])
        self.assertEqual(page["items"][0]["generation_kind"], "initial")

    def test_generation_partial_review_is_not_automatic_collection(self):
        p = self.ready()
        self.assertEqual(p["status"], "waiting_for_generation_review")
        self.assertIsNone(p["batch_id"])
        self.assertEqual(self.runtime.collection_values, [])
        self.assertEqual(self.manager.records.list("pipeline_owners"), [])
        self.assertEqual(self.tick(p, 2)["storage_revision"], p["storage_revision"])

    def test_confirm_replay_precedes_revision_and_freezes_configuration(self):
        p = self.ready()
        result = self.confirm(p)
        self.assertNotEqual(result["batch_id"], result["task_source_batch_id"])
        self.assertEqual(result["current_step"], "collection")
        self.assertEqual(self.confirm(p)["batch_id"], result["batch_id"])
        with self.assertRaises(PipelineError):
            self.confirm(p, collection_config={"repeat_times": 3})
        self.assertEqual(result["collection_config"]["repeat_times"], 2)
        self.assertEqual(len(self.manager.records.list("pipeline_owners")), 1)
        self.assertEqual(self.tick(result)["steps"][1]["job_ids"], ["cr-test"])

    def test_confirm_crash_reuses_intent_and_original_defaults(self):
        p = self.ready()
        original = self.runtime.prepare_collection
        with patch.object(self.runtime, "prepare_collection", side_effect=OSError("temporary disk failure")):
            with self.assertRaises(OSError):
                self.confirm(p)
        self.runtime.config["quality"] = "changed-after-first-confirm"
        with patch.object(self.runtime, "collection_options", side_effect=AssertionError("must reuse frozen options")):
            result = self.confirm(p)
        self.assertEqual(result["config_fingerprints"]["quality"], "quality-v1")
        self.assertEqual(len(self.runtime.generation.sources), 1)
        self.assertEqual(result["collection_config"], {"repeat_times": 2})

    def test_accepted_confirm_recovers_on_restart_without_form_resubmission(self):
        p = self.ready()
        with patch.object(self.runtime, "prepare_collection", side_effect=OSError("restart before batch commit")):
            with self.assertRaises(OSError):
                self.confirm(p)
        accepted = self.manager.get(p["pipeline_id"])
        self.assertEqual(accepted["generation_confirmation"]["status"], "preparing")
        self.assertEqual(accepted["collection_config"], {"repeat_times": 2})
        self.assertEqual(accepted["status"], "running")
        self.assertIsNone(accepted["batch_id"])
        self.manager = PipelineManager(self.runtime, self.root)
        self.runtime.config["quality"] = "new-page-default"
        with patch.object(self.runtime, "collection_options", side_effect=AssertionError("must not reread defaults")), \
                patch.object(self.runtime, "configurations", side_effect=AssertionError("must use accepted fingerprints")):
            restored = self.tick(p)
        self.assertEqual(restored["current_step"], "collection")
        self.assertEqual(restored["generation_confirmation"]["status"], "completed")
        self.assertEqual(restored["generation_confirm_request_id"], "confirm-one")
        self.assertEqual(restored["config_fingerprints"]["quality"], "quality-v1")
        self.assertEqual(restored["collection_config"], {"repeat_times": 2})
        self.assertEqual(len(self.manager.records.list("pipeline_generation_confirmations")), 1)
        self.assertEqual(self.confirm(p)["batch_id"], restored["batch_id"])

    def test_pending_confirmation_cannot_be_replaced_by_new_page_configuration(self):
        p = self.ready()
        with patch.object(self.runtime, "prepare_collection", side_effect=OSError("temporarily unavailable")):
            with self.assertRaises(OSError):
                self.confirm(p)
        pending = self.manager.get(p["pipeline_id"])
        with self.assertRaisesRegex(PipelineError, "已有确认请求"):
            self.confirm(pending, request_id="different-form-defaults", collection_config={"repeat_times": 8})
        self.assertEqual(len(self.manager.records.list("pipeline_generation_confirmations")), 1)
        self.assertEqual(self.tick(p)["collection_config"], {"repeat_times": 2})

    def test_pending_confirmation_respects_pause_and_terminate(self):
        p = self.ready()
        with patch.object(self.runtime, "prepare_collection", side_effect=OSError("temporarily unavailable")):
            with self.assertRaises(OSError):
                self.confirm(p)
        paused = self.control(p, "pause")
        with patch.object(self.runtime, "prepare_collection", side_effect=AssertionError("paused must not prepare")):
            self.assertEqual(self.tick(paused)["status"], "paused")
        resumed = self.control(paused, "resume")
        terminated = self.control(resumed, "terminate")
        with patch.object(self.runtime, "prepare_collection", side_effect=AssertionError("terminated must not prepare")):
            self.assertEqual(self.tick(terminated)["status"], "terminated")
        self.assertEqual(self.manager.records.list("pipeline_owners"), [])
        self.assertIsNone(self.manager.get(p["pipeline_id"])["batch_id"])

    def test_confirmation_commit_failure_keeps_no_batch_and_retries_with_backoff(self):
        p = self.ready()
        original_write = self.manager.records.put_many
        def reject_final_commit(entries, **kwargs):
            if any(entry["namespace"] == "pipeline_owners" for entry in entries):
                raise OSError("commit storage temporarily busy")
            return original_write(entries, **kwargs)
        with patch.object(self.manager.records, "put_many", side_effect=reject_final_commit):
            with self.assertRaises(OSError):
                self.confirm(p)
            with patch("backend.pipeline_generation.time.time", return_value=1000):
                waiting = self.tick(p)
        self.assertIsNone(waiting["batch_id"])
        self.assertEqual(self.manager.records.list("pipeline_owners"), [])
        self.assertIn("storage temporarily busy", waiting["error"])
        with patch("backend.pipeline_generation.time.time", return_value=1001), \
                patch.object(self.runtime, "prepare_collection", side_effect=AssertionError("backoff must skip work")):
            self.assertEqual(self.tick(p)["storage_revision"], waiting["storage_revision"])
        with patch("backend.pipeline_generation.time.time", return_value=1006):
            recovered = self.tick(p)
        self.assertEqual(recovered["current_step"], "collection")
        self.assertEqual(len(self.manager.records.list("pipeline_owners")), 1)
        self.assertIsNone(recovered["error"])

    def test_confirm_stale_revision_rejected_before_side_effects(self):
        p = self.ready()
        with self.assertRaises(RevisionConflict):
            self.confirm(p, expected_revision=0)
        self.assertEqual(self.runtime.generation.sources, {})

    def test_real_result_edits_cannot_change_accepted_tasks_during_recovery(self):
        from backend.task_generation.jobs import TaskGenerationJobManager, AugmentationStateError
        p = self.ready()
        job_id = p["generation_job_id"]
        generation = TaskGenerationJobManager(self.root / "jobs", self.root / "runs", self.root / "exports",
            self.root / "kb", self.root / "generation-logs", data_root=self.root)
        self.addCleanup(generation.shutdown)
        self.runtime.generation = generation
        row = {"result_id": "r1", "task_uuid": "task1", "task": "生成原任务", "app": "AppA", "deleted": False}
        generation.store.put("task_generation.results", job_id, {"job_id": job_id, "rows": [row]})
        generation.patch_result(job_id, "r1", {"task": "审核确认的任务"})
        with patch.object(generation, "submit_collection_batch", side_effect=OSError("restart before source freeze")):
            with self.assertRaises(OSError):
                self.confirm(p)
        self.assertFalse(generation._collection_batch_dir(job_id).exists())
        for change in ({"task": "另一页的迟到修改"}, {"deleted": True}):
            with self.assertRaisesRegex(AugmentationStateError, "已确认采集"):
                generation.patch_result(job_id, "r1", change)
        self.manager = PipelineManager(self.runtime, self.root)
        recovered = self.tick(p)
        source = generation.collection_batch(recovered["task_source_batch_id"])
        self.assertEqual(source["task_count"], 1)
        self.assertEqual(source["snapshot"]["tasks"][0]["task"], "审核确认的任务")
        # Once the immutable source exists, editing a draft cannot change it.
        generation.patch_result(job_id, "r1", {"task": "之后修改的草稿"})
        self.assertEqual(generation.collection_batch(job_id), source)
        # Existing unbound generation jobs retain their original edit behavior.
        old = {**generation.get(job_id), "job_id": "unbound-generation", "pipeline_id": None}
        generation.store.put("task_generation.jobs", old["job_id"], old)
        generation.store.put("task_generation.results", old["job_id"], {"job_id": old["job_id"], "rows": [row]})
        self.assertTrue(generation.patch_result(old["job_id"], "r1", {"deleted": True})["deleted"])

    def test_generated_entry_retains_collection_failure_and_identity_checks(self):
        p = self.confirm(self.ready())
        p["collection_run_ids"] = ["expected-run"]
        with self.assertRaisesRegex(PipelineError, "运行记录缺失"):
            PipelineRuntime.input_ready(self.runtime, p)
        self.runtime.collection_values = [{"collection_run_id": "expected-run", "status": "failed"}]
        with self.assertRaisesRegex(PipelineError, "失败或不完整"):
            PipelineRuntime.input_ready(self.runtime, p)

    def test_restart_recovers_job_persisted_before_binding(self):
        p = self.create()
        self.job(p, "interrupted", bind=False, error="service restarted")
        self.manager = PipelineManager(self.runtime, self.root)
        p = self.tick(p)
        self.assertEqual(p["status"], "failed")
        self.assertEqual(p["generation_job_id"], "generation-job")
        self.assertEqual(p["error"], "service restarted")
        self.assertEqual(self.runtime.collection_values, [])

    def test_pause_unconfigured_prevents_new_binding(self):
        p = self.control(self.create(), "pause")
        self.assertEqual(self.tick(p)["status"], "paused")
        with self.assertRaises(PipelineError):
            self.manager.validate_generation_binding(p["pipeline_id"], "initial", "request")
        p = self.control(p, "resume")
        self.assertEqual(p["status"], "waiting_for_generation_config")

    def test_failed_generation_retry_is_durable_and_keeps_frozen_input(self):
        p = self.create()
        self.job(p, "failed", knowledge_base_version="original-kb", model_config={"model": "original"})
        p = self.control(self.tick(p), "retry")
        self.assertEqual(p["status"], "running")
        self.assertEqual(p["generation_attempt"], 1)
        self.assertIsNone(p["generation_job_id"])
        self.manager = PipelineManager(self.runtime, self.root)
        p = self.tick(p)
        self.assertEqual(p["generation_job_id"], "generation-job-retry")
        retry = self.manager.records.get("task_generation.jobs", p["generation_job_id"])
        self.assertEqual(retry["knowledge_base_version"], "original-kb")
        self.assertEqual(retry["model_config"]["model"], "original")
        self.assertIn("generation-job", p["steps"][0]["previous_job_ids"])
        self.assertEqual(self.tick(p, 3)["generation_job_id"], retry["job_id"])
        self.assertEqual(len(self.manager.records.list("task_generation.jobs")), 2)

    def test_terminate_pending_retry_does_not_create_a_new_job(self):
        p = self.create()
        self.job(p, "failed")
        p = self.control(self.tick(p), "retry")
        p = self.control(p, "terminate")
        self.assertEqual(self.tick(p)["status"], "terminated")
        self.assertEqual(len(self.manager.records.list("task_generation.jobs")), 1)

    def test_pause_review_and_terminate_drain_active_generation(self):
        p = self.create()
        job = self.job(p, "running")
        p = self.control(p, "terminate")
        self.assertEqual(p["status"], "terminating")
        self.manager.records.put("task_generation.jobs", job["job_id"], {**job, "status": "succeeded"})
        p = self.tick(p)
        self.assertEqual(p["status"], "terminated")
        self.assertEqual(self.runtime.collection_values, [])
        with self.assertRaises(PipelineError):
            self.confirm(p)

    def test_unconfigured_terminate_finishes_without_batch(self):
        p = self.tick(self.control(self.create(), "terminate"))
        self.assertEqual(p["status"], "terminated")
        self.assertIsNone(p["batch_id"])

    def test_legacy_contract_keeps_original_step_list(self):
        p = self.manager.create({"request_id": "old", "name": "existing", "batch_id": "existing-batch",
            "mode": "automatic", "start_mode": "existing", "threshold": 3})
        self.assertEqual(len(p["steps"]), 8)
        self.assertNotIn("generation_kind", p)

    def test_request_models_accept_null_batch_and_typed_confirmation(self):
        request = CreatePipelineRequest(request_id="new", name="generation", mode="manual", start_mode="generate", generation_kind="initial")
        self.assertIsNone(request.batch_id)
        self.assertEqual(ConfirmGenerationRequest(request_id="confirmation", expected_revision=1, collection_config={}).collection_config, {})

    def test_real_generation_callback_retry_and_source_freeze(self):
        from backend.task_generation.jobs import TaskGenerationJobManager
        from backend.task_generation.knowledge_base import tree_payload
        from backend.task_generation.tree_store import flatten
        from backend.tests.test_task_generation import _write_knowledge_base

        class InlineExecutor:
            def submit(self, function, *args, **kwargs):
                future = Future()
                try:
                    future.set_result(function(*args, **kwargs))
                except Exception as exc:
                    future.set_exception(exc)
                    raise
                return future

        kb = self.root / "kb"
        _write_knowledge_base(kb)
        calls = []

        def runner(nodes, count, *, kb_root, progress):
            calls.append(kb_root)
            return {"results": [{"result_id": "r1", "task_uuid": "task1", "task": "测试原任务", "app": "AppA",
                "pre_dependency": "zero", "deleted": False}], "errors": [], "warnings": [], "total_items": 1}

        generation = TaskGenerationJobManager(self.root / "jobs", self.root / "runs", self.root / "exports",
            kb, self.root / "generation-logs", executor=InlineExecutor(), initial_runner=runner, data_root=self.root)
        self.runtime.generation = generation
        tree = tree_payload(kb)
        selection = [{"node_id": flatten(tree["scenes"])[0][0]["id"], "apps": ["AppA"]}]
        p = self.create()
        create = lambda: generation.submit_initial(selection, 1, version=tree["version"], request_id="initial-one",
            pipeline_id=p["pipeline_id"], on_created=lambda job: self.manager.bind_generation(p["pipeline_id"], job))
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(create) for _ in range(2)]
            jobs = [future.result(timeout=10) for future in futures]
        self.assertEqual(jobs[0]["job_id"], jobs[1]["job_id"])
        self.assertEqual(len(calls), 1)
        old = generation.get(jobs[0]["job_id"])
        generation._progress(old["job_id"], {"status": "failed", "error": "simulated interruption"})
        p = self.control(self.tick(p), "retry")
        with patch("backend.task_generation.config.load_model_config", side_effect=AssertionError("retry must not reload defaults")):
            p = self.tick(p)
        self.assertEqual(p["status"], "waiting_for_generation_review")
        new = generation.get(p["generation_job_id"])
        self.assertNotEqual(new["job_id"], old["job_id"])
        self.assertEqual(new["knowledge_base_version"], old["knowledge_base_version"])
        self.assertEqual(new["model_config"], old["model_config"])
        result = self.confirm(p)
        source = generation.collection_batch(result["task_source_batch_id"])
        self.assertEqual(source["snapshot"]["tasks"][0]["task"], "测试原任务")
        self.assertEqual(result["collection_config"], {"repeat_times": 2})


if __name__ == "__main__":
    unittest.main()
