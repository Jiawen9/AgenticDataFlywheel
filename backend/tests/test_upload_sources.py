"""Frozen selection -> complete directories, without validating raw evidence."""
from __future__ import annotations

from copy import deepcopy
import hashlib
from pathlib import Path
import tempfile
import unittest

from openpyxl import Workbook

from backend.data_publishing.service import DatasetReleaseRegistry
from backend.data_publishing.upload_sources import resolve_upload_sources, UploadSourceError
from backend.data_store import ArtifactStore
from backend.data_store.release_provenance import freeze_release_provenance


class UploadSourcesTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="upload-sources-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name) / "中文 平台"
        self.root.mkdir()
        self.registry = DatasetReleaseRegistry(project_root=self.root, data_root=self.root,
            releases_file=self.root / "system" / "unused.json")
        self.store = ArtifactStore(self.root)
        self.release_id = "rel261110-001"
        self.release_dir = self.root / "releases" / self.release_id
        self.release_dir.mkdir(parents=True)
        self.release = {"release_id": self.release_id, "name": "中文发布", "excel_paths": [],
                        "source_refs": [], "batch_ids": [], "trajectory_paths": ["wrong/shared/root"]}
        self.directories = []

    def add_batch(self, batch="batch_001", selected=(2, 7), *, hidden_ids=False,
                  same_folder_names=False, only_identity=False, images=None):
        raw = self.root / "raw" / "collection_batches" / batch
        raw.mkdir(parents=True)
        rows = []
        for repeat in range(1, 11):
            name = "手机__原轨迹" if same_folder_names else f"手机__轨迹{repeat:02}"
            relative = Path("runs") / f"run{repeat}" / "task-01" / name
            directory = raw / relative
            directory.mkdir(parents=True)
            self.directories.append(directory)
            # Deliberately incomplete/invalid evidence: transfer must not decode
            # images, validate XML or require the files mentioned by the table.
            (directory / "step001_vla_input.jpg").write_bytes(b"not-an-image")
            (directory / "step001_vla_input_ui.xml").write_text("broken XML", encoding="utf-8")
            (directory / "模型响应.json").write_text("incomplete", encoding="utf-8")
            (directory / "嵌套").mkdir()
            (directory / "嵌套" / "logprobs.json").write_bytes(b"arbitrary bytes")
            image = (relative / "step001_vla_input.jpg").as_posix()
            if images and repeat in images:
                image = images[repeat]
            rows.append({"image": image, "collection_case_id": "用例01", "task_id": "task-01",
                         "trajectory_id": f"trajectory-{batch}-{repeat}", "文件夹名": name,
                         "action": "invalid model response", "group_export": False})
        columns = ["image", "action"] if hidden_ids else list(rows[0])
        if only_identity:
            columns = ["trajectory_id", "action"]
        table = {"sheets": {"steps": rows}, "columns": {"steps": list(rows[0])}}
        conversion = self.store.publish(batch, "01_conversion", {**table, "raw_root": str(raw)},
                                        metadata={"raw_root": str(raw)})
        tree = self.store.publish(batch, "04_tree", {"raw_root": str(raw), "source_annotation": table},
                                  source_refs=[conversion])
        sequence = len(self.release["excel_paths"]) + 1
        path = self.release_dir / f"{sequence:03d}" / "发布数据表.xlsx"
        path.parent.mkdir()
        book = Workbook()
        book.active.append(columns)
        for index in selected:
            book.active.append([rows[index - 1][name] for name in columns])
        book.save(path)
        book.close()
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        # Frozen metadata deliberately contains every group, with all export
        # flags false. Only the actual published workbook determines selection.
        groups = [{"task_id": "task-01", "export": False, "rows": [row]} for row in rows]
        artifact = self.store.publish(batch, "07_cot", {"batch_id": batch, "groups": groups},
                                      workbooks={"full_dataset.xlsx": path}, source_refs=[tree])
        self.release["excel_paths"].append({"path": self.registry.project_path(path),
                                            "filename": path.name, "sha256": digest})
        self.release["source_refs"].append({"kind": "correction_session", "artifact": artifact})
        self.release["batch_ids"].append(batch)
        return raw, rows, path

    def freeze(self):
        self.release = freeze_release_provenance(self.root, self.release, self.release_dir, store=self.store)
        return self.release

    def test_ten_choose_two_full_directories_and_originals_unchanged(self):
        raw, _, _ = self.add_batch()
        release = self.freeze()
        before = {str(file): file.read_bytes() for file in self.root.rglob("*") if file.is_file()}
        items = resolve_upload_sources(self.registry, release)
        trajectories = [item for item in items if item["kind"] == "trajectory"]
        self.assertEqual(len(items), 3)
        self.assertEqual(items[0]["destination_relative"], "batch_001/发布表格/001")
        self.assertEqual(items[0]["size_bytes"], Path(items[0]["file_path"]).stat().st_size)
        self.assertEqual({Path(item["source_dir"]) for item in trajectories},
                         {self.directories[1], self.directories[6]})
        for item in trajectories:
            self.assertTrue(item["destination_relative"].startswith("batch_001/用例01/"))
            self.assertTrue((Path(item["source_dir"]) / "嵌套" / "logprobs.json").is_file())
            self.assertNotEqual(Path(item["source_dir"]), raw)
        self.assertEqual(before, {str(file): file.read_bytes() for file in self.root.rglob("*") if file.is_file()})
        self.assertEqual(items, resolve_upload_sources(self.registry, release))

    def test_full_dataset_uses_all_excel_rows_despite_frozen_export_false(self):
        self.add_batch(selected=tuple(range(1, 11)))
        items = resolve_upload_sources(self.registry, self.freeze())
        self.assertEqual(len([item for item in items if item["kind"] == "trajectory"]), 10)

    def test_hidden_identity_columns_resolve_from_frozen_image_rows(self):
        self.add_batch(hidden_ids=True)
        items = resolve_upload_sources(self.registry, self.freeze())
        trajectories = [item for item in items if item["kind"] == "trajectory"]
        self.assertEqual([item["case_id"] for item in trajectories], ["用例01", "用例01"])
        self.assertEqual([item["trajectory_id"] for item in trajectories],
                         ["trajectory-batch_001-2", "trajectory-batch_001-7"])

    def test_identity_only_rows_use_precise_frozen_directory(self):
        self.add_batch(only_identity=True)
        items = resolve_upload_sources(self.registry, self.freeze())
        self.assertEqual({Path(item["source_dir"]) for item in items if item["kind"] == "trajectory"},
                         {self.directories[1], self.directories[6]})

    def test_multiple_batches_same_directory_names_no_collisions(self):
        self.add_batch("batch_A", same_folder_names=True)
        self.add_batch("batch_B", same_folder_names=True)
        items = resolve_upload_sources(self.registry, self.freeze())
        self.assertEqual(len(items), 6)
        self.assertEqual(len({item["destination_relative"].casefold() for item in items}), 6)
        self.assertEqual([item["destination_relative"] for item in items if item["kind"] == "excel"],
                         ["batch_A/发布表格/001", "batch_B/发布表格/002"])

    def test_repeated_step_rows_deduplicate_trajectory(self):
        self.add_batch(selected=(2, 2, 7, 7))
        items = resolve_upload_sources(self.registry, self.freeze())
        self.assertEqual(len(items), 3)

    def test_missing_raw_directory_is_explicit_without_shared_root_fallback(self):
        self.add_batch()
        release = self.freeze()
        selected = self.directories[1]
        selected.rename(selected.with_name("renamed"))
        with self.assertRaisesRegex(FileNotFoundError, "轨迹目录不存在"):
            resolve_upload_sources(self.registry, release)

    def test_missing_provenance_never_uses_broad_trajectory_paths(self):
        self.add_batch()
        release = deepcopy(self.freeze())
        release["source_refs"][0].pop("artifact")
        with self.assertRaisesRegex(UploadSourceError, "不能用整个共享轨迹目录代替"):
            resolve_upload_sources(self.registry, release)

    def test_escaping_paths_are_not_uploaded(self):
        self.add_batch(selected=(2,), images={2: "../../outside/step001.jpg"})
        with self.assertRaisesRegex(UploadSourceError, "越界"):
            resolve_upload_sources(self.registry, self.freeze())

    def test_excel_only_does_not_require_trajectory_source(self):
        self.add_batch()
        release = deepcopy(self.freeze())
        release.pop("provenance")
        release["source_refs"][0].pop("artifact")
        items = resolve_upload_sources(self.registry, release, include_trajectories=False)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["kind"], "excel")

    def test_unbatched_external_excel_uses_release_root_without_inventing_batch(self):
        self.add_batch()
        release = deepcopy(self.freeze())
        release.update(source_kind="external_import", batch_ids=[],
                       source_refs=[{"kind": "external_import", "id": "external-one"}])
        release.pop("provenance")
        items = resolve_upload_sources(self.registry, release, include_trajectories=False)
        self.assertEqual(len(items), 1)
        self.assertIsNone(items[0]["batch_id"])
        self.assertEqual(items[0]["destination_relative"], f"{self.release_id}/发布表格/001")
        with self.assertRaisesRegex(UploadSourceError, "外部 Excel"):
            resolve_upload_sources(self.registry, release)
        release.pop("source_kind")
        release["source_refs"] = []
        with self.assertRaisesRegex(UploadSourceError, "冻结批次归属"):
            resolve_upload_sources(self.registry, release)

    def test_external_import_excluded_and_modified_excel_rejected(self):
        _, _, path = self.add_batch()
        release = self.freeze()
        with self.assertRaisesRegex(UploadSourceError, "外部 Excel"):
            resolve_upload_sources(self.registry, {**release, "source_kind": "external_import"})
        with self.assertRaisesRegex(UploadSourceError, "外部 Excel"):
            resolve_upload_sources(self.registry, {**release, "source_kind": "external_manual"})
        path.write_bytes(b"changed")
        with self.assertRaisesRegex(UploadSourceError, "SHA256"):
            resolve_upload_sources(self.registry, release)

    def test_real_pipeline_release_uses_frozen_selected_group(self):
        from backend.tests.test_pipeline_release import PipelineReleaseTests
        fixture = PipelineReleaseTests("test_threshold_tie_stability_and_automatic_no_session")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        selected = fixture.raw / "A" / "a-first"
        selected.mkdir(parents=True)
        (selected / "unreferenced-response.json").write_bytes(b"also upload this")
        release = fixture.publish(fixture.pipeline())
        # An upload must not look up mutable session/export controls or infer
        # directories from the release's old aggregate trajectory_paths field.
        fixture.registry.session_loader = lambda _: (_ for _ in ()).throw(AssertionError("mutable session read"))
        items = resolve_upload_sources(fixture.registry, release)
        self.assertEqual(len(items), 2)
        self.assertEqual(Path(items[1]["source_dir"]), selected)
        self.assertEqual(items[1]["case_id"], "case-A")
        self.assertEqual(items[1]["trajectory_id"], "a-first")


if __name__ == "__main__":
    unittest.main()
