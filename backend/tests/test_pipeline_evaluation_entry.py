"""Round ownership, dispatch recovery and final BadCase handoff at real routes."""
import base64
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend import model_iteration, phonefactory_client, pipeline_router
from backend.badcases import BadCaseService
from backend.phone_factory import PhoneFactoryStore, create_router
from backend.pipelines import PipelineManager, PipelineRuntime
from backend.tests.collector_protocol_support import current_capabilities
from backend.tests.test_model_iteration import evaluator_config, results
from backend.tests.test_offline_evaluation_jobs import ManualPool
from backend.tests.test_phone_factory_runtime import workbook_bytes


class EvaluationPipelineTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.calls, self.remote_runs = [], {}
        self.lose_start_response = False
        self.enterContext(patch.dict("os.environ", {"PHONE_FACTORY_SERVER": "http://localhost:9011"}))
        self.open_application()
        self.factory.initialize_settings({"phones": ["phone-1"], "apps": ["App"],
            "phoneApps": [{"phone_id": "phone-1", "app": "App"}], "vla": ["http://vla.invalid"]})
        self.factory.add_task({"filename": "eval.xlsx", "description": "cases",
            "content_base64": base64.b64encode(workbook_bytes()).decode()}, mode="modeliter")
        model_iteration.save_config(self.factory, evaluator_config())

    def open_application(self):
        self.factory = PhoneFactoryStore(self.root, run_client_fn=self.collector)
        self.runtime = self.factory.runtime
        self.runtime._evaluation_pool.close()
        self.runtime._evaluation_pool = ManualPool()
        self.addCleanup(self.runtime.close)
        self.records = self.factory.records
        pipeline_runtime = PipelineRuntime(self.root, factory=self.factory, preprocessing=None,
                                           tree=None, quality=None, cot=None, overview=None)
        self.pipeline = PipelineManager(pipeline_runtime, root=self.root)
        app = FastAPI()
        app.state.pipelines = self.pipeline
        app.include_router(pipeline_router.router)
        app.include_router(create_router(self.factory, prefix="/api/model-iter", mode="modeliter"))
        self.http = TestClient(app)
        self.addCleanup(self.http.close)

    def collector(self, args):
        self.calls.append(args)
        value = {"ok": True}
        if args[0] == "capabilities":
            value.update(protocol_version=1, batch_results=True, model_iteration_evaluation=1,
                         offline_evaluation_jobs={"version": 1, "readonly_input_version": 1})
            current_capabilities(value)
        elif args[0] == "start-run":
            parsed = phonefactory_client.build_parser().parse_args(args)
            self.assertEqual(parsed.server, "http://localhost:9011")
            self.assertEqual(parsed.runmode, "modeliter")
            value = self.remote_runs.setdefault(parsed.collection_run_id, {
                "ok": True, "run_id": parsed.collection_run_id, "run_mode": "modeliter", "status": "running"})
            if self.lose_start_response:
                self.lose_start_response = False
                return {"ok": False, "output": "lost response"}
        elif args[0] == "start-evaluation":
            request = json.loads(args[2])
            self.assertNotIn("pipeline_id", request)
            run_id = request["collection_run_id"]
            value = self.remote_runs.setdefault(run_id, {"ok": True, "run_id": run_id, "run_mode": "modeliter",
                "execution_mode": "evaluate_only", "status": "running"})
        elif args[0] == "run":
            if args[1] not in self.remote_runs:
                return {"ok": False, "output": "run not found"}
            value = self.remote_runs[args[1]]
        return {"ok": True, "output": json.dumps(value)}

    def new_round(self):
        response = self.http.post("/api/pipelines", json={"request_id": "create-eval-round", "start_mode": "evaluation",
            "mode": "manual", "name": "评测起点"})
        self.assertEqual(response.status_code, 201, response.text)
        pipeline = response.json()["pipeline"]
        self.assertEqual(pipeline["status"], "waiting_for_evaluation")
        self.assertEqual(pipeline["current_step"], "evaluation")
        self.assertIsNone(pipeline["batch_id"])
        return pipeline

    def start_online(self, pipeline, **options):
        body = {"pipeline_id": pipeline["pipeline_id"], "request_id": "online-one", "filename": "eval.xlsx",
                "vla": "http://vla.invalid", **options}
        return self.http.post("/api/model-iter/remote/start-run", json=body)

    def control(self, pipeline, action):
        return self.pipeline.control(pipeline["pipeline_id"], action, pipeline["storage_revision"])

    def test_online_restarts_before_dispatch_with_frozen_id_and_replay(self):
        pipeline = self.new_round()
        with patch.object(self.runtime, "_dispatch_registered", return_value={"persisted": True}):
            self.assertEqual(self.start_online(pipeline).json(), {"persisted": True})
        waiting = self.pipeline.get(pipeline["pipeline_id"])
        run_id = waiting["evaluation_run_id"]
        run = self.records.get("model_iter_runs", run_id)
        self.assertFalse(run["dispatch_attempted"])
        self.assertEqual(run["pipeline_id"], pipeline["pipeline_id"])
        self.assertEqual(self.start_online(pipeline, request_id="other-tab").status_code, 409)
        self.assertFalse(self.remote_runs)

        self.runtime.close()
        self.open_application()
        changed = evaluator_config()
        changed["text"]["model"] = "new-default"
        model_iteration.save_config(self.factory, changed)
        self.runtime._schedule_evaluations()
        self.runtime._evaluation_pool.drain()
        replay = self.start_online(pipeline)
        self.assertEqual(replay.status_code, 200, replay.text)
        self.assertEqual(replay.json()["run_id"], run_id)
        self.assertEqual(self.start_online(pipeline, vla="http://different.invalid").status_code, 409)
        launches = [call for call in self.calls if call[0] == "start-run"]
        self.assertEqual(len(launches), 1)
        self.assertEqual(json.loads(launches[0][launches[0].index("--evaluator-config") + 1]), evaluator_config())

    def test_scoped_dispatch_rejects_collector_address_change(self):
        pipeline = self.new_round()
        with patch.object(self.runtime, "_dispatch_registered", return_value={}):
            self.start_online(pipeline)
        with patch.dict("os.environ", {"PHONE_FACTORY_SERVER": "http://replacement.invalid:9011"}):
            replay = self.start_online(pipeline)
        self.assertEqual(replay.status_code, 409)
        self.assertIn("采集服务地址", replay.json()["error"])
        self.assertFalse(self.remote_runs)

    def test_failed_round_retry_fences_retired_request(self):
        pipeline = self.new_round()
        with patch.object(self.runtime, "_dispatch_registered", return_value={}):
            self.start_online(pipeline)
        run_id = self.pipeline.get(pipeline["pipeline_id"])["evaluation_run_id"]
        self.records.update("model_iter_runs", run_id, lambda row: row.update(status="failed", dispatch_error="测试下发失败"))
        self.pipeline.tick(pipeline["pipeline_id"])
        failed = self.pipeline.get(pipeline["pipeline_id"])
        self.assertEqual(failed["status"], "failed")
        retried = self.control(failed, "retry")
        self.assertEqual(retried["evaluation_attempt"], 1)
        self.assertIsNone(retried["evaluation_run_id"])
        self.assertEqual(self.start_online(pipeline).json()["run_id"], run_id)
        self.assertFalse(self.remote_runs)
        self.assertEqual(self.start_online(pipeline, request_id="online-retry").status_code, 200)
        self.assertEqual(len(self.remote_runs), 1)

    def test_lost_start_response_is_recovered_under_original_identity(self):
        pipeline = self.new_round()
        self.lose_start_response = True
        uncertain = self.start_online(pipeline)
        self.assertEqual(uncertain.status_code, 502, uncertain.text)
        run_id = self.pipeline.get(pipeline["pipeline_id"])["evaluation_run_id"]
        self.assertEqual(list(self.remote_runs), [run_id])

        self.runtime.close()
        self.open_application()
        self.runtime._schedule_evaluations()
        self.runtime._evaluation_pool.drain()
        self.assertEqual(self.records.get("model_iter_runs", run_id)["status"], "running")
        self.assertEqual([call[0] for call in self.calls].count("start-run"), 1)

    def test_terminate_unlaunched_registered_evaluation_does_not_wait(self):
        pipeline = self.new_round()
        with patch.object(self.runtime, "_dispatch_registered", return_value={}):
            self.start_online(pipeline)
        ready = self.pipeline.get(pipeline["pipeline_id"])
        terminated = self.control(ready, "terminate")
        self.pipeline.tick(pipeline["pipeline_id"])
        terminated = self.pipeline.get(pipeline["pipeline_id"])
        self.assertEqual(terminated["status"], "terminated")
        replay = self.start_online(pipeline)
        self.assertEqual(replay.status_code, 200, replay.text)
        self.assertEqual(replay.json()["run_id"], ready["evaluation_run_id"])
        self.runtime._schedule_evaluations()
        self.runtime._evaluation_pool.drain()
        self.assertFalse(self.remote_runs)

    def test_offline_evaluation_is_a_single_durable_operation(self):
        pipeline = self.new_round()
        preview_id = "preview-done"
        self.records.put("model_iter_previews", preview_id, {"paged": True, "cached_count": 1,
            "collector_server": "http://localhost:9011", "request": {"source_path": "D:/archive"},
            "preview": {"valid": True, "status": "completed", "trajectory_count": 1, "revision": 1,
                        "source": {"kind": "directory", "path": "D:/archive"}, "source_digest": "s"}})
        body = {"pipeline_id": pipeline["pipeline_id"], "request_id": "offline-one", "preview_id": preview_id,
                "evaluation_profile_id": "rules", "evaluation_profile_digest": "1" * 64}
        url = "/api/model-iter/remote/start-evaluation"
        initial = self.http.post(url, json=body)
        self.assertEqual(initial.status_code, 200, initial.text)
        run_id = initial.json()["run_id"]
        self.assertEqual(self.pipeline.get(pipeline["pipeline_id"])["evaluation_run_id"], run_id)
        self.assertEqual(self.http.post(url, json=body).json()["run_id"], run_id)
        paused = self.control(self.pipeline.get(pipeline["pipeline_id"]), "pause")
        self.runtime._evaluation_pool.drain()
        self.assertFalse(self.remote_runs)
        self.runtime.close()
        self.open_application()
        self.control(paused, "resume")
        self.runtime._schedule_evaluations()
        self.runtime._evaluation_pool.drain()
        self.assertEqual(self.http.post(url, json=body).json()["run_id"], run_id)
        self.assertEqual([call[0] for call in self.calls].count("start-evaluation"), 1)

    def test_empty_handoff_requires_final_even_when_result_has_not_changed(self):
        pipeline = self.new_round()
        run_id = "existing-evaluation"
        self.records.put("model_iter_runs", run_id, {"run_id": run_id, "status": "running", "request": {}})
        result = results(run_id)
        self.records.put("model_iter_results", run_id, {"result": result, "final": False})
        service = BadCaseService(self.root)
        self.assertFalse(service.refresh_run(run_id)["final"])
        body = {"run_id": run_id, "candidate_ids": [], "expected_revision": pipeline["storage_revision"],
                "request_id": "empty-feedback"}
        url = f"/api/pipelines/{pipeline['pipeline_id']}/confirm-badcases"
        self.assertEqual(self.http.post(url, json=body).status_code, 409)
        self.records.put("model_iter_results", run_id, {"result": result, "final": True})
        self.assertTrue(service.refresh_run(run_id)["final"])
        done = self.http.post(url, json=body)
        self.assertEqual(done.status_code, 200, done.text)
        value = done.json()["pipeline"]
        self.assertEqual(value["status"], "no_publishable_data")
        self.assertEqual(value["evaluation_run_id"], run_id)
        self.assertEqual(value["source_evaluation_run_id"], run_id)
        self.assertIsNone(value["batch_id"])
        self.assertIsNone(value["generation_job_id"])
        self.assertEqual(self.http.post(url, json=body).json()["pipeline"]["storage_revision"], value["storage_revision"])
        self.assertEqual(self.records.get("model_iter_results", run_id)["result"], result)
