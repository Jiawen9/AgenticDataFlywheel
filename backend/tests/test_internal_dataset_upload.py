"""Isolated Excel fixtures; no external website, credentials, or phone calls."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import hashlib
import threading
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from openpyxl import Workbook

from backend.data_publishing import internal_uploader
from backend.data_publishing.internal_jobs import InternalUploadConflict
from backend.data_publishing.internal_uploader import UploadError, UploadResult
from backend.data_publishing.router import router
from backend.data_publishing.upload_jobs import DatasetUploadJobManager
from backend.tests import test_data_publishing as publishing_fixtures
from backend.tests.test_data_publishing import ImmediateExecutor


class DeferredExecutor:
    def __init__(self):
        self.calls = []

    def submit(self, function, *args):
        self.calls.append((function, args))

    def run(self):
        function, args = self.calls.pop(0)
        function(*args)


class FakeAdapter:
    def __init__(self):
        self.configured = True
        self.calls = []
        self.fail_index = None
        self.result = None

    def is_configured(self):
        return self.configured

    def upload_excel(self, context):
        self.calls.append(context)
        if context.file_index == self.fail_index:
            raise UploadError("测试网站拒绝接收")
        return self.result or UploadResult(True, f"remote-{context.file_index}", f"https://internal.example.test/files/{context.file_index}")


class InternalUploadTests(unittest.TestCase):
    setUp = publishing_fixtures.DatasetPublishingTests.setUp
    tearDown = publishing_fixtures.DatasetPublishingTests.tearDown
    add_session = publishing_fixtures.DatasetPublishingTests.add_session
    registry = publishing_fixtures.DatasetPublishingTests.registry

    def prepare(self, count=2, adapter=None, executor=None):
        sessions = []
        for index in range(count):
            session = self.add_session(f"{index:016x}")
            workbook = Workbook()
            workbook.active.append(["任务", "Thought"])
            workbook.active.append([f"任务 {index}", "已发布的思考"])
            workbook.save(self.exports_root / session["session_id"] / "latest.xlsx")
            sessions.append(session["session_id"])
        self.store = self.registry()
        self.release = self.store.create("内部上传测试", sessions)
        self.adapter = adapter if adapter is not None else FakeAdapter()
        self.manager = self.new_manager(self.adapter, executor)
        self.release_id = self.release["release_id"]
        return self.release

    def new_manager(self, adapter, executor=None):
        return DatasetUploadJobManager(self.store, jobs_dir=self.release_root / "upload_jobs",
                                       internal_adapter=adapter, executor=executor or ImmediateExecutor(), step_delay=0)

    def submit(self):
        response = self.manager.submit(self.release_id, target="internal")
        return self.manager.get(response["job_id"])

    def test_default_unconfigured_does_not_write_or_call(self):
        self.prepare(adapter=internal_uploader)
        original = self.store.releases_file.read_bytes()
        with self.assertRaisesRegex(InternalUploadConflict, "云道S3上传尚未配置"):
            self.submit()
        self.assertEqual(original, self.store.releases_file.read_bytes())
        self.assertEqual(list(self.manager.jobs_dir.glob("*.json")), [])

    def test_one_and_multiple_excels_keep_original_bytes_and_registration_order(self):
        self.prepare(3)
        originals = [self.store.resolve_project_path(item["path"]).read_bytes() for item in self.release["excel_paths"]]
        # Only registered Excel files count, never any raw trajectory or old export.
        job = self.submit()
        self.assertEqual(job["status"], "succeeded")
        self.assertEqual(job["completed_files"], 3)
        self.assertEqual(job["percent"], 100)
        self.assertEqual([item.file_index for item in self.adapter.calls], [0, 1, 2])
        for index, context in enumerate(self.adapter.calls):
            self.assertTrue(context.file_path.is_absolute())
            self.assertEqual(context.file_path.read_bytes(), originals[index])
            self.assertEqual(context.sha256, hashlib.sha256(originals[index]).hexdigest())
            self.assertEqual(context.dataset_name, "内部上传测试")
            self.assertEqual(context.release_id, self.release_id)
            self.assertEqual(context.filename, "latest.xlsx")
            self.assertEqual(context.idempotency_key, f"{self.release_id}:{index}:{context.sha256}")
        self.assertEqual(self.submit()["job_id"], job["job_id"])
        self.assertEqual(len(self.adapter.calls), 3)
        release = self.store.get(self.release_id)
        self.assertEqual(release["upload_status"], "not_uploaded")
        self.assertEqual(release["internal_upload"]["file_results"][2]["remote_id"], "remote-2")

    def test_missing_trajectory_root_does_not_block_single_excel(self):
        self.prepare(1)
        self.store.update(self.release_id, {"trajectory_paths": [self.store.project_path(self.root / "missing-raw")]})
        self.assertFalse(self.store.get(self.release_id)["local_available"])
        self.assertEqual(self.submit()["completed_files"], 1)

    def test_preflight_rejects_all_invalid_sources_before_starting(self):
        self.prepare()
        original = deepcopy(self.release["excel_paths"])
        for change, message in [
            ({"sha256": "0" * 64}, "SHA256"),
            ({"sha256": ""}, "校验值"),
            ({"path": self.store.project_path(self.root / "missing.xlsx")}, "文件不存在"),
            ({"path": "AgenticDataFlywheel/../../secret.xlsx"}, "路径"),
            ({"path": self.store.project_path(self.trajectory_root / "TASK-A" / "_eval_queue.txt")}, "Excel"),
        ]:
            with self.subTest(change=change):
                files = deepcopy(original)
                files[1].update(change)
                self.store.update(self.release_id, {"excel_paths": files})
                with self.assertRaisesRegex(InternalUploadConflict, message):
                    self.submit()
                self.assertEqual(self.adapter.calls, [])
                self.assertEqual(list(self.manager.jobs_dir.glob("*.json")), [])
        self.store.update(self.release_id, {"excel_paths": []})
        with self.assertRaisesRegex(InternalUploadConflict, "没有"):
            self.submit()

    def test_partial_failure_stops_and_retry_only_uploads_remaining(self):
        self.prepare(3)
        self.adapter.fail_index = 1
        failed = self.submit()
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["completed_files"], 1)
        self.assertEqual([item["status"] for item in failed["file_results"]], ["succeeded", "failed", "pending"])
        self.assertEqual(failed["error"], "测试网站拒绝接收")
        self.assertEqual([item.file_index for item in self.adapter.calls], [0, 1])
        first_key = self.adapter.calls[1].idempotency_key
        self.adapter.fail_index = None
        retried = self.submit()
        self.assertNotEqual(retried["job_id"], failed["job_id"])
        self.assertEqual(retried["status"], "succeeded")
        self.assertEqual([item.file_index for item in self.adapter.calls], [0, 1, 1, 2])
        self.assertEqual(first_key, self.adapter.calls[2].idempotency_key)
        self.assertEqual(retried["file_results"][0], failed["file_results"][0])

    def test_retry_revalidates_successful_files_too(self):
        self.prepare()
        self.adapter.fail_index = 1
        self.submit()
        self.adapter.calls[0].file_path.write_bytes(b"changed after success")
        with self.assertRaisesRegex(InternalUploadConflict, "SHA256"):
            self.submit()
        self.assertEqual(len(self.adapter.calls), 2)

    def test_concurrent_queued_submits_schedule_exactly_once(self):
        executor = DeferredExecutor()
        self.prepare(executor=executor)
        with ThreadPoolExecutor(max_workers=8) as pool:
            jobs = list(pool.map(lambda _: self.submit(), range(16)))
        self.assertEqual(len({job["job_id"] for job in jobs}), 1)
        self.assertEqual(len(executor.calls), 1)
        executor.run()
        self.assertEqual(len(self.adapter.calls), 2)

    def test_duplicate_during_remote_call_returns_existing_job(self):
        self.prepare(1, executor=DeferredExecutor())
        entered, release = threading.Event(), threading.Event()
        original = self.adapter.upload_excel
        def blocking(context):
            entered.set()
            if not release.wait(5):
                raise UploadError("test timeout")
            return original(context)
        self.adapter.upload_excel = blocking
        queued = self.submit()
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self.manager._executor.run)
            try:
                self.assertTrue(entered.wait(5))
                self.assertEqual(self.submit()["job_id"], queued["job_id"])
                self.assertEqual(self.submit()["status"], "uploading")
            finally:
                release.set()
                future.result(5)
        self.assertEqual(len(self.adapter.calls), 1)

    def test_restart_preserves_receipts_and_repairs_summary(self):
        self.prepare()
        self.adapter.fail_index = 1
        failed = self.submit()
        # Simulate process exit while second request is in flight.
        failed.update(status="uploading", completed_at=None)
        failed["file_results"][1]["status"] = "uploading"
        self.manager._write(failed)
        self.manager = self.new_manager(self.adapter)
        recovered = self.store.get(self.release_id)["internal_upload"]
        self.assertEqual(recovered["status"], "interrupted")
        self.assertEqual(recovered["error"], "服务重启导致云道S3上传中断，可重试剩余文件")
        self.assertEqual(recovered["completed_files"], 1)
        self.assertEqual(recovered["file_results"][1]["status"], "interrupted")
        self.adapter.fail_index = None
        done = self.submit()
        self.assertEqual([item.file_index for item in self.adapter.calls], [0, 1, 1])
        # A crash between final receipt persistence and registry update is repaired.
        stale = deepcopy(recovered)
        stale["job_id"] = done["job_id"]
        stale["created_at"] = done["created_at"]
        stale["attempt"] = done["attempt"]
        # Force equal wall-clock timestamps: attempt order must win, not glob order.
        failed["created_at"] = done["created_at"]
        self.manager._write(failed)
        self.store.update(self.release_id, {"internal_upload": stale})
        self.manager = self.new_manager(self.adapter)
        self.assertEqual(self.store.get(self.release_id)["internal_upload"]["status"], "succeeded")
        self.assertEqual(self.submit()["job_id"], done["job_id"])

    def test_queued_restart_requires_manual_retry(self):
        self.prepare(executor=DeferredExecutor())
        queued = self.submit()
        self.manager = self.new_manager(self.adapter)
        self.assertEqual(self.manager.get(queued["job_id"])["status"], "interrupted")
        self.assertEqual(self.adapter.calls, [])
        self.assertEqual(self.submit()["status"], "succeeded")

    def test_source_changed_while_queued_fails_without_adapter_call(self):
        executor = DeferredExecutor()
        self.prepare(executor=executor)
        queued = self.submit()
        self.store.resolve_project_path(self.release["excel_paths"][0]["path"]).unlink()
        executor.run()
        job = self.manager.get(queued["job_id"])
        self.assertEqual(job["status"], "failed")
        self.assertEqual(self.adapter.calls, [])
        self.assertIn("文件不存在", job["error"])

    def test_adapter_requires_explicit_success_and_safe_receipt(self):
        self.prepare(1)
        for result in [UploadResult(False), {"success": True},
                       UploadResult(True, url="javascript:alert(1)"),
                       UploadResult(True, url="https://user:secret@example.test/file")]:
            with self.subTest(result=result):
                self.adapter.result = result
                job = self.submit()
                self.assertEqual(job["status"], "failed")
                self.assertEqual(job["completed_files"], 0)
                self.assertNotIn("secret", job["error"])
        with patch.object(self.adapter, "upload_excel", side_effect=RuntimeError("private token secret")):
            job = self.submit()
            self.assertNotIn("secret", job["error"])
            self.assertEqual(job["error"], "云道S3上传异常，请联系维护人员后重试")

    def test_configuration_exception_is_not_exposed(self):
        self.prepare()
        with patch.object(self.adapter, "is_configured", side_effect=RuntimeError("secret")):
            capability = self.manager.internal_capabilities()["internal"]
            self.assertFalse(capability["configured"])
            self.assertNotIn("secret", capability["reason"])
            self.assertEqual(capability["reason"], "云道S3上传配置检查失败，请联系维护人员")

    def test_api_contract_and_legacy_mock_are_separate(self):
        self.prepare(1)
        app = FastAPI()
        app.include_router(router)
        with patch("backend.data_publishing.router.registry", self.store), patch("backend.data_publishing.router.upload_manager", self.manager), TestClient(app) as client:
            url = f"/api/dataset-releases/{self.release_id}/upload"
            self.adapter.configured = False
            self.assertEqual(client.get("/api/dataset-upload-capabilities").json()["internal"],
                             {"configured": False, "reason": "云道S3上传尚未配置"})
            self.assertEqual(client.post(url, json={"target": "internal"}).status_code, 409)
            self.assertEqual(client.post("/api/dataset-releases/missing/upload", json={"target": "internal"}).status_code, 404)
            self.assertEqual(client.post(url, json={"target": "unknown"}).status_code, 422)
            mock = client.post(url)
            self.assertEqual(mock.status_code, 202)
            self.assertEqual(mock.json()["job"]["mode"], "mock")
            self.assertNotIn("internal_upload", self.store.get(self.release_id))
            self.adapter.configured = True
            response = client.post(url, json={"target": "internal"})
            self.assertEqual(response.status_code, 202)
            job_id = response.json()["job"]["job_id"]
            job = client.get(f"/api/dataset-upload-jobs/{job_id}").json()["job"]
            self.assertEqual(job["mode"], "internal")
            self.assertEqual(job["completed_files"], 1)
            self.assertEqual(job["file_results"][0]["remote_id"], "remote-0")
            self.assertEqual(client.post(url, json={"target": "internal"}).json()["job"]["job_id"], job_id)
            self.assertEqual(client.get("/api/dataset-upload-jobs/missing").status_code, 404)
            self.assertEqual(client.get(f"/api/dataset-releases/{self.release_id}/excels/0").content, self.adapter.calls[0].file_path.read_bytes())


if __name__ == "__main__":
    unittest.main()
