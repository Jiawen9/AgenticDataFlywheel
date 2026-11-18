"""Exercise the historical JSONL catalog with frozen files and the real queue."""
import copy
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from openpyxl import load_workbook

from backend.training_data_overview.converter import (
    ALL_DATA_COLUMNS,
    JSONL_MANUAL_RECORD_SOURCE,
    ConversionError,
    convert_release,
)
from backend.training_data_overview.service import CONVERSIONS, TrainingOverviewManager, summarize


def aggregate(identity, *, known=0, refined=None, grouping="single_step"):
    return {
        "trajectory_identity": identity, "step数量": 1,
        "APP": "未记录 App", "一级场景": "未分类", "二级场景": "未分类",
        "app_known": False, "level1_known": False, "level2_known": False,
        "时间": "2026-07-01", "生产方式": "数据飞轮",
        "action_box": {"click": 1}, "人工精修步骤数量": refined, "manual_known": known == 1,
        "manual_known_steps": known, "manual_unknown_steps": 1 - known,
        "manual_record_source": JSONL_MANUAL_RECORD_SOURCE,
        "manual_refined_jsonl_rows": [2] if refined else [], "trajectory_grouping": grouping,
    }


class Registry:
    def __init__(self, data_root):
        self.data_root = data_root
        self.releases = {}

    def get(self, release_id):
        return self.releases.get(release_id)

    def list_releases(self):
        return list(self.releases.values())

    def resolve_project_path(self, path):
        return self.data_root / path

    def write(self, path, data):
        if not isinstance(data, bytes):
            data = json.dumps(data, ensure_ascii=False).encode()
        target = self.data_root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return {"path": path, "sha256": hashlib.sha256(data).hexdigest()}

    def add(self, release_id, rows):
        root = f"releases/{release_id}"
        metadata = {
            "data_source": "数据飞轮", "data_date": "2026-07-01", "app": "", "level1": "", "level2": "",
            "version": release_id, "cloud_uri": "s3://example-bucket/historical/samples.jsonl",
            "adapter": "middle", "date_defaulted": False,
        }
        source = self.write(f"{root}/training/001/samples.jsonl", b'{"messages": [], "images": []}\n')
        header = {"metadata": metadata, "schema_version": 1, "parser_version": 1}
        summary = {"trajectory_count": len(rows), "step_count": sum(row["step数量"] for row in rows)}
        canonical = self.write(f"{root}/external-data.json", {
            **header, "rows": rows, "summary": summary, "warnings": [{"row": 3, "message": "样例诊断"}],
        })
        manifest = self.write(f"{root}/import-manifest.json", {
            **header, "release_id": release_id, "batch_id": f"external_{release_id}", "filename": "samples.jsonl",
            "source": source, "canonical": canonical,
        })
        imported = {**metadata, "format": "jsonl", "parser_version": 1, "source_sha256": source["sha256"],
                    "filename": "samples.jsonl", "source": source, "canonical": canonical, "manifest": manifest}
        release = {"release_id": release_id, "name": release_id, "source_kind": "external_manual",
                   "batch_ids": [f"external_{release_id}"], "external_import": imported, **summary,
                   "training_files": [{**source, "status": "ready"}]}
        self.releases[release_id] = release
        return release


class HistoricalJsonlOverviewTests(unittest.TestCase):
    def setUp(self):
        workspace = TemporaryDirectory()
        self.addCleanup(workspace.cleanup)
        self.registry = Registry(Path(workspace.name))

    def test_real_queue_and_workbook_keep_versions_and_single_step_bins(self):
        first = self.registry.add("rel_first", [aggregate("a")])
        second = self.registry.add("rel_second", [aggregate("a", known=1, refined=1)])
        self.assertEqual(first["external_import"]["source_sha256"], second["external_import"]["source_sha256"])
        manager = TrainingOverviewManager(self.registry)
        self.addCleanup(manager.close)
        manager.start()
        manager.wait()
        result = manager.query(source="数据飞轮")
        self.assertEqual(result["filters"]["sources"], ["数据飞轮"])
        self.assertEqual(result["overview"]["total_trajectories"], 2)
        self.assertEqual(result["overview"]["total_steps"], 2)
        self.assertEqual(result["overview"]["manual_known_steps"], 1)
        self.assertEqual(result["overview"]["manual_unknown_steps"], 1)
        self.assertTrue(result["overview"]["show_manual_refine_steps"])
        self.assertEqual(result["overview"]["manual_refine_steps"], 1)
        self.assertEqual(result["step_stats"], [{"steps": 1, "count": 2}])
        self.assertEqual(result["action_stats"], [{"category": "click", "count": 2}])
        workbook = load_workbook(manager.workbook(), read_only=True)
        try:
            rows = list(workbook.active.values)
            self.assertEqual(list(rows[0]), ALL_DATA_COLUMNS)
            self.assertEqual(len(rows), 3)
            self.assertEqual({values[0] for values in rows[1:]}, {"rel_first/001/a", "rel_second/001/a"})
            self.assertEqual([values[8] for values in rows[1:]], [None, 1])
        finally:
            workbook.close()
        self.assertEqual(manager.submit("rel_first")["status"], "succeeded")
        manager.wait()
        self.assertEqual(manager.query()["overview"]["total_trajectories"], 2)
        self.assertEqual(manager.records.get(CONVERSIONS, "rel_first")["status"], "succeeded")

    def test_unknown_manual_history_and_legacy_distribution_are_distinct(self):
        release = self.registry.add("rel_first", [aggregate("a")])
        converted = convert_release(self.registry, release)
        self.assertIn("samples.jsonl 第 3 行：样例诊断", converted["warnings"])
        filtered = summarize({"rows": converted["rows"]}, [], {})
        self.assertFalse(filtered["overview"]["show_manual_refine_steps"])
        self.assertEqual(filtered["overview"]["manual_unknown_steps"], 1)
        original = {**converted["rows"][0], "轨迹": "legacy", "子任务轨迹": "legacy"}
        original.pop("manual_record_source")
        combined = summarize({"rows": converted["rows"] + [original]}, [], {})
        self.assertEqual(combined["step_stats"], [{"steps": 0, "count": 1}, {"steps": 1, "count": 1}])

    def test_reject_changed_provenance_and_original_file(self):
        release = self.registry.add("rel_first", [aggregate("a")])
        corrupted = copy.deepcopy(release)
        corrupted["external_import"]["cloud_uri"] = "s3://example-bucket/elsewhere.jsonl"
        with self.assertRaisesRegex(ConversionError, "元数据"):
            convert_release(self.registry, corrupted)
        path = self.registry.resolve_project_path(release["external_import"]["source"]["path"])
        path.write_bytes(b"changed\n")
        with self.assertRaisesRegex(ConversionError, "校验"):
            convert_release(self.registry, release)
