from __future__ import annotations

import json
import multiprocessing
import shutil
import tempfile
import unittest
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from backend.data_store import RecordStore, RevisionConflict
from backend.file_io import io_path
from backend.identifiers import allocate_id, batch_uses_short_ids
from backend.short_paths import (adapt_conversion_identity, map_collection_manifest,
                                 short_cache_directory, short_temporary_file, validate_local_manifest)
from backend.tests.test_collection_runs import raw_trajectory
from backend.task_generation.collection_batches import workbook_digest

DAY = datetime(2026, 10, 14, 16, 0, tzinfo=timezone.utc)


def process_allocate(root):
    return allocate_id("j", Path(root), now=DAY)


class ShortIdTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_beijing_date_stable_mapping_and_parameter_conflict(self):
        first = allocate_id("b", self.root, key=["request", "one"], request_fingerprint="a", now=DAY)
        self.assertEqual(first, "b261015-001")
        self.assertTrue(batch_uses_short_ids(first, self.root))
        again = allocate_id("b", self.root, key=["request", "one"], request_fingerprint="a",
                            now=datetime(2026, 10, 19, tzinfo=timezone.utc))
        self.assertEqual(first, again)
        with self.assertRaises(RevisionConflict):
            allocate_id("b", self.root, key=["request", "one"], request_fingerprint="different", now=DAY)
        self.assertEqual(allocate_id("b", self.root, now=DAY), "b261015-002")

    def test_existing_record_directory_counter_overflow_and_missing_marker(self):
        store = RecordStore(self.root)
        store.put("legacy_jobs", "b261015-001", {"job_id": "b261015-001"})
        (self.root / "batches" / "b261015-002").mkdir(parents=True)
        self.assertFalse(batch_uses_short_ids("b261015-001", self.root))
        self.assertEqual(allocate_id("b", self.root, now=DAY), "b261015-003")
        store.put("short_id_counters", "j:261015", {"sequence": 999})
        self.assertEqual(allocate_id("j", self.root, now=DAY), "j261015-1000")

    def test_concurrent_threads_and_processes_share_one_counter(self):
        with ThreadPoolExecutor(max_workers=8) as executor:
            threaded = list(executor.map(lambda _: allocate_id("j", self.root, now=DAY), range(40)))
        with ProcessPoolExecutor(max_workers=3, mp_context=multiprocessing.get_context("spawn")) as executor:
            processed = list(executor.map(process_allocate, [str(self.root)] * 12))
        self.assertEqual(len(set(threaded + processed)), 52)
        with ThreadPoolExecutor(max_workers=8) as executor:
            replay = list(executor.map(lambda _: allocate_id("r", self.root, key=["same"], now=DAY), range(20)))
        self.assertEqual(len(set(replay)), 1)

    def test_full_cache_fingerprint_and_legacy_layout(self):
        parent = self.root / "cache" / "checkpoints"
        signature = "a" * 64
        self.assertEqual(short_cache_directory(self.root, "legacy", "quality", signature, parent), parent / signature)
        batch = allocate_id("b", self.root, now=DAY)
        current = short_cache_directory(self.root, batch, "quality", signature, parent)
        self.assertLess(len(current.name), 20)
        self.assertEqual(json.loads((current / "identity.json").read_text())["fingerprint"], signature)
        self.assertEqual(current, short_cache_directory(self.root, batch, "quality", signature, parent))
        marker = json.loads((current / "identity.json").read_text())
        marker["fingerprint"] = "b" * 64
        (current / "identity.json").write_text(json.dumps(marker))
        with self.assertRaisesRegex(ValueError, "指纹"):
            short_cache_directory(self.root, batch, "quality", signature, parent)

    def test_short_temporary_name_does_not_repeat_long_target(self):
        target = self.root / ("x" * 170 + ".json")
        with short_temporary_file(target) as first:
            with short_temporary_file(target) as second:
                self.assertNotEqual(first, second)
                self.assertLess(len(first.name), 25)
                self.assertTrue(first.is_file())
        self.assertFalse(first.exists())

    def test_cache_parent_link_is_rejected_before_resolving(self):
        batch = allocate_id("b", self.root, now=DAY)
        junction = self.root / "cache"
        with patch("backend.short_paths.is_link_or_junction", side_effect=lambda p: p == junction):
            with self.assertRaisesRegex(ValueError, "不允许链接"):
                short_cache_directory(self.root, batch, "quality", "a" * 64, junction / "nested")

    def test_uncommitted_cache_marker_can_recover_but_unknown_cache_cannot(self):
        batch = allocate_id("b", self.root, now=DAY)
        parent = self.root / "cache"
        current = short_cache_directory(self.root, batch, "quality", "a" * 64, parent)
        (current / "identity.json").unlink()
        (current / ".0123456789ab.tmp").write_text('{"partial":', encoding="utf-8")
        self.assertEqual(short_cache_directory(self.root, batch, "quality", "a" * 64, parent), current)
        self.assertFalse((current / ".0123456789ab.tmp").exists())
        (current / "identity.json").unlink()
        (current / "unexpected.jsonl").write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "缺少完整指纹"):
            short_cache_directory(self.root, batch, "quality", "a" * 64, parent)

    def fixture(self, short=True):
        batch = allocate_id("b", self.root, now=DAY) if short else "legacy"
        run_id = allocate_id("r", self.root, now=DAY)
        task = allocate_id("t", self.root, now=DAY)
        source = self.root / "source"
        case, name = "业务用例-" + "C" * 60, "同名轨迹-" + "X" * 60
        raw_trajectory({"output_dir": str(source)}, case_id=case, name=name)
        files = [{"path": p.relative_to(source).as_posix(), "size": p.stat().st_size,
                  "sha256": workbook_digest(p)} for p in source.rglob("*") if p.is_file()]
        run = {"batch_id": batch, "collection_run_id": run_id, "created_at": DAY.isoformat(),
               "batch_tasks": {case: {"task_id": task, "source_result_id": None, "task": "打开设置"}}}
        manifest = {"batch_id": batch, "collection_run_id": run_id,
                    "trajectories": [{"collection_case_id": case, "source_trajectory_id": name,
                                      "relative_dir": case + "/" + name, "files": files}], "errors": []}
        return run, manifest, source

    def test_short_archive_preserves_source_bytes_and_replays_stable_identity(self):
        run, source_manifest, source = self.fixture()
        mapped = map_collection_manifest(self.root, run, source_manifest)
        self.assertEqual(mapped["mapping"]["original_manifest"], source_manifest)
        self.assertEqual(mapped, map_collection_manifest(self.root, run, source_manifest))
        destination = self.root / "local"
        for incoming, relative in mapped["mapping"]["files"].items():
            path = destination / relative
            io_path(path.parent).mkdir(parents=True, exist_ok=True)
            shutil.copyfile(io_path(source / incoming), io_path(path))
            self.assertEqual(workbook_digest(source / incoming), workbook_digest(path))
        validate_local_manifest(self.root, run, mapped, root_override=destination)
        trajectory = mapped["manifest"]["trajectories"][0]
        self.assertEqual(len(trajectory["relative_dir"].split("/")), 2)
        self.assertLess(len(trajectory["relative_dir"]), 32)
        row = {"task_id": trajectory["task_id"], "collection_run_id": run["collection_run_id"],
               "trajectory_id": "tr_" + "f" * 64,
               "image": "runs/" + run["collection_run_id"] + "/" + trajectory["relative_dir"] + "/step001_vla_input.jpg"}
        payload = {"sheets": {"VLA trajectories": [row]}}
        converted = adapt_conversion_identity(payload, mapped["manifest"], self.root)
        self.assertEqual(converted["sheets"]["VLA trajectories"][0]["trajectory_id"], trajectory["trajectory_id"])
        # Corrupt copies and modified mappings must not become completed inputs.
        target = destination / next(iter(mapped["mapping"]["files"].values()))
        io_path(target).write_bytes(b"broken")
        with self.assertRaises(ValueError):
            validate_local_manifest(self.root, run, mapped, root_override=destination)

    def test_legacy_identity_and_paths_are_unchanged(self):
        run, manifest, _ = self.fixture(short=False)
        mapped = map_collection_manifest(self.root, run, manifest)
        self.assertEqual(mapped["storage_layout"], "legacy")
        self.assertTrue(all(k == v for k, v in mapped["mapping"]["files"].items()))
        payload = {"sheets": {"VLA trajectories": []}}
        self.assertIs(adapt_conversion_identity(payload, mapped["manifest"], self.root), payload)

    def test_new_batch_with_unadapted_phone_layout_keeps_source_paths(self):
        run, manifest, _ = self.fixture()
        from backend.collection_runs import CollectionRunStore
        normalized = CollectionRunStore(self.root)._normalize(run, manifest, run["created_at"])
        trajectory = normalized["trajectories"][0]
        image = "runs/" + run["collection_run_id"] + "/" + trajectory["relative_dir"] + "/step001_vla_input.jpg"
        payload = {"sheets": {"VLA trajectories": [{"task_id": trajectory["task_id"],
                   "collection_run_id": run["collection_run_id"], "image": image, "trajectory_id": "legacy-derived"}]}}
        first = adapt_conversion_identity(payload, normalized, self.root)
        second = adapt_conversion_identity(payload, normalized, self.root)
        self.assertEqual(first, second)
        self.assertEqual(first["sheets"]["VLA trajectories"][0]["image"], image)
        mapped = map_collection_manifest(self.root, run, manifest)
        self.assertEqual(first["sheets"]["VLA trajectories"][0]["trajectory_id"],
                         mapped["manifest"]["trajectories"][0]["trajectory_id"])

    def test_mapping_rejects_forged_destination_and_source_path(self):
        run, manifest, _ = self.fixture()
        mapped = map_collection_manifest(self.root, run, manifest)
        mapped["manifest"]["trajectories"][0]["relative_dir"] = "../escape"
        with self.assertRaisesRegex(ValueError, "冻结路径"):
            validate_local_manifest(self.root, run, mapped)
        manifest["trajectories"][0]["files"][0]["path"] = "../escape"
        with self.assertRaises(ValueError):
            map_collection_manifest(self.root, run, manifest)


if __name__ == "__main__":
    unittest.main()
