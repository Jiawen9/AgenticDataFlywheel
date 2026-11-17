"""Real XLSX round trips: no collector, images, model or upload is required."""
from copy import deepcopy
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from openpyxl import Workbook, load_workbook

from backend.data_publishing.paired_training import write_paired_workbook, image_prefix
from backend.tests import test_corrected_selection_export
from backend.trajectory_correction import service, draft_store


class ExcelOnlyTrainingTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def workbook(self, rows):
        path = self.root / "数据.xlsx"
        book = Workbook()
        headers = list(dict.fromkeys(k for row in rows for k in row))
        book.active.append(headers)
        for row in rows:
            book.active.append([row.get(k) for k in headers])
        book.save(path)
        book.close()
        return path

    def row(self, trajectory="轨迹一", step=1, **changes):
        return {"task": "搜索音乐", "task_id": "task-A", "trajectory_id": trajectory, "step": step,
                "action": '{"action":"wait"}', "sop": f"{trajectory}步骤{step}", "thought": "",
                "training_image_relative": f"batch/任务/{trajectory}/step{step}.jpg", "history": "过期历史不应使用",
                "is_key_step": None, "is_refined": False, **changes}

    def convert(self, workbook, prefix="s3://bucket/图片/", output="train.jsonl"):
        result = write_paired_workbook(workbook, self.root / output, self.root / (output + ".report.json"), prefix, source_id="export-fixed")
        samples = [json.loads(line) for line in (self.root / output).read_text(encoding="utf-8").splitlines()] if result.get("sha256") else []
        return result, samples

    def test_two_trajectories_history_identity_and_template_without_images(self):
        rows = [self.row(t, s) for t in ("轨迹一", "轨迹二") for s in (1, 2)]
        path = self.workbook(rows)
        before = path.read_bytes()
        result, samples = self.convert(path)
        self.assertEqual((result["included_count"], result["excluded_count"]), (4, 0))
        self.assertIn("Empty", samples[2]["messages"][1]["content"])
        self.assertNotIn("轨迹一", samples[3]["messages"][1]["content"])
        self.assertIn("轨迹二步骤1", samples[3]["messages"][1]["content"])
        self.assertNotIn("步骤2", samples[3]["messages"][1]["content"])
        self.assertNotIn("过期历史", json.dumps(samples, ensure_ascii=False))
        self.assertIn("<thought>\n\n</thought>\n<tool_call>", samples[0]["solution"])
        self.assertIn("metaID:2.0", samples[2]["solution"])
        self.assertEqual(samples[0]["images"], ["s3://bucket/图片/batch/任务/轨迹一/step1.jpg"])
        _, moved = self.convert(path, prefix="s3://bucket/other/", output="other.jsonl")
        self.assertEqual(samples[0]["solution"], moved[0]["solution"])
        self.assertEqual(before, path.read_bytes())

    def test_invalid_action_excludes_only_affected_trajectory_history(self):
        path = self.workbook([self.row(step=1, action="broken"), self.row(step=2), self.row("other")])
        result, samples = self.convert(path)
        self.assertEqual((result["included_count"], result["excluded_count"]), (1, 2))
        self.assertIn("other", samples[0]["images"][0])

    def test_empty_result_missing_prefix_and_unsafe_paths(self):
        path = self.workbook([self.row(training_image_relative="../escape.jpg")])
        result, _ = self.convert(path)
        self.assertEqual(result["status"], "empty")
        pending, samples = self.convert(path, prefix="", output="pending.jsonl")
        self.assertEqual(pending["status"], "needs_configuration")
        self.assertEqual(samples, [])
        for prefix in ("https://bucket/path", "s3://token@bucket/path", "s3://bucket/a/../b", "s3://bucket/a?token=x"):
            with self.assertRaises(ValueError):
                image_prefix(prefix)

    def test_pixels_flags_and_action_fallback_are_preserved(self):
        path = self.workbook([self.row(action='{"action":"click","coordinate":[1080,238]}',
            actions_box="", is_key_step=True, is_refined=True)])
        _, samples = self.convert(path)
        self.assertIn('"coordinate":[1080,238]', samples[0]["solution"])
        self.assertIs(samples[0]["is_key_step"], True)
        self.assertIs(samples[0]["is_refined"], True)

    def test_twenty_steps_one_jsonl_for_two_selected_trajectories(self):
        path = self.workbook([self.row(t, s) for t in ("selected-2", "selected-8") for s in range(1, 11)])
        result, samples = self.convert(path)
        self.assertEqual(result["included_count"], 20)
        self.assertEqual(len(samples), 20)
        self.assertIn("Empty", samples[10]["messages"][1]["content"])


class PairedServiceTests(unittest.TestCase):
    def setUp(self):
        self.f = test_corrected_selection_export.CompleteSelectionTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        root = self.f.fixture.root
        for p in (patch.object(draft_store, "CORRECTION_SESSIONS_DIR", root / "sessions"),
                  patch.object(service, "CORRECTION_EXPORTS_DIR", root / "exports")):
            p.start()
            self.addCleanup(p.stop)

    def test_export_download_cache_prefix_and_draft_immutability(self):
        f, session = self.f.fixture, self.f.session
        before = deepcopy(session)
        result = service.export_selected_session(session["session_id"], session["storage_revision"], cot_policy="skip", training_image_prefix="s3://bucket/a/")
        self.assertEqual(result["training"]["status"], "ready")
        self.assertEqual(result["training"]["included_count"], 12)
        train = service.download_export(session["session_id"], result["training"]["filename"])
        saved = f.records.get("correction_sessions", session["session_id"])
        for field in ("snapshot_payload", "row_edits", "cot", "group_exports", "workbook_payload"):
            self.assertEqual(saved[field], before[field])
        again = service.export_selected_session(session["session_id"], saved["storage_revision"], cot_policy="skip", training_image_prefix="s3://bucket/a")
        self.assertEqual(again["export_id"], result["export_id"])
        changed = service.export_selected_session(session["session_id"], saved["storage_revision"], cot_policy="skip", training_image_prefix="s3://bucket/b")
        self.assertNotEqual(changed["export_id"], result["export_id"])
        self.assertEqual(service.download_export(session["session_id"], result["training"]["filename"]), train)
        train.write_text("tampered", encoding="utf-8")
        with self.assertRaises(ValueError):
            service.download_export(session["session_id"], result["training"]["filename"])

    def test_projection_columns_current_action_sop_history_and_no_metadata_dependency(self):
        s, f = self.f.session, self.f.fixture
        s["row_edits"]["2"] = {"actions": '{"action":"click","coordinate":[1080,238]}', "original_actions": '{"action":"wait"}'}
        s["row_edits"]["3"] = {"deleted": True}
        s = f.records.put("correction_sessions", s["session_id"], s)
        result = service.export_selected_session(s["session_id"], s["storage_revision"], cot_policy="skip", training_image_prefix="s3://bucket/a")
        path = service.download_export(s["session_id"], result["filename"])
        book = load_workbook(path, read_only=True)
        try:
            rows = book.active.iter_rows(values_only=True)
            headers = next(rows)
            values = [dict(zip(headers, r)) for r in rows]
        finally:
            book.close()
        self.assertTrue({"task", "task_id", "trajectory_id", "step", "history", "training_image_relative"}.issubset(headers))
        self.assertFalse(values[0]["actions_box"])
        self.assertFalse(values[0]["sop"])
        self.assertIn("1080", values[1]["history"])
        out, report = f.root / "standalone.jsonl", f.root / "report.json"
        converted = write_paired_workbook(path, out, report, "s3://bucket/standalone", source_id="standalone")
        self.assertEqual(converted["included_count"], 11)

    def test_real_request_exports_and_downloads_registered_training_files(self):
        from backend.trajectory_correction.router import router
        app = FastAPI()
        app.include_router(router)
        client = TestClient(app)
        s = self.f.session
        response = client.post(f"/api/correction/sessions/{s['session_id']}/dataset-export", json={
            "expected_revision": s["storage_revision"], "export_profile": "corrected_selection_v1",
            "cot_policy": "skip", "training_image_prefix": "s3://bucket/真实请求"})
        self.assertEqual(response.status_code, 200, response.text)
        descriptor = response.json()["training"]
        downloaded = client.get(descriptor["download_url"])
        self.assertEqual(downloaded.status_code, 200)
        self.assertEqual(downloaded.headers["content-type"], "application/x-ndjson")
        self.assertEqual(len(downloaded.text.splitlines()), 12)
        self.assertEqual(client.get(descriptor["report_download_url"]).json()["included_count"], 12)

    def test_nested_screenshots_keep_trajectory_owner_and_child_path(self):
        from backend.trajectory_correction.selected_export import _trajectory_directory, _training_image
        raw = self.f.fixture.raw
        owner = raw / "runs/r01/p01/i01"
        owner.mkdir(parents=True)
        (owner / "_trajectory_for_evaluate.json").write_text("{}")
        group = {"task_id": "task-A", "rows": [{"image": "runs/r01/p01/i01/screens/step1.jpg"}]}
        directory = _trajectory_directory(group, raw)
        image, cloud_dir = _training_image(group["rows"][0], group, "batch-A", raw, directory)
        self.assertEqual(directory.as_posix(), "runs/r01/p01/i01")
        self.assertEqual(image, cloud_dir + "/screens/step1.jpg")

    def test_actual_export_release_and_upload_use_identical_image_paths(self):
        from backend.data_publishing.service import DatasetReleaseRegistry
        from backend.data_publishing.upload_jobs import DatasetUploadJobManager
        from backend.tests.test_directory_upload_jobs import DirectoryAdapter
        from backend.tests.test_data_publishing import ImmediateExecutor
        f, s = self.f.fixture, self.f.session
        for group in s["snapshot_payload"]["groups"]:
            for row in group["rows"]:
                image = f.raw / row["image"]
                image.parent.mkdir(parents=True, exist_ok=True)
                image.write_bytes(b"original evidence; converter does not decode images")
                (image.parent / "nested").mkdir(exist_ok=True)
                (image.parent / "nested" / "keep.txt").write_text("complete directory")
        exported = service.export_selected_session(s["session_id"], s["storage_revision"], cot_policy="skip",
            training_image_prefix="s3://fixture-bucket/images/")
        training_path = service.download_export(s["session_id"], exported["training"]["filename"])
        original = training_path.read_bytes()
        registry = DatasetReleaseRegistry(data_root=f.root, project_root=f.root, trajectory_root=f.raw,
            releases_file=f.root / "releases.json", correction_exports_dir=f.root / "exports")
        release = registry.create("真实Excel转换与上传", [s["session_id"]])
        self.assertEqual(registry.training_file(release["release_id"], 0, "jsonl")[0].read_bytes(), original)
        adapter = DirectoryAdapter()
        manager = DatasetUploadJobManager(registry, jobs_dir=f.root / "upload-jobs", executor=ImmediateExecutor(), internal_adapter=adapter)
        self.addCleanup(manager.shutdown)
        job = manager.submit(release["release_id"], target="internal", include_trajectories=True,
            training_jsonl=True, destination_prefix="tables", training_destination_prefix="training/first")
        job = manager.get(job["job_id"])
        self.assertEqual(job["status"], "succeeded", job.get("error"))
        samples = [json.loads(line) for line in original.decode("utf-8").splitlines()]
        for sample in samples:
            uri = sample["images"][0]
            candidates = [call for call in adapter.calls if uri.startswith(f"s3://{call.bucket}/{call.bucket_path}")]
            self.assertEqual(len(candidates), 1)
            call = candidates[0]
            suffix = uri.removeprefix(f"s3://{call.bucket}/{call.bucket_path}")
            self.assertTrue((call.source_dir / suffix).is_file(), (uri, call.source_dir))
            self.assertTrue((call.source_dir / "nested/keep.txt").is_file())
        call_count = len(adapter.calls)
        separate = manager.submit(release["release_id"], target="internal", training_only=True,
            training_destination_prefix="training/separate")
        separate = manager.get(separate["job_id"])
        self.assertEqual(separate["status"], "succeeded", separate.get("error"))
        self.assertEqual(len(adapter.calls), call_count + 1)
        self.assertEqual((adapter.calls[-1].source_dir / "train.jsonl").read_bytes(), original)
        self.assertTrue(adapter.calls[-1].bucket_path.startswith("training/separate/"))

    def test_pipeline_retry_preserves_original_image_prefix(self):
        from backend import pipeline_release
        f, p, s = self.f.fixture, self.f.pipeline, self.f.session
        p.update(export_profile="corrected_selection_v1", cot_policy="skip", session_id=s["session_id"],
                 confirmation=pipeline_release.confirm_manual(s["session_id"], root=f.root))
        with patch.dict(os.environ, {"ADF_YUNDAO_BUCKET": "bucket", "ADF_YUNDAO_PREFIX": "original"}), \
             patch.object(pipeline_release, "freeze_release_provenance", side_effect=OSError("publication interrupted")):
            with self.assertRaises(OSError):
                f.publish(p)
        with patch.dict(os.environ, {"ADF_YUNDAO_BUCKET": "bucket", "ADF_YUNDAO_PREFIX": "changed"}):
            release = f.publish(p)
        self.assertEqual(release["training_files"][0]["image_prefix"], "s3://bucket/original/")

