"""Directory task orchestration; transport fixtures never contact cloud storage."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.data_publishing.internal_uploader import UploadError, UploadResult
from backend.data_publishing.router import router
from backend.tests import test_internal_dataset_upload as internal_tests
from backend.tests.test_internal_dataset_upload import DeferredExecutor


class DirectoryAdapter:
    def __init__(self):
        self.calls = []
        self.fail_at = None
        self.process_state = "dead"

    def is_configured(self):
        return True

    def get_configuration(self):
        return {"configured": True, "directory_upload": True, "default_prefix": "发布/默认",
                "bucket": "fixture-bucket", "region": "test-region"}

    def probe_process(self, record):
        return self.process_state

    def upload_directory(self, context, on_progress, on_process):
        self.calls.append(context)
        on_process({"pid": 2345, "create_time": 123.5, "script": "fixture.py"})
        on_progress({"message": "uploading 2 MiB/s", "speed_text": "2 MiB/s"})
        if len(self.calls) == self.fail_at:
            raise UploadError("fixture rejected directory")
        return UploadResult(True)


class DirectoryJobTests(unittest.TestCase):
    setUp = internal_tests.InternalUploadTests.setUp
    tearDown = internal_tests.InternalUploadTests.tearDown
    add_session = internal_tests.InternalUploadTests.add_session
    registry = internal_tests.InternalUploadTests.registry
    prepare = internal_tests.InternalUploadTests.prepare
    new_manager = internal_tests.InternalUploadTests.new_manager

    def setup_directory(self, executor=None):
        self.prepare(1, DirectoryAdapter(), executor=executor)
        excel = self.release["excel_paths"][0]
        path = self.store.resolve_project_path(excel["path"])
        self.sources = [{"kind": "excel", "source_dir": str(path.parent), "file_path": str(path),
                         "filename": path.name, "sha256": excel["sha256"], "source_identity": "excel-1",
                         "batch_id": "batch-1", "case_id": "", "destination_relative": "batch-1/发布表格/001"}]
        for i in (1, 2):
            folder = self.trajectory_root / "case-1" / f"trajectory-{i}"
            folder.mkdir(parents=True)
            (folder / "step.png").write_bytes(b"evidence; no quality inspection")
            self.sources.append({"kind": "trajectory", "source_dir": str(folder), "filename": folder.name,
                                 "batch_id": "batch-1", "case_id": "case-1", "source_identity": str(i),
                                 "destination_relative": f"batch-1/case-1/trajectory-{i}"})
        self.resolver = patch("backend.data_publishing.upload_sources.resolve_upload_sources", return_value=self.sources)
        self.resolve = self.resolver.start()
        self.addCleanup(self.resolver.stop)

    def submit_folders(self, prefix=None):
        response = self.manager.submit(self.release_id, target="internal", include_trajectories=True,
                                       destination_prefix=prefix)
        return self.manager.get(response["job_id"])

    def test_background_acceptance_no_source_scan_and_concurrent_dedup(self):
        executor = DeferredExecutor()
        self.setup_directory(executor)
        with ThreadPoolExecutor(max_workers=4) as pool:
            jobs = list(pool.map(lambda _: self.submit_folders(), range(8)))
        self.assertEqual(len({job["job_id"] for job in jobs}), 1)
        self.assertEqual(jobs[0]["status"], "queued")
        self.assertEqual(self.resolve.call_count, 0)
        self.assertEqual(len(executor.calls), 1)
        executor.run()
        job = self.manager.get(jobs[0]["job_id"])
        self.assertEqual(job["status"], "succeeded")
        self.assertEqual(job["source_summary"], {"case_count": 1, "trajectory_count": 2, "excel_count": 1})
        self.assertEqual(job["completed_items"], 3)
        self.assertEqual(job["percent"], 100)
        self.assertEqual(self.adapter.calls[1].bucket_path, "发布/默认/batch-1/case-1/trajectory-1/")
        self.assertEqual(self.adapter.calls[1].source_dir, Path(self.sources[1]["source_dir"]))

    def test_retry_skips_confirmed_dirs_and_different_destination_is_new(self):
        self.setup_directory()
        self.adapter.fail_at = 2
        failed = self.submit_folders("group/a")
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["completed_items"], 1)
        self.assertEqual(failed["progress"]["speed_text"], "2 MiB/s")
        self.adapter.fail_at = None
        done = self.submit_folders("group/a/")
        self.assertEqual(done["status"], "succeeded")
        self.assertEqual(len(self.adapter.calls), 4)
        self.assertEqual(self.resolve.call_count, 1)
        self.assertEqual(done["item_results"][0], failed["item_results"][0])
        self.assertEqual(self.submit_folders("group/a")["job_id"], done["job_id"])
        other = self.submit_folders("group/b")
        self.assertNotEqual(other["job_id"], done["job_id"])
        self.assertEqual(len(self.adapter.calls), 7)
        self.assertEqual(self.submit_folders("group/a")["job_id"], done["job_id"])

    def test_restart_does_not_start_second_child_while_original_alive_or_unknown(self):
        self.setup_directory()
        self.adapter.fail_at = 2
        failed = self.submit_folders()
        failed.update(status="uploading", stage="uploading")
        failed["item_results"][1]["status"] = "uploading"
        self.manager._write(failed)
        self.adapter.process_state = "alive"
        self.manager = self.new_manager(self.adapter)
        recovering = self.submit_folders()
        self.assertEqual(recovering["job_id"], failed["job_id"])
        self.assertEqual(recovering["stage"], "recovering")
        self.assertEqual(len(self.adapter.calls), 2)
        self.adapter.process_state = "unknown"
        self.assertEqual(self.submit_folders()["stage"], "recovering")
        self.adapter.process_state = "dead"
        interrupted = self.manager.get(failed["job_id"])
        self.assertEqual(interrupted["status"], "interrupted")
        self.adapter.fail_at = None
        self.assertEqual(self.submit_folders()["status"], "succeeded")
        self.assertEqual(len(self.adapter.calls), 4)

    def test_queued_restart_is_retryable_and_keeps_frozen_release(self):
        self.setup_directory(DeferredExecutor())
        job = self.submit_folders()
        snapshot = deepcopy(job["release_snapshot"])
        self.store.update(self.release_id, {"name": "changed display only"})
        self.manager = self.new_manager(self.adapter)
        self.assertEqual(self.manager.get(job["job_id"])["status"], "interrupted")
        self.assertEqual(self.submit_folders()["status"], "succeeded")
        self.assertEqual(self.resolve.call_args.args[1]["excel_paths"], snapshot["excel_paths"])

    def test_bad_source_is_transport_failure_not_quality_validation(self):
        self.setup_directory()
        self.sources[1]["source_dir"] = str(self.root / "不存在的轨迹")
        job = self.submit_folders()
        self.assertEqual(job["status"], "failed")
        self.assertIn("不存在", job["error"])
        self.assertEqual(job["completed_items"], 1)
        self.assertEqual(len(self.adapter.calls), 1)

    def test_progress_small_record_not_whole_release_per_line(self):
        self.setup_directory()
        original = self.adapter.upload_directory
        def noisy(context, on_progress, on_process):
            for i in range(100):
                on_progress({"message": str(i), "speed_text": "1 MB/s"})
            return original(context, on_progress, on_process)
        self.adapter.upload_directory = noisy
        with patch.object(self.manager, "_write", wraps=self.manager._write) as writes:
            job = self.submit_folders()
        self.assertEqual(job["status"], "succeeded")
        self.assertLess(writes.call_count, 25)
        self.assertLessEqual(len(job["recent_output"]), 20)

    def test_http_contract_old_target_and_new_options(self):
        self.setup_directory(DeferredExecutor())
        app = FastAPI()
        app.include_router(router)
        with patch("backend.data_publishing.router.registry", self.store), \
             patch("backend.data_publishing.router.upload_manager", self.manager), TestClient(app) as client:
            url = f"/api/dataset-releases/{self.release_id}/upload"
            response = client.post(url, json={"target": "internal", "include_trajectories": True,
                                               "destination_prefix": "团队/上传"})
            self.assertEqual(response.status_code, 202)
            self.assertEqual(response.json()["job"]["destination"]["prefix"], "团队/上传")
            self.assertEqual(self.resolve.call_count, 0)
            self.assertEqual(client.post(url, json={"target": "mock", "include_trajectories": True}).status_code, 422)
            self.assertEqual(client.post(url, json={"target": "internal", "include_trajectories": True,
                                                     "destination_prefix": "../escape"}).status_code, 422)

    def test_retry_does_not_silently_switch_bucket_after_config_change(self):
        self.setup_directory()
        self.adapter.fail_at = 2
        failed = self.submit_folders()
        config = self.adapter.get_configuration()
        with patch.object(self.adapter, "get_configuration", return_value={**config, "bucket": "new-bucket"}):
            with self.assertRaisesRegex(ValueError, "原上传目标"):
                self.manager.submit(self.release_id, target="internal", include_trajectories=True,
                                    destination_prefix=failed["destination"]["prefix"], retry_job_id=failed["job_id"])
        self.assertEqual(len(self.adapter.calls), 2)

    def test_runtime_availability_changes_do_not_change_dedup_identity(self):
        self.setup_directory(DeferredExecutor())
        queued = self.submit_folders()
        Path(self.sources[0]["file_path"]).unlink()
        self.assertEqual(self.submit_folders()["job_id"], queued["job_id"])


class RealTransportChainTests(unittest.TestCase):
    def test_http_frozen_release_to_real_local_script_sends_only_selected_complete_directories(self):
        import hashlib
        import json
        import os
        import sys
        from backend.tests.test_upload_sources import UploadSourcesTests
        from backend.tests.test_data_publishing import ImmediateExecutor
        from backend.data_publishing.upload_jobs import DatasetUploadJobManager
        from backend.data_publishing import internal_uploader
        fixture = UploadSourcesTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.add_batch(selected=(2, 7))
        release = fixture.freeze()
        fixture.registry._records.put("dataset_releases", fixture.release_id, release)
        script = fixture.root / "模拟上传.py"
        script.write_text('''import json, sys
from pathlib import Path
args = dict(arg[2:].split('=', 1) for arg in sys.argv[1:] if '=' in arg)
source = Path(args['local_folder_absolute_path'])
with Path('calls.jsonl').open('a', encoding='utf-8') as out:
    out.write(json.dumps({'source': str(source), 'target': args['bucket_path'],
                         'files': sorted(str(p.relative_to(source)) for p in source.rglob('*') if p.is_file())}, ensure_ascii=False) + '\\n')
print('1MiB/1MiB 2MiB/s', end='\\r', flush=True)
''', encoding="utf-8")
        before = {str(p): hashlib.sha256(p.read_bytes()).hexdigest()
                  for directory in fixture.directories for p in directory.rglob("*") if p.is_file()}
        with patch.dict(os.environ, {"ADF_YUNDAO_PYTHON": sys.executable, "ADF_YUNDAO_SCRIPT": str(script),
                                    "ADF_YUNDAO_APP_TOKEN": "fake-chain-token", "ADF_YUNDAO_REGION": "region-test",
                                    "ADF_YUNDAO_BUCKET": "bucket-test", "ADF_YUNDAO_PREFIX": "prefix"}):
            manager = DatasetUploadJobManager(fixture.registry, jobs_dir=fixture.root / "jobs",
                                              executor=ImmediateExecutor(), internal_adapter=internal_uploader)
            app = FastAPI()
            app.include_router(router)
            with patch("backend.data_publishing.router.registry", fixture.registry), \
                 patch("backend.data_publishing.router.upload_manager", manager), TestClient(app) as client:
                response = client.post(f"/api/dataset-releases/{fixture.release_id}/upload",
                                       json={"target": "internal", "include_trajectories": True,
                                             "destination_prefix": "团队/已发布"})
                self.assertEqual(response.status_code, 202, response.text)
                job = client.get("/api/dataset-upload-jobs/" + response.json()["job"]["job_id"]).json()["job"]
                self.assertEqual(job["status"], "succeeded", job.get("error"))
                self.assertNotIn("fake-chain-token", json.dumps(job))
                old_response = client.post(f"/api/dataset-releases/{fixture.release_id}/upload",
                                           json={"target": "internal"})
                self.assertEqual(old_response.status_code, 202, old_response.text)
                old_job = client.get("/api/dataset-upload-jobs/" + old_response.json()["job"]["job_id"]).json()["job"]
                self.assertEqual(old_job["status"], "succeeded", old_job.get("error"))
                self.assertFalse(old_job["include_trajectories"])
                self.assertEqual(old_job["completed_files"], 1)
                self.assertGreater(old_job["completed_bytes"], 0)
                self.assertEqual(old_job["source_summary"]["trajectory_count"], 0)
            calls = [json.loads(line) for line in (fixture.root / "calls.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertEqual(len(calls), 4)
            self.assertEqual({Path(c["source"]) for c in calls[1:3]}, {fixture.directories[1], fixture.directories[6]})
            self.assertTrue(all(len(c["files"]) == 4 for c in calls[1:3]))
            self.assertEqual(calls[0]["target"], "团队/已发布/batch_001/发布表格/001/")
            self.assertTrue(all(c["target"].startswith("团队/已发布/batch_001/用例01/") for c in calls[1:3]))
        after = {str(p): hashlib.sha256(p.read_bytes()).hexdigest()
                 for directory in fixture.directories for p in directory.rglob("*") if p.is_file()}
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
