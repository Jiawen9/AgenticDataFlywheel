from __future__ import annotations

import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from backend.batch_lifecycle import lifecycle
from backend.data_publishing import historical_jsonl
from backend.data_publishing.service import DatasetReleaseRegistry
from backend.data_store import RecordStore
from backend.training_data_overview.converter import convert_release
from backend.training_data_overview.service import TrainingOverviewManager


class LocalApi:
    def __init__(self, root):
        self.root = root
        self.calls = []

    def open(self, request, *, timeout):
        url = request if isinstance(request, str) else request.full_url
        self.calls.append(url)
        if url.endswith("/api/data-storage"):
            return io.BytesIO(json.dumps({"data_root": str(self.root)}).encode("utf-8"))
        if request.get_method() != "POST" or not url.endswith("/retry"):
            raise AssertionError("unexpected API call")
        return io.BytesIO(b'{"conversion":{"status":"queued"}}')


class HistoricalJsonlImportTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name)
        self.root = self.work / "data"
        self.source = self.work / "示例.jsonl"
        self.manifest = self.work / "import.json"
        self.raw = self.sample("improve list view", "click") + self.sample("open collection", "open")
        self.source.write_bytes(self.raw)
        self.item = {"file": self.source.name, "name": "工厂训练", "version": "2026-v1",
                     "data_source": "工厂", "cloud_uri": "s3://bucket/training/factory.jsonl", "adapter": "middle"}
        self.api = LocalApi(self.root)
        self.persist([self.item])

    @staticmethod
    def sample(task, action):
        row = {"messages": [{"role": "user", "content": task}],
               "solution": json.dumps({"sftcall": {"action": action}}),
               "images": ["same.png"]}
        return (json.dumps(row) + "\r\n").encode("utf-8")

    def persist(self, items):
        self.manifest.write_text(json.dumps({"datasets": items}, ensure_ascii=False), encoding="utf-8")

    def run_import(self, *, apply=False, current_day="2026-11-18"):
        with patch.object(historical_jsonl, "build_opener", return_value=self.api):
            return historical_jsonl.run_import(self.manifest, apply=apply, data_root=self.root, registered_day=current_day)

    def test_preview_read_only_and_apply_reuses_release_across_default_dates(self):
        dry_run = self.run_import()
        self.assertTrue(dry_run["ok"], dry_run)
        self.assertFalse(self.root.exists())
        self.assertEqual(self.api.calls, [])
        self.assertEqual(dry_run["datasets"][0]["summary"]["trajectory_count"], 2)
        one = self.run_import(apply=True)["datasets"][0]
        self.assertTrue(one["valid"], one)
        self.assertEqual(one["registration"], "registered")
        self.assertEqual(one["overview"], "queued")
        two = self.run_import(apply=True, current_day="2026-11-21")["datasets"][0]
        self.assertEqual(two["registration"], "existing")
        self.assertEqual(two["release_id"], one["release_id"])
        self.assertEqual(two["data_date"], "2026-11-18")
        registry = DatasetReleaseRegistry(data_root=self.root, releases_file=self.root / "records.json")
        release = registry.get(one["release_id"])
        self.assertEqual(release["name"], "工厂训练 · 2026-v1")
        self.assertEqual(release["external_import"]["cloud_uri"], self.item["cloud_uri"])
        self.assertEqual(release["external_import"]["format"], "jsonl")
        self.assertEqual(lifecycle(release["batch_ids"][0], self.root)["status"], "published")
        path, filename = registry.training_file(one["release_id"], 0)
        self.assertEqual(filename, self.source.name)
        self.assertEqual(path.read_bytes(), self.raw)
        conversion = convert_release(registry, release)
        self.assertEqual([row["step数量"] for row in conversion["rows"]], [1, 1])
        self.assertEqual(self.api.calls.count(f"http://127.0.0.1:8765/api/training-data-overview/releases/{one['release_id']}/retry"), 2)

    def test_identical_sources_count_as_two_distinct_versions_and_rebuild_no_double_count(self):
        self.persist([self.item, {**self.item, "name": "健康云训练", "data_source": "健康云", "version": "2026-v1",
                                 "cloud_uri": "s3://health-bucket/training/health.jsonl"}])
        report = self.run_import(apply=True)
        self.assertTrue(report["ok"], report)
        records = RecordStore(self.root).list("dataset_releases")
        self.assertEqual(len(records), 2)
        self.assertEqual(len({r["external_import"]["source_sha256"] for r in records}), 1)
        registry = DatasetReleaseRegistry(data_root=self.root, releases_file=self.root / "records.json")
        manager = TrainingOverviewManager(registry)
        self.addCleanup(manager.close)
        manager.start()
        manager.wait(timeout=15)
        for version in report["datasets"]:
            self.assertEqual(manager.submit(version["release_id"])["status"], "succeeded")
        summary = manager.query()
        self.assertEqual(summary["overview"]["total_steps"], 4)
        self.assertEqual(summary["overview"]["total_trajectories"], 4)
        self.assertEqual(summary["step_stats"], [{"steps": 1, "count": 4}])
        for source in ("工厂", "健康云"):
            self.assertEqual(manager.query(source=source)["overview"]["total_steps"], 2)

    def test_same_version_conflict_and_running_root_guard(self):
        self.api.root = self.work / "different"
        with self.assertRaisesRegex(ValueError, "目录.*不同"):
            self.run_import(apply=True)
        self.assertFalse(self.root.exists())
        self.api.root = self.root
        self.assertTrue(self.run_import(apply=True)["ok"])
        self.source.write_bytes(self.raw + self.sample("another task", "click"))
        changed = self.run_import(apply=True)
        self.assertFalse(changed["ok"])
        self.assertIn("内容或配置不同", changed["datasets"][0]["errors"][0]["message"])
        self.assertEqual(len(RecordStore(self.root).list("dataset_releases")), 1)

    def test_interrupted_database_commit_reuses_the_frozen_copy(self):
        with patch.object(RecordStore, "put_many", side_effect=OSError("injected DB failure")):
            incomplete = self.run_import(apply=True)
        self.assertFalse(incomplete["ok"])
        self.assertFalse(RecordStore(self.root).list("dataset_releases"))
        (prepared,) = (self.root / "releases").glob("rel*")
        committed = self.run_import(apply=True)
        self.assertTrue(committed["ok"], committed)
        self.assertEqual(committed["datasets"][0]["release_id"], prepared.name)


if __name__ == "__main__":
    unittest.main()
