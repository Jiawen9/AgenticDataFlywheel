"""Offline model-config smoke: platform -> CLI multipart -> real Flask -> worker -> runner.

No sockets, ADB, model requests or collection/evaluation processes are started.
Only process launch/background scheduling is replaced; configuration parsing,
request identities, local persistence and runner environment freezing are real.
"""
from __future__ import annotations

import argparse
import base64
import contextlib
import copy
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import urllib.error
from urllib.parse import urlsplit


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--platform-root", required=True)
    parser.add_argument("--collector-root", required=True)
    parser.add_argument("--pipeline-root", required=True)
    parser.add_argument("--temp-root", required=True)
    args = parser.parse_args()
    platform, collector, pipeline = (Path(getattr(args, key)).resolve()
                                    for key in ("platform_root", "collector_root", "pipeline_root"))
    temporary = Path(args.temp_root).resolve()
    temporary.mkdir(parents=True, exist_ok=True)
    tempfile.tempdir = str(temporary)
    sys.path[:0] = [str(platform), str(pipeline), str(collector / "tests"), str(collector)]
    from backend.phone_factory import DEFAULT_CONFIG, PhoneFactoryStore
    from backend import model_iteration, phonefactory_client
    from backend.tests.test_phone_factory_runtime import workbook_bytes
    from backend.tests.test_model_iteration import evaluator_config
    import test_phonefactory_engines as fixtures
    import collector_runner as runner
    from collector_runtime import redact

    manager = fixtures.manager
    roles = {"gui_vla": "GUI_VLA", "planner": "PLANNER", "llm_check": "LLM_CHECK",
             "vlm_check": "VLM_CHECK", "supervisor": "SUPERVISOR", "replanner": "REPLANNER",
             "formalizer": "FORMALIZATION"}
    secret = "unit-test-credential-value"

    class ModelCrossChecks(unittest.TestCase):
        def setUp(self):
            self.fixture = fixtures.EngineTests()
            self.fixture.setUp()
            self.addCleanup(self.fixture.doCleanups)
            self.root = self.fixture.root
            self.flask = self.fixture.client
            self.fixture.enable_model_overrides()
            self.cli_calls, self.requests, self.frozen = [], [], []
            self.integer_capability = False
            profile = {"models": {role: {"base_url": f"http://{role}.invalid/v1",
                "model": f"original-{role}", "api_key": f"original-key-{role}"} for role in roles}}
            self.env_file = self.fixture.pipeline / ".env"
            self.env_file.write_text(f"PIPELINE_SMOKE_TOKEN={secret}\n", encoding="utf-8")
            profile_path = self.root / "pipeline-profile.json"
            profile_path.write_text(json.dumps(profile), encoding="utf-8")
            env = {"PHONE_FACTORY_PIPELINE_PROFILE": str(profile_path),
                   "PHONE_FACTORY_EVALUATOR_DIR": str(collector / "evaluator_runtime"),
                   "PHONE_FACTORY_EVALUATOR_PYTHON": sys.executable,
                   **{value["api_key_env"]: "offline-evaluator-key" for value in evaluator_config().values()}}
            environment = patch.dict(os.environ, env)
            environment.start()
            self.addCleanup(environment.stop)
            runtime_root = patch.object(runner, "ROOT", self.fixture.pipeline)
            runtime_root.start()
            self.addCleanup(runtime_root.stop)
            dispatcher = manager._evaluation_dispatcher()
            schedule = patch.object(dispatcher, "start")
            schedule.start()
            self.addCleanup(schedule.stop)
            self.addCleanup(dispatcher.close)
            self.fixture.pipeline_launch.side_effect = self.launch_worker
            self.store = PhoneFactoryStore(self.root / "platform", run_client_fn=self.client_call)
            self.store.initialize_settings({"phones": ["phone-a"], "apps": ["App"],
                "phoneApps": [{"phone_id": "phone-a", "app": "App"}], "vla": ["http://execution.invalid/v1"]})
            model_iteration.save_config(self.store, evaluator_config())
            self.addCleanup(self.store.runtime.transfer.close)

        def launch_worker(self, worker):
            # The actual worker JSON is consumed by the actual runner's validation
            # and freezing functions, but no child or native model is constructed.
            config = runner.validate_config(json.loads(Path(worker["config_path"]).read_text(encoding="utf-8")))
            with patch.dict(os.environ, {}, clear=True):
                runner.freeze_environment(config)
            self.frozen.append(config)
            return SimpleNamespace(pid=None)

        def client_call(self, argv):
            self.cli_calls.append(list(argv))
            def urlopen(request, timeout=None):
                url = urlsplit(request.full_url)
                path = url.path + ("?" + url.query if url.query else "")
                headers = dict(request.header_items())
                response = self.flask.open(path, method=request.get_method(), data=request.data, headers=headers)
                if url.path == "/start_run":
                    self.requests.append((request.data, headers))
                if response.status_code >= 400:
                    raise urllib.error.HTTPError(request.full_url, response.status_code,
                        response.get_data(as_text=True), response.headers, io.BytesIO(response.data))
                body = response.data
                if url.path == "/capabilities" and self.integer_capability:
                    value = response.get_json()
                    for capability in value["agent_types"].values():
                        capability["model_iteration_evaluation"] = 1
                        capability["offline_evaluation"] = 1
                    body = json.dumps(value).encode("utf-8")
                stream = io.BytesIO(body)
                stream.headers = response.headers
                return stream
            output = io.StringIO()
            with patch.object(sys, "argv", ["phonefactory_client", *argv]), \
                 patch.object(phonefactory_client.urllib.request, "urlopen", urlopen), \
                 contextlib.redirect_stdout(output):
                try:
                    phonefactory_client.main()
                except SystemExit as exc:
                    return {"ok": False, "output": output.getvalue() + str(exc)}
            return {"ok": True, "output": output.getvalue()}

        def exercise(self, mode, overrides=True):
            added = self.store.add_task({"filename": "models.xlsx", "description": "Model configuration smoke",
                "content_base64": base64.b64encode(workbook_bytes()).decode()}, mode=mode)
            planner = {"base_url": "planner.invalid:8001/v1/chat/completions/", "model": "planner-smoke",
                       "api_key_env": "PIPELINE_SMOKE_TOKEN"}
            request = {"filename": added["imported_task"]["filename"], "request_id": "model-smoke-" + mode,
                       "agent_type": "pipelineagent", "vla": "http://execution.invalid/v1/chat/completions/",
                       "phone_id": "phone-a", "app": "App"}
            if overrides:
                request.update(execution_model="execution-smoke", planner_config=planner)
            run = self.store.remote_start(request, mode=mode)
            self.assertEqual(len(self.frozen), 1)
            record = manager._load_http_run(run["collection_run_id"])
            self.assertEqual(record["run_mode"], mode)
            self.assertEqual(record["agent_type"], "pipelineagent")
            metadata = run["request"]["metadata"] if mode == "generate" else run["request"]
            config = self.frozen[0]
            frozen = config["frozen_environment"]
            self.assertEqual(frozen["GUI_VLA_BASE_URL"], "http://execution.invalid/v1")
            self.assertEqual(frozen["GUI_VLA_MODEL"], "execution-smoke" if overrides else "original-gui_vla")
            self.assertEqual(frozen["PLANNER_MODEL"], "planner-smoke" if overrides else "original-planner")
            for role, prefix in roles.items():
                if role not in {"gui_vla", "planner"}:
                    self.assertEqual(frozen[prefix + "_MODEL"], "original-" + role)
            if overrides:
                self.assertEqual(config["model"], "execution-smoke")
                self.assertEqual(record["params"]["planner_config"], metadata["planner_config"])
                self.assertEqual(frozen["PLANNER_BASE_URL"], "http://planner.invalid:8001/v1/chat/completions")
                self.assertEqual(frozen["PLANNER_API_KEY"], secret)
                self.assertNotIn("api_key", record["params"]["planner_config"])
            else:
                for item, key in ((record["params"], "execution_model"), (metadata, "execution_model"),
                                  (config, "model"), (config, "planner_config")):
                    self.assertNotIn(key, item)
            for public in (metadata, record["params"], redact(config), self.store.runtime.run_views(mode)):
                self.assertNotIn(secret, json.dumps(public))
            body, headers = self.requests[-1]
            self.assertNotIn(secret.encode(), body)
            digest_before = record["request_digest"]
            replay = self.flask.open("/start_run", method="POST", data=body, headers=headers)
            self.assertEqual(replay.status_code, 200, replay.get_json())
            self.assertTrue(replay.get_json()["reused"])
            self.assertEqual(manager._load_http_run(run["collection_run_id"])["request_digest"], digest_before)
            if overrides:
                for original, changed in ((b"planner-smoke", b"planner-changed"),
                                          (b"execution-smoke", b"execution-changed")):
                    conflict = self.flask.open("/start_run", method="POST", data=body.replace(original, changed), headers=headers)
                    self.assertEqual(conflict.status_code, 409, conflict.get_json())
                self.assertEqual(manager._load_http_run(run["collection_run_id"])["request_digest"], digest_before)
                for changed in ({"model": "another-executor"},
                                {"planner_config": {**config["planner_config"], "model": "another-planner"}}):
                    variation = runner.validate_config({**copy.deepcopy(config), **changed})
                    with patch.dict(os.environ, {}, clear=True):
                        runner.freeze_environment(variation)
                    self.assertNotEqual(config["configuration_digest"], variation["configuration_digest"])
            self.store.dispatch("config", "POST", {**DEFAULT_CONFIG, "model_defaults": {
                "vla": "http://changed.invalid/v1", "execution_model": "changed-default", "planner_config": None}}, mode=mode)
            self.assertTrue(self.store.remote_start(request, mode=mode)["reused"])
            self.assertEqual(len(self.frozen), 1)
            self.env_file.write_text("PIPELINE_SMOKE_TOKEN=changed-token\n", encoding="utf-8")
            with patch.dict(os.environ, {"PLANNER_MODEL": "changed-environment"}, clear=True):
                runner.apply_frozen_environment(config)
                self.assertEqual(os.environ["PLANNER_MODEL"], frozen["PLANNER_MODEL"])
                if overrides:
                    self.assertEqual(os.environ["PLANNER_API_KEY"], secret)
            if mode == "modeliter":
                self.assertEqual(record["params"]["evaluator_config"], evaluator_config())
                self.assertEqual(self.store.records.list("collection_runs"), [])

        def test_generate_model_config_roundtrip(self):
            self.exercise("generate")

        def test_modeliter_model_config_roundtrip(self):
            self.exercise("modeliter")

        def test_modeliter_legacy_integer_capability_roundtrip(self):
            self.integer_capability = True
            self.exercise("modeliter")

        def test_omitted_overrides_preserve_collector_models_and_request_identity(self):
            self.exercise("generate", overrides=False)

    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(ModelCrossChecks))
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
