"""Offline tests for per-mode defaults and immutable model override dispatch."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.phone_factory import DEFAULT_CONFIG, PhoneFactoryError, PhoneFactoryStore, create_router
from backend.pipeline_model_config import default_models, normalize_defaults, normalize_overrides
from backend.phonefactory_client import build_parser, cmd_start_run
from backend.tests import test_phone_factory_runtime as runtime_fixture
from backend.tests import test_pipeline_result_batches as pipeline_fixture
from backend.pipelines import PipelineError
from backend.agent_types import engine_capabilities


PLANNER = {"base_url": "http://planner.invalid/v1", "model": "planner-model", "api_key_env": "PIPELINE_TEST_KEY"}
MODELS = {"vla": "http://executor.invalid/v1", "execution_model": "executor-model", "planner_config": PLANNER}


class ModelDefaultTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = PhoneFactoryStore(Path(self.temp.name) / "data")

    def test_two_routes_save_independent_models_and_keep_shared_sampling(self):
        app = FastAPI()
        app.include_router(create_router(self.store))
        app.include_router(create_router(self.store, prefix="/api/model-iter", mode="modeliter"))
        with TestClient(app) as client:
            self.assertEqual(client.get("/api/model-iter/config").json()["model_defaults"], default_models())
            self.assertFalse(self.store.records.database_path.exists())
            one = {**DEFAULT_CONFIG, "agent_type": "pipelineagent", "temperature": 0.2, "model_defaults": MODELS}
            self.assertEqual(client.post("/api/phone-factory/config", json=one).status_code, 200)
            evaluation = {**MODELS, "execution_model": "evaluation-executor"}
            two = {**DEFAULT_CONFIG, "temperature": 0.3, "model_defaults": evaluation}
            self.assertEqual(client.post("/api/model-iter/config", json=two).status_code, 200)
            production = client.get("/api/phone-factory/config").json()
            self.assertEqual(production["model_defaults"], MODELS)
            self.assertEqual(production["temperature"], 0.3)
            self.assertEqual(production["agent_type"], "pipelineagent")
            self.assertEqual(client.get("/api/model-iter/config").json()["model_defaults"], evaluation)
            self.assertEqual(client.post("/api/phone-factory/config", json=DEFAULT_CONFIG).status_code, 200)
            self.assertEqual(client.get("/api/phone-factory/config").json()["model_defaults"], MODELS)
            self.assertEqual(client.post("/api/phone-factory/config", json={**DEFAULT_CONFIG, "model_defaults": {}}).status_code, 200)
            self.assertEqual(client.get("/api/phone-factory/config").json()["model_defaults"], default_models())
            self.assertEqual(client.get("/api/model-iter/config").json()["model_defaults"], evaluation)

    def test_validation_rejects_secret_and_partial_planner_without_writes(self):
        invalid_models = [
            {"planner_config": {"base_url": "http://planner.invalid"}},
            {"planner_config": {"model": "model"}},
            {"planner_config": {**PLANNER, "api_key": "secret-value"}},
            {"planner_config": {**PLANNER, "api_key_env": "invalid-key"}},
            {"planner_config": {**PLANNER, "base_url": "https://user:password@host/v1"}},
            {"planner_config": {**PLANNER, "base_url": "https://host/v1?key=secret"}},
            {"planner_config": {**PLANNER, "model": "model\n"}},
            {"execution_model": 123}, {"unexpected": "value"},
        ]
        for value in invalid_models:
            with self.subTest(value=value), self.assertRaises(PhoneFactoryError):
                self.store.dispatch("config", "POST", {**DEFAULT_CONFIG, "model_defaults": value})
        self.assertFalse(self.store.records.database_path.exists())

    def test_url_normalization_and_optional_environment_reference(self):
        for source, expected in (("planner.invalid:8000", "http://planner.invalid:8000/v1"),
                                 ("https://planner.invalid/custom/", "https://planner.invalid/custom"),
                                 ("https://planner.invalid/v1/chat/completions/", "https://planner.invalid/v1/chat/completions")):
            result = normalize_defaults({"planner_config": {"base_url": source, "model": " model ", "api_key_env": ""}})
            self.assertEqual(result["planner_config"], {"base_url": expected, "model": "model"})
        self.assertEqual(normalize_overrides({"execution_model": " ", "planner_config": None}, "newagent"), {})


class ModelRunTests(unittest.TestCase):
    setUp = runtime_fixture.FactoryRuntimeTests.setUp
    request = runtime_fixture.FactoryRuntimeTests.request

    def client(self, args):
        result = runtime_fixture.FactoryRuntimeTests.client(self, args)
        value = json.loads(result["output"])
        if args[0] == "capabilities":
            value["agent_types"] = {name: {"available": True, "collection": True, "capture_logprobs": True,
                "model_iteration_evaluation": True, "model_overrides": True} for name in ("pipelineagent", "newagent")}
        elif args[0] == "start-run":
            value["agent_type"] = args[args.index("--agent-type") + 1]
            self.remote[value["run_id"]]["agent_type"] = value["agent_type"]
        result["output"] = json.dumps(value)
        return result

    def save_models(self, mode, models=MODELS):
        self.store.dispatch("config", "POST", {**DEFAULT_CONFIG, "model_defaults": models}, mode=mode)

    def test_dispatch_freezes_overrides_in_both_modes_and_retries_after_default_change(self):
        for mode in ("generate", "modeliter"):
            with self.subTest(mode=mode):
                self.store.add_task(self.upload, mode=mode)
                data = self.request(request_id="models-" + mode, agent_type="pipelineagent",
                    execution_model=MODELS["execution_model"], planner_config=PLANNER)
                run = self.store.remote_start(data, mode=mode)
                args = [item for item in self.calls if item[0] == "start-run"][-1]
                self.assertEqual(args[args.index("--execution-model") + 1], MODELS["execution_model"])
                self.assertEqual(json.loads(args[args.index("--planner-config") + 1]), PLANNER)
                metadata = run["request"]["metadata"] if mode == "generate" else run["request"]
                self.assertEqual(metadata["planner_config"], PLANNER)
                view = next(item for item in self.store.runtime.run_views(mode) if item["collection_run_id"] == run["collection_run_id"])
                self.assertEqual(view["planner_config"], PLANNER)
                self.assertEqual(view["execution_model"], MODELS["execution_model"])
                self.save_models(mode, {**MODELS, "execution_model": "new-default"})
                starts = len([item for item in self.calls if item[0] == "start-run"])
                replay = self.store.remote_start(data, mode=mode)
                self.assertEqual(replay["collection_run_id"], run["collection_run_id"])
                self.assertTrue(replay["reused"])
                self.assertEqual(len([item for item in self.calls if item[0] == "start-run"]), starts)
                for change in ({"execution_model": "changed"}, {"planner_config": {**PLANNER, "model": "changed"}}):
                    with self.assertRaises(ValueError):
                        self.store.remote_start({**data, **change}, mode=mode)

    def test_legacy_requests_keep_no_new_fields_even_when_defaults_saved(self):
        for mode in ("generate", "modeliter"):
            self.store.add_task(self.upload, mode=mode)
            data = self.request(request_id="legacy-models-" + mode, agent_type="pipelineagent")
            initial = self.store.remote_start(data, mode=mode)
            self.save_models(mode)
            replay = self.store.remote_start(data, mode=mode)
            self.assertTrue(replay["reused"])
            self.assertEqual(initial["request"], replay["request"])
            options = self.store.runtime._options(data, mode)
            self.assertNotIn("execution_model", options)
            self.assertNotIn("planner_config", options)
        for args in self.calls:
            self.assertNotIn("--execution-model", args)
            self.assertNotIn("--planner-config", args)

    def test_old_collector_cannot_silently_ignore_new_overrides(self):
        self.store.add_task(self.upload)
        client = self.store.run_client
        def old_collector(args):
            result = client(args)
            if args[0] == "capabilities":
                value = json.loads(result["output"])
                value["agent_types"]["pipelineagent"].pop("model_overrides")
                result["output"] = json.dumps(value)
            return result
        self.store.run_client = old_collector
        with self.assertRaisesRegex(PhoneFactoryError, "模型覆盖配置"):
            self.store.remote_start(self.request(agent_type="pipelineagent", execution_model="new-model"))
        self.assertFalse(any(item[0] == "start-run" for item in self.calls))
        self.store.remote_start(self.request(request_id="old-compatible", agent_type="pipelineagent"))
        self.assertTrue(any(item[0] == "start-run" for item in self.calls))

    def test_newagent_and_evaluate_only_reject_model_overrides(self):
        for data in ({"execution_model": "model"}, {"planner_config": PLANNER}):
            with self.assertRaisesRegex(PhoneFactoryError, "PipelineAgent"):
                self.store.runtime._options(self.request(**data), "generate")
            with self.assertRaisesRegex(PhoneFactoryError, "独立评测"):
                self.store.runtime.remote_start_evaluation(data)
        self.assertFalse(self.calls)

    def test_legacy_integer_evaluation_capability_is_boolean_at_platform_boundary(self):
        options = self.store.runtime._options(self.request(agent_type="pipelineagent"), "modeliter")
        for value in (True, 1, False, 0, "1", 1.0):
            remote = {"protocol_version": 1, "batch_results": True, "agent_types": {"pipelineagent": {
                "available": True, "collection": True, "model_iteration_evaluation": value,
                "offline_evaluation": value}}}
            expected = value is True or (type(value) is int and value == 1)
            normalized = engine_capabilities(remote)["pipelineagent"]
            self.assertIs(normalized["model_iteration_evaluation"], expected)
            self.assertIs(normalized["offline_evaluation"], expected)
            with patch.object(self.store, "_remote_result", return_value=remote):
                if expected:
                    self.store.runtime._require_agent_capability(options)
                else:
                    with self.assertRaisesRegex(PhoneFactoryError, "模型迭代评测"):
                        self.store.runtime._require_agent_capability(options)

    def test_client_emits_new_fields_only_when_requested(self):
        task, apps = self.root / "task.xlsx", self.root / "apps.json"
        task.write_bytes(b"workbook")
        apps.write_text("[]")
        for enabled in (False, True):
            command = ["start-run", str(task), str(apps), "--agent-type", "pipelineagent"]
            if enabled:
                command += ["--execution-model", "executor-model", "--planner-config", json.dumps(PLANNER)]
            with patch("backend.phonefactory_client.post_multipart", return_value={}) as post:
                cmd_start_run(build_parser().parse_args(command))
            fields = post.call_args.args[1]
            self.assertEqual("execution_model" in fields, enabled)
            self.assertEqual("planner_config" in fields, enabled)
            if enabled:
                self.assertEqual(json.loads(fields["planner_config"]), PLANNER)


class ModelPipelineTests(unittest.TestCase):
    setUp = pipeline_fixture.PipelineResultBatchTests.setUp
    request = pipeline_fixture.PipelineResultBatchTests.request

    def client(self, args):
        result = pipeline_fixture.PipelineResultBatchTests.client(self, args)
        if args[0] == "capabilities":
            value = json.loads(result["output"])
            value["agent_types"]["pipelineagent"]["model_overrides"] = True
            result["output"] = json.dumps(value)
        return result

    def test_pipeline_keeps_frozen_models_when_defaults_change(self):
        for enabled in (False, True):
            request = self.request("models" if enabled else "legacy")
            if enabled:
                request["collection_config"].update(execution_model="frozen-executor", planner_config=PLANNER)
            pipeline = self.manager.create(request)
            self.factory.dispatch("config", "POST", {**DEFAULT_CONFIG, "model_defaults": MODELS})
            self.manager.tick(pipeline["pipeline_id"])
            current = self.manager.get(pipeline["pipeline_id"])
            self.assertEqual(current["status"], "running", current.get("error"))
            run = self.factory.collection_runs.get(current["collection_run_ids"][0])
            metadata = run["request"]["metadata"]
            self.assertEqual("execution_model" in metadata, enabled)
            if enabled:
                self.assertEqual(metadata["execution_model"], "frozen-executor")
                self.assertEqual(metadata["planner_config"], PLANNER)
                self.assertEqual(pipeline["collection_config"]["planner_config"], PLANNER)
                changed = {**request, "collection_config": {**request["collection_config"], "execution_model": "changed"}}
                with self.assertRaises(PipelineError):
                    self.manager.create(changed)
