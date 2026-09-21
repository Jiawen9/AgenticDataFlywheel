from __future__ import annotations

import base64
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.phone_factory import DEFAULT_CONFIG, PhoneFactoryError, PhoneFactoryStore, create_router, run_client
from backend.tests.test_manual_collection import workbook_bytes


def batch(identifier="batch-a", content=b"workbook bytes"):
    return {
        "description": f"采集批次 {identifier}",
        "filename": f"collection-batch-{identifier}.xlsx",
        "content_base64": base64.b64encode(content).decode(),
        "source_batch_id": identifier,
    }


class PhoneFactoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / "data"
        self.legacy = self.root / "legacy" / "frontend"
        self.uploads = self.root / "legacy" / "uploads"
        self.calls = []
        self.store = self.new_store()

    def new_store(self):
        return PhoneFactoryStore(self.root, run_client_fn=self.mock_client)

    def mock_client(self, args):
        if args[0] == "capabilities":
            return {"ok": True, "output": '{"protocol_version":1,"batch_results":true}'}
        call = {"args": args}
        if args[0] == "start-run":
            call["task"] = Path(args[1]).read_bytes()
            call["phoneApps"] = json.loads(Path(args[2]).read_text(encoding="utf-8"))
        self.calls.append(call)
        return {"ok": True, "output": '{"ok": true, "message": "mocked"}'}

    def test_get_and_first_edit_ignore_old_state_without_modifying_old_files(self):
        self.legacy.mkdir(parents=True)
        (self.legacy / "phones.json").write_text('["old-phone"]', encoding="utf-8")
        (self.legacy / "config.json").write_text('{"temperature": 0.1}', encoding="utf-8")
        (self.legacy / "tasks.json").write_text('[{"filename": "old.xlsx", "status": "运行中"}]', encoding="utf-8")
        old_bytes = {file.name: file.read_bytes() for file in self.legacy.iterdir()}
        self.assertEqual(self.store.state(), {key: [] for key in ("phones", "apps", "phoneApps", "vla", "tasks")})
        self.assertEqual(self.store.config(), DEFAULT_CONFIG)
        self.assertFalse(self.store.records.database_path.exists())
        state = self.store.dispatch("phone-apps", "POST", {"phone_id": "new-phone", "app": "App"})
        self.assertEqual(state["phones"], ["new-phone"])
        self.assertEqual(state["apps"], ["App"])
        self.assertEqual(state["tasks"], [])
        self.assertEqual({file.name: file.read_bytes() for file in self.legacy.iterdir()}, old_bytes)
        before = {file.relative_to(self.root): (file.read_bytes(), file.stat().st_mtime_ns) for file in self.root.rglob("*") if file.is_file()}
        self.assertEqual(self.new_store().state(), state)
        self.store.config()
        after = {file.relative_to(self.root): (file.read_bytes(), file.stat().st_mtime_ns) for file in self.root.rglob("*") if file.is_file()}
        self.assertEqual(before, after)

    def test_settings_initialization_is_explicit_excludes_history_and_does_not_overwrite(self):
        settings = {"phones": ["phone"], "apps": ["App"], "vla": ["http://mock.invalid/vla"],
                    "phoneApps": [{"phone_id": "phone", "app": "App", "status": "运行中"}],
                    "config": {"temperature": 0.4}}
        state = self.store.initialize_settings(settings)
        self.assertEqual(state["tasks"], [])
        self.assertEqual(state["phoneApps"], [{"phone_id": "phone", "app": "App", "status": "空闲"}])
        self.assertEqual(self.new_store().state(), state)
        self.assertEqual(self.store.config(), {**DEFAULT_CONFIG, "temperature": 0.4})
        self.assertEqual(settings["phoneApps"][0]["status"], "运行中")
        self.store.add_task(batch())
        self.store.start_task(batch()["filename"])
        with self.assertRaises(PhoneFactoryError) as context:
            self.new_store().initialize_settings({"phones": ["replacement"]})
        self.assertEqual(context.exception.status, 409)
        self.assertEqual(self.store.state()["phones"], ["phone"])
        self.assertEqual(self.store.state()["tasks"][0]["status"], "运行中")
        self.assertEqual(self.calls, [])

    def test_settings_initialization_rejects_tasks_and_invalid_config_without_writes(self):
        for settings in ({"tasks": []}, {"phones": "phone"}, {"phoneApps": [{}]},
                         {"config": {"top_p": float("nan")}}, {"config": {"sampling_enabled": "true"}}):
            with self.subTest(settings=settings):
                with self.assertRaises(PhoneFactoryError):
                    self.store.initialize_settings(settings)
                self.assertFalse(self.root.exists())
        self.assertEqual(self.calls, [])

    def test_batch_retry_running_state_conflict_and_missing_file_recovery(self):
        request = batch()
        self.store.add_task(request)
        self.store.start_task(request["filename"])
        self.assertEqual(len(self.store.add_task({**request, "description": "ignored"})["tasks"]), 1)
        self.assertEqual(self.store.state()["tasks"][0]["status"], "运行中")
        file = self.store.upload_dir / request["filename"]
        with self.assertRaises(PhoneFactoryError) as context:
            self.store.add_task(batch(content=b"changed"))
        self.assertEqual(context.exception.status, 409)
        self.assertEqual(file.read_bytes(), b"workbook bytes")
        file.unlink()
        self.store.add_task(request)
        self.assertEqual(file.read_bytes(), b"workbook bytes")
        self.assertEqual(self.new_store().state()["tasks"][0]["status"], "运行中")

    def test_parallel_import_edit_delete_keep_all_unrelated_state(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda index: self.new_store().add_task(batch("a" if index < 5 else f"a{index}")), range(12)))
        self.assertEqual(len(self.store.state()["tasks"]), 8)
        with ThreadPoolExecutor(max_workers=3) as pool:
            list(pool.map(lambda fn: fn(), [
                lambda: self.new_store().start_task(batch("a")["filename"]),
                lambda: self.new_store().remove_task(batch("a5")["filename"]),
                lambda: self.new_store().dispatch("phone-apps", "POST", {"phone_id": "phone", "app": "App"}),
            ]))
        state = self.store.state()
        self.assertEqual(len(state["tasks"]), 7)
        self.assertEqual(state["phones"], ["phone"])
        self.assertEqual(next(row for row in state["tasks"] if row["source_batch_id"] == "a")["status"], "运行中")

    def test_remote_protocol_uses_frozen_upload_and_current_app_config_without_real_phone(self):
        original = workbook_bytes()
        request = {**batch(content=original), "filename": "manual.xlsx"}
        request.pop("source_batch_id")
        registered = self.store.add_task(request)
        request["filename"] = registered["tasks"][0]["filename"]
        self.store.dispatch("vla", "POST", {"value": "http://model.invalid"})
        self.store.dispatch("phone-apps", "POST", {"phone_id": "phone", "app": "App"})
        self.store.dispatch("config", "POST", {"sampling_enabled": True, "temperature": 0.5, "top_p": 0.9, "use_experience_lib": True})
        result = self.store.remote_start({"filename": request["filename"], "phone_id": "phone", "app": "App"})
        self.assertTrue(result["ok"])
        call = self.calls[-1]
        self.assertEqual(call["task"], original)
        self.assertEqual(call["phoneApps"], [{"phone_id": "phone", "app": "App", "status": "空闲"}])
        self.assertEqual(call["args"][3:5], ["phone", "App"])
        self.assertIn("--sampling", call["args"])
        self.assertIn("--exp", call["args"])
        self.assertFalse(Path(call["args"][2]).exists())
        self.assertEqual(self.store.state()["tasks"][0]["status"], "运行中")
        self.store.dispatch("remote/add-phone", "POST", {"phone_id": "phone"})
        self.assertEqual(self.calls[-1]["args"], ["add-phone", "phone"])
        self.store.run_client = lambda args: {"ok": False, "output": "mock timeout"}
        with self.assertRaises(PhoneFactoryError) as context:
            self.store.remote_start({"filename": request["filename"]})
        self.assertEqual(context.exception.status, 502)
        self.assertEqual(self.store.state()["tasks"][0]["status"], "失败")

    def test_old_tasks_and_uploads_cannot_be_used_by_new_flow(self):
        self.legacy.mkdir(parents=True)
        self.uploads.mkdir(parents=True)
        row = {"filename": "old.xlsx", "description": "legacy", "status": "未运行"}
        (self.legacy / "tasks.json").write_text(json.dumps([row]), encoding="utf-8")
        (self.uploads / "old.xlsx").write_bytes(b"legacy")
        with self.assertRaises(PhoneFactoryError) as context:
            self.store.remote_start({"filename": "old.xlsx"})
        self.assertEqual(context.exception.status, 404)
        self.assertFalse(self.store.records.database_path.exists())
        self.assertEqual(self.store.task_path("old.xlsx"), self.store.upload_dir / "old.xlsx")
        current = self.store._empty_state()
        current["tasks"] = [row]
        self.store.records.put("phone_factory", "state", current, expected_revision=0)
        with self.assertRaises(PhoneFactoryError) as context:
            self.store.remote_start({"filename": "old.xlsx"})
        self.assertEqual(context.exception.status, 409)
        self.assertEqual(self.calls, [])
        self.assertEqual((self.uploads / "old.xlsx").read_bytes(), b"legacy")
        self.store.remove_task("old.xlsx")
        self.store.add_task({"filename": "old.xlsx", "description": "new", "content_base64": base64.b64encode(b"new").decode()}, mode="modeliter")
        self.store.start_task("old.xlsx")
        with self.assertRaises(PhoneFactoryError) as context:
            self.store.add_task({"filename": "old.xlsx", "description": "replacement", "content_base64": base64.b64encode(b"changed").decode()}, mode="modeliter")
        self.assertEqual(context.exception.status, 409)
        self.assertEqual((self.uploads / "old.xlsx").read_bytes(), b"legacy")
        self.assertEqual((self.store.upload_dir / "old.xlsx").read_bytes(), b"new")
        self.store.remove_task("old.xlsx")
        self.assertEqual(self.store.state()["tasks"], [])
        self.assertTrue((self.uploads / "old.xlsx").exists())

    def test_router_compatibility_and_validation_do_not_call_remote(self):
        app = FastAPI()
        app.include_router(create_router(self.store))
        with TestClient(app) as client:
            response = client.get("/api/phone-factory/state")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers["cache-control"], "no-store")
            self.assertFalse(self.root.exists())
            self.assertEqual(client.post("/api/phone-factory/phones", json={"phone_id": ""}).status_code, 400)
            self.assertEqual(client.post("/api/phone-factory/tasks/start", json={"filename": "missing.xlsx"}).status_code, 404)
            self.assertEqual(client.post("/api/phone-factory/tasks", json=batch()).status_code, 200)
            conflict = client.post("/api/phone-factory/tasks", json=batch(content=b"other"))
            self.assertEqual(conflict.status_code, 409)
            self.assertIn("error", conflict.json())
            self.assertEqual(client.get("/api/phone-factory/unknown").status_code, 404)
        self.assertEqual(self.calls, [])

    def test_subprocess_uses_current_interpreter_and_timeout(self):
        with patch("backend.phone_factory.subprocess.run") as runner:
            runner.return_value = subprocess.CompletedProcess([], 0, '{"ok":true}', "")
            self.assertTrue(run_client(["add-phone", "mock"])["ok"])
            self.assertEqual(runner.call_args.args[0][0], sys.executable)
            self.assertEqual(runner.call_args.kwargs["timeout"], 35)
            runner.side_effect = subprocess.TimeoutExpired("mock", 12)
            self.assertFalse(run_client(["add-phone", "mock"])["ok"])


if __name__ == "__main__":
    unittest.main()
