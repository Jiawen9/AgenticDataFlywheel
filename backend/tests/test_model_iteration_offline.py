"""Independent evaluation orchestration without phones, network, or model calls."""
from __future__ import annotations

import contextlib
import copy
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend import model_iteration, phonefactory_client
from backend.phone_factory import PhoneFactoryError, PhoneFactoryStore, create_router
from backend.tests.test_model_iteration import evaluator_config, results


class OfflineEvaluationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.calls, self.runs, self.snapshots = [], {}, {}
        self.accepted_payloads = {}
        self.online, self.offline, self.profiles = 1, 1, 1
        self.profile = {"id": "rules-v1", "name": "规则 V1", "digest": "1" * 64}
        self.preview = {"preview_id": "preview_one", "valid": True, "trajectory_count": 2,
            "source": {"kind": "directory", "path": "D:/collector/archive"}, "source_digest": "2" * 64,
            "cases": [{"source_trajectory_id": "trajectory-a", "case_id": "same-id", "task": "搜索商品", "phone_id": ""},
                      {"source_trajectory_id": "trajectory-b", "case_id": "same-id", "task": "搜索商品", "phone_id": ""}],
            "errors": []}
        self.sources = {"sources": [{"run_id": "collection_1", "run_mode": "generate", "status": "completed",
                                     "created_at": "2026-10-23T00:00:00Z", "trajectory_count": 2}],
                        "allowed_roots": ["D:/collector/archive"]}
        self.lose_ack = False
        self.store = PhoneFactoryStore(Path(temporary.name), run_client_fn=self.client)
        # Deliberately omit all phone, APP, workbook and target VLA settings.
        model_iteration.save_config(self.store, evaluator_config())

    def client(self, args):
        self.calls.append(args)
        command = args[0]
        value = {"ok": True}
        if command == "capabilities":
            value.update(protocol_version=1, batch_results=True, model_iteration_evaluation=self.online,
                         offline_evaluation=self.offline, evaluation_profiles=self.profiles,
                         checklist_policies=["llm", "auto"])
        elif command == "evaluation-profiles":
            value = {"profiles": [self.profile], "errors": []}
        elif command == "evaluation-sources":
            value = self.sources
        elif command == "evaluation-preview":
            value = self.preview
        elif command == "start-evaluation":
            payload = json.loads(args[2])
            run_id = payload["collection_run_id"]
            if run_id in self.accepted_payloads:
                if self.accepted_payloads[run_id] != payload:
                    return {"ok": False, "output": "remote request digest mismatch"}
                return {"ok": True, "output": json.dumps({**self.runs[run_id], "reused": True})}
            self.accepted_payloads[run_id] = payload
            self.runs[run_id] = {"ok": True, "run_id": run_id, "run_mode": "modeliter",
                                 "execution_mode": "evaluate_only", "status": "running", "errors": []}
            rows = [{"case_key": row["source_trajectory_id"], "source_trajectory_id": row["source_trajectory_id"],
                     "case_id": row["case_id"], "phone_id": row["phone_id"], "collection_status": "completed",
                     "evaluation_status": "pending", "verdict": None, "checklist_source": "fixed_id"}
                    for row in self.preview["cases"]]
            self.snapshots[run_id] = results(run_id, rows)
            if self.lose_ack:
                self.lose_ack = False
                return {"ok": False, "output": "lost acknowledgement"}
            value = self.runs[run_id]
        elif command == "run":
            if args[1] not in self.runs:
                return {"ok": False, "output": "not found"}
            value = self.runs[args[1]]
        elif command == "run-evaluations":
            value = self.snapshots[args[1]]
        elif command == "cancel-run":
            self.runs[args[1]]["status"] = "cancelled"
            for row in self.snapshots[args[1]]["cases"]:
                row.update(evaluation_status="needs_review", verdict=None)
        return {"ok": True, "output": json.dumps(value)}

    def dispatch(self, action, method="GET", data=None):
        return self.store.dispatch(action, method, data or {}, mode="modeliter")

    def make_preview(self, **source):
        return self.dispatch("evaluation-preview", "POST", source or {"source_path": "D:/collector/archive"})

    def start(self, **updates):
        data = {"request_id": "offline-one", "evaluation_profile_id": "rules-v1",
                "evaluation_profile_digest": "1" * 64, "preview_id": "preview_one", "checklist_source": "auto",
                "evaluator_config": evaluator_config(), **updates}
        return self.dispatch("remote/start-evaluation", "POST", data)

    def test_empty_phone_settings_allow_evaluation_and_freeze_source_configuration(self):
        self.make_preview()
        run = self.start()
        self.assertEqual(run["execution_mode"], "evaluate_only")
        self.assertEqual(run["request"]["source_cases"], self.preview["cases"])
        self.assertEqual(run["request"]["source_digest"], "2" * 64)
        self.assertEqual(run["request"]["evaluator_config"], evaluator_config())
        self.assertNotIn("phone_ids", run["request"])
        self.assertEqual(self.store.records.list("collection_runs"), [])
        self.assertFalse(any(args[0] in {"start-run", "adb-devices", "status"} for args in self.calls))
        payload = json.loads(next(args for args in self.calls if args[0] == "start-evaluation")[2])
        self.assertEqual(payload["evaluation_profile_digest"], "1" * 64)
        self.assertNotIn("source_path", payload)
        public = self.dispatch("runs")["runs"][0]
        self.assertEqual(public["source"], self.preview["source"])

    def test_new_rule_version_is_new_run_and_old_results_remain_frozen(self):
        self.make_preview()
        first = self.start()
        for row in self.snapshots[first["run_id"]]["cases"]:
            row.update(evaluation_status="completed", verdict="success")
        self.runs[first["run_id"]]["status"] = "succeeded"
        saved = self.dispatch(f"runs/{first['run_id']}/evaluations")
        self.assertEqual(saved["summary"]["success_rate"], 1)
        self.profile.update(id="rules-v2", name="规则 V2", digest="3" * 64)
        second = self.start(request_id="offline-two", evaluation_profile_id="rules-v2", evaluation_profile_digest="3" * 64)
        self.assertNotEqual(first["run_id"], second["run_id"])
        self.assertEqual(second["request"]["source_digest"], first["request"]["source_digest"])
        self.assertEqual(self.dispatch(f"runs/{first['run_id']}/evaluations"), saved)
        self.assertTrue(self.start()["reused"])  # Old version may be retired after acceptance.

    def test_retry_rejects_config_or_profile_changes_and_recovers_lost_ack(self):
        self.make_preview()
        self.lose_ack = True
        with self.assertRaisesRegex(PhoneFactoryError, "lost acknowledgement"):
            self.start()
        self.profile['digest'] = '4' * 64  # Retry must reuse the already frozen version.
        recovered = self.start()
        self.assertTrue(recovered["reused"])
        self.assertEqual(sum(args[0] == "start-evaluation" for args in self.calls), 2)
        self.assertEqual(len(self.accepted_payloads), 1)
        self.assertFalse(any(args[0] == 'run' for args in self.calls))
        config = evaluator_config()
        config["text"]["model"] = "changed"
        for changes in ({"evaluator_config": config}, {"evaluation_profile_digest": "3" * 64}):
            with self.subTest(changes=changes), self.assertRaisesRegex(PhoneFactoryError, "request_id"):
                self.start(**changes)

    def test_retry_does_not_adopt_remote_run_with_different_request(self):
        self.make_preview()
        self.lose_ack = True
        with self.assertRaises(PhoneFactoryError):
            self.start()
        run_id = next(iter(self.accepted_payloads))
        self.accepted_payloads[run_id]['preview_id'] = 'other-preview'
        with self.assertRaisesRegex(PhoneFactoryError, 'remote request digest mismatch'):
            self.start()
        self.assertEqual(self.store.records.get('model_iter_runs', run_id)['status'], 'failed')

    def test_late_start_ack_does_not_overwrite_local_cancellation(self):
        self.make_preview()
        def cancel_before_ack(args):
            result = self.client(args)
            if args[0] == 'start-evaluation':
                run_id = json.loads(args[2])['collection_run_id']
                self.store.records.update('model_iter_runs', run_id, lambda item: item.update(status='cancelled'))
            return result
        self.store.run_client = cancel_before_ack
        self.assertEqual(self.start()['status'], 'cancelled')

    def test_profile_digest_changes_block_dispatch(self):
        self.make_preview()
        self.profile["digest"] = "4" * 64
        with self.assertRaisesRegex(PhoneFactoryError, "已变化"):
            self.start()
        self.assertFalse(any(args[0] == "start-evaluation" for args in self.calls))

    def test_offline_results_use_source_identity_and_allow_repeated_original_case_ids(self):
        self.make_preview()
        run = self.start()
        result = self.dispatch(f"runs/{run['run_id']}/evaluations")
        self.assertEqual(result["summary"]["total"], 2)
        self.assertEqual(result["execution_mode"], "evaluate_only")
        self.assertEqual(result["evaluation_profile_id"], "rules-v1")
        before = copy.deepcopy(self.snapshots[run["run_id"]])
        for changes in ({"source_trajectory_id": "other"}, {"case_id": "other"}, {"phone_id": "other"}):
            self.snapshots[run["run_id"]] = copy.deepcopy(before)
            self.snapshots[run["run_id"]]["cases"][0].update(changes)
            with self.subTest(changes=changes), self.assertRaisesRegex(PhoneFactoryError, "输入轨迹身份"):
                self.store.runtime.sync_evaluation(run["run_id"])
        self.snapshots[run["run_id"]] = copy.deepcopy(before)
        self.snapshots[run["run_id"]]["cases"][1]["source_trajectory_id"] = "trajectory-a"
        with self.assertRaisesRegex(PhoneFactoryError, "重复"):
            self.store.runtime.sync_evaluation(run["run_id"])

    def test_wrong_remote_execution_mode_does_not_finalize(self):
        self.make_preview()
        run = self.start()
        self.runs[run["run_id"]].update(execution_mode="collect_and_evaluate", status="completed")
        with self.assertRaisesRegex(PhoneFactoryError, "不是独立评测"):
            self.store.runtime.sync_evaluation(run["run_id"])
        self.assertEqual(self.store.records.get("model_iter_runs", run["run_id"])["status"], "running")

    def test_preview_rejects_ambiguous_sources_and_duplicate_identity(self):
        for source in ({}, {"source_path": "x", "source_run_id": "y"}, {"source_path": "x", "extra": 1}):
            with self.subTest(source=source), self.assertRaises(PhoneFactoryError):
                self.dispatch("evaluation-preview", "POST", source)
        self.preview["cases"][1]["source_trajectory_id"] = "trajectory-a"
        with self.assertRaisesRegex(PhoneFactoryError, "重复"):
            self.make_preview()
        self.assertEqual(self.store.records.list("model_iter_previews"), [])

    def test_invalid_preview_is_visible_but_cannot_launch(self):
        self.preview.update(valid=False, errors=["missing screenshot"])
        result = self.make_preview()
        self.assertEqual(result["errors"], ["missing screenshot"])
        with self.assertRaisesRegex(PhoneFactoryError, "先预览"):
            self.start()

    def test_previews_are_immutable_and_run_source_selection_supported(self):
        self.make_preview(source_run_id="collection_1")
        self.assertEqual(json.loads(next(args for args in self.calls if args[0] == "evaluation-preview")[2]),
                         {"source_run_id": "collection_1"})
        self.preview["source_digest"] = "5" * 64
        with self.assertRaisesRegex(PhoneFactoryError, "复用"):
            self.make_preview(source_run_id="collection_1")

    def test_capability_gate_keeps_legacy_supported_but_rejects_new_entry(self):
        self.offline = 0
        value = self.dispatch("evaluation-capabilities")
        self.assertTrue(value["supported"])
        self.assertFalse(value["offline_supported"])
        self.assertTrue(value["profiles_supported"])
        with self.assertRaisesRegex(PhoneFactoryError, "独立评测能力"):
            self.make_preview()
        self.profiles = 0
        with self.assertRaisesRegex(PhoneFactoryError, "规则版本"):
            self.dispatch("evaluation-profiles")

    def test_http_routes_and_cancel_without_phone_side_effects(self):
        app = FastAPI()
        app.include_router(create_router(self.store, prefix="/api/model-iter", mode="modeliter"))
        with TestClient(app) as client:
            self.assertEqual(client.get("/api/model-iter/evaluation-profiles").json()["profiles"][0]["id"], "rules-v1")
            self.assertEqual(client.get("/api/model-iter/evaluation-sources").json(), self.sources)
            preview = client.post("/api/model-iter/evaluation-preview", json={"source_path": "D:/collector/archive"})
            self.assertEqual(preview.status_code, 200)
            response = client.post("/api/model-iter/remote/start-evaluation", json={
                "request_id": "http-one", "evaluation_profile_id": "rules-v1", "evaluation_profile_digest": "1" * 64,
                "preview_id": "preview_one", "evaluator_config": evaluator_config()})
            self.assertEqual(response.status_code, 200, response.text)
            run_id = response.json()["run_id"]
            cancelled = client.post(f"/api/model-iter/runs/{run_id}/cancel", json={})
            self.assertEqual(cancelled.status_code, 200, cancelled.text)
            self.assertEqual(cancelled.json()["status"], "cancelled")

    def test_old_llm_config_remains_valid_auto_requires_profile(self):
        self.assertEqual(model_iteration.evaluation_options(self.store, {})["checklist_source"], "llm")
        for changes in ({"checklist_source": "auto"}, {"checklist_source": "fixed"},
                        {"checklist_source": "llm", "evaluation_profile_id": "rules-v1"}):
            with self.subTest(changes=changes), self.assertRaises(PhoneFactoryError):
                model_iteration.evaluation_options(self.store, changes)
        self.make_preview()
        with self.assertRaisesRegex(PhoneFactoryError, "不接受"):
            self.start(vla="http://target.invalid")


class OfflineClientTests(unittest.TestCase):
    def test_online_auto_form_preserves_rule_identity_and_legacy_defaults(self):
        with tempfile.TemporaryDirectory() as temporary:
            task, apps = Path(temporary) / 'task.xlsx', Path(temporary) / 'apps.json'
            task.write_bytes(b'fake workbook')
            apps.write_text('[]')
            arguments = ['start-run', str(task), str(apps), '--runmode', 'modeliter',
                         '--evaluator-config', json.dumps(evaluator_config())]
            legacy = phonefactory_client.build_parser().parse_args(arguments)
            automatic = phonefactory_client.build_parser().parse_args(arguments + [
                '--checklist-source', 'auto', '--evaluation-profile-id', 'rules-v1',
                '--evaluation-profile-digest', '1' * 64])
            for args in (legacy, automatic):
                with patch.object(phonefactory_client, 'post_multipart', return_value={}) as post:
                    phonefactory_client.cmd_start_run(args)
                fields = post.call_args.args[1]
                self.assertEqual(fields['checklist_source'], args.checklist_source)
                if args.checklist_source == 'auto':
                    self.assertEqual(fields['evaluation_profile_id'], 'rules-v1')
                    self.assertEqual(fields['evaluation_profile_digest'], '1' * 64)
                else:
                    self.assertNotIn('evaluation_profile_id', fields)

    def test_json_commands_and_run_bound_cancel(self):
        for command, path in (("evaluation-preview", "/evaluation_preview"), ("start-evaluation", "/start_evaluation")):
            with self.subTest(command=command), patch.object(sys, "argv", ["client", command, "--payload", '{"x":1}']), \
                    patch.object(phonefactory_client, "post_json", return_value={"ok": True}) as post, \
                    contextlib.redirect_stdout(io.StringIO()):
                phonefactory_client.main()
                post.assert_called_once_with(path, {"x": 1})
        with patch.object(sys, "argv", ["client", "cancel-run", "run-1"]), \
                patch.object(phonefactory_client, "post_json", return_value={"ok": True}) as post, \
                contextlib.redirect_stdout(io.StringIO()):
            phonefactory_client.main()
            post.assert_called_once_with("/runs/run-1/cancel", {})


if __name__ == "__main__":
    unittest.main()
