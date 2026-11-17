"""Frozen release -> real converter -> simulated cloud adapter. No live service."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from openpyxl import Workbook, load_workbook
from PIL import Image

from backend.data_publishing.training_jsonl import effective_tool_call, write_training_bundle, parse_action
from backend.data_publishing.upload_sources import resolve_upload_sources
from backend.data_publishing.upload_jobs import DatasetUploadJobManager
from backend.tests import test_upload_sources as source_fixtures
from backend.tests.test_directory_upload_jobs import DirectoryAdapter
from backend.tests.test_data_publishing import ImmediateExecutor


class TrainingUploadTests(unittest.TestCase):
    setUp = source_fixtures.UploadSourcesTests.setUp
    add_batch = source_fixtures.UploadSourcesTests.add_batch
    freeze = source_fixtures.UploadSourcesTests.freeze

    def prepare(self, mutate=None):
        raw, original, path = self.add_batch(selected=(2, 7))
        all_rows = []
        groups = []
        for ordinal, repeat in enumerate((2, 7)):
            base = original[repeat - 1]
            rows = []
            for step in (1, 2, 3):
                image = self.directories[repeat - 1] / f"step{step:03d}_vla_input.jpg"
                Image.new("RGB", (120, 264)).save(image)
                action = {"action": "click", "coordinate": [1080, 238]} if step < 3 else {"action": "wait"}
                row = {**base, "step": step, "image": image.relative_to(raw).as_posix(), "action": json.dumps(action),
                       "actions": json.dumps(action), "original_action": json.dumps(action), "task": "打开中文 App", "sop": "" if step != 2 else "修正步骤描述",
                       "summary": f"操作 {step}", "thought": "不应出现在 OPD", "is_key_step": True if step == 1 else None,
                       "is_refined": step == 2, "excel_row": len(all_rows) + 2, "step_key": f"{repeat}-{step}", "actions_box": "click(bbox=<bbox>[1,2,30,40]</bbox>)" if step == 1 else ""}
                rows.append(row); all_rows.append(row)
            groups.append({"task_id": "task-01", "meta_task": base["trajectory_id"], "task": "打开中文 App", "rows": rows})
        if mutate:
            mutate(all_rows)
        columns = ["image", "action", "summary", "sop", "thought", "actions_box", "is_key_step", "is_refined"]
        book = Workbook(); book.active.append(columns)
        for row in all_rows:
            book.active.append([row.get(key) for key in columns])
        book.save(path); book.close()
        old = self.release["source_refs"][0]["artifact"]
        artifact = self.store.publish("batch_001", "07_cot", {"batch_id": "batch_001", "export_profile": "corrected_selection_v1", "cot_policy": "skip", "groups": groups}, workbooks={"full_dataset.xlsx": path}, source_refs=old["source_refs"])
        self.release["source_refs"][0]["artifact"] = artifact
        self.release["excel_paths"][0]["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        self.freeze()
        self.registry._records.put("dataset_releases", self.release_id, self.release)
        self.adapter = DirectoryAdapter()
        self.manager = DatasetUploadJobManager(self.registry, jobs_dir=self.root / "jobs", executor=ImmediateExecutor(), internal_adapter=self.adapter)
        self.addCleanup(self.manager.shutdown)
        return all_rows

    def submit(self):
        response = self.manager.submit(self.release_id, target="internal", include_trajectories=True, destination_prefix="数据/发布", training_jsonl=True)
        return self.manager.get(response["job_id"])

    def test_full_chain_six_samples_three_source_dirs_then_training_no_source_mutation(self):
        self.prepare()
        before = {str(p): p.read_bytes() for p in self.root.rglob("*.jpg")}
        job = self.submit()
        self.assertEqual(job["status"], "succeeded", job.get("error"))
        self.assertEqual([item["kind"] for item in job["item_results"]], ["excel", "trajectory", "trajectory", "training"])
        self.assertEqual(len(self.adapter.calls), 4)
        samples = [json.loads(line) for line in self.manager.training_file(job["job_id"], "jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(samples), 6)
        self.assertEqual(set(samples[0]), {"messages", "solution", "images", "is_key_step", "is_refined"})
        self.assertIn("<thought>\n\n</thought>", samples[0]["solution"])
        self.assertNotIn("不应出现在 OPD", samples[0]["solution"])
        self.assertIn('"coordinate":[1080,238]', samples[1]["solution"])
        self.assertIn('"action":"wait"', samples[2]["solution"])
        self.assertIn("修正步骤描述", samples[2]["messages"][1]["content"])
        self.assertNotIn("修正步骤描述", samples[1]["messages"][1]["content"])
        self.assertTrue(samples[0]["images"][0].startswith("s3://fixture-bucket/数据/发布/batch_001/用例01/"))
        self.assertTrue(samples[0]["is_key_step"])
        self.assertIsNone(samples[1]["is_key_step"])
        self.assertTrue(samples[1]["is_refined"])
        self.assertEqual(before, {str(p): p.read_bytes() for p in self.root.rglob("*.jpg")})
        self.assertEqual(self.submit()["job_id"], job["job_id"])

    def test_training_failure_keeps_original_receipts_retry_reuses_same_bytes(self):
        self.prepare(); self.adapter.fail_at = 4
        failed = self.submit()
        self.assertEqual(failed["status"], "failed")
        self.assertTrue(all(item["status"] == "succeeded" for item in failed["item_results"][:3]))
        before = self.manager.training_file(failed["job_id"], "jsonl").read_bytes()
        self.adapter.fail_at = None
        with patch("backend.data_publishing.training_jsonl.write_training_bundle", side_effect=AssertionError("must reuse")):
            done = self.submit()
        self.assertEqual(done["status"], "succeeded", done.get("error"))
        self.assertEqual(len(self.adapter.calls), 5)
        self.assertEqual(self.manager.training_file(done["job_id"], "jsonl").read_bytes(), before)

    def test_missing_image_excludes_only_one_sample_keeps_history_and_original_upload(self):
        self.prepare()
        (self.directories[1] / "step002_vla_input.jpg").unlink()
        job = self.submit()
        self.assertEqual(job["status"], "succeeded", job.get("error"))
        self.assertEqual(job["training"]["included_count"], 5)
        self.assertEqual(job["training"]["excluded_count"], 1)
        report = json.loads(self.manager.training_file(job["job_id"], "report").read_text(encoding="utf-8"))
        self.assertEqual(report["exclusions"][0]["step"], 2)
        samples = [json.loads(line) for line in self.manager.training_file(job["job_id"], "jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertIn("修正步骤描述", samples[1]["messages"][1]["content"])

    def test_generation_failure_retry_does_not_reupload_sources(self):
        self.prepare()
        with patch("backend.data_publishing.training_jsonl.write_training_bundle", side_effect=OSError("disk temporary")):
            failed = self.submit()
        self.assertEqual(failed["completed_items"], 3)
        done = self.submit()
        self.assertEqual(done["status"], "succeeded", done.get("error"))
        self.assertEqual(len(self.adapter.calls), 4)

    def test_missing_task_text_excludes_sample_without_short_id_or_losing_history(self):
        self.prepare(lambda rows: rows[1].update(task="  "))
        job = self.submit()
        self.assertEqual(job["status"], "succeeded", job.get("error"))
        self.assertEqual(job["training"]["included_count"], 5)
        report = json.loads(self.manager.training_file(job["job_id"], "report").read_text(encoding="utf-8"))
        self.assertEqual([item["code"] for item in report["exclusions"]], ["task_text_unavailable"])
        self.assertEqual(report["exclusions"][0]["step"], 2)
        samples = [json.loads(line) for line in self.manager.training_file(job["job_id"], "jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertIn("修正步骤描述", samples[1]["messages"][1]["content"])
        self.assertTrue(all("## Task\n打开中文 App\n" in sample["messages"][1]["content"] for sample in samples))

    def test_bad_action_excludes_affected_history_only_and_keeps_other_trajectory(self):
        self.prepare(lambda rows: rows[1].update(action="two actions", actions="two actions"))
        job = self.submit()
        self.assertEqual(job["status"], "succeeded", job.get("error"))
        self.assertEqual(job["training"]["included_count"], 4)
        self.assertEqual(job["training"]["excluded_count"], 2)
        report = json.loads(self.manager.training_file(job["job_id"], "report").read_text(encoding="utf-8"))
        self.assertEqual([item["code"] for item in report["exclusions"]], ["invalid_action", "history_unavailable"])
        self.assertEqual(len(self.adapter.calls), 4)

    def test_nested_image_keeps_relative_suffix_under_frozen_upload_root(self):
        def nested(rows):
            old = Path(rows[0]["image"])
            nested_path = old.parent / "screens" / old.name
            rows[0]["image"] = nested_path.as_posix()
            local = self.directories[1] / "screens" / old.name
            local.parent.mkdir()
            Image.new("RGB", (120, 264)).save(local)
        self.prepare(nested)
        sources = resolve_upload_sources(self.registry, self.release)
        # The complete trajectory already owns and uploads this child folder.
        sources = [item for item in sources if Path(item["source_dir"]).name != "screens"]
        for item in sources:
            item["bucket_path"] = "目标/" + item["destination_relative"] + "/"
        result = write_training_bundle(self.registry, self.release, sources, {"bucket": "fixture", "prefix": "目标"}, "nested" * 12)
        self.assertEqual(result["included_count"], 6)
        samples = [json.loads(line) for line in self.registry.resolve_project_path(result["file_path"]).read_text(encoding="utf-8").splitlines()]
        self.assertTrue(samples[0]["images"][0].endswith("/screens/step001_vla_input.jpg"))

    def test_download_verifies_owned_files_and_report_independent_of_training_transport(self):
        self.prepare(); self.adapter.fail_at = 4
        job = self.submit()
        path = self.manager.training_file(job["job_id"], "report")
        self.assertTrue(path.is_file())
        path.write_text("changed", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "摘要"):
            self.manager.training_file(job["job_id"], "report")
        with self.assertRaises(FileNotFoundError):
            self.manager.training_file(job["job_id"], "other")

    def test_same_frozen_bundle_has_stable_uuid_bytes_and_no_hardcoded_mount(self):
        self.prepare()
        items = resolve_upload_sources(self.registry, self.release)
        for item in items:
            item["bucket_path"] = "目标/" + item["destination_relative"] + "/"
        target = {"bucket": "fixture-bucket", "prefix": "目标"}
        a = write_training_bundle(self.registry, self.release, items, target, "a" * 64)
        first = self.registry.resolve_project_path(a["file_path"]).read_bytes()
        b = write_training_bundle(self.registry, self.release, items, target, "a" * 64)
        self.assertEqual(first, self.registry.resolve_project_path(b["file_path"]).read_bytes())
        self.assertNotIn(b"/cache/", first)

    def test_multi_batch_one_jsonl_uses_each_actual_image_destination(self):
        self.prepare()
        _, rows, path = self.add_batch(batch="batch_002", selected=(2,))
        Image.new("RGB", (120, 264)).save(self.directories[11] / "step001_vla_input.jpg")
        book = load_workbook(path)
        headers = [cell.value for cell in book.active[1]]
        book.active.cell(2, headers.index("action") + 1).value = '{"action":"wait"}'
        book.save(path); book.close()
        previous = self.release["source_refs"][1]["artifact"]
        rows[1]["task"] = "打开第二批 App"
        artifact = self.store.publish("batch_002", "07_cot", {"groups": [{"task_id": "task-01", "task": "打开第二批 App", "rows": [rows[1]]}]}, workbooks={"full_dataset.xlsx": path}, source_refs=previous["source_refs"])
        self.release["source_refs"][1]["artifact"] = artifact
        self.release["excel_paths"][1]["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        self.freeze(); self.registry._records.put("dataset_releases", self.release_id, self.release)
        job = self.submit()
        self.assertEqual(job["status"], "succeeded", job.get("error"))
        self.assertEqual(job["training"]["included_count"], 7)
        samples = [json.loads(line) for line in self.manager.training_file(job["job_id"], "jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertIn("/batch_002/", samples[-1]["images"][0])
        self.assertIn(f"/{self.release_id}/训练数据/", job["training"]["s3_uri"])
        self.assertEqual(sum(item["kind"] == "training" for item in job["item_results"]), 1)

    def test_http_training_option_and_download_contract(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from backend.data_publishing.router import router
        self.prepare()
        app = FastAPI(); app.include_router(router)
        with patch("backend.data_publishing.router.registry", self.registry), patch("backend.data_publishing.router.upload_manager", self.manager), TestClient(app) as client:
            response = client.post(f"/api/dataset-releases/{self.release_id}/upload", json={"target": "internal", "include_trajectories": True, "training_jsonl": True})
            self.assertEqual(response.status_code, 202, response.text)
            job_id = response.json()["job"]["job_id"]
            self.assertTrue(client.get(f"/api/dataset-upload-jobs/{job_id}").json()["job"]["training_jsonl"])
            jsonl = client.get(f"/api/dataset-upload-jobs/{job_id}/training/jsonl")
            self.assertEqual(jsonl.status_code, 200)
            self.assertEqual(len(jsonl.text.splitlines()), 6)
            self.assertEqual(client.get(f"/api/dataset-upload-jobs/{job_id}?download=jsonl").content, jsonl.content)
            report = client.get(f"/api/dataset-upload-jobs/{job_id}/training/report")
            self.assertEqual(report.json()["excluded_count"], 0)
            invalid = client.post(f"/api/dataset-releases/{self.release_id}/upload", json={"target": "internal", "training_jsonl": True})
            self.assertEqual(invalid.status_code, 422)


class TrainingActionTests(unittest.TestCase):
    def test_missing_stale_and_out_of_range_box_fallback_without_coordinate_conversion(self):
        action = {"action": "click", "coordinate": [1080, 238]}
        original = {"action": "click", "coordinate": [3, 4]}
        row = {"image": "a.jpg", "actions_box": "click(bbox=<bbox>[1,2,30,40]</bbox>)", "original_action": original, "action_edited": True}
        value, mode = effective_tool_call(row, action, (1200, 2640))
        self.assertEqual(json.loads(value), action); self.assertEqual(mode, "bbox_missing_or_stale")
        row["bbox_action_hash"] = hashlib.sha256(json.dumps(action, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        row["bbox_image"] = "a.jpg"
        self.assertEqual(effective_tool_call(row, action, (1200, 2640))[1], "bbox")
        row.update(image="中文目录/a.jpg", bbox_image="中文目录\\a.jpg")
        self.assertEqual(effective_tool_call(row, action, (1200, 2640))[1], "bbox")
        row["actions_box"] = "click(bbox=<bbox>[1,2,3000,40]</bbox>)"
        self.assertEqual(effective_tool_call(row, action, (1200, 2640))[1], "bbox_missing_or_stale")

    def test_history_description_uses_only_current_sop_or_bound_summary(self):
        from backend.data_publishing.training_jsonl import _description
        original = {"action": "click", "coordinate": [10, 20]}
        current = {"action": "click", "coordinate": [1080, 238]}
        row = {"original_action": original, "sop": "点击旧位置", "sop_source": "original", "summary": "旧摘要"}
        self.assertEqual(_description(row, original), "点击旧位置")
        self.assertEqual(_description(row, current), '执行动作：{"action":"click","coordinate":[1080,238]}')
        row.update(sop="人工指定新动作", sop_source="manual")
        self.assertEqual(_description(row, current), "人工指定新动作")
        row.update(sop="", summary="已核验的当前摘要", summary_source="generated")
        self.assertEqual(_description(row, current), "已核验的当前摘要")
        row.update(sop="旧SOP", sop_source="original")
        self.assertEqual(_description(row, current), "已核验的当前摘要")
        row.update(summary_source="original")
        self.assertTrue(_description(row, current).startswith("执行动作："))

    def test_bad_actions_rejected_but_nonspatial_has_no_box_requirement(self):
        for invalid in ["not json", [{"action": "wait"}, {"action": "wait"}], {"action": "click", "coordinate": [float("nan"), 4]}]:
            with self.assertRaises(ValueError):
                parse_action(invalid)
        action = parse_action({"action": "type", "text": "中文"})
        self.assertEqual(json.loads(effective_tool_call({}, action, (2, 2))[0]), action)


class CorrectionTrainingChainTests(unittest.TestCase):
    def test_real_ten_choose_two_projection_release_markers_and_cloud_training(self):
        from backend.tests import test_pipeline_release as pipeline_fixtures
        from backend import pipeline_release
        from backend.batch_results import digest
        from backend.data_store.release_provenance import FrozenReleaseSources
        fixture = pipeline_fixtures.PipelineReleaseTests()
        fixture.setUp(); self.addCleanup(fixture.doCleanups)
        fixture.seed({"A": {f"a-{i}": 5 for i in range(10)}})
        tree = fixture.store.read_payload(fixture.store.get(fixture.batch, "04_tree"))
        tree["trees"]["A"] = {"id": 0, "children": [{"id": i + 1, "children": [], "occurrences": [{"trajectory": f"a-{i}", "step": 1, "image": f"A/a-{i}/step001.jpg"}]} for i in range(10)]}
        tree["tree_hashes"] = {"A": digest(tree["trees"]["A"])}
        old_tree = fixture.store.get(fixture.batch, "04_tree")
        tree_ref = fixture.store.publish(fixture.batch, "04_tree", tree, metadata={"run_id": fixture.batch}, source_refs=old_tree["source_refs"])
        quality = fixture.store.read_payload(fixture.store.get(fixture.batch, "05_quality"))
        quality["source_tree_hashes"] = tree["tree_hashes"]
        for task in quality["tasks"]:
            task.update(scoring_version="checklist_fusion_v1", fusion_complete=True, node_scores={str(i + 1): {"q": i / 10, "scored_count": 1, "total_count": 1} for i in range(10)})
            for i in range(10):
                task["evaluations"][f"a-{i}"]["fusion"] = {"valid": True, "score": i / 10}
        fixture.store.publish(fixture.batch, "05_quality", quality, source_refs=[tree_ref])
        for i in range(10):
            directory = fixture.raw / "A" / f"a-{i}"; directory.mkdir(parents=True)
            for step in range(1, 4):
                Image.new("RGB", (120, 264)).save(directory / f"step{step:03}.jpg")
        pipeline = {"pipeline_id": "pipe-training-chain", "name": "训练链路", "batch_id": fixture.batch, "mode": "manual",
                    "selection": pipeline_release.build_selection(fixture.batch, ["A"], "manual", 4, root=fixture.root)}
        pipeline.update(export_profile="corrected_selection_v1", cot_policy="skip",
                        training_image_prefix="s3://fixture-bucket/frozen-images/")
        manual = pipeline_release.prepare_manual(fixture.batch, pipeline["selection"], root=fixture.root)
        session = fixture.records.get("correction_sessions", manual["session_id"])
        for group in session["snapshot_payload"]["groups"]:
            session["group_exports"][group["group_id"]] = group["meta_task"] in {"a-1", "a-7"}
            if group["meta_task"] == "a-1":
                number = str(group["rows"][1]["excel_row"])
                session["row_edits"][number] = {"sop": "人工修改本步说明"}
        fixture.records.put("correction_sessions", session["session_id"], session)
        pipeline.update(session_id=session["session_id"], confirmation=pipeline_release.confirm_manual(session["session_id"], root=fixture.root))
        release = fixture.publish(pipeline)
        frozen = FrozenReleaseSources(fixture.root, release)
        projection = json.loads(frozen.resolve_file(release["source_refs"][0]["artifact"], "result.json").read_text(encoding="utf-8"))
        self.assertEqual({group["meta_task"] for group in projection["groups"]}, {"a-1", "a-7"})
        for group in projection["groups"]:
            self.assertTrue(group["rows"][0]["is_key_step"])
            self.assertEqual(group["rows"][0]["source_tree_version"], tree_ref["version"])
            self.assertEqual(group["rows"][0]["source_score_coverage"]["total_count"], 1)
        adapter = DirectoryAdapter()
        manager = DatasetUploadJobManager(fixture.registry, jobs_dir=fixture.root / "upload-jobs", executor=ImmediateExecutor(), internal_adapter=adapter)
        self.addCleanup(manager.shutdown)
        job = manager.submit(release["release_id"], target="internal", include_trajectories=True, training_jsonl=True)
        job = manager.get(job["job_id"])
        self.assertEqual(job["status"], "succeeded", job.get("error"))
        self.assertEqual(job["source_summary"]["trajectory_count"], 2)
        self.assertEqual(len(adapter.calls), 4)
        samples = [json.loads(line) for line in manager.training_file(job["job_id"], "jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(samples), 6)
        self.assertEqual(sum(row["is_key_step"] is True for row in samples), 2)
        self.assertEqual(sum(row["is_refined"] for row in samples), 1)
        self.assertTrue(all("<thought>\n\n</thought>" in row["solution"] for row in samples))


if __name__ == "__main__":
    unittest.main()
