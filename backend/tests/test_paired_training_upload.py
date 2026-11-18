"""Paired workbook publication and standalone JSONL transport; no cloud calls."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from openpyxl import load_workbook

from backend.data_publishing.upload_jobs import DatasetUploadJobManager
from backend.data_publishing.upload_sources import resolve_upload_sources
from backend.data_publishing.read_models import project_release
from backend.tests import test_data_publishing as publication_fixtures
from backend.tests.test_data_publishing import ImmediateExecutor
from backend.tests.test_directory_upload_jobs import DirectoryAdapter
from backend.tests import test_upload_sources as source_fixtures


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class PairedPublicationTests(unittest.TestCase):
    setUp = publication_fixtures.DatasetPublishingTests.setUp
    tearDown = publication_fixtures.DatasetPublishingTests.tearDown
    add_session = publication_fixtures.DatasetPublishingTests.add_session
    registry = publication_fixtures.DatasetPublishingTests.registry

    def test_publish_copies_exact_pair_outside_excel_directory_and_downloads(self):
        session = self.add_session()
        directory = self.exports_root / session["session_id"]
        source = directory / "train_export.jsonl"
        report = directory / "report_export.json"
        source.write_text('{"images":["s3://bucket/images/case/step.jpg"]}\n', encoding="utf-8")
        report.write_text('{"excluded_count":0}', encoding="utf-8")
        session["exports"][-1]["training"] = {
            "version": 2, "status": "ready", "filename": source.name, "report_filename": report.name,
            "sha256": sha(source), "report_sha256": sha(report), "included_count": 1, "excluded_count": 0,
            "image_prefix": "s3://bucket/images/", "download_url": "/old-session-download"}
        registry = self.registry()
        release = registry.create("paired", [session["session_id"]])
        item = release["training_files"][0]
        path, name = registry.training_file(release["release_id"], 0)
        report_copy, _ = registry.training_file(release["release_id"], 0, "report")
        self.assertEqual(path.read_bytes(), source.read_bytes())
        self.assertEqual(report_copy.read_bytes(), report.read_bytes())
        self.assertEqual(name, "train.jsonl")
        self.assertNotEqual(path.parent, registry.excel_file(release["release_id"], 0)[0].parent)
        self.assertEqual(list(path.parent.iterdir()), [path])
        self.assertNotIn("download_url", item)
        source.write_text("changed original", encoding="utf-8")
        self.assertEqual(sha(path), item["sha256"])
        path.write_text("tampered", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "摘要"):
            registry.training_file(release["release_id"], 0)
        summary = project_release(release)
        self.assertEqual(summary["training_ready_count"], 1)
        self.assertNotIn("training_files", summary)


class PairedUploadTests(unittest.TestCase):
    setUp = source_fixtures.UploadSourcesTests.setUp
    add_batch = source_fixtures.UploadSourcesTests.add_batch
    freeze = source_fixtures.UploadSourcesTests.freeze

    def pair(self, index=0):
        folder = self.release_dir / "training" / f"{index + 1:03d}"
        path = folder / "files" / "train.jsonl"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"images": ["s3://fixture-bucket/frozen/batch/case/step.jpg"], "uuid": str(index)}) + "\n", encoding="utf-8")
        report = folder / "report.json"
        report.write_text("{}", encoding="utf-8")
        entry = {"version": 2, "status": "ready", "index": index, "filename": path.name,
                 "path": self.registry.project_path(path), "sha256": sha(path), "report_filename": report.name,
                 "report_path": self.registry.project_path(report), "report_sha256": sha(report),
                 "image_prefix": "s3://fixture-bucket/frozen/", "included_count": 1, "excluded_count": 0}
        self.release.setdefault("training_files", []).append(entry)
        return path

    def manager(self):
        self.registry._records.put("dataset_releases", self.release_id, self.release)
        adapter = DirectoryAdapter()
        manager = DatasetUploadJobManager(self.registry, jobs_dir=self.root / "jobs", executor=ImmediateExecutor(), internal_adapter=adapter)
        self.addCleanup(manager.shutdown)
        return manager, adapter

    def test_standalone_skips_images_and_excel_and_target_does_not_rewrite_bytes(self):
        path = self.pair()
        before = path.read_bytes()
        manager, adapter = self.manager()
        with patch("backend.data_publishing.upload_sources.resolve_upload_sources", side_effect=AssertionError("must not inspect originals")), patch("backend.data_publishing.training_jsonl.write_training_bundle", side_effect=AssertionError("must not regenerate")):
            result = manager.submit(self.release_id, target="internal", training_only=True, training_destination_prefix="训练/批次一")
            job = manager.get(result["job_id"])
            self.assertEqual(job["status"], "succeeded", job.get("error"))
            self.assertEqual(len(adapter.calls), 1)
            self.assertEqual(adapter.calls[0].source_dir, path.parent)
            self.assertEqual(adapter.calls[0].bucket_path, "训练/批次一/")
            again = manager.submit(self.release_id, target="internal", training_only=True, training_destination_prefix="训练/批次一")
            self.assertEqual(again["job_id"], result["job_id"])
            self.assertEqual(len(adapter.calls), 1)
            manager.submit(self.release_id, target="internal", training_only=True, training_destination_prefix="另一个目录")
        self.assertEqual(adapter.calls[-1].bucket_path, "另一个目录/")
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(manager.training_file(result["job_id"], "jsonl").read_bytes(), before)

    def test_http_download_and_standalone_target_contract(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from backend.data_publishing.router import router
        path = self.pair()
        manager, adapter = self.manager()
        app = FastAPI(); app.include_router(router)
        with patch("backend.data_publishing.router.registry", self.registry), patch("backend.data_publishing.router.upload_manager", manager), TestClient(app) as client:
            base = f"/api/dataset-releases/{self.release_id}"
            downloaded = client.get(base + "/training/0?kind=jsonl")
            self.assertEqual(downloaded.content, path.read_bytes())
            self.assertIn("application/x-ndjson", downloaded.headers["content-type"])
            self.assertEqual(client.get(base + "/training/0?kind=report").json(), {})
            response = client.post(base + "/upload", json={"target": "internal", "training_only": True,
                                                          "training_jsonl": True, "training_destination_prefix": "独立目标"})
            self.assertEqual(response.status_code, 202, response.text)
            self.assertTrue(response.json()["job"]["training_only"])
            self.assertEqual(adapter.calls[0].bucket_path, "独立目标/")

    def test_multiple_files_retry_only_failed_file_and_never_upload_report(self):
        self.pair(0); self.pair(1)
        manager, adapter = self.manager()
        adapter.fail_at = 2
        configuration = adapter.get_configuration()
        with patch.object(adapter, "get_configuration", return_value={**configuration, "default_prefix": "dataset/figures/"}):
            first = manager.submit(self.release_id, target="internal", training_only=True)
        self.assertEqual(manager.get(first["job_id"])["status"], "failed")
        with patch.object(adapter, "get_configuration", return_value={**configuration, "training_prefix": "new-default"}):
            second = manager.submit(self.release_id, target="internal", training_only=True,
                                    retry_job_id=first["job_id"])
        self.assertEqual(manager.get(second["job_id"])["status"], "succeeded")
        root = f"dataset/training_jsonl/{self.release_id}"
        self.assertEqual([call.bucket_path for call in adapter.calls], [f"{root}/001/", f"{root}/002/", f"{root}/002/"])
        for call in adapter.calls:
            self.assertEqual([file.name for file in call.source_dir.iterdir()], ["train.jsonl"])

    def test_retry_preserves_legacy_implicit_training_directory(self):
        self.pair()
        self.release["batch_ids"] = ["batch_001"]
        manager, adapter = self.manager()
        legacy_id = "ab" * 16
        manager._write({
            "job_id": legacy_id, "release_id": self.release_id, "status": "failed",
            "mode": "internal", "folder_upload": True, "include_trajectories": False,
            "training_only": True, "training_jsonl": True, "training_destination_prefix": None,
            "destination": {"prefix": "old/figures", "bucket": "fixture-bucket", "region": "test-region"},
        })
        resumed = manager.submit(self.release_id, target="internal", training_only=True, retry_job_id=legacy_id)
        job = manager.get(resumed["job_id"])
        self.assertEqual(job["status"], "succeeded", job.get("error"))
        self.assertIsNone(job["training_destination_prefix"])
        self.assertEqual(adapter.calls[0].bucket_path, "old/figures/batch_001/训练数据/")

    def test_unavailable_pair_does_not_block_ready_pair(self):
        self.pair(0)
        self.release["training_files"].insert(0, {"version": 2, "index": 1, "status": "needs_configuration", "error": "未填写图片前缀"})
        manager, adapter = self.manager()
        first = manager.submit(self.release_id, target="internal", training_only=True)
        job = manager.get(first["job_id"])
        self.assertEqual(job["status"], "failed")
        self.assertIn("部分", job["error"])
        self.assertEqual(job["completed_items"], 1)
        self.assertEqual(job["training"]["excluded_files"][0]["index"], 1)
        manager.submit(self.release_id, target="internal", training_only=True, retry_job_id=first["job_id"])
        self.assertEqual(len(adapter.calls), 1)

    def test_temporarily_missing_pair_is_rechecked_after_recovery(self):
        self.pair(0)
        second_path = self.pair(1)
        original = second_path.read_bytes()
        second_path.unlink()
        manager, adapter = self.manager()
        result = manager.submit(self.release_id, target="internal", training_only=True, training_destination_prefix="target")
        self.assertEqual(manager.get(result["job_id"])["status"], "failed")
        self.assertEqual(len(adapter.calls), 1)
        second_path.write_bytes(original)
        resumed = manager.submit(self.release_id, target="internal", training_only=True, retry_job_id=result["job_id"])
        job = manager.get(resumed["job_id"])
        self.assertEqual(job["status"], "succeeded", job.get("error"))
        self.assertEqual([call.bucket_path for call in adapter.calls], ["target/001/", "target/002/"])

    def test_confirmed_receipt_survives_local_file_removal_during_other_retry(self):
        confirmed = self.pair(0)
        self.pair(1)
        manager, adapter = self.manager()
        adapter.fail_at = 2
        first = manager.submit(self.release_id, target="internal", training_only=True, training_destination_prefix="target")
        self.assertEqual(manager.get(first["job_id"])["status"], "failed")
        confirmed.unlink()
        resumed = manager.submit(self.release_id, target="internal", training_only=True, retry_job_id=first["job_id"])
        self.assertEqual(manager.get(resumed["job_id"])["status"], "succeeded")
        self.assertEqual([call.bucket_path for call in adapter.calls], ["target/001/", "target/002/", "target/002/"])

    def test_missing_training_prefix_does_not_block_ordinary_image_upload(self):
        _, rows, path = self.add_batch(selected=(2,))
        book = load_workbook(path)
        sheet = book.active
        sheet.cell(1, sheet.max_column + 1, "training_image_relative")
        sheet.cell(2, sheet.max_column, "batch_001/case/trajectory/step001_vla_input.jpg")
        sheet.cell(1, sheet.max_column + 1, "training_image_prefix")
        book.save(path); book.close()
        old = self.release["source_refs"][0]["artifact"]
        artifact = self.store.publish("batch_001", "07_cot", {"batch_id": "batch_001", "groups": [{"rows": rows}]},
                                      workbooks={"full_dataset.xlsx": path}, source_refs=old["source_refs"])
        self.release["source_refs"][0]["artifact"] = artifact
        self.release["excel_paths"][0]["sha256"] = sha(path)
        self.release["training_files"] = [{"version": 2, "index": 0, "status": "needs_configuration"}]
        self.freeze()
        manager, adapter = self.manager()
        result = manager.submit(self.release_id, target="internal", include_trajectories=True, destination_prefix="后来配置")
        job = manager.get(result["job_id"])
        self.assertEqual(job["status"], "succeeded", job.get("error"))
        self.assertEqual(len(adapter.calls), 2)
        self.assertTrue(adapter.calls[1].bucket_path.startswith("后来配置/batch_001/"))

    def test_paired_upload_uses_frozen_image_destination_and_complete_nested_directory(self):
        raw, rows, path = self.add_batch(selected=(2,))
        owner = self.directories[1]
        nested = owner / "screens" / "嵌套.jpg"
        nested.parent.mkdir()
        nested.write_bytes(b"image evidence")
        book = load_workbook(path)
        sheet = book.active
        relative = "batch_001/用例01/trajectory2"
        for name in ("training_image_prefix", "training_image_relative", "training_trajectory_relative"):
            sheet.cell(1, sheet.max_column + 1, name)
        headers = [cell.value for cell in sheet[1]]
        row = {**rows[1], "training_image_prefix": "s3://fixture-bucket/frozen/",
               "training_image_relative": relative + "/step001_vla_input.jpg", "training_trajectory_relative": relative}
        for col, name in enumerate(headers, 1):
            sheet.cell(2, col, row.get(name))
        row.update(image=nested.relative_to(raw).as_posix(), training_image_relative=relative + "/screens/嵌套.jpg")
        sheet.append([row.get(name) for name in headers])
        book.save(path); book.close()
        old = self.release["source_refs"][0]["artifact"]
        artifact = self.store.publish("batch_001", "07_cot", {"batch_id": "batch_001", "groups": [{"rows": rows}]},
                                      workbooks={"full_dataset.xlsx": path}, source_refs=old["source_refs"])
        self.release["source_refs"][0]["artifact"] = artifact
        self.release["excel_paths"][0]["sha256"] = sha(path)
        self.freeze(); self.pair()
        sources = resolve_upload_sources(self.registry, self.release)
        trajectories = [item for item in sources if item["kind"] == "trajectory"]
        self.assertEqual(len(trajectories), 1)
        self.assertEqual(Path(trajectories[0]["source_dir"]), owner)
        self.assertEqual(trajectories[0]["destination_relative"], relative)
        manager, adapter = self.manager()
        result = manager.submit(self.release_id, target="internal", include_trajectories=True, training_jsonl=True,
                                destination_prefix="new-excel", training_destination_prefix="json-only")
        job = manager.get(result["job_id"])
        self.assertEqual(job["status"], "succeeded", job.get("error"))
        self.assertEqual([call.bucket_path for call in adapter.calls], ["new-excel/batch_001/发布表格/001/", "frozen/" + relative + "/", "json-only/"])


if __name__ == "__main__":
    unittest.main()
