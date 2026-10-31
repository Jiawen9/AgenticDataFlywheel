"""Dual-engine boundaries: identity, idempotency and real normalized raw files."""
from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.agent_types import engine_capabilities
from backend.collection_runs import CollectionRunError, CollectionRunStore
from backend.export_vla_trajectories import collect_rows, extract_response_fields
from backend.phone_factory import DEFAULT_CONFIG, PhoneFactoryError
from backend.pipelines import PipelineRuntime
from backend.preprocessing_service import convert_input, SHEET
from backend.task_generation.collection_batches import payload_digest
from backend.tests import test_phone_factory_runtime as fixtures
from backend.trajectory_context import row_identity_metadata


def capabilities(**overrides):
    available = dict(available=True, collection=True, capture_logprobs=True,
                     model_iteration_evaluation=True, offline_evaluation=True,
                     collection_artifacts_version=1, repeat_times=True, max_repeat_times=100)
    return {"ok": True, "protocol_version": 1, "batch_results": True,
            "model_iteration_evaluation": 1,
            "collection_artifacts_version": 1,
            "agent_types": {"newagent": available, "pipelineagent": {**available, **overrides}}}


class DualAgentDispatchTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.FactoryRuntimeTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.store = self.fixture.store
        self.caps = capabilities()
        self.calls = []
        self.responses = {}
        self.lose_response = False
        self.wrong_engine = False

        def client(args):
            self.calls.append(args)
            if args[0] == "capabilities":
                return {"ok": True, "output": json.dumps(self.caps)}
            if args[0] == "start-run":
                run = args[args.index("--collection-run-id") + 1]
                agent = args[args.index("--agent-type") + 1]
                mode = args[args.index("--runmode") + 1]
                value = {"ok": True, "run_id": run, "collection_run_id": run,
                         "run_mode": mode, "agent_type": "newagent" if self.wrong_engine else agent,
                         "status": "running", "errors": []}
                self.responses[run] = value
                if self.lose_response:
                    self.lose_response = False
                    return {"ok": False, "output": "response lost"}
                return {"ok": True, "output": json.dumps(value)}
            if args[0] == "run" and args[1] in self.responses:
                return {"ok": True, "output": json.dumps(self.responses[args[1]])}
            return self.fixture.client(args)

        self.store.run_client = client
        self.task = self.store.add_task(self.fixture.upload)["imported_task"]

    def request(self, **extra):
        return self.fixture.request(filename=self.task["filename"], agent_type="pipelineagent", **extra)

    def starts(self):
        return [args for args in self.calls if args[0] == "start-run"]

    def test_saved_default_is_separate_from_run_sampling_and_omitted_old_requests(self):
        self.store.dispatch("config", "POST", {**DEFAULT_CONFIG, "agent_type": "pipelineagent"})
        self.assertEqual(self.store.config()["agent_type"], "pipelineagent")
        options = self.store.runtime._options({"vla": "http://vla.invalid"}, "generate")
        self.assertEqual(options["agent_type"], "newagent")
        self.assertNotIn("agent_type", options["config"])
        for bad in (None, "unknown", 0, {}):
            with self.subTest(bad=bad), self.assertRaisesRegex(PhoneFactoryError, "agent_type"):
                self.store.runtime._options({"agent_type": bad}, "generate")

    def test_pipeline_upgrade_uses_fresh_capability_even_with_legacy_protocol_cache(self):
        self.store.runtime._protocol_cache = "unsupported"
        result = self.store.remote_start(self.request())
        self.assertEqual(result["request"]["metadata"]["agent_type"], "pipelineagent")
        self.assertEqual(len(self.starts()), 1)

    def test_pipeline_request_is_frozen_sent_and_idempotent(self):
        first = self.store.remote_start(self.request(config={"capture_logprobs": True, "temperature": .3}))
        again = self.store.remote_start(self.request(config={"capture_logprobs": True, "temperature": .3}))
        self.assertEqual(first["collection_run_id"], again["collection_run_id"])
        self.assertEqual(first["request"]["metadata"]["agent_type"], "pipelineagent")
        self.assertEqual(len(self.starts()), 1)
        self.assertIn("--capture-logprobs", self.starts()[0])
        with self.assertRaisesRegex(ValueError, "request_id"):
            self.store.remote_start({**self.request(), "agent_type": "newagent"})

    def test_lost_ack_queries_original_engine_without_reexecution(self):
        self.lose_response = True
        with self.assertRaisesRegex(PhoneFactoryError, "response lost"):
            self.store.remote_start(self.request())
        again = self.store.remote_start(self.request())
        self.assertTrue(again["reused"])
        self.assertEqual(len(self.starts()), 1)

    def test_capability_failure_and_wrong_engine_never_silently_run_newagent(self):
        self.caps = {"protocol_version": 1, "batch_results": True}
        with self.assertRaises(PhoneFactoryError):
            self.store.remote_start(self.request())
        self.assertFalse(self.starts())
        self.caps = capabilities(capture_logprobs=False)
        with self.assertRaisesRegex(PhoneFactoryError, "Logprobs"):
            self.store.remote_start(self.request(request_id="logprobs", config={"capture_logprobs": True}))
        self.assertFalse(self.starts())
        self.caps = capabilities()
        self.wrong_engine = True
        with self.assertRaisesRegex(PhoneFactoryError, "引擎"):
            self.store.remote_start(self.request(request_id="wrong-engine"))
        with self.assertRaisesRegex(PhoneFactoryError, "不能重新下发"):
            self.store.remote_start(self.request(request_id="wrong-engine"))
        self.assertEqual(len(self.starts()), 1)

    def test_historical_newagent_replay_does_not_rewrite_frozen_request(self):
        request = {**self.request(), "agent_type": "newagent"}
        first = self.store.runtime.remote_start(request, "generate", legacy_batch=True)
        def old_record(item):
            del item["request"]["metadata"]["agent_type"]
            del item["request"]["metadata"]["config"]["capture_logprobs"]
            item["request_digest"] = payload_digest(item["request"])
        frozen = self.store.records.update("collection_runs", first["run_id"], old_record)
        self.assertTrue(self.store.remote_start(request)["reused"])
        self.assertEqual(self.store.collection_runs.get(first["run_id"]), frozen)
        with self.assertRaises(ValueError):
            self.store.remote_start(self.request())

    def test_automatic_pipeline_freezes_engine_and_legacy_defaults(self):
        runtime = PipelineRuntime(self.fixture.root, preprocessing=None, tree=None, quality=None,
                                  cot=None, factory=self.store, overview=None)
        frozen = runtime.collection_options({"agent_type": "pipelineagent"})
        pipeline = {"pipeline_id": "pipeline-dual", "name": "both", "batch_id": self.task["source_batch_id"],
                    "collection_config": frozen}
        self.store.dispatch("config", "POST", {**DEFAULT_CONFIG, "agent_type": "newagent"})
        result = runtime.dispatch_collection(pipeline)
        self.assertEqual(result["request"]["metadata"]["agent_type"], "pipelineagent")
        legacy = runtime.collection_options({})
        del legacy["agent_type"]
        before = copy.deepcopy(legacy)
        result = runtime.dispatch_collection({**pipeline, "pipeline_id": "legacy-pipeline", "collection_config": legacy})
        self.assertEqual(result["request"]["metadata"]["agent_type"], "newagent")
        self.assertEqual(legacy, before)

    def test_modeliter_keeps_pipeline_engine_and_links_collection_batch(self):
        task = self.store.add_task(self.fixture.upload, mode="modeliter")["imported_task"]
        request = {**self.request(), "filename": task["filename"], "run_mode": "modeliter"}
        result = self.store.remote_start(request, mode="modeliter")
        self.assertEqual(result["request"]["agent_type"], "pipelineagent")
        self.assertEqual(self.store.collection_runs.get(result["run_id"])["batch_id"], result["batch_id"])
        self.assertEqual(self.store.runtime.run_views("modeliter")[0]["agent_type"], "pipelineagent")

    def test_collection_cancel_confirms_remote_identity_before_retaining_artifacts(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from backend.phone_factory import create_router
        app = FastAPI()
        app.include_router(create_router(self.store))
        for engine in ("newagent", "pipelineagent"):
            with self.subTest(engine=engine):
                started = self.store.remote_start({**self.request(request_id="cancel-" + engine), "agent_type": engine})
                run_id = started["collection_run_id"]
                sentinel = self.fixture.root / (engine + "-result.json")
                sentinel.write_text("preserved", encoding="utf-8")
                self.store.records.update("collection_runs", run_id, lambda item: item.update(
                    trajectories=[{"existing": str(sentinel)}]))
                with patch.object(self.store, "_remote_result", side_effect=[self.responses[run_id], {"run_id": run_id, "status": "cancelled"}]) as remote:
                    result = TestClient(app).post(f"/api/phone-factory/collection-runs/{run_id}/cancel")
                self.assertEqual(result.status_code, 200, result.text)
                self.assertEqual(result.json()["status"], "running")
                self.assertEqual(result.json()["collection_status"], "cancelled")
                self.assertEqual(result.json()["agent_type"], engine)
                self.assertEqual(remote.call_args_list[-1].args[0], ["cancel-run", run_id])
                self.assertEqual(sentinel.read_text(encoding="utf-8"), "preserved")
                self.assertEqual(result.json()["trajectories"], [{"existing": str(sentinel)}])
                with patch.object(self.store, "_remote_result") as retry:
                    again = self.store.runtime.cancel_collection(run_id)
                self.assertEqual(again["status"], "running")
                self.assertEqual(again["collection_status"], "cancelled")
                retry.assert_not_called()

    def test_collection_cancel_failures_never_claim_stopped(self):
        started = self.store.remote_start(self.request())
        run_id = started["collection_run_id"]
        before = self.store.collection_runs.get(run_id)
        for acknowledgement in (PhoneFactoryError("offline", 502), {"run_id": run_id, "status": "running"},
                                {"run_id": "other", "status": "cancelled"}, {"run_id": run_id, "status": "succeeded"}):
            with self.subTest(acknowledgement=acknowledgement):
                with patch.object(self.store, "_remote_result", side_effect=[self.responses[run_id], acknowledgement]), self.assertRaises(PhoneFactoryError):
                    self.store.runtime.cancel_collection(run_id)
                self.assertEqual(self.store.collection_runs.get(run_id), before)
        with patch.object(self.store, "_remote_result", return_value={**self.responses[run_id], "agent_type": "newagent"}) as remote:
            with self.assertRaisesRegex(PhoneFactoryError, "未请求停止"):
                self.store.runtime.cancel_collection(run_id)
        self.assertEqual(remote.call_count, 1)

    def test_collection_cancel_honors_pipeline_owner_and_final_state(self):
        from backend.pipeline_access import PipelineManagedError
        started = self.store.remote_start(self.request())
        run_id, batch_id = started["collection_run_id"], started["batch_id"]
        self.store.records.put("pipeline_owners", batch_id, {"pipeline_id": "managed"})
        self.store.records.put("pipelines", "managed", {"pipeline_id": "managed", "batch_id": batch_id, "status": "running"})
        with patch.object(self.store, "_remote_result") as remote, self.assertRaises(PipelineManagedError):
            self.store.runtime.cancel_collection(run_id)
        remote.assert_not_called()
        self.store.records.update("pipelines", "managed", lambda item: item.update(status="terminated"))
        self.store.records.update("collection_runs", run_id, lambda item: item.update(status="completed"))
        with patch.object(self.store, "_remote_result") as remote, self.assertRaisesRegex(PhoneFactoryError, "活动采集"):
            self.store.runtime.cancel_collection(run_id)
        remote.assert_not_called()

    def test_starting_evaluation_is_polled_as_active(self):
        task = self.store.add_task(self.fixture.upload, mode="modeliter")["imported_task"]
        result = self.store.remote_start({**self.request(), "filename": task["filename"]}, mode="modeliter")
        self.responses[result["run_id"]]["status"] = "starting"
        self.assertEqual(self.store.runtime.sync_evaluation(result["run_id"])["status"], "starting")

    def test_cli_serializes_explicit_engine_in_multipart(self):
        from backend.phonefactory_client import build_parser, cmd_start_run
        tasks, apps = self.fixture.root / "input.xlsx", self.fixture.root / "apps.json"
        tasks.write_bytes(b"test"); apps.write_text("[]", encoding="utf-8")
        args = build_parser().parse_args(["start-run", str(tasks), str(apps), "--agent-type", "pipelineagent"])
        with patch("backend.phonefactory_client.post_multipart") as send:
            cmd_start_run(args)
        self.assertEqual(send.call_args.args[1]["agent_type"], "pipelineagent")


class PipelineTrajectoryContractTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.directory = self.root / "runs" / "run-a" / "case-a" / "phone__session"
        self.directory.mkdir(parents=True)
        self.response = self.directory / "step001_vla_model_response.json"
        self.payload = {"agent_type": "pipelineagent", "content": "original imperfect model reply",
                        "parsed_action": {"action": "click", "coordinate": [.2, .3]},
                        "parsed_summary": "Search", "parse_status": "repaired", "repair_applied": True,
                        "global_step": 1, "actual_action_coordinate_system": "pixel",
                        "executed_action": {"action": "click", "coordinate": [200, 300]}}
        self.response.write_text(json.dumps(self.payload), encoding="utf-8")
        (self.directory / "_trajectory_for_evaluate.json").write_text(json.dumps({"actions_flat": [
            {"global_step": 1, "action": {"action": "click", "coordinate": [200, 300]}}]}), encoding="utf-8")
        (self.directory / "step001_vla_input.jpg").write_bytes(b"image")
        (self.directory / "step001_vla_input_ui.xml").write_text("old XML", encoding="utf-8")
        (self.directory / "step001_vla_input_stability_ui.xml").write_text("stable XML", encoding="utf-8")

    def test_fallback_requires_explicit_pipeline_parse_evidence_and_keeps_raw(self):
        original = self.response.read_bytes()
        action, summary = extract_response_fields(self.response)
        self.assertEqual(json.loads(action), self.payload["executed_action"])
        self.assertEqual(summary, "Search")
        self.assertEqual(self.response.read_bytes(), original)
        for invalid in ({"agent_type": "newagent"}, {"parse_status": "invalid"},
                        {"repair_applied": False}, {"parsed_action": {}}, {"parsed_summary": 3}):
            self.response.write_text(json.dumps({**self.payload, **invalid}), encoding="utf-8")
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                extract_response_fields(self.response)

    def test_pipeline_wrapped_tool_call_uses_recorded_parser_action(self):
        payload = {**self.payload, "parse_status": "parsed", "repair_applied": False,
                   "content": '<tool_call>{"name":"computer","arguments":{"action":"click","coordinate":[0.2,0.3]}}</tool_call>'}
        self.response.write_text(json.dumps(payload), encoding="utf-8")
        action, _ = extract_response_fields(self.response)
        self.assertEqual(json.loads(action), payload["executed_action"])
        self.assertEqual(json.loads(self.response.read_text(encoding="utf-8"))["content"], payload["content"])

    def test_pipeline_valid_raw_stays_original_but_exports_executed_pixels(self):
        self.response.write_text(json.dumps({**self.payload, "content": '<tool_call>{"action":"wait"}</tool_call><summary>Raw</summary>'}), encoding="utf-8")
        action, summary = extract_response_fields(self.response)
        self.assertEqual(json.loads(action), self.payload["executed_action"])
        self.assertEqual(summary, "Raw")

    def test_missing_or_mismatched_executed_action_rejects_formal_pipeline_source(self):
        store = CollectionRunStore(self.root)
        for invalid in ({"executed_action": None}, {"executed_action": {"action": "click", "coordinate": [201, 300]}},
                        {"global_step": 2}, {"actual_action_coordinate_system": "model"}, {"agent_type": "newagent"}):
            with self.subTest(invalid=invalid):
                self.response.write_text(json.dumps({**self.payload, **invalid}), encoding="utf-8")
                with self.assertRaises(CollectionRunError):
                    store._validate_trajectory_content(self.directory, "case", agent_type="pipelineagent")

    def test_swipe_raw_model_coordinates_export_real_pixels_for_tree_and_bbox(self):
        from backend.trajectories_tree.tree_builder import direction, load_trajectories
        from backend.trajectory_data import _format_manual_actions_box
        actual = {"action": "swipe", "start_coordinate": [200, 600], "end_coordinate": [200, 100]}
        raw = '<tool_call>{"action":"swipe","coordinate":[499,800],"coordinate2":[499,100]}</tool_call><summary>Up</summary>'
        payload = {**self.payload, "content": raw, "parse_status": "parsed", "repair_applied": False,
                   "parsed_action": {"action": "swipe", "start_coordinate": [499,800], "end_coordinate": [499,100]},
                   "executed_action": actual}
        self.response.write_text(json.dumps(payload), encoding="utf-8")
        (self.directory / "_trajectory_for_evaluate.json").write_text(json.dumps({"actions_flat": [{"global_step": 1, "action": actual}]}), encoding="utf-8")
        CollectionRunStore(self.root)._validate_trajectory_content(self.directory, "case", agent_type="pipelineagent")
        action, _ = extract_response_fields(self.response)
        self.assertEqual(json.loads(action), actual)
        box = _format_manual_actions_box(json.loads(action), (150, 500, 250, 700))
        self.assertIn("direction=up", box)
        path = self.root / "swipe.json"
        path.write_text(json.dumps({"columns": {SHEET: ["文件夹名", "image", "xml", "action", "summary", "actions_box"]},
            "sheets": {SHEET: [{"文件夹名": "case", "image": "step001_vla_input.jpg", "xml": "step001_vla_input_ui.xml", "action": action, "summary": "Up", "actions_box": box}]}}), encoding="utf-8")
        step = load_trajectories(path, None)[0][1][0]
        self.assertEqual(direction(step.action), "up")
        self.assertEqual(step.action["start_coordinate"], [200, 600])
        self.assertEqual(json.loads(self.response.read_text(encoding="utf-8"))["content"], raw)

    def test_stability_xml_priority_and_provenance_survive_conversion_tree_and_correction(self):
        rows, warnings = collect_rows(self.directory)
        self.assertTrue(rows[0][2].endswith("_stability_ui.xml"), warnings)
        provenance = {"agent_type": "pipelineagent", "agent_version": "collector-v1",
                      "trajectory_format": "newagent-compatible-v1", "normalizer_version": "pipeline-normalizer-v1", "runtime_digest": "a" * 64, "configuration_digest": "b" * 64}
        relative = "case-a/phone__session"
        files = [{"path": relative + "/" + p.name, "size": p.stat().st_size,
                  "sha256": hashlib.sha256(p.read_bytes()).hexdigest()} for p in self.directory.iterdir()]
        run = {"batch_id": "batch-a", "collection_run_id": "run-a", "request": {"metadata": {"agent_type": "pipelineagent"}},
               "batch_tasks": {"case-a": {"task_id": "task-a", "source_result_id": "result-a", "task": "Search", "app": "App"}}}
        manifest = {"batch_id": "batch-a", "collection_run_id": "run-a", **provenance,
                    "trajectories": [{"collection_case_id": "case-a", "phone_id": "phone", "source_trajectory_id": "session",
                                      "relative_dir": relative, "files": files}], "errors": []}
        normalized = CollectionRunStore(self.root)._normalize(run, manifest, "2026-01-01T00:00:00+00:00")
        store = CollectionRunStore(self.root)
        completed = {**run, **normalized, "status": "completed", "completed_at": "2026-01-01T00:00:00+00:00",
                     "manifest_sha256": payload_digest(normalized)}
        with patch.object(store, "batch", return_value={}), patch.object(store, "list_runs", return_value=[completed]), patch.object(store, "_validate_sources"):
            ready = store.ready_input("batch-a")
            self.assertEqual(ready["trajectories"][0]["agent_type"], "pipelineagent")
            completed["agent_version"] = "tampered-version"
            with self.assertRaisesRegex(CollectionRunError, "清单校验失败"):
                store.ready_input("batch-a")
        snapshot = {"batch_id": "batch-a", "raw_root": str(self.root), "trajectories": normalized["trajectories"]}
        converted, _ = convert_input(snapshot, lambda _: None)
        row = converted["sheets"][SHEET][0]
        self.assertTrue(row["xml"].endswith("_stability_ui.xml"))
        self.assertEqual({k: row[k] for k in provenance}, provenance)
        self.assertEqual({k: row_identity_metadata(row)[k] for k in provenance}, provenance)
        row["actions_box"] = "click(bbox=<bbox>[10,20,30,40]</bbox>)"
        converted["columns"][SHEET] = [*converted["columns"][SHEET], "actions_box"]
        path = self.root / "annotation.json"
        path.write_text(json.dumps(converted), encoding="utf-8")
        from backend.trajectories_tree.tree_builder import load_trajectories
        step = load_trajectories(path, None)[0][1][0]
        self.assertEqual({k: step.identity_dict()[k] for k in provenance}, provenance)
        from backend.trajectory_correction.workbook import load_snapshot
        corrected = load_snapshot(path.with_suffix(".xlsx"), asset_root=self.root, source_kind="annotated_workbook")
        self.assertEqual({k: corrected["groups"][0]["rows"][0][k] for k in provenance}, provenance)
        with self.assertRaisesRegex(CollectionRunError, "引擎"):
            CollectionRunStore(self.root)._normalize(run, {**manifest, "agent_type": "newagent"}, "2026-01-01T00:00:00+00:00")


if __name__ == "__main__":
    unittest.main()
