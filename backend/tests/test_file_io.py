"""Long filesystem names must not change stored batch and artifact identities."""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from backend.data_store import ArtifactStore
from backend.data_store.paths import contained_path
from backend.file_io import (
    io_path, is_link_or_junction, iterdir, logical_path, resolve_path,
    rglob, windows_io_path,
)


@contextmanager
def legacy_path_limit():
    """Reproduce a Windows host whose plain-path operations fail at 260."""
    original_open, original_stat, original_scandir = Path.open, Path.stat, os.scandir

    def check(path):
        value = os.fspath(path)
        if isinstance(value, str) and len(value) >= 260 and not value.startswith("\\\\?\\"):
            raise FileNotFoundError(2, "legacy Windows path limit", value)

    def open_file(path, *args, **kwargs):
        check(path)
        return original_open(path, *args, **kwargs)

    def stat_file(path, *args, **kwargs):
        check(path)
        return original_stat(path, *args, **kwargs)

    def scan(path):
        if not isinstance(path, int):
            check(path)
        return original_scandir(path)

    with patch.object(Path, "open", open_file), patch.object(Path, "stat", stat_file), patch("os.scandir", scan):
        yield


class WindowsPathSpellingTests(unittest.TestCase):
    def test_drive_unc_and_existing_prefix(self):
        for source, expected in [
            (r"C:\数据\批次\..\轨迹.json", r"\\?\C:\数据\轨迹.json"),
            ("D:/data/task.json", r"\\?\D:\data\task.json"),
            (r"\\server\share\轨迹\input.png", r"\\?\UNC\server\share\轨迹\input.png"),
            (r"\\server\share", r"\\?\UNC\server\share"),
            (r"\\?\C:\data\input.png", r"\\?\C:\data\input.png"),
            (r"\\?\UNC\server\share\input.png", r"\\?\UNC\server\share\input.png"),
        ]:
            with self.subTest(source=source):
                self.assertEqual(windows_io_path(source), expected)

    def test_relative_device_and_incomplete_unc_rejected(self):
        for source in ("relative/file", r"C:file", r"\data\file", r"\\.\C:\file",
                       r"\\?\GLOBALROOT\Device\file", r"\\server", "\\\\server\\"):
            with self.subTest(source=source), self.assertRaises(ValueError):
                windows_io_path(source)


@unittest.skipUnless(os.name == "nt", "Windows filesystem regression")
class WindowsLongFilesystemTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="adf-longio-")
        self.root = Path(self.directory.name)
        self.addCleanup(self.directory.cleanup)
        # Use extended paths when removing these deliberately long fixtures.
        self.addCleanup(lambda: shutil.rmtree(io_path(self.root), ignore_errors=True))

    def file_of_length(self, length):
        parent = self.root / "中文任务"
        while length - len(str(parent)) > 90:
            parent /= "轨迹_" + "x" * 30
        return parent / ("f" * (length - len(str(parent)) - 6) + ".json")

    def test_260_261_and_deeper_files_keep_normal_names_and_bytes(self):
        paths = [self.file_of_length(length) for length in (260, 261, 420)]
        for path in paths:
            io_path(path.parent).mkdir(parents=True, exist_ok=True)
            io_path(path).write_bytes('{"步骤":"点击"}'.encode("utf-8"))
        self.assertEqual([len(str(path)) for path in paths], [260, 261, 420])
        with legacy_path_limit():
            for path in paths:
                with self.assertRaises(FileNotFoundError):
                    path.read_bytes()
                self.assertEqual(json.loads(io_path(path).read_bytes()), {"步骤": "点击"})
                self.assertEqual(resolve_path(io_path(path)), path)
                self.assertEqual(logical_path(io_path(path)), path)
                self.assertIn(path, list(iterdir(path.parent)))
            self.assertEqual(set(rglob(self.root, "*.json")), set(paths))
        self.assertTrue(all(not str(path).startswith("\\\\?\\") for path in rglob(self.root)))

    def test_paired_artifacts_and_recovery_with_long_batch_paths(self):
        root = self.root / ("工作区" + "x" * 65)
        batch = "batch-" + "x" * 110
        store = ArtifactStore(root)
        rows = [{"任务": "中文原件", "image": str(self.file_of_length(420))}]
        entries = [
            {"stage": "03_observation", "payload": {"rows": rows}, "tables": {"步骤": rows}},
            {"stage": "04_tree", "payload": {"tree": "原值"}, "source_stages": ["03_observation"]},
        ]
        with legacy_path_limit():
            first = store.publish_many(batch, entries)
            path = store.resolve_file(first[0], "result.json")
            self.assertGreaterEqual(len(str(path)), 260)
            self.assertEqual(store.read_payload(first[0]), {"rows": rows})
            self.assertNotIn("\\\\?\\", json.dumps(first, ensure_ascii=False))
            self.assertTrue(store.read_file(first[0], "result.xlsx").startswith(b"PK"))
            original_commit = store.records.put_many
            entries[0]["payload"] = {"rows": "已修改"}
            with patch.object(store.records, "put_many", side_effect=RuntimeError("commit interrupted")):
                with self.assertRaisesRegex(RuntimeError, "interrupted"):
                    store.publish_many(batch, entries)
            self.assertEqual(store.read_payload(first[0]), {"rows": rows})
            self.assertEqual(store.read_payload(first[1]), {"tree": "原值"})

            def acknowledgement_lost(*args, **kwargs):
                original_commit(*args, **kwargs)
                raise RuntimeError("acknowledgement lost")

            with patch.object(store.records, "put_many", acknowledgement_lost):
                with self.assertRaisesRegex(RuntimeError, "acknowledgement lost"):
                    store.publish_many(batch, entries)
            restarted = ArtifactStore(root)
            restarted.recover()
            current = restarted.get(batch, "03_observation")
            self.assertEqual(restarted.read_payload(current), {"rows": "已修改"})
            self.assertEqual(restarted.publish_many(batch, entries)[0], current)
            self.assertEqual(len(restarted.list(batch)), 2)

    def test_extended_spelling_does_not_bypass_containment_or_junction_check(self):
        inside = self.file_of_length(330)
        self.assertEqual(contained_path(self.root, str(io_path(inside))), inside)
        for outside in (self.root.parent / "outside", self.root / ".." / "outside"):
            with self.assertRaises(ValueError):
                contained_path(self.root, str(io_path(outside)))
        with patch.object(Path, "lstat") as lstat:
            lstat.return_value.st_mode = 0
            lstat.return_value.st_file_attributes = 0x400
            self.assertTrue(is_link_or_junction(inside))


if __name__ == "__main__":
    unittest.main()
