"""Mock-only coverage for the intranet collector compatibility contract."""
from __future__ import annotations

import base64
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.phone_factory import PhoneFactoryStore, PhoneFactoryError, create_router
from backend.model_iteration import save_config
from backend.tests.test_model_iteration import evaluator_config, results
from backend.tests.test_phone_factory_runtime import workbook_bytes


class HandoffLegacyCompatibilityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.protocol = "legacy"
        self.calls = []
        self.dispatched = []
        self.remote_runs = {}
        self.download_fails = False

    def client(self, args):
        self.calls.append(list(args))
        value = {"ok": True}
        if args[0] == "capabilities":
            if self.protocol == "legacy":
                return {"ok": False, "output": "404: legacy collector has no capabilities endpoint"}
            value.update(protocol_version=1, batch_results=True, model_iteration_evaluation=1)
        elif args[0] == "start-run":
            captured = {"args": list(args), "apps": json.loads(Path(args[2]).read_text(encoding="utf-8"))}
            if "--task-manifest" in args:
                captured["tasks"] = json.loads(Path(args[args.index("--task-manifest") + 1]).read_text(encoding="utf-8"))["tasks"]
            self.dispatched.append(captured)
            if "--collection-run-id" in args:
                run_id = args[args.index("--collection-run-id") + 1]
                mode = args[args.index("--runmode") + 1]
                value.update(run_id=run_id, collection_run_id=run_id, run_mode=mode)
                self.remote_runs[run_id] = {**value, "status": "succeeded", "errors": []}
        elif args[0] == "run":
            value = self.remote_runs[args[1]]
        elif args[0] == "run-evaluations":
            value = results(args[1])
        elif args[0] == "reports":
            value["reports"] = [{"folder": "round-one", "name": "report.xlsx"}]
        elif args[0] == "report-download":
            Path(args[args.index("--out") + 1]).write_bytes(b"mock report")
            if self.download_fails:
                return {"ok": False, "output": "mock report transfer failed"}
        else:
            raise AssertionError(f"Unexpected remote command: {args[0]}")
        return {"ok": True, "output": json.dumps(value)}

    def make_store(self, name="platform"):
        store = PhoneFactoryStore(self.root / name, run_client_fn=self.client)
        store.initialize_settings({"phones": ["phone-1"], "apps": ["App"],
            "phoneApps": [{"phone_id": "phone-1", "app": "App"}],
            "vla": ["http://mock.invalid/vla"]})
        save_config(store, evaluator_config())
        self.addCleanup(store.runtime.close)
        return store

    @staticmethod
    def historical_evaluation(store, suffix):
        run_id = "eval_" + suffix
        return store.records.put("model_iter_runs", run_id, {"run_id": run_id, "collection_run_id": run_id,
            "status": "running", "created_at": "2026-10-20T00:00:00Z", "filename": "tasks.xlsx",
            "request": {"result_transport": "legacy"}, "errors": []})

    @staticmethod
    def upload(store, mode):
        store.add_task({"filename": "tasks.xlsx", "description": "mock handoff",
            "content_base64": base64.b64encode(workbook_bytes()).decode()}, mode=mode)

    @staticmethod
    def request(request_id="request-one", **extra):
        return {"filename": "tasks.xlsx", "request_id": request_id,
            "phone_id": "phone-1", "app": "App", "vla": "http://mock.invalid/custom-vla",
            "config": {"sampling_enabled": True, "temperature": 0.35,
                "top_p": 0.65, "use_experience_lib": True}, **extra}

    def test_protocol_and_mode_matrix_preserves_per_run_parameters_and_isolation(self):
        for protocol in ("legacy", "http"):
            for mode in ("generate", "modeliter"):
                with self.subTest(protocol=protocol, mode=mode):
                    self.protocol = protocol
                    store = self.make_store(protocol + "-" + mode)
                    self.upload(store, mode)
                    if protocol == "legacy" and mode == "modeliter":
                        count = len(self.dispatched)
                        with self.assertRaisesRegex(PhoneFactoryError, "能力检查失败"):
                            store.remote_start(self.request(run_mode=mode), mode=mode)
                        self.assertEqual(len(self.dispatched), count)
                        continue
                    global_config = copy.deepcopy(store.config())
                    run = store.remote_start(self.request(run_mode=mode), mode=mode)
                    dispatch = self.dispatched[-1]
                    args = dispatch["args"]
                    self.assertEqual(args[3:5], ["phone-1", "App"])
                    for flag, expected in (("--runmode", mode), ("--vla", "http://mock.invalid/custom-vla"),
                            ("--temperature", "0.35"), ("--top-p", "0.65")):
                        self.assertEqual(args[args.index(flag) + 1], expected)
                    self.assertIn("--sampling", args)
                    self.assertIn("--exp", args)
                    self.assertEqual(dispatch["apps"], [{"phone_id": "phone-1", "app": "App", "status": "空闲"}])
                    for flag in ("--collection-run-id", "--task-manifest"):
                        self.assertEqual(flag in args, protocol == "http")
                    for flag in ("--batch-id", "--output-dir"):
                        self.assertEqual(flag in args, protocol == "http" and mode == "generate")
                    if protocol == "http":
                        self.assertEqual(args[args.index("--collection-run-id") + 1], run["collection_run_id"])
                        self.assertEqual(len(dispatch["tasks"]), 1)
                    request = run["request"]
                    metadata = request["metadata"] if mode == "generate" else request
                    self.assertEqual(metadata["result_transport"], protocol)
                    self.assertEqual(store.config(), global_config)
                    start_count = len(self.dispatched)
                    repeated = store.remote_start(self.request(run_mode=mode), mode=mode)
                    self.assertEqual(repeated["collection_run_id"], run["collection_run_id"])
                    self.assertEqual(len(self.dispatched), start_count)
                    if mode == "modeliter":
                        self.assertEqual(store.collection_runs.list_runs(), [])
                        self.assertEqual(store.runtime.batches(), [])
                        self.assertFalse((store.root / "batches").exists())
                    else:
                        self.assertEqual(store.records.list("model_iter_runs"), [])

    def test_evaluation_background_sync_skips_both_legacy_metadata_shapes(self):
        store = self.make_store()
        self.upload(store, "modeliter")
        legacy = self.historical_evaluation(store, "legacy")
        nested = self.historical_evaluation(store, "legacy-nested")
        def nest_transport(item):
            item["request"]["metadata"] = {"result_transport": item["request"].pop("result_transport")}
        store.records.update("model_iter_runs", nested["run_id"], nest_transport)
        self.protocol = "http"
        modern = store.remote_start(self.request("modern"), mode="modeliter")
        before = {identifier: store.records.get("model_iter_runs", identifier)
            for identifier in (legacy["run_id"], nested["run_id"])}
        stop = Mock()
        stop.wait.side_effect = [False, True]
        stop.is_set.return_value = False
        with patch.object(store.runtime, "_stop", stop):
            store.runtime._poll_evaluations()
        self.assertEqual([args for args in self.calls if args[0] == "run"], [["run", modern["run_id"]]])
        self.assertEqual(store.records.get("model_iter_runs", modern["run_id"])["status"], "succeeded")
        for identifier, saved in before.items():
            self.assertEqual(store.records.get("model_iter_runs", identifier), saved)
            self.assertEqual(saved["status"], "running")
        self.assertEqual(store.collection_runs.list_runs(), [])

    def test_production_recovery_skips_legacy_even_with_old_transfer_state(self):
        store = self.make_store()
        self.upload(store, "generate")
        legacy = store.remote_start(self.request("legacy"))
        store.records.update("collection_runs", legacy["collection_run_id"],
            lambda item: item.update(transfer_status="waiting"))
        self.protocol = "http"
        modern = store.remote_start(self.request("modern"))
        before = store.collection_runs.get(legacy["collection_run_id"])
        with patch.object(store.runtime.transfer, "sync", return_value={"status": "waiting"}) as sync:
            self.assertEqual(store.runtime.transfer.recover(), [{"status": "waiting"}])
        sync.assert_called_once_with(modern["collection_run_id"])
        self.assertEqual(store.collection_runs.get(legacy["collection_run_id"]), before)
        self.assertEqual([args for args in self.calls if args[0] in {"run", "run-archive"}], [])

    def test_legacy_reports_remain_available_and_download_failures_cleanup(self):
        store = self.make_store()
        self.upload(store, "modeliter")
        evaluation = self.historical_evaluation(store, "historical")
        app = FastAPI()
        app.include_router(create_router(store))
        app.include_router(create_router(store, prefix="/api/model-iter", mode="modeliter"))
        with TestClient(app) as client:
            listing = client.get("/api/model-iter/reports")
            self.assertEqual(listing.status_code, 200)
            self.assertEqual(listing.json()["reports"], [{"folder": "round-one", "name": "report.xlsx"}])
            params = {"folder": "round-one", "name": "report.xlsx"}
            response = client.get("/api/model-iter/report-download", params=params)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.content, b"mock report")
            self.download_fails = True
            self.assertEqual(client.get("/api/model-iter/report-download", params=params).status_code, 502)
            self.assertEqual(client.get("/api/phone-factory/runs").json()["runs"], [])
            self.assertEqual(client.get("/api/model-iter/runs").json()["runs"][0]["status"], "running")
            self.assertEqual(client.post("/api/model-iter/collection-runs/forbidden/sync").status_code, 409)
        self.assertEqual(list((store.root / "tmp" / "phone_factory_reports").iterdir()), [])
        report_calls = [args for args in self.calls if args[0] in {"reports", "report-download"}]
        self.assertEqual(len(report_calls), 3)
        for args in report_calls:
            self.assertEqual(args[args.index("--runmode") + 1], "modeliter")
        self.assertEqual(store.records.get("model_iter_runs", evaluation["run_id"])["status"], "running")
        self.assertEqual(store.collection_runs.list_runs(), [])


if __name__ == "__main__":
    unittest.main()
