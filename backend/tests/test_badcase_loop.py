from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.badcases import BadCaseService, BadCaseError, close_refresh, digest, pipeline_loop_summary
from backend.data_store import RecordStore
from backend.task_generation.jobs import TaskGenerationJobManager, AugmentationStateError
from backend.task_generation.prompts import flywheel_prompt
from backend.task_generation.knowledge_base import tree_payload
from backend.task_generation.tree_store import flatten
from backend.tests.test_task_generation import _write_knowledge_base


class Deferred:
    def __init__(self):
        self.calls = []
    def submit(self, function, *args, **kwargs):
        self.calls.append((function, args, kwargs))


class BadCaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.records = RecordStore(self.root)
        self.service = BadCaseService(self.root)
        self.addCleanup(lambda: close_refresh(self.root))

    def seed(self, rows=None):
        source_cases = [{"source_trajectory_id": "tr1", "case_id": "original-1", "task": "App 中搜索", "app": "App"},
                        {"source_trajectory_id": "tr2", "case_id": "original-1", "task": "App 中搜索", "app": "App"}]
        run = {"run_id": "eval1", "request": {"source_cases": source_cases, "source_digest": "source1", "execution_mode": "evaluate_only",
                "evaluation_profile_id": "rules1", "evaluation_profile_digest": "frozen", "evaluator_config": {"text": {"model": "model1"}}}}
        self.records.put("model_iter_runs", "eval1", run)
        rows = rows or [{"case_key": "k1", "source_trajectory_id": "tr1", "case_id": "original-1", "evaluation_status": "completed", "verdict": "fail", "report_file_id": "r1"},
                        {"case_key": "k2", "source_trajectory_id": "tr2", "case_id": "original-1", "evaluation_status": "completed", "verdict": "partial_success", "report_file_id": "r2"}]
        self.records.put("model_iter_results", "eval1", {"result": {"cases": rows}, "final": True})
        self.service.refresh_run("eval1")
        return self.service.page("eval1")

    def test_repeated_trajectories_group_by_frozen_task_identity(self):
        page = self.seed()
        self.assertEqual(page["total"], 1)
        item = page["items"][0]
        self.assertEqual(item["source_count"], 2)
        self.assertEqual(set(item["verdicts"]), {"fail", "partial_success"})
        self.assertEqual(item["status"], "candidate")
        self.assertEqual(item["evaluation_profile_digest"], "frozen")

    def test_technical_failure_is_separate_and_no_guessing_from_text(self):
        page = self.seed([
            {"case_key": "tech", "case_id": "original-1", "source_trajectory_id": "tr1", "evaluation_status": "needs_review", "errors": ["network timeout"]},
            {"case_key": "unknown", "case_id": "other-id", "evaluation_status": "completed", "verdict": "fail"},
        ])
        self.assertEqual(page["summary"], {"technical": 1, "needs_completion": 1})
        technical = next(row for row in page["items"] if row["status"] == "technical")
        self.assertEqual(technical["failures"][0]["description"], "network timeout")
        with self.assertRaises(BadCaseError):
            self.service.freeze("eval1", [technical["candidate_id"]], "r")

    def test_patch_and_refresh_preserve_original_and_feedback_snapshot(self):
        page = self.seed([{"case_key": "unknown", "case_id": "other-id", "evaluation_status": "completed", "verdict": "fail"}])
        original = self.records.get("model_iter_results", "eval1")
        key = page["items"][0]["candidate_id"]
        self.service.patch(key, {"app": "外部App", "task": "外部任务"})
        first = self.service.freeze("eval1", [key], "request1")
        self.service.patch(key, {"task": "人工后改"})
        repeated = self.service.freeze("eval1", [key], "request1")
        self.assertEqual(first, repeated)
        self.assertEqual(repeated["seeds"][0]["task"], "外部任务")
        self.assertEqual(original, self.records.get("model_iter_results", "eval1"))
        self.records.update("model_iter_results", "eval1", lambda row: row["result"].update(updated=True))
        self.service.refresh_run("eval1")
        self.assertEqual(self.service.page("eval1")["items"][0]["task"], "人工后改")
        with self.assertRaises(BadCaseError):
            self.service.freeze("eval1", [key, "another"], "request1")

    def test_report_failure_is_optional_success_is_cached_and_prompt_uses_evidence(self):
        item = self.seed()["items"][0]
        calls = []
        def loader(run, fid):
            calls.append(fid)
            if fid == "r2":
                raise OSError("missing")
            return {"checklist_summary": {"items": [{"id": "c1", "description": "必须完成搜索", "status": "failed", "evidence": ["step2"]}]}}
        self.service.enrich_reports("eval1", loader)
        self.service.enrich_reports("eval1", loader)
        self.assertEqual(calls, ["r1", "r2"])
        row = self.service.page("eval1")["items"][0]
        self.assertEqual(row["status"], "candidate")
        self.assertIn("必须完成搜索", flywheel_prompt(row, [], 2))
        self.assertEqual(self.service.freeze("eval1", [item["candidate_id"]], "f")["count"], 1)

    def test_shutdown_does_not_write_late_report(self):
        import threading
        self.seed()
        stop = threading.Event()
        def loader(_run, _file):
            stop.set()
            return {"checklist_summary": {"items": [{"id": "x", "description": "late", "status": "failed"}]}}
        self.service.enrich_reports("eval1", loader, stop=stop)
        self.assertEqual(self.records.list("badcase_reports"), [])
        self.assertEqual(self.service.page("eval1")["items"][0]["failures"], [])

    def test_page_does_not_refresh_or_contact_remote(self):
        self.seed()
        with patch.object(self.service, "refresh_run", side_effect=AssertionError("GET refreshed")):
            page = self.service.page("eval1", status="technical")
        self.assertEqual(page["items"], [])
        self.assertEqual(page["summary"]["candidate"], 1)

    def test_missing_frozen_preview_does_not_hide_other_candidate(self):
        self.seed()
        self.records.update("model_iter_runs", "eval1", lambda row: row["request"].update(source_preview_id="missing"))
        self.records.update("model_iter_results", "eval1", lambda row: row["result"].update(new_revision=True))
        self.service.refresh_run("eval1")
        self.assertEqual(self.service.page("eval1")["summary"]["candidate"], 1)

    def test_patch_updates_pipeline_head_without_mutating_evaluation(self):
        page = self.seed([{"case_key": "unknown", "case_id": "other-id", "evaluation_status": "completed", "verdict": "fail"}])
        key = page["items"][0]["candidate_id"]
        self.service.patch(key, {"app": "App", "task": "补充任务"})
        self.assertEqual(self.records.get("badcase_heads", "eval1")["summary"], {"candidate": 1})

    def test_real_phone_factory_workbook_identity_is_resolved(self):
        from backend.tests.test_model_iteration import ModelIterationTests
        from backend import model_iteration
        fixture = ModelIterationTests("test_settings_are_separate_and_reject_secret_fields_and_invalid_addresses")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.addCleanup(fixture.store.runtime.close)
        run = fixture.start()
        rid = run["run_id"]
        fixture.snapshots[rid]["cases"][0]["verdict"] = "fail"
        record = fixture.store.records.get("model_iter_runs", rid)
        with patch("backend.badcases.schedule_refresh"):
            model_iteration.capture_results(fixture.store.runtime, record, final=True)
        service = BadCaseService(fixture.store.root)
        service.refresh_run(rid)
        item = service.page(rid)["items"][0]
        self.assertEqual(item["task"], "搜索商品")
        self.assertEqual(item["app"], "App")
        self.assertEqual(item["case_id"], "case-a")
        self.assertEqual(item["status"], "candidate")


class GenerationSubmissionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        kb = self.root / "kb"
        _write_knowledge_base(kb)
        self.executor = Deferred()
        self.manager = TaskGenerationJobManager(jobs_dir=self.root / "jobs", runs_dir=self.root / "runs", exports_dir=self.root / "exports",
            knowledge_base_dir=kb, logs_dir=self.root / "logs", data_root=self.root / "data", executor=self.executor)
        tree = tree_payload(kb)
        self.version = tree["version"]
        self.selections = [{"node_id": flatten(tree["scenes"])[0][0]["id"], "apps": ["AppA"]}]

    def start(self, **kw):
        return self.manager.submit_initial(self.selections, 2, version=self.version, request_id="r", pipeline_id="p", **kw)

    def test_persist_before_binding_and_idempotent_replay(self):
        observed = []
        def bind(job):
            saved = self.manager.store.get("task_generation.jobs", job["job_id"])
            observed.append(saved["pipeline_id"])
            self.assertEqual(len(self.executor.calls), 0)
        first = self.start(on_created=bind)
        second = self.start()
        self.assertEqual(first["job_id"], second["job_id"])
        self.assertEqual(observed, ["p"])
        self.assertEqual(len(self.executor.calls), 1)
        with self.assertRaises(AugmentationStateError):
            self.manager.submit_initial(self.selections, 3, version=self.version, request_id="r", pipeline_id="p")

    def test_binding_failure_does_not_queue_worker(self):
        def broken(_job):
            raise ValueError("pipeline stopped")
        with self.assertRaisesRegex(ValueError, "pipeline stopped"):
            self.start(on_created=broken)
        self.assertEqual(self.executor.calls, [])
        self.assertEqual(self.manager.list_jobs()[0]["status"], "interrupted")

    def test_retry_uses_original_model_and_knowledge_base(self):
        with patch.dict("os.environ", {"TASK_GENERATION_MODEL_NAME": "first-model"}):
            old = self.start()
        self.manager._progress(old["job_id"], {"status": "failed"})
        with patch.dict("os.environ", {"TASK_GENERATION_MODEL_NAME": "new-model"}):
            new = self.manager.retry_generation(old["job_id"], request_id="retry", pipeline_id="p")
        self.assertNotEqual(old["job_id"], new["job_id"])
        self.assertEqual(new["model_config"]["model"], "first-model")
        self.assertNotIn("api_key", new["model_config"])
        self.assertEqual(old["knowledge_base_version"], new["knowledge_base_version"])
        self.assertEqual(len(self.executor.calls), 2)

    def test_structured_feedback_uses_existing_classification_queue(self):
        records = self.manager.store
        records.put("badcase_feedback", "fb1", {"feedback_id": "fb1", "digest": "d", "run_id": "eval1", "seeds": [
            {"candidate_id": "bc1", "app": "AppA", "task": "原任务", "failures": [{"description": "未搜索"}]}]})
        def bind(job):
            self.assertEqual(self.manager._read_seeds(job["job_id"])[0]["candidate_id"], "bc1")
            self.assertEqual(self.executor.calls, [])
        first = self.manager.submit_badcase_seeds("fb1", 3, request_id="f", pipeline_id="p", on_created=bind)
        second = self.manager.submit_badcase_seeds("fb1", 3, request_id="f", pipeline_id="p")
        self.assertEqual(first["job_id"], second["job_id"])
        self.assertEqual(first["feedback_id"], "fb1")
        seeds = self.manager._read_seeds(first["job_id"])
        self.assertEqual(seeds[0]["failures"], [{"description": "未搜索"}])
        self.assertEqual(self.executor.calls[0][0].__name__, "_run_augmentation_classification")

    def test_paused_owner_does_not_start_second_augmentation_phase(self):
        from backend.task_generation.jobs import GenerationPaused
        job = self.manager._new_job("augmentation", total_items=2, generate_n=1, parameters={"pipeline_id": "p", "augmentation_preview_version": 1})
        self.manager.store.put("pipelines", "p", {"pipeline_id": "p", "status": "paused", "generation_job_id": job["job_id"]})
        self.manager._progress(job["job_id"], {"status": "awaiting_confirmation", "classification_completed": True})
        with self.assertRaises(GenerationPaused):
            self.manager.start_augmentation(job["job_id"])
        self.assertEqual(self.manager.get(job["job_id"])["status"], "awaiting_confirmation")
        self.assertEqual(self.executor.calls, [])

    def test_upload_replay_uses_content_not_temporary_filename(self):
        import pandas as pd
        one, two = self.root / "one.xlsx", self.root / "two.xlsx"
        pd.DataFrame([{"任务": "AppA搜索", "涉及APP": "AppA"}]).to_excel(one, index=False)
        two.write_bytes(one.read_bytes())
        a = self.manager.submit_augmentation(one, "first.xlsx", 2, auto_start=False, request_id="upload")
        b = self.manager.submit_augmentation(two, "renamed.xlsx", 2, auto_start=False, request_id="upload")
        self.assertEqual(a["job_id"], b["job_id"])
        self.assertEqual(len(self.executor.calls), 1)


class BadCaseHttpTests(unittest.TestCase):
    setUp = GenerationSubmissionTests.setUp
    # Deliberately use real FastAPI request parsing and real local stores.
    def test_http_feedback_and_specific_evaluation_route(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from backend import badcase_router, pipeline_router
        from backend.task_generation import router as generation_router
        old = generation_router.manager
        generation_router.configure_job_manager(self.manager)
        self.addCleanup(lambda: generation_router.configure_job_manager(old))
        app = FastAPI()
        app.include_router(badcase_router.router)
        app.include_router(generation_router.router)
        app.include_router(pipeline_router.router)
        store = self.manager.store
        pipeline = store.put("pipelines", "p1", {"pipeline_id": "p1", "status": "succeeded", "steps": []})
        store.put("model_iter_runs", "eval1", {"run_id": "eval1", "status": "succeeded", "request": {"execution_mode": "evaluate_only", "source": {"source_path": "D:/external"}}})
        store.put("model_iter_results", "eval1", {"result": {"cases": [{"case_key": "c", "case_id": "c", "task": "App搜索", "app": "App", "evaluation_status": "completed", "verdict": "fail"}]}})
        service = BadCaseService(self.manager.data_root)
        service.refresh_run("eval1")
        with TestClient(app) as client, patch("backend.badcase_router.schedule_refresh", return_value=True):
            response = client.post("/api/pipelines/p1/evaluation", json={"run_id": "eval1", "expected_revision": pipeline["storage_revision"]})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["pipeline"]["evaluation_run_id"], "eval1")
            self.assertEqual(response.json()["pipeline"]["status"], "succeeded")
            page = client.get("/api/badcases", params={"run_id": "eval1"}).json()
            frozen = client.post("/api/badcases/feedback", json={"run_id": "eval1", "candidate_ids": [page["items"][0]["candidate_id"]], "request_id": "feedback"})
            self.assertEqual(frozen.status_code, 201, frozen.text)
            job = client.post("/api/task-generation/badcase-jobs", json={"feedback_id": frozen.json()["feedback_id"], "generate_n": 2, "request_id": "new"})
            self.assertEqual(job.status_code, 202, job.text)
            self.assertEqual(job.json()["source_evaluation_run_id"], "eval1")


if __name__ == "__main__":
    unittest.main()
