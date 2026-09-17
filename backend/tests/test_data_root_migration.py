from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.data_store import ArtifactStore, RecordStore, rebase_data_path
from backend.data_store import migrate_root
from backend.stage_artifacts import read_workbook_payload, write_sidecar, write_payload_workbook
from backend.trajectory_context import resolve_batch_context


class DataRootMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name)
        self.source = self.project / "data"
        self.target = self.project / "backend_workspace"
        self.target.mkdir()
        (self.target / "old.txt").write_text("old workspace", encoding="utf-8")
        self.raw = self.source / "raw/rollout_trajectories"
        (self.raw / "T/T-1").mkdir(parents=True)
        (self.raw / "T/T-1/step001_vla_input.jpg").write_bytes(b"original image")
        self.payload = {"schema_version": 1, "columns": {"Steps": ["文件夹名", "image", "action", "actions_box"]},
            "sheets": {"Steps": [{"文件夹名": "T-1", "image": "T/T-1/step001_vla_input.jpg",
                                   "action": "wait", "actions_box": "manual-value"}]}}
        self.excel = self.source / "system/preprocessing/annotated_trajectories.xlsx"
        write_payload_workbook(self.excel, self.payload)
        self.artifact = ArtifactStore(self.source).publish("validation", "02_annotation", self.payload,
            workbooks={self.excel.name: self.excel}, source_refs=[{"kind": "raw_trajectories", "path": str(self.raw)}])
        write_sidecar(self.excel, self.payload, source_ref=self.artifact)
        self.old_key = hashlib.sha256(str(self.excel.resolve()).encode()).hexdigest()
        RecordStore(self.source).put("trajectory_annotations", self.old_key,
            {"path": str(self.excel.resolve()), "payload": self.payload, "artifact": self.artifact})

    def files(self, root):
        return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}

    def test_check_is_read_only_and_missing_roots_are_not_imported(self):
        before = self.files(self.project)
        result = migrate_root.check(self.project)
        self.assertTrue(result["ready"])
        self.assertEqual(self.files(self.project), before)
        with self.assertRaises(ValueError):
            rebase_data_path(self.raw, self.target)
        self.assertFalse((self.target / "system/app.sqlite").exists())

    def test_apply_preserves_all_frozen_bytes_and_rekeys_manual_value(self):
        before = self.files(self.source)
        result = migrate_root.apply(self.project)
        self.assertEqual(result["phase"], "applied")
        self.assertFalse(self.source.exists())
        self.assertTrue((self.project / migrate_root.STATE_DIR / "legacy_backend_workspace/old.txt").exists())
        for name, value in before.items():
            if name != "system/app.sqlite": self.assertEqual((self.target / name).read_bytes(), value)
        new_excel = self.target / self.excel.relative_to(self.source)
        new_key = hashlib.sha256(str(new_excel.resolve()).encode()).hexdigest()
        records = RecordStore(self.target)
        self.assertIsNone(records.get("trajectory_annotations", self.old_key))
        self.assertEqual(records.get("trajectory_annotations", new_key)["payload"], self.payload)
        self.assertEqual(read_workbook_payload(self.excel, data_root=self.target)["sheets"], self.payload["sheets"])
        context = resolve_batch_context("validation", root=self.target)
        self.assertEqual(context.raw_root, self.target / "raw/rollout_trajectories")
        self.assertEqual(context.annotation_version, self.artifact["version"])
        self.assertEqual(rebase_data_path(self.raw, self.target), context.raw_root)
        self.assertEqual(rebase_data_path("raw/rollout_trajectories", self.target), context.raw_root)
        for path in (self.project / "other", "../outside", self.source / "../outside"):
            with self.assertRaises(ValueError): rebase_data_path(path, self.target)
        after = self.files(self.project)
        self.assertEqual(migrate_root.apply(self.project)["phase"], "applied")
        self.assertTrue(migrate_root.check(self.project)["verified"])
        self.assertEqual(self.files(self.project), after)

    def test_rollback_restores_original_database_and_both_roots(self):
        source, target = self.files(self.source), self.files(self.target)
        migrate_root.apply(self.project)
        self.assertEqual(migrate_root.rollback(self.project)["phase"], "rolled_back")
        self.assertEqual(self.files(self.source), source)
        self.assertEqual(self.files(self.target), target)
        self.assertEqual(migrate_root.rollback(self.project)["phase"], "rolled_back")

    def test_finalize_requires_unchanged_artifacts_and_is_idempotent(self):
        migrate_root.apply(self.project)
        source = ArtifactStore(self.target).resolve_file(self.artifact, "result.json")
        original = source.read_bytes(); source.write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "checksum"):
            migrate_root.finalize(self.project)
        retained = self.project / migrate_root.STATE_DIR / "legacy_backend_workspace"
        self.assertTrue(retained.exists())
        source.write_bytes(original)
        self.assertEqual(migrate_root.finalize(self.project)["phase"], "finalized")
        self.assertFalse(retained.exists())
        self.assertEqual(migrate_root.finalize(self.project)["phase"], "finalized")
        with self.assertRaises(ValueError): migrate_root.rollback(self.project)

    def test_database_failure_rolls_back_rekey_and_apply_can_resume(self):
        original_save = RecordStore._save
        def fail(connection, namespace, key, payload, revision):
            if namespace == "data_root_relocations": raise RuntimeError("migration DB failure")
            return original_save(connection, namespace, key, payload, revision)
        with patch.object(RecordStore, "_save", side_effect=fail):
            with self.assertRaisesRegex(RuntimeError, "DB failure"):
                migrate_root.apply(self.project)
        self.assertIsNotNone(RecordStore(self.target).get("trajectory_annotations", self.old_key))
        self.assertEqual(migrate_root.apply(self.project)["phase"], "applied")
        self.assertTrue(migrate_root.verify(self.project)["verified"])

    def test_rollback_refuses_to_discard_new_database_writes(self):
        migrate_root.apply(self.project)
        RecordStore(self.target).put("new_work", "one", {"task": "keep"})
        with self.assertRaisesRegex(ValueError, "later writes"):
            migrate_root.rollback(self.project)
        self.assertIsNotNone(RecordStore(self.target).get("new_work", "one"))

    def test_active_job_and_office_lock_block_apply(self):
        RecordStore(self.source).put("preprocessing_jobs", "active", {"status": "running"})
        self.assertFalse(migrate_root.check(self.project)["ready"])
        with self.assertRaisesRegex(ValueError, "Active record"):
            migrate_root.apply(self.project)
        RecordStore(self.source).delete("preprocessing_jobs", "active")
        lock = self.excel.with_name("~$annotated_trajectories.xlsx"); lock.write_bytes(b"office lock")
        self.assertFalse(migrate_root.check(self.project)["ready"])
        with self.assertRaisesRegex(ValueError, "Office lock"):
            migrate_root.apply(self.project)
        self.assertFalse((self.project / migrate_root.STATE_DIR).exists())

    def test_annotation_key_conflict_aborts_whole_database_transaction(self):
        new_excel = self.target / self.excel.relative_to(self.source)
        key = hashlib.sha256(str(new_excel.resolve()).encode()).hexdigest()
        RecordStore(self.source).put("trajectory_annotations", key, {"path": str(new_excel), "payload": {"keep": True}})
        with self.assertRaisesRegex(ValueError, "index conflicts"):
            migrate_root.apply(self.project)
        self.assertTrue(self.source.exists())
        self.assertIsNotNone(RecordStore(self.source).get("trajectory_annotations", self.old_key))
        self.assertEqual(RecordStore(self.source).list("data_root_relocations"), [])

    def test_commit_before_journal_crash_never_loses_later_records(self):
        original_save = migrate_root._save_state
        def crash(journal, state, phase):
            if phase == "applied": raise RuntimeError("after database commit")
            return original_save(journal, state, phase)
        with patch.object(migrate_root, "_save_state", side_effect=crash):
            with self.assertRaisesRegex(RuntimeError, "after database"):
                migrate_root.apply(self.project)
        RecordStore(self.target).put("new_work", "one", {"task": "must survive"})
        for operation in (migrate_root.apply, migrate_root.rollback):
            with self.assertRaisesRegex(ValueError, "later writes"):
                operation(self.project)
        self.assertIsNotNone(RecordStore(self.target).get("new_work", "one"))

    def test_rollback_can_resume_after_database_restore_before_rename(self):
        before = self.files(self.source)
        migrate_root.apply(self.project)
        original_rename = Path.rename
        def crash(path, destination):
            if path == self.target and destination == self.source:
                raise RuntimeError("before rollback rename")
            return original_rename(path, destination)
        with patch.object(Path, "rename", new=crash):
            with self.assertRaisesRegex(RuntimeError, "before rollback"):
                migrate_root.rollback(self.project)
        self.assertEqual(migrate_root.rollback(self.project)["phase"], "rolled_back")
        self.assertEqual(self.files(self.source), before)

    def test_uploading_blocks_even_without_running_status(self):
        for namespace, payload in (("dataset_upload_jobs", {"status": "uploading"}),
                                   ("dataset_releases", {"internal_upload": {"status": "uploading"}})):
            RecordStore(self.source).put(namespace, "busy", payload)
            self.assertFalse(migrate_root.check(self.project)["ready"])
            RecordStore(self.source).delete(namespace, "busy")

    def test_finalize_refuses_new_office_lock_files_in_retained_directory(self):
        migrate_root.apply(self.project)
        retained = self.project / migrate_root.STATE_DIR / "legacy_backend_workspace"
        lock = retained / "~$new.xlsx"; lock.write_bytes(b"new lock")
        with self.assertRaisesRegex(ValueError, "Office lock"):
            migrate_root.finalize(self.project)
        self.assertTrue(lock.exists())

    def test_rollback_accepts_physical_sqlite_changes_without_logical_writes(self):
        import sqlite3
        migrate_root.apply(self.project)
        connection = sqlite3.connect(self.target / "system/app.sqlite")
        try:
            connection.execute("PRAGMA user_version=7"); connection.commit()
        finally:
            connection.close()
        self.assertEqual(migrate_root.rollback(self.project)["phase"], "rolled_back")

    def test_preprocessing_retry_uses_original_json_after_root_move(self):
        from backend.tests.test_preprocessing_jobs import PreprocessingJobTests, CONFIG
        from backend.preprocessing_jobs import PreprocessingJobManager
        from backend.preprocessing_service import annotate_input
        from backend.collection_runs import CollectionRunStore
        fixture = PreprocessingJobTests(); fixture.setUp()
        try:
            fixture.complete()
            fixture.manager.annotator = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("model unavailable"))
            failed = fixture.start()
            original_input = fixture.manager._get(failed["job_id"])["input_path"]
            before = (fixture.root / original_input).read_bytes()
            project = fixture.root.parent
            migrate_root.apply(project)
            new_root = project / "backend_workspace"
            manager = PreprocessingJobManager(new_root, executor=fixture.queue,
                source_store=CollectionRunStore(new_root), config_loader=lambda: dict(CONFIG),
                reviewer_factory=fixture.factory)
            retried = manager.retry(failed["job_id"])
            fixture.queue.drain()
            done = manager.get(retried["job_id"])
            self.assertEqual(done["status"], "succeeded", done.get("error"))
            self.assertEqual((new_root / original_input).read_bytes(), before)
            context = resolve_batch_context("batch-one", root=new_root)
            from backend.trajectory_context import validate_batch_sources
            validate_batch_sources(context)
            self.assertEqual(context.raw_root, new_root / "raw/collection_batches/batch-one")
        finally:
            fixture.doCleanups()


if __name__ == "__main__": unittest.main()
