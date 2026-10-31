from __future__ import annotations

import base64
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend import model_iteration, phonefactory_client
from backend.phone_factory import PhoneFactoryStore, PhoneFactoryError, create_router
from backend.tests.test_phone_factory_runtime import workbook_bytes
from backend.tests.collector_protocol_support import current_capabilities


def evaluator_config():
    return {role: {"base_url": "https://judge.invalid/gateway/v1/", "model": role + "-judge",
                   "api_key_env": f"PHONE_FACTORY_EVALUATOR_{role.upper()}_API_KEY"} for role in ("text", "vision")}


def results(run_id, rows=None):
    rows = rows if rows is not None else [{"case_key": "phone-1/case-a", "phone_id": "phone-1", "case_id": "case-a",
        "session_id": "case-a-001", "collection_status": "completed", "evaluation_status": "completed",
        "verdict": "success", "failure_reason": "", "attempt_count": 1, "report_file_id": "report-a", "report_name": "case-a.json", "errors": []}]
    summary = {key: 0 for key in model_iteration.COUNTS}
    summary.update(total=len(rows), collection_completed=len(rows), evaluation_completed=len(rows), success_rate=None)
    return {"run_id": run_id, "run_mode": "modeliter", "summary": summary, "cases": rows}


class ModelIterationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.calls, self.runs, self.snapshots = [], {}, {}
        self.supported, self.unreachable = True, False
        self.store = PhoneFactoryStore(Path(temporary.name), run_client_fn=self.client)
        self.store.initialize_settings({"phones": ["phone-1"], "apps": ["App"], "phoneApps": [{"phone_id": "phone-1", "app": "App"}], "vla": ["http://vla.invalid"]})
        self.store.add_task({"filename": "eval.xlsx", "description": "cases", "content_base64": base64.b64encode(workbook_bytes()).decode()}, mode="modeliter")
        model_iteration.save_config(self.store, evaluator_config())

    def client(self, args):
        self.calls.append(args)
        if self.unreachable:
            return {"ok": False, "output": "offline"}
        value = {"ok": True}
        if args[0] == "capabilities":
            value.update(protocol_version=1, batch_results=True, model_iteration_evaluation=int(self.supported))
            current_capabilities(value)
        elif args[0] == "start-run":
            run_id = args[args.index("--collection-run-id") + 1]
            mode = args[args.index("--runmode") + 1]
            self.runs[run_id] = {"run_id": run_id, "run_mode": mode, "status": "running", "errors": []}
            self.snapshots[run_id] = results(run_id)
            value.update(self.runs[run_id])
        elif args[0] == "run":
            if args[1] not in self.runs:
                return {"ok": False, "output": "run not found"}
            value = self.runs[args[1]]
        elif args[0] == "run-evaluations":
            value = self.snapshots[args[1]]
        elif args[0] == "report-download":
            Path(args[args.index("--out") + 1]).write_bytes(b'{"verdict":"success"}')
        return {"ok": True, "output": json.dumps(value)}

    def start(self, **extra):
        return self.store.remote_start({"filename": "eval.xlsx", "request_id": "one", "vla": "http://vla.invalid", **extra}, mode="modeliter")

    def test_settings_are_separate_and_reject_secret_fields_and_invalid_addresses(self):
        before = self.store.config()
        value = evaluator_config(); value["text"]["base_url"] = "judge:8000"
        self.assertEqual(model_iteration.save_config(self.store, value)["text"]["base_url"], "http://judge:8000/v1/")
        self.assertEqual(self.store.config(), before)
        for bad in ("https://user:password@host/v1", "https://host/v1?api_key=x", "http://host:99999", "http://host/ a"):
            value = evaluator_config(); value["text"]["base_url"] = bad
            with self.subTest(bad=bad), self.assertRaises(PhoneFactoryError): model_iteration.save_config(self.store, value)
        value = evaluator_config(); value["vision"]["api_key"] = "never-save"
        with self.assertRaises(PhoneFactoryError): model_iteration.save_config(self.store, value)
        model_iteration.save_config(self.store, model_iteration.default_config())
        with self.assertRaisesRegex(PhoneFactoryError, "地址与模型名"): self.start()
        self.assertEqual(self.calls, [])

    def test_config_routes_and_unsupported_service_gate_do_not_affect_generate(self):
        app = FastAPI(); app.include_router(create_router(self.store, prefix="/api/model-iter", mode="modeliter"))
        with TestClient(app) as client:
            self.assertEqual(client.get("/api/model-iter/evaluation-config").json(), evaluator_config())
            self.assertEqual(client.put("/api/model-iter/evaluation-config", json=evaluator_config()).status_code, 200)
            self.supported = False
            self.assertFalse(client.get("/api/model-iter/evaluation-capabilities").json()["supported"])
            with self.assertRaisesRegex(PhoneFactoryError, "升级"): self.start()
            self.assertFalse(any(call[0] == "start-run" for call in self.calls))
            self.store.remote_start({"filename": "eval.xlsx", "request_id": "generate", "vla": "http://vla.invalid"})
            self.assertTrue(any(call[0] == "start-run" for call in self.calls))

    def test_dispatch_snapshot_idempotency_and_per_run_override(self):
        custom = evaluator_config(); custom["vision"]["model"] = "vision-v2"
        run = self.start(evaluator_config=custom)
        self.assertEqual(run["request"]["evaluator_config"], custom)
        self.assertEqual(run["request"]["checklist_source"], "llm")
        self.assertEqual(run["request"]["evaluation_mode"], "checklist")
        args = next(args for args in self.calls if args[0] == "start-run")
        self.assertEqual(json.loads(args[args.index("--evaluator-config") + 1]), custom)
        self.assertEqual(args[args.index("--batch-id") + 1], run["batch_id"])
        self.assertEqual(model_iteration.get_config(self.store), evaluator_config())
        changed = evaluator_config(); changed["text"]["model"] = "text-new-default"
        model_iteration.save_config(self.store, changed)
        self.assertTrue(self.start(evaluator_config=custom)["reused"])
        self.assertEqual(self.store.records.get("model_iter_runs", run["run_id"])["request"]["evaluator_config"], custom)
        with self.assertRaisesRegex(PhoneFactoryError, "request_id"): self.start()
        self.assertEqual(self.store.collection_runs.get(run["run_id"])["batch_id"], run["batch_id"])

    def test_evaluator_upgrade_is_detected_without_reusing_old_protocol_cache(self):
        self.store.runtime._protocol_cache = "unsupported"
        self.supported = False
        with self.assertRaisesRegex(PhoneFactoryError, "升级"): self.start()
        self.supported = True
        run = self.start()
        self.assertEqual(run["status"], "running")
        args = next(args for args in self.calls if args[0] == "start-run")
        self.assertIn("--collection-run-id", args)

    def test_running_snapshot_and_final_snapshot_survive_remote_loss_and_restart(self):
        run = self.start(); run_id = run["run_id"]
        self.store.runtime.sync_evaluation(run_id)
        self.assertEqual(self.store.records.get("model_iter_runs", run_id)["evaluation_summary"]["success_rate"], 1)
        self.unreachable = True
        policy = self.store.runtime.sync_policy
        # Page reads now respect the successful poll's ten-second interval.
        with patch.object(policy, "clock", return_value=policy.clock() + 11):
            stale = self.store.dispatch(f"runs/{run_id}/evaluations", "GET", {}, mode="modeliter")
        self.assertTrue(stale["stale"])
        self.unreachable = False; self.runs[run_id]["status"] = "succeeded"
        self.store.runtime.sync_evaluation(run_id)
        self.unreachable = True
        rebuilt = PhoneFactoryStore(self.store.root, run_client_fn=self.client)
        final = rebuilt.dispatch(f"runs/{run_id}/evaluations", "GET", {}, mode="modeliter")
        self.assertNotIn("stale", final)
        self.assertEqual(final["cases"][0]["case_id"], "case-a")

    def test_case_identity_failure_denominator_and_invalid_payload(self):
        sample = results("run")
        first = sample["cases"][0]
        rows = [first, {**first, "case_key": "phone-2/case-a", "phone_id": "phone-2", "verdict": "fail"},
                {**first, "case_key": "review", "evaluation_status": "needs_review", "verdict": "unchecked"},
                {**first, "case_key": "partial", "verdict": "partial_success"}]
        value = model_iteration.validate_results(results("run", rows), "run", ["phone-1", "phone-2"])
        self.assertEqual(value["summary"]["success_rate"], 1 / 3)
        self.assertEqual(len(value["cases"]), 4)
        no_outcome = model_iteration.validate_results(results("run", [rows[2]]), "run", ["phone-1"])
        self.assertIsNone(no_outcome["summary"]["success_rate"])
        for bad in ({**sample, "run_id": "other"}, results("run", [first, first]), results("run", [{**first, "phone_id": "unknown"}])):
            with self.assertRaises(PhoneFactoryError): model_iteration.validate_results(bad, "run", ["phone-1"])

    def test_bad_remote_identity_does_not_finalize_run(self):
        run_id = self.start()["run_id"]
        self.runs[run_id]["status"] = "succeeded"
        self.snapshots[run_id]["run_id"] = "other"
        with self.assertRaises(PhoneFactoryError): self.store.runtime.sync_evaluation(run_id)
        self.assertEqual(self.store.records.get("model_iter_runs", run_id)["status"], "running")
        self.assertIsNone(self.store.records.get("model_iter_results", run_id))

    def test_all_progress_counts_are_derived_from_case_states(self):
        sample = results("run"); first = sample["cases"][0]
        rows = [first,
                {**first, "case_key": "collecting", "collection_status": "running", "evaluation_status": "pending", "verdict": None},
                {**first, "case_key": "retry", "evaluation_status": "retry_wait", "verdict": None},
                {**first, "case_key": "review", "evaluation_status": "needs_review", "verdict": "unchecked"},
                {**first, "case_key": "collect-failure", "collection_status": "failed", "evaluation_status": "collection_failed", "verdict": None},
                {**first, "case_key": "judging", "evaluation_status": "running", "verdict": None}]
        payload = results("run", rows)
        payload["summary"].update(collection_completed=99, evaluation_completed=99, evaluation_failed=99)
        summary = model_iteration.validate_results(payload, "run", ["phone-1"])["summary"]
        self.assertEqual(summary, {"total": 6, "collection_completed": 4, "collection_failed": 1,
            "evaluation_completed": 1, "evaluation_failed": 2, "evaluation_running": 1,
            "evaluation_pending": 2, "success": 1, "partial_success": 0, "fail": 0, "success_rate": 1})
        for changes in ({"collection_status": "unknown"}, {"verdict": "unchecked"}, {"evaluation_status": "pending"}):
            with self.subTest(changes=changes), self.assertRaises(PhoneFactoryError):
                model_iteration.validate_results(results("run", [{**first, **changes}]), "run", ["phone-1"])

    def test_premature_terminal_state_never_freezes_pending_cases(self):
        run_id = self.start()["run_id"]
        self.runs[run_id]["status"] = "succeeded"
        case = self.snapshots[run_id]["cases"][0]
        case.update(evaluation_status="pending", verdict=None)
        with self.assertRaisesRegex(PhoneFactoryError, "未完成用例"):
            self.store.runtime.sync_evaluation(run_id)
        self.assertIsNone(self.store.records.get("model_iter_results", run_id))
        self.assertEqual(self.store.records.get("model_iter_runs", run_id)["status"], "running")
        case.update(evaluation_status="needs_review", verdict="unchecked")
        with self.assertRaisesRegex(PhoneFactoryError, "成功状态"):
            self.store.runtime.sync_evaluation(run_id)
        self.runs[run_id]["status"] = "partial"
        self.assertEqual(self.store.runtime.sync_evaluation(run_id)["status"], "partial")
        self.assertTrue(self.store.records.get("model_iter_results", run_id)["final"])

    def test_json_report_download_retains_media_type_and_filename(self):
        run_id = self.start()["run_id"]
        app = FastAPI(); app.include_router(create_router(self.store, prefix="/api/model-iter", mode="modeliter"))
        with TestClient(app) as client:
            response = client.get("/api/model-iter/report-download", params={"run_id": run_id, "file_id": "report-a", "name": "case-a.json"})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers["content-type"], "application/json")
            self.assertIn("case-a.json", response.headers["content-disposition"])
            self.assertEqual(response.json()["verdict"], "success")

    def test_client_form_carries_only_environment_variable_names(self):
        task = self.store.task_path("eval.xlsx"); apps = self.store.root / "apps.json"; apps.write_text("[]")
        args = phonefactory_client.build_parser().parse_args(["start-run", str(task), str(apps), "--runmode", "modeliter", "--collection-run-id", "eval_1", "--evaluator-config", json.dumps(evaluator_config())])
        with patch.object(phonefactory_client, "post_multipart", return_value={}) as post:
            phonefactory_client.cmd_start_run(args)
        fields = post.call_args.args[1]
        self.assertEqual(json.loads(fields["evaluator_config"]), evaluator_config())
        self.assertEqual((fields["checklist_source"], fields["evaluation_mode"]), ("llm", "checklist"))
        self.assertNotIn("batch_id", fields)


if __name__ == "__main__": unittest.main()
