from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
import importlib
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import pandas as pd
from fastapi.testclient import TestClient
from openpyxl import Workbook

from backend.api import app
from backend.task_generation import adapter, store
from backend.task_generation.jobs import TaskGenerationJobManager


task_router = importlib.import_module("backend.task_generation.router")


class TaskGenerationWebTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.patches = [
            patch.object(store, "TASK_GENERATION_ROOT", root),
            patch.object(store, "INPUTS_DIR", root / "inputs"),
            patch.object(store, "JOBS_DIR", root / "jobs"),
            patch.object(store, "OUTPUTS_DIR", root / "outputs"),
            patch.object(task_router, "job_manager", TaskGenerationJobManager(ThreadPoolExecutor(max_workers=1))),
        ]
        for item in self.patches:
            item.start()
        self.client = TestClient(app)

    def tearDown(self) -> None:
        for item in reversed(self.patches):
            item.stop()
        self.temp_dir.cleanup()

    @staticmethod
    def _workbook_bytes(valid: bool = True) -> bytes:
        workbook = Workbook()
        sheet = workbook.active
        if valid:
            sheet.append(["任务结果", "任务", "涉及APP"])
            sheet.append(["FALSE", "搜索一个测试内容", "测试 App"])
        else:
            sheet.append(["invalid"])
        stream = BytesIO()
        workbook.save(stream)
        return stream.getvalue()

    @staticmethod
    def _wait(client: TestClient, job_id: str) -> dict:
        for _ in range(100):
            response = client.get(f"/api/task-generation/jobs/{job_id}")
            assert response.status_code == 200, response.text
            payload = response.json()
            if payload["status"] in {"succeeded", "failed", "interrupted"}:
                return payload
            time.sleep(0.02)
        raise AssertionError("作业未在测试时间内完成")

    def test_source_endpoint_reports_local_knowledge_base(self) -> None:
        response = self.client.get("/api/task-generation/source")
        self.assertEqual(response.status_code, 200)
        self.assertIn("files", response.json())
        self.assertEqual(len(response.json()["files"]), 3)

    def test_knowledge_job_persists_preview_and_downloads(self) -> None:
        with patch.object(adapter, "source_status", return_value={"ready": True, "errors": []}), patch.object(
            adapter,
            "task_generate",
            return_value=[{"task": "生成的任务", "app": "测试 App", "pre_dependency": "zero"}],
        ):
            response = self.client.post(
                "/api/task-generation/knowledge/jobs",
                json={"app": "测试 App", "generate_per_sub_capability": 2},
            )
            self.assertEqual(response.status_code, 202, response.text)
            job = self._wait(self.client, response.json()["job_id"])

        self.assertEqual(job["status"], "succeeded")
        self.assertEqual(job["result_count"], 1)
        preview = self.client.get(f"/api/task-generation/jobs/{job['job_id']}/preview")
        self.assertEqual(preview.status_code, 200)
        self.assertEqual(preview.json()["rows"][0]["task"], "生成的任务")
        self.assertEqual(self.client.get(f"/api/task-generation/jobs/{job['job_id']}/download?format=json").status_code, 200)
        self.assertEqual(self.client.get(f"/api/task-generation/jobs/{job['job_id']}/download?format=xlsx").status_code, 200)

    def test_flywheel_upload_scene_match_and_variant_are_separate(self) -> None:
        upload = self.client.post(
            "/api/task-generation/flywheel/inputs",
            files={"file": ("failed.xlsx", self._workbook_bytes(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
        )
        self.assertEqual(upload.status_code, 201, upload.text)
        input_id = upload.json()["input_id"]

        with patch.object(adapter, "match_scene_by_task", return_value=[
            {"app": "测试 App", "task": "搜索一个测试内容", "scene": "场景", "capability": "能力", "sub_capability": "子能力"}
        ]):
            scene = self.client.post("/api/task-generation/flywheel/scene-match/jobs", json={"input_id": input_id})
            self.assertEqual(scene.status_code, 202, scene.text)
            scene_job = self._wait(self.client, scene.json()["job_id"])
        self.assertEqual(scene_job["status"], "succeeded")

        def fake_generate(scene_path: Path, output_path: Path, generate_n: int = 10, progress_callback=None) -> None:
            pd.DataFrame([{
                "用例编号": "SCENE-APP-001-1",
                "源失败任务": "搜索一个测试内容",
                "app": "测试 App",
                "scene": "场景",
                "capability": "能力",
                "sub_capability": "子能力",
                "生成的变体任务": "搜索另一个内容",
                "run": "flywheel",
                "审核状态": "待人工Review",
            }]).to_excel(output_path, index=False)

        with patch.object(adapter, "generate_flywheel_rows", side_effect=fake_generate):
            variant = self.client.post(
                "/api/task-generation/flywheel/variant/jobs",
                json={"scene_match_job_id": scene_job["job_id"], "generate_n": 3},
            )
            self.assertEqual(variant.status_code, 202, variant.text)
            variant_job = self._wait(self.client, variant.json()["job_id"])

        self.assertEqual(variant_job["status"], "succeeded", variant_job)
        self.assertEqual(variant_job["result_count"], 1)
        self.assertEqual(self.client.get(f"/api/task-generation/jobs/{variant_job['job_id']}/preview").json()["total"], 1)

    def test_invalid_upload_is_rejected_without_accepting_a_client_path(self) -> None:
        response = self.client.post(
            "/api/task-generation/flywheel/inputs",
            files={"file": ("..\\outside.xlsx", self._workbook_bytes(valid=False), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
        )
        self.assertEqual(response.status_code, 422)

    def test_browser_cannot_override_model_connection_settings(self) -> None:
        response = self.client.post(
            "/api/task-generation/knowledge/jobs",
            json={
                "generate_per_sub_capability": 1,
                "base_url": "https://untrusted.example.invalid/v1",
                "api_key": "should-not-be-accepted",
            },
        )
        self.assertEqual(response.status_code, 422)


if __name__ == "__main__":
    unittest.main()
