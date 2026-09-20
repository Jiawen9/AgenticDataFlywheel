from __future__ import annotations

import hashlib
import json
import tempfile
from copy import deepcopy
from unittest.mock import patch
import unittest
from pathlib import Path

from backend.data_store import ArtifactStore, RecordStore
from backend.data_store.migrate_single_artifact import apply, check, rollback, verify, _inventory, _paths
from backend.batch_results import current_tree_payload, current_quality_payload, resolve_current_batch_id


class LegacyFixture:
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir()
        self.records = RecordStore(root)
        self.number = 0
        self.batch = "fixture-batch"
        self.annotation_payload = {"schema_version": 1,
            "columns": {"VLA trajectories": ["task_id", "trajectory_id", "image", "action"]},
            "sheets": {"VLA trajectories": [
                {"task_id": task, "trajectory_id": task + "-1", "image": task + "/1.png", "action": "wait"}
                for task in ("A", "B")]}}
        self.collection = self.add("00_collection", {"snapshot": {"tasks": [
            {"task_id": task, "collection_case_id": task, "task": "Frozen " + task} for task in ("A", "B")]}})
        self.conversion = self.add("01_conversion", self.annotation_payload, [self.collection,
            {"kind": "raw_trajectories", "path": str(root / "raw")}])
        self.annotation = self.add("02_annotation", self.annotation_payload, [self.conversion],
                                   metadata={"raw_root": str(root / "raw")})

    def add(self, stage, payload, refs=None, metadata=None):
        self.number += 1
        version = f"legacy-{self.number:04d}"
        directory = self.root / "batches" / self.batch / stage / version
        directory.mkdir(parents=True)
        path = directory / "result.json"
        raw = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode()
        path.write_bytes(raw)
        manifest = {"schema_version": 1, "batch_id": self.batch, "stage": stage, "version": version,
            "created_at": f"2026-09-15T00:00:{self.number:02d}Z", "source_refs": refs or [],
            "metadata": metadata or {}, "files": [{"name": "result.json", "kind": "json",
                "path": path.relative_to(self.root).as_posix(), "size": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}]}
        (directory / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        self.records.put("artifacts", f"{self.batch}/{stage}/{version}", manifest, expected_revision=0)
        return manifest

    def tree(self, task, run_id, marker=None):
        observed = self.add("03_observation", {"trajectories": [
            {"task_id": task, "trajectory_id": task + "-1", "steps": [{"step": 1, "observation": marker or run_id}]}]},
            [self.annotation], metadata={"run_id": run_id})
        quality = {"schema_version": 1, "columns": {}, "sheets": {
            "Tasks": [{"task_id": task}], "Trajectories": [{"task_id": task, "trajectory_id": task + "-1"}],
            "Steps": [{"trajectory_id": task + "-1", "step_id": 1, "observation": marker or run_id}]}}
        tree = {"task_id": task, "model_name": "fake-model", "marker": marker or run_id,
                "source_trajectories": [{"trajectory_id": task + "-1", "steps": [{"step": 1}]}]}
        ref = self.add("04_tree", {"schema_version": 1, "run_id": run_id, "trees": {task: tree}, "quality_input": quality},
                       [observed], metadata={"run_id": run_id})
        run_dir = self.root / "system" / "trajectory_tree_runs" / run_id
        run_dir.mkdir(parents=True)
        (run_dir / "manifest.json").write_text(json.dumps({"run_id": run_id, "batch_id": self.batch,
            "model_name": "fake-model", "tasks": [{"task_id": task, "trajectory_count": 1}]}), encoding="utf-8")
        return ref

    def quality(self, task, run_id, tree_ref, score):
        return self.add("05_quality", {"run_id": run_id, "tasks": [{"task_id": task, "run_id": run_id,
            "average_score": score, "evaluations": {}, "rubric": {}}]}, [tree_ref])

    def session(self, session_id, run_id, *, edited=False, storage_batch=True):
        value = {"session_id": session_id, "tree_run_id": run_id, "updated_at": "2026-09-16T00:00:00Z",
                 "row_edits": {"2": {"summary": "human correction"}} if edited else {},
                 "cot": {"2": {"status": "succeeded", "thought": "saved thought"}} if edited else {},
                 "group_exports": {"old-group": True}, "exports": [], "selection": {}}
        if storage_batch:
            value["storage_batch_id"] = self.batch
        snapshot = {"row_count": 1, "groups": [{"group_id": "old-group", "task_id": "A", "task": "A",
            "meta_task": "A-1", "export": True, "rows": [{"excel_row": 2, "task_id": "A", "step": 1,
            "image": "A/1.png", "summary": "before", "values": {"summary": "before"}}]}]}
        directory = self.root / "system" / "trajectory_correction" / "inputs" / session_id
        directory.mkdir(parents=True)
        (directory / "snapshot.json").write_text(json.dumps(snapshot), encoding="utf-8")
        self.records.put("correction_sessions", session_id, value)
        return value


class SingleArtifactMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "data"
        self.fixture = LegacyFixture(self.root)

    def history(self):
        f = self.fixture
        old_a = f.tree("A", "run-a-old", "old-a")
        f.quality("A", "run-a-old", old_a, 1)
        b = f.tree("B", "run-b", "valid-b")
        f.quality("B", "run-b", b, 4)
        new_a = f.tree("A", "run-a-current", "valid-a")
        f.quality("A", "run-a-current", new_a, 5)

    def test_apply_merges_task_history_and_preserves_exact_backup(self):
        self.history()
        before = _inventory(self.root)
        report = check(self.root)
        self.assertGreater(report["legacy_versions"], 3)
        result = apply(self.root)
        self.assertEqual(result["status"], "verified")
        backup = Path(result["backup"])
        self.assertFalse(backup.is_relative_to(self.root / "batches"))
        self.assertEqual(_inventory(backup), before)
        store = ArtifactStore(self.root)
        for ref in store.list():
            directory = self.root / "batches" / ref["batch_id"] / ref["stage"]
            self.assertEqual(ref["schema_version"], 2)
            self.assertFalse(any(path.is_dir() for path in directory.iterdir()))
            store.read_payload(ref)
        trees = current_tree_payload(self.fixture.batch, self.root)
        self.assertEqual({task: value["marker"] for task, value in trees["trees"].items()}, {"A": "valid-a", "B": "valid-b"})
        scores = {item["task_id"]: item["average_score"] for item in current_quality_payload(self.fixture.batch, self.root)["tasks"]}
        self.assertEqual(scores, {"A": 5, "B": 4})
        self.assertIsNone(resolve_current_batch_id("run-a-old", self.root))
        self.assertEqual(resolve_current_batch_id("run-a-current", self.root), self.fixture.batch)
        self.assertEqual(resolve_current_batch_id("run-b", self.root), self.fixture.batch)
        self.assertFalse((self.root / "system" / "trajectory_tree_runs" / "run-b").exists())
        self.assertEqual(verify(self.root)["status"], "verified")

    def test_conversion_and_collection_follow_chosen_annotation_lineage(self):
        self.history()
        later_collection = self.fixture.add("00_collection", {"snapshot": {"tasks": [{"task_id": "A", "task": "Wrong newer goal"}]}})
        self.fixture.add("01_conversion", {"schema_version": 1, "sheets": {"VLA trajectories": []}}, [later_collection])
        apply(self.root)
        store = ArtifactStore(self.root)
        current_conversion = store.read_payload(store.get(self.fixture.batch, "01_conversion"))
        self.assertEqual(current_conversion, self.fixture.annotation_payload)
        collection = store.read_payload(store.get(self.fixture.batch, "00_collection"))
        self.assertEqual(collection["snapshot"]["tasks"][0]["task"], "Frozen A")

    def test_old_empty_session_removed_current_manual_work_is_preserved(self):
        self.history()
        f = self.fixture
        f.session("old-empty", "run-a-old")
        original = f.session("current-edited", "run-a-current", edited=True)
        apply(self.root)
        records = RecordStore(self.root)
        self.assertIsNone(records.get("correction_sessions", "old-empty"))
        current = records.get("correction_sessions", "current-edited")
        self.assertEqual(current["row_edits"], original["row_edits"])
        self.assertEqual(current["cot"], original["cot"])
        self.assertEqual(current["tree_run_id"], f.batch)
        self.assertFalse((self.root / "system/trajectory_correction/inputs/old-empty").exists())

    def test_legacy_session_without_storage_batch_is_mapped_through_run(self):
        self.history()
        original = self.fixture.session("old-format", "run-a-current", edited=True, storage_batch=False)
        apply(self.root)
        current = RecordStore(self.root).get("correction_sessions", "old-format")
        self.assertEqual(current.get("storage_batch_id"), self.fixture.batch)
        self.assertEqual(current["row_edits"], original["row_edits"])

    def test_running_tree_job_blocks_migration_before_backup(self):
        self.fixture.records.put("tree_jobs", "active", {"job_id": "active", "batch_id": self.fixture.batch, "status": "running"})
        with self.assertRaisesRegex(ValueError, "Stop"):
            apply(self.root)
        _, journal = _paths(self.root)
        self.assertFalse((journal / "backup").exists())

    def test_rollback_restores_bytes_but_refuses_new_business_writes(self):
        self.history()
        before = _inventory(self.root)
        apply(self.root)
        self.assertEqual(rollback(self.root)["status"], "rolled_back")
        self.assertEqual(_inventory(self.root), before)

    def test_rollback_refuses_new_business_writes(self):
        self.history()
        apply(self.root)
        RecordStore(self.root).put("unrelated_business", "new", {"saved": True})
        with self.assertRaisesRegex(ValueError, "changed"):
            rollback(self.root)

    def test_annotation_change_keeps_only_matching_task_history(self):
        self.history()
        changed = deepcopy(self.fixture.annotation_payload)
        changed["sheets"]["VLA trajectories"][0]["action"] = "click"
        conversion = self.fixture.add("01_conversion", changed, [self.fixture.collection])
        self.fixture.add("02_annotation", changed, [conversion], metadata={"raw_root": str(self.root / "raw")})
        apply(self.root)
        self.assertEqual(set(current_tree_payload(self.fixture.batch, self.root)["trees"]), {"B"})
        self.assertEqual([item["task_id"] for item in current_quality_payload(self.fixture.batch, self.root)["tasks"]], ["B"])
        self.assertIsNone(resolve_current_batch_id("run-a-current", self.root))

    def test_partially_superseded_multi_task_run_alias_is_retired(self):
        f = self.fixture
        observed = f.add("03_observation", {"trajectories": [
            {"task_id": task, "trajectory_id": task + "-1", "steps": []} for task in ("A", "B")]}, [f.annotation])
        quality = {"schema_version": 1, "columns": {}, "sheets": {
            "Tasks": [{"task_id": task} for task in ("A", "B")],
            "Trajectories": [{"task_id": task, "trajectory_id": task + "-1"} for task in ("A", "B")],
            "Steps": []}}
        f.add("04_tree", {"run_id": "old-ab", "trees": {
            task: {"task_id": task, "marker": "old-" + task, "source_trajectories": []} for task in ("A", "B")},
            "quality_input": quality}, [observed], metadata={"run_id": "old-ab"})
        f.tree("A", "new-a", "new-A")
        apply(self.root)
        current = current_tree_payload(f.batch, self.root)
        self.assertEqual(current["trees"]["A"]["marker"], "new-A")
        self.assertEqual(current["trees"]["B"]["marker"], "old-B")
        self.assertIsNone(resolve_current_batch_id("old-ab", self.root))
        self.assertTrue(RecordStore(self.root).get("batch_run_aliases", "old-ab")["retired"])
        self.assertEqual(resolve_current_batch_id("new-a", self.root), f.batch)

    def test_backup_copy_corruption_is_detected_before_migration_changes(self):
        import shutil
        self.history()
        before = _inventory(self.root)
        copytree = shutil.copytree
        def corrupt(source, destination, *args, **kwargs):
            result = copytree(source, destination, *args, **kwargs)
            if Path(destination).name == "backup":
                next((Path(destination) / "batches").rglob("result.json")).write_text("corrupt", encoding="utf-8")
            return result
        with patch("backend.data_store.migrate_single_artifact.shutil.copytree", side_effect=corrupt):
            with self.assertRaisesRegex(ValueError, "checksum"):
                apply(self.root)
        self.assertEqual(_inventory(self.root), before)

    def runtime_leftovers(self):
        self.history()
        f = self.fixture
        preprocessing = self.root / "system" / "preprocessing"
        preprocessing.mkdir(parents=True, exist_ok=True)
        removable = []
        annotation_key = None
        for stem, ref in (("trajectories_to_excel", f.conversion), ("annotated_trajectories", f.annotation)):
            workbook = preprocessing / (stem + ".xlsx")
            workbook.write_bytes(("old workbook " + stem).encode())
            sidecar = workbook.with_suffix(".json")
            sidecar.write_text(json.dumps({**f.annotation_payload, "source_ref": ref}), encoding="utf-8")
            removable.extend([workbook, sidecar])
            if ref["stage"] == "02_annotation":
                annotation_key = hashlib.sha256(str(workbook.resolve()).encode()).hexdigest()
                f.records.put("trajectory_annotations", annotation_key,
                    {"path": str(workbook.resolve()), "payload": f.annotation_payload, "artifact": ref})
        validation = self.root / "batches" / f.batch / "validation"
        validation.mkdir()
        (validation / "acceptance.log").write_text("old validation output", encoding="utf-8")
        removable.append(validation / "acceptance.log")
        preserved = {}
        for relative, payload in (("raw/input.json", {"raw": "keep"}),
                                  ("system/preprocessing/job/input.json", {"batch_id": f.batch, "input": "keep"}),
                                  ("system/preprocessing/unrelated.json", {"source_ref": {
                                      "batch_id": "other-batch", "stage": "02_annotation", "version": "other-v"},
                                      "sheets": {"VLA trajectories": []}})):
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload), encoding="utf-8")
            preserved[path] = path.read_bytes()
        other_workbook = preprocessing / "unrelated.xlsx"
        other_workbook.write_bytes(b"unrelated workbook")
        preserved[other_workbook] = other_workbook.read_bytes()
        f.records.put("trajectory_annotations", "other-index", {"path": str(other_workbook),
            "artifact": {"batch_id": "other-batch", "stage": "02_annotation", "version": "other-v"}})
        for run_id in ("run-a-old", "run-a-current", "run-b", "other-run"):
            f.records.put("quality_results", run_id + ":A", {"run_id": run_id, "task_id": "A", "marker": "old record"})
            f.records.put("quality_manifests", run_id, {"run_id": run_id, "tasks": [{"task_id": "A"}]})
        return removable, preserved, annotation_key

    def assert_runtime_cleaned(self, result, removable, preserved, annotation_key):
        backup = Path(result["backup"])
        records = RecordStore(self.root)
        for path in removable:
            self.assertFalse(path.exists(), str(path))
            self.assertTrue((backup / path.relative_to(self.root)).is_file(), str(path))
        self.assertFalse((self.root / "batches" / self.fixture.batch / "validation").exists())
        for path, contents in preserved.items():
            self.assertEqual(path.read_bytes(), contents)
        self.assertIsNone(records.get("trajectory_annotations", annotation_key))
        self.assertIsNotNone(RecordStore(backup).get("trajectory_annotations", annotation_key))
        self.assertIsNotNone(records.get("trajectory_annotations", "other-index"))
        for run_id in ("run-a-old", "run-a-current", "run-b"):
            self.assertIsNone(records.get("quality_results", run_id + ":A"))
            self.assertIsNone(records.get("quality_manifests", run_id))
            self.assertIsNotNone(RecordStore(backup).get("quality_manifests", run_id))
        self.assertIsNotNone(records.get("quality_results", "other-run:A"))
        self.assertIsNotNone(records.get("quality_manifests", "other-run"))
        self.assertEqual({item["task_id"] for item in current_quality_payload(self.fixture.batch, self.root)["tasks"]}, {"A", "B"})
        _, journal = _paths(self.root)
        state = json.loads((journal / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["runtime_cleanup_version"], 2)

    def test_runtime_cleanup_only_removes_known_migrated_outputs(self):
        removable, preserved, annotation_key = self.runtime_leftovers()
        before = _inventory(self.root)
        result = apply(self.root)
        self.assert_runtime_cleaned(result, removable, preserved, annotation_key)
        self.assertEqual(_inventory(Path(result["backup"])), before)

    def test_verified_v1_apply_finishes_runtime_cleanup_from_original_backup(self):
        removable, preserved, annotation_key = self.runtime_leftovers()
        with patch("backend.data_store.migrate_single_artifact._clean_legacy_runtime"):
            first = apply(self.root)
        backup_before = _inventory(Path(first["backup"]))
        _, journal = _paths(self.root)
        state_path = journal / "state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["runtime_cleanup_version"] = 1
        state_path.write_text(json.dumps(state), encoding="utf-8")
        result = apply(self.root)
        self.assert_runtime_cleaned(result, removable, preserved, annotation_key)
        self.assertEqual(_inventory(Path(result["backup"])), backup_before)
        self.assertEqual(verify(self.root)["status"], "verified")

    def test_runtime_cleanup_upgrade_refuses_new_business_writes(self):
        self.runtime_leftovers()
        with patch("backend.data_store.migrate_single_artifact._clean_legacy_runtime"):
            apply(self.root)
        _, journal = _paths(self.root)
        state_path = journal / "state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["runtime_cleanup_version"] = 1
        state_path.write_text(json.dumps(state), encoding="utf-8")
        RecordStore(self.root).put("business", "new-write", {"preserve": True})
        with self.assertRaisesRegex(ValueError, "changed"):
            apply(self.root)

    def test_rollback_refuses_tampered_backup(self):
        self.history()
        result = apply(self.root)
        backup = Path(result["backup"])
        path = next((backup / "batches").rglob("result.json"))
        path.write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "checksum"):
            rollback(self.root)


if __name__ == "__main__":
    unittest.main()
