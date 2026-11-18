"""HTTP generation -> frozen workbook -> BadCase -> next-round integration.

Uses real managers, SQLite, workbooks and augmentation services. Only model and
collector transports are offline; no app lifespan or production data is used.
"""
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend import badcase_router
from backend.badcases import BadCaseService
from backend.pipeline_router import router as pipeline_router
from backend.task_generation import router as generation_router
from backend.task_generation.jobs import TaskGenerationJobManager
from backend.task_generation.knowledge_base import tree_payload
from backend.task_generation.service import run_augmentation_classification, run_augmentation_generation
from backend.task_generation.tree_store import flatten
from backend.tests.test_augmentation_preview import DeferredExecutor, LocalModel
from backend.tests import test_pipeline_result_batches as fixtures
from backend.data_store import RecordStore
from backend.phone_factory import PhoneFactoryStore
from backend.pipelines import PipelineManager, PipelineRuntime
from backend.tests.test_task_generation import _write_knowledge_base


class PipelineFeedbackHttpTests(unittest.TestCase):
    client = fixtures.PipelineResultBatchTests.client

    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.records = RecordStore(self.root)
        self.calls, self.remote = [], {}
        self.factory = PhoneFactoryStore(self.root, run_client_fn=self.client)
        self.addCleanup(self.factory.runtime.close)
        self.factory.initialize_settings({"phones": ["phone-a"], "apps": ["AppA"],
            "phoneApps": [{"phone_id": "phone-a", "app": "AppA"}], "vla": ["http://mock.invalid/vla"]})
        self.runtime = PipelineRuntime(self.root, preprocessing=None, tree=None, quality=None,
            cot=None, factory=self.factory, overview=None)
        self.runtime.configurations = lambda: {}
        self.manager = PipelineManager(self.runtime, self.root)
        self.queue = DeferredExecutor()
        self.model = LocalModel()
        kb = self.root / "kb"
        _write_knowledge_base(kb)

        def initial(_units, _n, **options):
            options["progress"]({"stage": "generating", "percent": 100})
            return {"results": [{"result_id": "r-first", "task_uuid": "original-task", "task": "请在 AppA 查找收藏内容",
                "app": "AppA", "scene": "场景A", "capability": "能力A", "sub_capability": "子能力A",
                "pre_dependency": "zero", "deleted": False}], "errors": [], "warnings": []}

        base = self.root / "system" / "task_generation"
        self.generation = TaskGenerationJobManager(base / "jobs", base / "runs", base / "exports", kb, base / "logs",
            data_root=self.root, executor=self.queue, initial_runner=initial,
            collection_batches_dir=base / "collection_batches",
            classification_runner=lambda seeds, **kw: run_augmentation_classification(seeds, model=self.model, **kw),
            generation_runner=lambda seeds, n, **kw: run_augmentation_generation(seeds, n, model=self.model, **kw))
        self.addCleanup(self.generation.shutdown)
        self.runtime.generation = self.generation
        for target, name, value in [(generation_router, "manager", self.generation),
                                    (generation_router, "pipeline_manager", self.manager),
                                    (badcase_router, "schedule_refresh", lambda *a, **kw: True)]:
            patcher = patch.object(target, name, value)
            patcher.start(); self.addCleanup(patcher.stop)
        app = FastAPI()
        app.state.pipelines = self.manager
        app.include_router(generation_router.router)
        app.include_router(badcase_router.router)
        app.include_router(pipeline_router)
        self.http = TestClient(app)
        self.addCleanup(self.http.close)
        tree = tree_payload(kb)
        self.generate_body = {"request_id": "gen-http", "version": tree["version"], "generate_n": 1,
            "selections": [{"node_id": flatten(tree["scenes"])[0][0]["id"], "apps": ["AppA"]}]}

    def post(self, path, body, status=200):
        response = self.http.post(path, json=body)
        self.assertEqual(response.status_code, status, response.text)
        return response.json()

    def prepare(self):
        p = self.post("/api/pipelines", {"request_id": "p-http", "name": "HTTP 贯通", "mode": "manual",
            "start_mode": "generate", "generation_kind": "initial"}, 201)["pipeline"]
        body = {**self.generate_body, "pipeline_id": p["pipeline_id"]}
        job = self.post("/api/task-generation/jobs", body, 202)
        self.assertEqual(self.post("/api/task-generation/jobs", body, 202)["job_id"], job["job_id"])
        self.assertEqual(self.queue.submissions, 1)
        self.queue.run_all()
        self.manager.tick(p["pipeline_id"])
        p = self.manager.get(p["pipeline_id"])
        self.assertEqual(p["status"], "waiting_for_generation_review")
        self.assertIsNone(p["batch_id"])
        confirm = {"request_id": "confirm-http", "expected_revision": p["storage_revision"], "collection_config": {
            "agent_type": "newagent", "phone_id": "phone-a", "app": "AppA", "vla": "http://mock.invalid/vla", "repeat_times": 2}}
        route = "/api/pipelines/" + p["pipeline_id"] + "/confirm-generation"
        p = self.post(route, confirm)["pipeline"]
        self.assertEqual(self.post(route, confirm)["pipeline"]["batch_id"], p["batch_id"])
        return p

    def test_generation_http_freezes_real_source_and_does_not_dispatch_before_review(self):
        p = self.prepare()
        source = self.factory.collection_runs.batch(p["task_source_batch_id"], require_workbook=True)
        result = self.factory.collection_runs.batch(p["batch_id"], require_workbook=True)
        self.assertNotEqual(source["batch_id"], result["batch_id"])
        self.assertEqual(result["snapshot"]["tasks"][0]["task"], "请在 AppA 查找收藏内容")
        self.assertEqual(result["execution_metadata"]["repeat_times"], 2)
        self.assertFalse(any(call[0] == "start-run" for call in self.calls))
        self.manager.tick(p["pipeline_id"])
        self.assertEqual(sum(call[0] == "start-run" for call in self.calls), 1)

    def test_real_source_evaluation_feedback_augmentation_and_next_round(self):
        p = self.prepare()
        result_batch = self.factory.collection_runs.batch(p["batch_id"])
        task = result_batch["snapshot"]["tasks"][0]
        run_id = "eval-offline-feedback"
        self.records.put("model_iter_runs", run_id, {"run_id": run_id, "batch_id": p["batch_id"], "status": "succeeded",
            "request": {"execution_mode": "collect_and_evaluate", "evaluation_profile_digest": "rules-frozen"}})
        cases = [{"case_key": str(i), "case_id": task["collection_case_id"], "phone_id": "phone-a",
            "collection_status": "completed", "evaluation_status": "completed", "verdict": verdict,
            "failure_reason": "没有确认收藏结果"} for i, verdict in enumerate(["fail", "partial_success", "success"])]
        cases.append({**cases[0], "case_key": "technical", "evaluation_status": "needs_review", "verdict": "unchecked"})
        frozen = {"result": {"run_id": run_id, "cases": cases, "summary": {}}, "final": True}
        self.records.put("model_iter_results", run_id, frozen)
        before = deepcopy(self.records.get("model_iter_results", run_id))
        service = BadCaseService(self.root)
        service.refresh_run(run_id)
        page = self.http.get("/api/badcases", params={"run_id": run_id, "status": "candidate"}).json()
        self.assertEqual(page["total"], 1, page)
        candidate = page["items"][0]
        self.assertEqual(candidate["source_count"], 2)
        self.assertEqual(candidate["task"], task["task"])
        self.assertEqual(candidate["app"], "AppA")
        self.assertEqual(page["summary"]["technical"], 1)
        binding = self.post(f"/api/pipelines/{p['pipeline_id']}/evaluation", {"run_id": run_id, "expected_revision": p["storage_revision"]})["pipeline"]
        self.assertEqual(binding["evaluation"]["run_id"], run_id)
        feedback_body = {"run_id": run_id, "candidate_ids": [candidate["candidate_id"]], "request_id": "feedback-http"}
        feedback = self.post("/api/badcases/feedback", feedback_body, 201)
        self.assertEqual(self.post("/api/badcases/feedback", feedback_body, 201)["feedback_id"], feedback["feedback_id"])
        child_body = {"request_id": "child-http", "name": "BadCase 下一轮", "mode": "manual", "start_mode": "generate",
            "generation_kind": "augmentation", "feedback_id": feedback["feedback_id"], "parent_pipeline_id": p["pipeline_id"]}
        child = self.post("/api/pipelines", child_body, 201)["pipeline"]
        self.assertEqual(self.post("/api/pipelines", child_body, 201)["pipeline"]["pipeline_id"], child["pipeline_id"])
        job = self.post("/api/task-generation/badcase-jobs", {"feedback_id": feedback["feedback_id"], "request_id": "augment-http",
            "pipeline_id": child["pipeline_id"], "generate_n": 1, "auto_start": True}, 202)
        self.queue.run_all()
        self.assertEqual(self.generation.get(job["job_id"])["status"], "succeeded", self.generation.get(job["job_id"]))
        self.assertTrue(any("没有确认收藏结果" in prompt for _stage, _item, prompt in self.model.calls))
        self.manager.tick(child["pipeline_id"])
        self.assertEqual(self.manager.get(child["pipeline_id"])["status"], "waiting_for_generation_review")
        self.assertIn(child["pipeline_id"], self.manager.get(p["pipeline_id"])["next_pipeline_ids"])
        self.assertEqual(self.records.get("model_iter_results", run_id), before)


if __name__ == "__main__":
    unittest.main()
