"""External publication invariants using only temporary files and local fake services."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
from openpyxl import Workbook
from openpyxl.styles import Font

from backend.batch_lifecycle import lifecycle, ensure_batch_active, BatchPublishedError
from backend.data_publishing.external_imports import ExternalReleaseImporter, ImportFailure, IMPORTS
from backend.data_publishing.service import DatasetReleaseRegistry
from backend.data_publishing.upload_jobs import DatasetUploadJobManager
from backend.data_publishing.router import router
from backend.data_store import RecordStore
from backend.tests.test_data_publishing import ImmediateExecutor
from backend.tests.test_internal_dataset_upload import FakeAdapter
from backend.training_data_overview.service import TrainingOverviewManager, CONVERSIONS
from backend.training_data_overview.converter import convert_release


class SimulatedCrash(BaseException):
    pass


class ExternalDatasetImportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.data = self.root / "data"
        self.registry = DatasetReleaseRegistry(project_root=self.root, data_root=self.data,
            releases_file=self.data / "system" / "dataset_release" / "releases.json",
            trajectory_root=self.data / "raw" / "absent",
            correction_exports_dir=self.data / "system" / "exports",
            session_loader=lambda key: self.fail("External import must not load a session"),
            session_lister=lambda: [])
        self.importer = ExternalReleaseImporter(self.registry)
        self.records = RecordStore(self.data)
        self.book = self.workbook()

    def tearDown(self):
        self.temporary.cleanup()

    def workbook(self, *, title="完整步骤", style=False, identity="tr-one", auxiliary=False):
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = title
        sheet.append(["trajectory_id", "image", "actions", "sop", "APP", "一级场景", "二级场景"])
        sheet.append([identity, "arbitrary/step001.jpg", '{"action":"click"}', "点击", "表内App", "业务", "查询"])
        sheet.append([identity, "arbitrary/step002.jpg", '{"action":"finish"}', "结束", "", "", ""])
        if style:
            sheet["A1"].font = Font(bold=True)
        if auxiliary:
            other = workbook.create_sheet("另一批")
            other.append(["trajectory_id", "actions"])
            other.append(["different", '{"action":"wait"}'])
        stream = BytesIO()
        workbook.save(stream)
        workbook.close()
        return stream.getvalue()

    def preview(self, data=None, **options):
        args = {"data_source": "人工采集", "data_date": "2024-02-03", "app": "补充App",
                "level1": "补充一级", "level2": "补充二级", "sheet_name": ""}
        args.update(options)
        return self.importer.preview("外部完整数据集.xlsx", BytesIO(data or self.book), **args)

    def publish(self, preview, *, name="历史人工数据", request_id=None):
        return self.importer.publish(preview["import_id"], name, request_id or uuid4().hex)

    def test_release_is_self_contained_and_closed_without_workflow_artifacts(self):
        preview = self.preview()
        self.assertTrue(preview["valid"], preview)
        release = self.publish(preview)
        self.assertEqual(release["source_kind"], "external_manual")
        self.assertEqual((release["trajectory_count"], release["step_count"]), (1, 2))
        self.assertEqual(release["trajectory_paths"], [])
        batch_id, = release["batch_ids"]
        self.assertTrue(batch_id.startswith("external_"))
        self.assertEqual(lifecycle(batch_id, self.data)["release_id"], release["release_id"])
        with self.assertRaises(BatchPublishedError):
            ensure_batch_active(batch_id, self.data)
        self.assertFalse((self.data / "batches").exists())
        self.assertEqual(self.records.list("correction_sessions"), [])
        self.assertEqual(self.registry.candidates(), [])
        path, name = self.registry.excel_file(release["release_id"], 0)
        self.assertEqual(path.read_bytes(), self.book)
        self.assertEqual(name, "外部完整数据集.xlsx")
        self.assertFalse(self.importer._directory(preview["import_id"]).exists())
        converted = convert_release(self.registry, release)
        row, = converted["rows"]
        self.assertEqual(row["APP"], "表内App")
        self.assertEqual(row["一级场景"], "业务")
        self.assertEqual(row["生产方式"], "人工采集")
        self.assertTrue(row["时间"].startswith("2024-02-03"))
        self.assertIsNone(row["人工精修步骤数量"])
        self.assertFalse(row["manual_known"])
        self.assertTrue(row["轨迹"].startswith(release["release_id"] + "/"))

    def test_request_and_result_retries_never_create_second_release(self):
        preview = self.preview()
        original = self.publish(preview, request_id="once")
        repeated = self.publish(preview, request_id="once")
        self.assertEqual(original["release_id"], repeated["release_id"])
        duplicate = self.preview()
        self.assertEqual(duplicate["duplicate_release"]["release_id"], original["release_id"])
        self.assertEqual(self.publish(duplicate)["release_id"], original["release_id"])
        self.assertEqual(len(self.registry.list_releases()), 1)
        with self.assertRaises(ImportFailure) as caught:
            self.publish(preview, name="different", request_id="once")
        self.assertEqual(caught.exception.detail["code"], "idempotency_conflict")

    def test_renamed_sheet_and_style_only_changes_reuse_normalized_result(self):
        original = self.publish(self.preview())
        copied = self.workbook(title="重命名工作表", style=True)
        self.assertNotEqual(copied, self.book)
        preview = self.preview(copied)
        self.assertEqual(preview["duplicate_release"]["release_id"], original["release_id"])
        self.assertEqual(self.publish(preview)["release_id"], original["release_id"])

    def test_same_file_conflicting_metadata_is_blocked_in_preview_and_at_commit(self):
        stale = self.preview(data_date="2020-01-01")
        original = self.publish(self.preview())
        conflict = self.preview(data_source="其它来源")
        self.assertFalse(conflict["valid"])
        self.assertIsNone(conflict["import_id"])
        self.assertEqual(conflict["duplicate_release"]["release_id"], original["release_id"])
        with self.assertRaises(ImportFailure) as caught:
            self.publish(stale)
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(caught.exception.detail["release_id"], original["release_id"])
        self.assertEqual(len(self.registry.list_releases()), 1)

    def test_different_business_sheets_are_independent_batches(self):
        workbook = self.workbook(auxiliary=True)
        first = self.publish(self.preview(workbook))
        second = self.publish(self.preview(workbook, sheet_name="另一批"))
        self.assertNotEqual(first["release_id"], second["release_id"])
        self.assertEqual(second["step_count"], 1)

    def test_parallel_previews_publish_exactly_once(self):
        first, second = self.preview(), self.preview()
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(self.publish, [first, second]))
        self.assertEqual(results[0]["release_id"], results[1]["release_id"])
        self.assertEqual(len(self.registry.list_releases()), 1)
        self.assertEqual(len(self.records.list("batch_lifecycle")), 1)

    def test_transaction_failure_leaves_no_release_and_allows_retry(self):
        preview = self.preview()
        with patch.object(self.importer.records, "put_many", side_effect=RuntimeError("transaction failure")):
            with self.assertRaises(RuntimeError):
                self.publish(preview, request_id="retry")
        self.assertEqual(self.registry.list_releases(), [])
        self.assertEqual(self.records.list("batch_lifecycle"), [])
        self.assertEqual(self.records.get(IMPORTS, preview["import_id"])["status"], "validated")
        self.assertEqual(list((self.data / "releases").iterdir()), [])
        self.assertIsNotNone(self.publish(preview, request_id="retry"))

    def test_commit_response_loss_keeps_files_and_reuses_registered_release(self):
        preview = self.preview()
        commit = self.importer.records.put_many
        def uncertain(entries):
            commit(entries)
            raise OSError("connection lost after commit")
        with patch.object(self.importer.records, "put_many", side_effect=uncertain):
            with self.assertRaises(OSError):
                self.publish(preview, request_id="retry")
        release = self.publish(preview, request_id="retry")
        path, _ = self.registry.excel_file(release["release_id"], 0)
        self.assertEqual(path.read_bytes(), self.book)
        self.assertEqual(len(self.registry.list_releases()), 1)

    def test_crash_after_file_replace_recovers_without_exposing_half_release(self):
        preview = self.preview()
        prepare = self.importer._prepare
        def crash(*args):
            prepare(*args)
            raise SimulatedCrash()
        with patch.object(self.importer, "_prepare", side_effect=crash):
            with self.assertRaises(SimulatedCrash):
                self.publish(preview, request_id="retry")
        self.assertEqual(self.registry.list_releases(), [])
        record = self.records.get(IMPORTS, preview["import_id"])
        self.assertEqual(record["status"], "publishing")
        self.assertTrue((self.data / "releases" / record["target_release_id"]).exists())
        ExternalReleaseImporter(self.registry).recover()
        self.assertEqual(list((self.data / "releases").iterdir()), [])
        release = self.publish(preview, request_id="retry")
        self.assertEqual(release["release_id"], record["target_release_id"])

    def test_expiration_and_tampering_require_new_preview(self):
        expired = self.preview()
        self.records.update(IMPORTS, expired["import_id"], lambda record: record.update(
            expires_at=(datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()))
        with self.assertRaises(ImportFailure) as caught:
            self.publish(expired)
        self.assertEqual(caught.exception.status, 410)
        self.assertFalse(self.importer._directory(expired["import_id"]).exists())
        changed = self.preview()
        (self.importer._directory(changed["import_id"]) / "source.xlsx").write_bytes(b"modified")
        with self.assertRaises(ImportFailure) as caught:
            self.publish(changed)
        self.assertEqual(caught.exception.detail["code"], "import_changed")
        self.assertEqual(self.registry.list_releases(), [])

    def test_empty_invalid_oversized_files_and_cleanup_boundary(self):
        for filename, data in [("bad.csv", b"x"), ("empty.xlsx", b"")]:
            with self.subTest(filename=filename), self.assertRaises(ImportFailure):
                self.importer.preview(filename, BytesIO(data), data_source="人工采集", data_date="2024-02-03")
        tiny = ExternalReleaseImporter(self.registry, max_upload_bytes=8)
        with self.assertRaises(ImportFailure) as caught:
            tiny.preview("large.xlsx", BytesIO(self.book), data_source="人工采集", data_date="2024-02-03")
        self.assertEqual(caught.exception.status, 413)
        outside = self.root / "outside"
        outside.mkdir()
        with self.assertRaises(ImportFailure):
            self.importer._remove(outside, self.data)
        self.assertTrue(outside.exists())

    def test_external_s3_adapter_receives_only_frozen_original_and_reuses_success(self):
        release = self.publish(self.preview())
        adapter = FakeAdapter()
        manager = DatasetUploadJobManager(self.registry, jobs_dir=self.data / "system" / "upload_jobs",
            internal_adapter=adapter, executor=ImmediateExecutor(), step_delay=0)
        queued = manager.submit(release["release_id"], target="internal")
        completed = manager.get(queued["job_id"])
        self.assertEqual(completed["status"], "succeeded")
        self.assertEqual(completed["completed_files"], 1)
        context, = adapter.calls
        self.assertEqual(context.file_path.read_bytes(), self.book)
        self.assertEqual(context.filename, "外部完整数据集.xlsx")
        self.assertEqual(context.sha256, release["external_import"]["source_sha256"])
        self.assertEqual(manager.submit(release["release_id"], target="internal")["job_id"], queued["job_id"])
        self.assertEqual(len(adapter.calls), 1)
        self.assertEqual(self.registry.get(release["release_id"])["source_kind"], "external_manual")

    def test_conversion_failure_preserves_other_catalog_rows_and_retry_counts_once(self):
        original = self.publish(self.preview())
        manager = TrainingOverviewManager(self.registry, executor=ImmediateExecutor())
        manager.start()
        try:
            version = manager.query()["version"]
            # Same trajectory name in a different batch remains independent.
            other = self.publish(self.preview(self.workbook(title="different"),
                data_source="补录", data_date="2023-01-01"))
            canonical = self.registry.resolve_project_path(other["external_import"]["canonical"]["path"])
            intact = canonical.read_bytes()
            canonical.write_bytes(b"broken")
            with self.assertLogs("backend.training_data_overview.service", level="ERROR"):
                manager.submit(other["release_id"])
            self.assertEqual(manager.records.get(CONVERSIONS, other["release_id"])["status"], "failed")
            self.assertEqual(manager.query()["version"], version)
            self.assertEqual(manager.query()["overview"]["total_steps"], 2)
            self.assertIsNotNone(self.registry.get(other["release_id"]))
            canonical.write_bytes(intact)
            manager.submit(other["release_id"])
            self.assertEqual(manager.query()["overview"]["total_steps"], 4)
            self.assertEqual(manager.query()["overview"]["total_trajectories"], 2)
            final_version = manager.query()["version"]
            manager.submit(other["release_id"])
            self.assertEqual(manager.query()["version"], final_version)
            path, _ = self.registry.excel_file(original["release_id"], 0)
            self.assertEqual(path.read_bytes(), self.book)
        finally:
            manager.close()

    def test_http_publication_automatically_merges_into_overview_without_real_services(self):
        manager = TrainingOverviewManager(self.registry, executor=ImmediateExecutor())
        manager.start()
        app = FastAPI()
        app.include_router(router)
        try:
            with patch("backend.data_publishing.router.registry", self.registry), \
                 patch("backend.data_publishing.router.overview_manager", manager), TestClient(app) as client:
                preview = client.post("/api/dataset-release-imports/preview",
                    files={"file": ("人工.xlsx", self.book, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
                    data={"data_source": "人工采集", "data_date": "2020-01-02"}).json()
                response = client.post("/api/dataset-releases/import",
                    json={"import_id": preview["import_id"], "name": "人工历史", "request_id": "http-one"})
                self.assertEqual(response.status_code, 201, response.text)
                release = response.json()["release"]
                result = manager.query()
                self.assertEqual(result["overview"]["total_steps"], 2)
                self.assertFalse(result["overview"]["show_manual_refine_steps"])
                self.assertEqual(result["filters"]["sources"], ["人工采集"])
                self.assertEqual(result["trend"][0]["date"], "2020-01-02")
                self.assertEqual(result["conversions"][0]["status"], "succeeded")
                version = result["version"]
                replay = client.post("/api/dataset-releases/import",
                    json={"import_id": preview["import_id"], "name": "人工历史", "request_id": "http-one"})
                self.assertEqual(replay.json()["release"]["release_id"], release["release_id"])
                self.assertEqual(manager.query()["version"], version)
                second = self.preview(self.workbook(identity="tr-other"), data_source="数据飞轮", data_date="2024-03-04")
                other = self.publish(second)
                manager.submit(other["release_id"])
                combined = manager.query()
                self.assertEqual(combined["overview"]["total_steps"], 4)
                self.assertEqual(combined["overview"]["total_trajectories"], 2)
                self.assertTrue(combined["overview"]["show_manual_refine_steps"])
                self.assertEqual(combined["overview"]["manual_refine_steps"], 0)
                self.assertEqual(set(combined["filters"]["sources"]), {"人工采集", "数据飞轮"})
                download = client.get(f"/api/dataset-releases/{release['release_id']}/excels/0")
                self.assertEqual(download.content, self.book)
        finally:
            manager.close()


if __name__ == "__main__":
    unittest.main()
