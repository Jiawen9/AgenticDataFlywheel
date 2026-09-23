from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.batch_lifecycle import BatchPublishedError
from backend.collection_runs import CollectionRunError, CollectionRunStore
from backend.data_store import ArtifactStore, RecordStore
from backend.rollout_imports import RolloutImportError, RolloutImportStore
from backend.rollout_import_router import router, configure_rollout_import_store
from backend.task_generation.collection_batches import workbook_digest
from backend.tests.test_collection_runs import raw_trajectory


def fixture(source, case="TASK-A", trajectory="same-name", goal="Open settings", **metadata):
    raw_trajectory({"output_dir": str(source)}, case_id=case, name=trajectory)
    directory = source / case / trajectory
    request = {"messages": [{"role": "user", "content": "**原始目标**: " + goal + "\n\nnext"}], **metadata}
    (directory / "turn001_orch_model_request.json").write_text(json.dumps(request, ensure_ascii=False), encoding="utf-8")
    return directory


class RolloutImportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "data"
        self.source = self.root / "raw" / "rollout_trajectories"
        self.directory = fixture(self.source)
        self.store = RolloutImportStore(self.root)
        self.data = {"source_path": str(self.source), "batch_id": "rollout-test", "name": "测试导入",
                     "app": "Demo", "scene": "设置", "capability": "操作"}

    def preview(self, **values):
        return self.store.preview({**self.data, **values})

    def hashes(self):
        return {str(path.relative_to(self.source)): workbook_digest(path) for path in self.source.rglob("*") if path.is_file()}

    def test_import_registers_frozen_raw_and_only_collection_artifact(self):
        before = self.hashes()
        value = self.preview()
        self.assertTrue(value["valid"], value["errors"])
        self.assertEqual((value["task_count"], value["trajectory_count"], value["step_count"]), (1, 1, 1))
        result = self.store.commit(value["import_id"], "request-one")
        self.assertFalse(result["reused"])
        batch = self.store.get_batch(result["batch_id"])
        self.assertEqual(batch["kind"], "rollout_import")
        self.assertIsNone(batch["source_job_id"])
        self.assertIsNone(batch["filename"])
        self.assertEqual(batch["snapshot"]["tasks"][0]["source_row_id"], "TASK-A")
        self.assertEqual([a["stage"] for a in self.store.artifacts.list(result["batch_id"])], ["00_collection"])
        run = self.store.runs.get(result["collection_run_id"])
        self.assertEqual(run["status"], "completed")
        self.assertEqual(run["source_kind"], "rollout_import")
        self.assertEqual(run["trajectories"][0]["source_kind"], "rollout_import")
        self.assertNotIn("transfer_status", run)
        self.assertFalse(self.store.records.list("task_generation_jobs"))
        ready = CollectionRunStore(self.root).ready_input(result["batch_id"])
        self.assertEqual(len(ready["trajectories"]), 1)
        self.assertEqual(before, self.hashes())
        self.assertNotEqual(Path(run["output_dir"]), self.source)
        self.assertEqual(before, {str(p.relative_to(Path(run["output_dir"]))): workbook_digest(p)
                                  for p in Path(run["output_dir"]).rglob("*") if p.is_file()})
        with self.assertRaisesRegex(RolloutImportError, "不能下发"):
            self.store.get_batch(result["batch_id"], require_workbook=True)

    def test_missing_goal_remains_editable_and_override_is_frozen(self):
        (self.directory / "turn001_orch_model_request.json").unlink()
        invalid = self.preview(app=None, scene=None, capability=None)
        self.assertFalse(invalid["valid"])
        self.assertEqual(invalid["tasks"][0]["collection_case_id"], "TASK-A")
        self.assertEqual(invalid["tasks"][0]["task"], "")
        self.assertTrue(invalid["warnings"])
        with self.assertRaisesRegex(RolloutImportError, "校验未通过"):
            self.store.commit(invalid["import_id"], "invalid")
        valid = self.preview(task_overrides=[{"collection_case_id": "TASK-A", "task": "打开设置"}])
        self.assertTrue(valid["valid"])
        self.store.commit(valid["import_id"], "valid")
        self.assertEqual(self.store.get_batch("rollout-test")["snapshot"]["tasks"][0]["task"], "打开设置")

    def test_goal_and_classification_conflicts_do_not_guess(self):
        fixture(self.source, trajectory="another", goal="A different goal", app="One")
        invalid = self.preview()
        self.assertTrue(any("目标不一致" in error for error in invalid["errors"]))
        valid = self.preview(task_overrides=[{"collection_case_id": "TASK-A", "task": "Reviewed goal"}])
        self.assertTrue(valid["valid"])
        self.assertEqual(valid["tasks"][0]["app"], "One")
        self.assertEqual(valid["tasks"][0]["scene"], "设置")
        fixture(self.source, trajectory="third", app="Two")
        conflict = self.preview(task_overrides=[{"collection_case_id": "TASK-A", "task": "Reviewed goal"}])
        self.assertTrue(any("分类冲突" in error for error in conflict["errors"]))

    def test_same_named_trajectories_have_stable_task_identity(self):
        fixture(self.source, case="TASK-B", trajectory="same-name", goal="Second goal")
        first, second = self.preview(), self.preview()
        self.assertEqual([task["task_id"] for task in first["tasks"]], [task["task_id"] for task in second["tasks"]])
        self.assertEqual(len({task["task_id"] for task in first["tasks"]}), 2)
        result = self.store.commit(first["import_id"], "same-name")
        run = self.store.runs.get(result["collection_run_id"])
        self.assertEqual({entry["relative_dir"] for entry in run["trajectories"]}, {"TASK-A/same-name", "TASK-B/same-name"})

    def test_prefetch_candidates_are_not_counted_but_broken_final_directory_blocks(self):
        candidate = self.source / "TASK-A" / "_prefetch_staging" / "broken"
        candidate.mkdir(parents=True)
        self.assertTrue(self.preview()["valid"])
        (self.source / "TASK-A" / "broken-final").mkdir()
        value = self.preview()
        self.assertFalse(value["valid"])
        self.assertTrue(any("TASK-A/broken-final" in error for error in value["errors"]))
        self.assertFalse(self.store.records.list("collection_runs"))

    def test_each_missing_required_file_blocks_without_silent_step_drop(self):
        names = ["step001_vla_input.jpg", "step001_vla_input_ui.xml", "_trajectory_for_evaluate.json",
                 "step001_vla_model_response.json"]
        for name in names:
            with self.subTest(file=name):
                path = self.directory / name
                content = path.read_bytes()
                path.unlink()
                try:
                    value = self.preview()
                    self.assertFalse(value["valid"])
                    self.assertTrue(any("TASK-A/same-name" in error for error in value["errors"]))
                finally:
                    path.write_bytes(content)

    def test_invalid_action_evaluation_and_step_gap_block(self):
        response = self.directory / "step001_vla_model_response.json"
        original = response.read_bytes()
        response.write_text('{"content": "unparseable"}')
        self.assertFalse(self.preview()["valid"])
        response.write_bytes(original)
        evaluation = self.directory / "_trajectory_for_evaluate.json"
        evaluation.write_text('{"actions_flat": []}')
        self.assertFalse(self.preview()["valid"])
        fixture(self.source)
        response.rename(self.directory / "step003_vla_model_response.json")
        self.assertTrue(any("不连续" in value for value in self.preview()["errors"]))

    def test_source_changes_after_preview_require_new_validation(self):
        value = self.preview()
        (self.directory / "extra.log").write_text("added")
        with self.assertRaisesRegex(RolloutImportError, "已变化"):
            self.store.commit(value["import_id"], "changed")
        self.assertFalse(self.store.records.list("collection_runs"))
        self.assertFalse(self.store.artifacts.list())

    def test_path_boundary_and_managed_directories_are_rejected(self):
        candidates = [self.root / "raw", self.root / "raw" / "collection_batches", self.root / "batches",
                      Path(self.temp.name), Path("relative-path")]
        for source in candidates:
            with self.subTest(path=source), self.assertRaises(RolloutImportError):
                self.preview(source_path=str(source))
        external = Path(self.temp.name) / "external"
        fixture(external)
        with self.assertRaises(RolloutImportError):
            self.preview(source_path=str(external))
        with patch.dict(os.environ, {"ADF_ROLLOUT_IMPORT_ROOTS": str(external)}):
            self.assertTrue(self.preview(source_path=str(external))["valid"])

    def test_symlink_is_rejected_without_following_target(self):
        link = self.source / "TASK-A" / "linked"
        try:
            link.symlink_to(self.directory, target_is_directory=True)
        except OSError:
            self.skipTest("This Windows account cannot create symlinks")
        with self.assertRaisesRegex(RolloutImportError, "符号链接"):
            self.preview()

    def test_limits_block_before_registering_results(self):
        self.store.max_files = 1
        with self.assertRaises(RolloutImportError) as error:
            self.preview()
        self.assertEqual(error.exception.status, 413)
        self.assertFalse(self.store.records.list("collection_runs"))

    def test_all_existing_batch_representations_block_new_import(self):
        for namespace in ("batch_lifecycle", "preprocessing_jobs", "collection_runs", "batch_run_aliases"):
            with self.subTest(namespace=namespace):
                self.store.records.put(namespace, "old-key", {"batch_id": "rollout-test", "status": "active"})
                try:
                    with self.assertRaises(RolloutImportError):
                        self.preview()
                finally:
                    self.store.records.delete(namespace, "old-key")
        self.store.artifacts.publish("rollout-test", "01_conversion", {"rows": []})
        with self.assertRaises(RolloutImportError):
            self.preview()

    def test_concurrent_duplicate_commit_and_request_conflicts(self):
        value = self.preview()
        with ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(lambda _: self.store.commit(value["import_id"], "same-request"), range(4)))
        self.assertEqual(sum(not result["reused"] for result in results), 1)
        self.assertEqual(len(self.store.records.list("collection_runs")), 1)
        again = self.store.commit(value["import_id"], "another-request")
        self.assertTrue(again["reused"])
        another = self.preview(batch_id="different-batch")
        with self.assertRaisesRegex(RolloutImportError, "request_id"):
            self.store.commit(another["import_id"], "same-request")
        # A distinct test batch may intentionally reuse previously imported bytes.
        self.assertFalse(self.store.commit(another["import_id"], "different-request")["reused"])

    def test_rename_acknowledgement_loss_reuses_owned_frozen_copy(self):
        value = self.preview()
        original = Path.rename
        def fail_after_rename(path, target):
            result = original(path, target)
            if path == self.root / "tmp" / "rollout_imports" / value["import_id"]:
                raise OSError("rename acknowledgement lost")
            return result
        with patch.object(Path, "rename", fail_after_rename):
            with self.assertRaisesRegex(OSError, "acknowledgement"):
                self.store.commit(value["import_id"], "rename-retry")
        self.assertFalse(self.store.records.list("collection_runs"))
        restarted = RolloutImportStore(self.root)
        restarted.recover()
        self.assertFalse(restarted.commit(value["import_id"], "rename-retry")["reused"])

    def test_database_failure_is_atomic_and_retry_survives_empty_artifact_parent(self):
        value = self.preview()
        with patch.object(self.store.artifacts.records, "put_many", side_effect=OSError("database down")):
            with self.assertRaisesRegex(OSError, "database down"):
                self.store.commit(value["import_id"], "db-retry")
        self.assertFalse(self.store.records.list("collection_runs"))
        self.assertFalse(self.store.artifacts.list())
        self.assertFalse(self.store.commit(value["import_id"], "db-retry")["reused"])

    def test_unknown_database_commit_result_never_deletes_registered_files(self):
        value = self.preview()
        original = self.store.artifacts.records.put_many
        def commit_then_fail(*args, **kwargs):
            original(*args, **kwargs)
            raise OSError("commit acknowledgement lost")
        with patch.object(self.store.artifacts.records, "put_many", side_effect=commit_then_fail):
            result = self.store.commit(value["import_id"], "uncertain")
        self.assertTrue(result["reused"])
        self.assertEqual(len(CollectionRunStore(self.root).ready_input(result["batch_id"])["trajectories"]), 1)
        self.assertTrue(self.store.commit(value["import_id"], "uncertain")["reused"])

    def test_failed_commit_reserves_request_identity(self):
        first = self.preview()
        second = self.preview(batch_id="another-batch")
        with patch.object(self.store.artifacts.records, "put_many", side_effect=OSError("stop")):
            with self.assertRaises(OSError):
                self.store.commit(first["import_id"], "reserved")
        with self.assertRaisesRegex(RolloutImportError, "request_id"):
            self.store.commit(second["import_id"], "reserved")

    def test_each_interrupted_request_stays_bound_to_its_original_import(self):
        first, second = self.preview(), self.preview(batch_id="another-batch")
        with patch.object(self.store.artifacts.records, "put_many", side_effect=OSError("stop")):
            for request in ("first-attempt", "second-attempt"):
                with self.assertRaises(OSError):
                    self.store.commit(first["import_id"], request)
        with self.assertRaisesRegex(RolloutImportError, "request_id"):
            self.store.commit(second["import_id"], "second-attempt")
        self.assertFalse(self.store.commit(first["import_id"], "second-attempt")["reused"])

    def test_published_batch_rejects_new_import_and_old_commit_response(self):
        value = self.preview()
        self.store.commit(value["import_id"], "published")
        self.store.records.put("batch_lifecycle", "rollout-test", {"batch_id": "rollout-test", "status": "published", "release_id": "release-one"})
        with self.assertRaises(BatchPublishedError):
            self.preview()
        with self.assertRaises(BatchPublishedError):
            self.store.commit(value["import_id"], "published")
        with self.assertRaises(BatchPublishedError):
            self.store.commit(value["import_id"], "new-request")
        self.assertIsNotNone(self.store.get_batch("rollout-test"))

    def test_expired_uncommitted_preview_cleans_only_owned_copies(self):
        before = self.hashes()
        value = self.preview()
        with patch.object(self.store.artifacts.records, "put_many", side_effect=OSError("stop")):
            with self.assertRaises(OSError):
                self.store.commit(value["import_id"], "expire")
        preview = self.store.records.get("rollout_import_previews", value["import_id"])
        preview["expires_at"] = "2000-01-01T00:00:00+00:00"
        self.store.records.put("rollout_import_previews", value["import_id"], preview)
        self.assertEqual(self.store.recover()["removed"], 1)
        self.assertEqual(before, self.hashes())
        self.assertFalse(self.store.runs.run_root("rollout-test", "ri_" + value["import_id"]).exists())
        self.assertTrue(self.preview()["valid"])
        with self.assertRaises(RolloutImportError):
            self.store.commit(value["import_id"], "expire")

    def test_copy_corruption_never_becomes_a_ready_batch_and_retry_is_safe(self):
        value = self.preview()
        before = self.hashes()
        original = shutil.copyfileobj
        def corrupt(reader, writer, *args, **kwargs):
            original(reader, writer, *args, **kwargs)
            writer.write(b"corruption")
        with patch("backend.rollout_imports.shutil.copyfileobj", side_effect=corrupt):
            with self.assertRaises(CollectionRunError):
                self.store.commit(value["import_id"], "copy-retry")
        self.assertFalse(self.store.records.list("collection_runs"))
        self.assertEqual(before, self.hashes())
        self.assertFalse(self.store.commit(value["import_id"], "copy-retry")["reused"])

    def test_directory_change_during_copy_requires_new_preview(self):
        value = self.preview()
        original = shutil.copyfileobj
        def add_file(reader, writer, *args, **kwargs):
            original(reader, writer, *args, **kwargs)
            (self.directory / "late.log").write_text("added after preview")
        with patch("backend.rollout_imports.shutil.copyfileobj", side_effect=add_file):
            with self.assertRaisesRegex(RolloutImportError, "复制期间"):
                self.store.commit(value["import_id"], "late-file")
        self.assertFalse(self.store.records.list("collection_runs"))

    def test_cleanup_failure_does_not_hide_successful_registration(self):
        value = self.preview()
        original = self.store._remove_owned
        calls = 0
        def cleanup(path, parent):
            nonlocal calls
            calls += 1
            if calls > 1:
                raise PermissionError("cleanup unavailable")
            return original(path, parent)
        with patch.object(self.store, "_remove_owned", side_effect=cleanup), self.assertLogs("backend.rollout_imports", level="ERROR"):
            result = self.store.commit(value["import_id"], "cleanup")
        self.assertFalse(result["reused"])
        self.assertTrue(self.store.commit(value["import_id"], "cleanup")["reused"])

    def test_reparse_point_guard_is_checked_before_directory_read(self):
        from backend.rollout_imports import _linked
        with patch("backend.rollout_imports._linked", side_effect=lambda path: path == self.directory or _linked(path)):
            with self.assertRaisesRegex(RolloutImportError, "目录联接"):
                self.preview()

    def test_router_options_preview_commit_and_closed_workspace(self):
        app = FastAPI()
        app.include_router(router)
        configure_rollout_import_store(self.store)
        self.addCleanup(lambda: configure_rollout_import_store(None))
        with TestClient(app) as client:
            response = client.get("/api/rollout-imports/options")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers["cache-control"], "no-store")
            preview = client.post("/api/rollout-imports/preview", json=self.data)
            self.assertEqual(preview.status_code, 200, preview.text)
            response = client.post("/api/rollout-imports", json={"import_id": preview.json()["import_id"], "request_id": "http"})
            self.assertEqual(response.status_code, 200, response.text)
            detail = client.get("/api/rollout-imports/batches/rollout-test")
            self.assertEqual(detail.status_code, 200, detail.text)
            self.assertEqual(detail.json()["snapshot"]["tasks"][0]["collection_case_id"], "TASK-A")
            self.store.records.put("batch_lifecycle", "rollout-test", {"batch_id": "rollout-test", "status": "published", "release_id": "r-one"})
            self.assertEqual(client.get("/api/rollout-imports/batches/rollout-test").status_code, 409)
            self.assertEqual(client.post("/api/rollout-imports", json={"import_id": preview.json()["import_id"], "request_id": "http"}).status_code, 409)


if __name__ == "__main__":
    unittest.main()
