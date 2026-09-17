from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from openpyxl import load_workbook

from backend.data_store import ArtifactStore, RecordStore, RevisionConflict
from backend.data_store.paths import contained_path


class DataStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "data"
        self.records = RecordStore(self.root)
        self.artifacts = ArtifactStore(self.root)

    def test_missing_reads_do_not_create_files_and_empty_database_is_readable(self):
        self.assertIsNone(self.records.get("jobs", "one"))
        self.assertEqual(self.records.list("jobs"), [])
        self.assertFalse(self.records.delete("jobs", "one"))
        self.assertEqual(self.artifacts.list(), [])
        self.assertFalse(self.root.exists())
        self.records.database_path.parent.mkdir(parents=True)
        sqlite3.connect(self.records.database_path).close()
        self.assertIsNone(self.records.get("jobs", "one"))
        self.assertEqual(self.records.list("jobs"), [])

    def test_revision_cas_and_restart_preserve_values(self):
        payload = {"summary": "", "thought": "生成内容", "nested": {"count": 0}, "unknown": None}
        saved = self.records.put("sessions", "one", payload, expected_revision=0)
        self.assertEqual(saved["storage_revision"], 1)
        self.assertNotIn("storage_revision", payload)
        with self.assertRaises(RevisionConflict):
            self.records.put("sessions", "one", {"summary": "stale"}, expected_revision=0)
        saved["summary"] = "手工值"
        updated = self.records.put("sessions", "one", saved, expected_revision=1)
        restored = RecordStore(self.root).get("sessions", "one")
        self.assertEqual(restored, updated)
        self.assertEqual(restored["thought"], "生成内容")
        self.assertEqual(restored["storage_revision"], 2)
        self.assertEqual(len(self.records.list("sessions")), 1)
        self.assertEqual(self.records.list("other"), [])
        with self.assertRaises(RevisionConflict):
            self.records.delete("sessions", "one", expected_revision=1)
        self.assertTrue(self.records.delete("sessions", "one", expected_revision=2))

    def test_update_atomic_and_callback_failure_rolls_back(self):
        self.records.put("counts", "one", {"count": 0, "summary": ""})

        def increment(_):
            RecordStore(self.root).update("counts", "one", lambda value: value.update(count=value["count"] + 1))

        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(increment, range(40)))
        before = self.records.get("counts", "one")
        self.assertEqual(before["count"], 40)
        self.assertEqual(before["summary"], "")

        def fail(value):
            value["count"] = -1
            raise RuntimeError("simulated callback failure")

        with self.assertRaises(RuntimeError):
            self.records.update("counts", "one", fail)
        self.assertEqual(self.records.get("counts", "one"), before)
        created = self.records.update("counts", "two", lambda value: {"count": 1}, default={})
        self.assertEqual(created["count"], 1)
        with self.assertRaises(KeyError):
            self.records.update("counts", "missing", lambda value: value)

    def test_multi_record_commit_and_cas_failure_are_atomic(self):
        self.records.put("sessions", "one", {"summary": ""})
        entries = [
            {"namespace": "releases", "key": "new", "payload": {"status": "ready"}, "expected_revision": 0},
            {"namespace": "sessions", "key": "one", "payload": {"summary": "kept", "published": True}, "expected_revision": 0},
        ]
        with self.assertRaises(RevisionConflict):
            self.records.put_many(entries)
        self.assertIsNone(self.records.get("releases", "new"))
        self.assertEqual(self.records.get("sessions", "one")["summary"], "")
        entries[1]["expected_revision"] = 1
        saved = self.records.put_many(entries)
        self.assertEqual(saved[1]["storage_revision"], 2)
        self.assertEqual(self.records.get("releases", "new")["status"], "ready")

    def test_json_excel_frozen_and_text_literal(self):
        rows = [{"用例编号": "001", "任务": "=HYPERLINK(1)", "summary": "", "thought": "原值", "细节": {"广告": False}}]
        manifest = self.artifacts.publish("batch-1", "06_correction", {"steps": rows}, tables={"修正步骤": rows}, source_refs=[{"batch_id": "parent"}])
        json_path = self.artifacts.resolve_file(manifest, "result.json")
        excel_path = self.artifacts.resolve_file(manifest, "result.xlsx")
        self.assertEqual(json.loads(json_path.read_text(encoding="utf-8")), {"steps": rows})
        workbook = load_workbook(excel_path)
        try:
            sheet = workbook.active
            self.assertEqual(sheet["A2"].value, "001")
            self.assertEqual(sheet["B2"].value, rows[0]["任务"])
            self.assertEqual(sheet["B2"].data_type, "s")
            self.assertEqual(sheet["D2"].value, "原值")
        finally:
            workbook.close()
        self.assertEqual(ArtifactStore(self.root).get("batch-1", "06_correction", manifest["version"]), manifest)
        before = {file: file.read_bytes() for file in (json_path, excel_path)}
        rows[0]["thought"] = "新值"
        second = self.artifacts.publish("batch-1", "06_correction", {"steps": rows}, tables={"修正步骤": rows})
        self.assertNotEqual(second["version"], manifest["version"])
        self.assertEqual(len(self.artifacts.list(batch_id="batch-1", stage="06_correction")), 2)
        for file, content in before.items():
            self.assertEqual(file.read_bytes(), content)

    def test_copy_preserves_bytes_and_download_rejects_tampering(self):
        original = self.artifacts.publish("a", "01_conversion", {}, tables={"轨迹": [{"task": "原始任务"}]})
        original_path = self.artifacts.resolve_file(original, "result.xlsx")
        copied = self.artifacts.publish("a", "07_cot", {"thought": ""}, workbooks={"发布表.xlsx": original_path})
        copy_path = self.artifacts.resolve_file(copied, "发布表.xlsx")
        self.assertEqual(original_path.read_bytes(), copy_path.read_bytes())
        copy_path.write_bytes(b"altered")
        with self.assertRaisesRegex(ValueError, "checksum"):
            self.artifacts.resolve_file(copied, "发布表.xlsx")
        with self.assertRaises(FileNotFoundError):
            self.artifacts.resolve_file(original, "other.xlsx")

    def test_failure_invisible_and_export_retry_needs_no_model(self):
        with patch.object(self.artifacts.records, "put", side_effect=RuntimeError("database failure")):
            with self.assertRaisesRegex(RuntimeError, "database failure"):
                self.artifacts.publish("batch", "stage", {"task": "kept"}, tables={"表": [{"task": "kept"}]})
        self.assertEqual(self.artifacts.list(), [])
        self.assertEqual(list(self.root.glob("batches/*/*/*/manifest.json")), [])
        self.assertEqual(list(self.root.glob("tmp/artifacts/*")), [])
        with patch("backend.data_store.artifacts._write_tables", side_effect=RuntimeError("excel failure")):
            with self.assertRaises(RuntimeError):
                self.artifacts.publish("batch", "stage", {}, tables={"表": []})
        self.assertEqual(self.artifacts.list(), [])
        recovered = self.artifacts.publish("batch", "stage", {"task": "kept"}, tables={"表": [{"task": "kept"}]})
        self.assertIsNotNone(self.artifacts.get("batch", "stage", recovered["version"]))

    def test_concurrent_publish_and_orphan_isolation(self):
        def publish(index):
            return ArtifactStore(self.root).publish("batch", "stage", {"index": index})

        with ThreadPoolExecutor(max_workers=8) as pool:
            manifests = list(pool.map(publish, range(12)))
        self.assertEqual(len({item["version"] for item in manifests}), 12)
        self.assertEqual(len(self.artifacts.list()), 12)
        orphan = self.root / "batches" / "batch" / "stage" / "orphan"
        orphan.mkdir()
        (orphan / "manifest.json").write_text("{}", encoding="utf-8")
        self.assertIsNone(self.artifacts.get("batch", "stage", "orphan"))
        self.assertEqual(len(self.artifacts.list()), 12)

    def test_paths_cannot_escape_root_and_excel_cannot_truncate(self):
        for batch in ("../outside", "a/b", "NUL", "bad."):
            with self.assertRaises(ValueError):
                self.artifacts.publish(batch, "stage", {})
        for filename in ("../other.xlsx", "CON.xlsx", "result.json"):
            with self.assertRaises(ValueError):
                self.artifacts.publish("a", "stage", {}, workbooks={filename: Path(self.temp.name)})
        with self.assertRaises(ValueError):
            contained_path(self.root, "..", "outside")
        self.assertFalse(self.root.exists())
        with self.assertRaisesRegex(ValueError, "32767"):
            self.artifacts.publish("a", "stage", {}, tables={"表": [{"task": "x" * 32768}]})
        self.assertEqual(self.artifacts.list(), [])


if __name__ == "__main__":
    unittest.main()
