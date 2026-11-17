"""Physical paths may change; transport identity, frozen inputs and bytes may not."""
import copy
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
import zipfile
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from unittest.mock import patch
from types import SimpleNamespace

from backend.collection_runs import CollectionRunStore
from backend.collection_transfer import CollectionTransferManager
from backend.collection_receipts import ReceiptStore
from backend.collection_artifact_transfer import digest
from backend.file_io import io_path
from backend.identifiers import allocate_id
from backend.preprocessing_service import convert_input, verify_input
from backend.short_paths import (collection_layout, collection_entry, reserve_collection_layout,
                                 adapt_conversion_identity, collection_path_budget)
from backend.tests.test_collection_runs import seed_batch
from backend.tests.test_collection_artifact_transfer import StreamRemote
from backend.tests.test_collection_receipts import V2Remote


class CompactCollectionTests(unittest.TestCase):
    def test_path_budget_is_diagnostic_and_persisted_only_on_new_mapping(self):
        root, runs, run, _, _ = self.fixture()
        layout = collection_layout(root, run["batch_id"], run["collection_run_id"])
        self.assertIn("within_budget", layout["storage_path_budget"])
        self.assertNotIn("storage_path_budget", run["request"])
        self.assertEqual(layout, reserve_collection_layout(root, run["batch_id"], run["collection_run_id"]))
        long_path = root / ("😀" * 130)
        budget = collection_path_budget(long_path)
        self.assertGreater(budget["path_length"], len(str(long_path)))
        self.assertFalse(budget["within_budget"])
        self.assertEqual(budget["warning"], "generated_path_exceeds_budget")

    def fixture(self, version=2, engine="newagent", *, legacy=False):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name) / "中文平台"
        batch = allocate_id("b", root)
        seed_batch(root, batch)
        runs = CollectionRunStore(root)
        options = {"run_mode": "generate", "result_transport": "http", "repeat_times": 1,
                   "agent_type": engine, "collection_artifacts_version": version,
                   "phone_ids": ["phone-one"], "phone_apps": [{"phone_id": "phone-one", "app": "Demo"}]}
        if legacy:
            with patch("backend.short_paths.reserve_collection_layout", return_value=None):
                run, _ = runs.create(batch, dispatch_key="request", metadata=options)
        else:
            run, _ = runs.create(batch, dispatch_key="request", metadata=options)
        remote_type = V2Remote if version == 2 else StreamRemote
        class Remote(remote_type):
            def envelope(self):
                return {**super().envelope(), "agent_type": engine}
        remote = Remote(run, Path(temporary.name) / "原件")
        manager = CollectionTransferManager(root, runs, remote)
        self.addCleanup(manager.close)
        return root, runs, run, remote, manager

    def add(self, remote, engine):
        aid = remote.add()
        manifest = remote.manifests[aid]
        entry = manifest["trajectory"]
        directory = remote.directory / entry["relative_dir"]
        action = {"action": "click", "coordinate": [10, 10]}
        metadata = {"agent_type": engine, "task": "Open settings",
                    "actions_flat": [{"global_step": 1, "action": action}],
                    "final_orch_screenshot": "step001_vla_input.jpg"}
        (directory / "_trajectory_for_evaluate.json").write_text(json.dumps(metadata), encoding="utf-8")
        if engine == "pipelineagent":
            response = {"agent_type": engine, "actual_action_coordinate_system": "pixel",
                        "global_step": 1, "executed_action": action,
                        "content": '<tool_call>' + json.dumps(action) + '</tool_call><summary>Tap</summary>'}
            (directory / "step001_vla_model_response.json").write_text(json.dumps(response), encoding="utf-8")
        entry["files"] = [{"path": p.relative_to(remote.directory).as_posix(), "size": p.stat().st_size,
                           "sha256": hashlib.sha256(p.read_bytes()).hexdigest()} for p in directory.iterdir()]
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            for item in entry["files"]:
                archive.write(remote.directory / item["path"], item["path"])
        remote.archives[aid] = buffer.getvalue()
        manifest["archive"] = {"size": len(buffer.getvalue()), "sha256": hashlib.sha256(buffer.getvalue()).hexdigest()}
        remote.items[0].update(archive=manifest["archive"], manifest_sha256=digest(manifest))
        return aid

    def test_both_engines_and_protocols_keep_wire_bytes_and_convert_actual_paths(self):
        for version in (1, 2):
            for engine in ("newagent", "pipelineagent"):
                with self.subTest(version=version, engine=engine):
                    root, runs, run, remote, manager = self.fixture(version, engine)
                    aid = self.add(remote, engine)
                    original = copy.deepcopy(remote.manifests[aid])
                    original_zip = remote.archives[aid]
                    manager.sync(run["collection_run_id"])
                    source = manager.artifact_source(run["collection_run_id"], aid)
                    self.assertEqual(source["manifest"], original)
                    for name, path in source["file_paths"].items():
                        self.assertEqual(io_path(Path(source["root"]) / path).read_bytes(), (remote.directory / name).read_bytes())
                        self.assertNotIn(run["collection_run_id"], str(Path(source["root"]) / path))
                    from backend.collection_evidence_api import dispatch
                    image = "task-one/artifact1/step001_vla_input.jpg"
                    response = dispatch(SimpleNamespace(store=SimpleNamespace(collection_runs=runs), transfer=manager),
                        ["collection-runs", run["collection_run_id"], "artifacts", aid, "file"],
                        {"path": image}, "generate")
                    self.assertEqual(Path(response.path).read_bytes(), (remote.directory / image).read_bytes())
                    self.assertEqual(len(remote.downloads), 1)
                    manager.sync(run["collection_run_id"])
                    self.assertEqual(len(remote.downloads), 1)
                    remote.finish()
                    manager.sync(run["collection_run_id"])
                    snapshot = runs.ready_input(run["batch_id"])
                    verify_input(snapshot, root)
                    payload, counts = convert_input(snapshot, lambda _: None, data_root=root)
                    payload = adapt_conversion_identity(payload, snapshot, root)
                    row = payload["sheets"]["VLA trajectories"][0]
                    self.assertEqual(counts["trajectory_count"], 1)
                    self.assertEqual(row["collection_run_id"], run["collection_run_id"])
                    self.assertEqual(row["collection_case_id"], "task-one")
                    self.assertRegex(row["image"], r"^runs/r\d{6}-\d+/tr\d{6}-\d+/step001_vla_input.jpg$")
                    self.assertTrue(io_path(Path(snapshot["raw_root"]) / row["image"]).is_file())
                    self.assertEqual(remote.archives[aid], original_zip)
                    self.assertEqual(ReceiptStore(runs.records, run["collection_run_id"]).manifest(aid), original)
                    replay, created = runs.create(run["batch_id"], dispatch_key="request", metadata=run["request"]["metadata"])
                    self.assertFalse(created)
                    self.assertEqual(replay["request_digest"], run["request_digest"])
                    self.assertEqual(replay["output_dir"], run["output_dir"])

    def test_old_run_replay_and_receipt_keep_legacy_locations(self):
        root, runs, run, remote, manager = self.fixture(legacy=True)
        aid = self.add(remote, "newagent")
        replay, _ = runs.create(run["batch_id"], dispatch_key="request", metadata=run["request"]["metadata"])
        self.assertIsNone(collection_layout(root, run["batch_id"], run["collection_run_id"]))
        self.assertEqual(replay["output_dir"], run["output_dir"])
        manager.sync(run["collection_run_id"])
        source = manager.artifact_source(run["collection_run_id"], aid)
        self.assertTrue((Path(source["root"]) / "task-one/artifact1/step001_vla_input.jpg").is_file())
        self.assertEqual(source["file_paths"], {name: name for name in source["files"]})

    def test_alias_reservation_is_concurrent_and_mapping_corruption_is_rejected(self):
        root, runs, run, remote, _ = self.fixture()
        self.add(remote, "newagent")
        entry = remote.manifests["artifact1"]["trajectory"]
        with ThreadPoolExecutor(max_workers=4) as pool:
            mapped = list(pool.map(lambda _: collection_entry(root, run, entry, reserve=True)[0], range(8)))
        self.assertEqual(len({item["relative_dir"] for item in mapped}), 1)
        other = {**entry, "source_trajectory_id": "different"}
        with self.assertRaisesRegex(ValueError, "身份"):
            collection_entry(root, run, other, reserve=True)
        original = runs.records.get("collection_storage_layouts", run["collection_run_id"])
        original["value"]["directory"] = "r991231-999"
        runs.records.put("collection_storage_layouts", run["collection_run_id"], original)
        with self.assertRaisesRegex(ValueError, "登记损坏"):
            runs.get(run["collection_run_id"])

    def test_execution_layout_is_independent_and_does_not_relocate_root(self):
        root, runs, run, _, _ = self.fixture(legacy=True)
        prior = run["output_dir"]
        child = "cx_" + "e" * 48
        value = reserve_collection_layout(root, run["batch_id"], child)
        self.assertNotEqual(runs.run_root(run["batch_id"], child).name, child)
        self.assertEqual(runs.get(run["collection_run_id"])["output_dir"], prior)
        self.assertEqual(reserve_collection_layout(root, run["batch_id"], child), value)

    def test_mixed_old_and_new_run_inputs_keep_each_actual_directory(self):
        root, runs, old, remote, manager = self.fixture(legacy=True)
        self.add(remote, "newagent")
        remote.finish()
        manager.sync(old["collection_run_id"])
        new, _ = runs.create(old["batch_id"], dispatch_key="new", metadata=old["request"]["metadata"])
        newer = V2Remote(new, remote.directory.parent / "新原件")
        self.add(newer, "newagent")
        newer.finish()
        transfer = CollectionTransferManager(root, runs, newer)
        self.addCleanup(transfer.close)
        transfer.sync(new["collection_run_id"])
        snapshot = runs.ready_input(old["batch_id"])
        verify_input(snapshot, root)
        payload, counts = convert_input(snapshot, lambda _: None, data_root=root)
        paths = {row["collection_run_id"]: row["image"] for row in payload["sheets"]["VLA trajectories"]}
        self.assertEqual(counts["trajectory_count"], 2)
        self.assertTrue(paths[old["collection_run_id"]].startswith("runs/" + old["collection_run_id"] + "/"))
        self.assertTrue(paths[new["collection_run_id"]].startswith("runs/" + Path(new["output_dir"]).name + "/"))
        self.assertEqual(runs.get(old["collection_run_id"])["request_digest"], old["request_digest"])

    def test_extraction_checks_wire_names_even_when_mapping_is_short(self):
        _, _, run, remote, manager = self.fixture()
        self.add(remote, "newagent")
        entry = remote.manifests["artifact1"]["trajectory"]
        _, mapping = collection_entry(manager.root, run, entry, reserve=True)
        descriptors = {item["path"]: (item["size"], item["sha256"]) for item in entry["files"]}
        archive = manager._temporary(run["collection_run_id"]) / "broken.zip"
        io_path(archive.parent).mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(io_path(archive), "w") as out:
            out.writestr("../outside.txt", b"bad")
        with self.assertRaises(ValueError):
            manager._extract(archive, archive.parent / "out", descriptors, mapping=mapping)
        self.assertFalse((archive.parent.parent / "outside.txt").exists())

    def test_compact_input_reaches_tree_quality_and_coordinate_readers(self):
        from backend.data_store import ArtifactStore
        from backend.coordinate_info import resolve_coordinate_info
        from backend.quality_input_builder import build_quality_workbook
        from backend.tree_build_service import build_tree_run
        from backend.tests.test_tree_build_service import FakeClassifier, FakeAlignmentReviewer, FakeSummarizer
        for engine in ("newagent", "pipelineagent"):
            with self.subTest(engine=engine):
                root, runs, run, remote, manager = self.fixture(engine=engine)
                self.add(remote, engine)
                remote.finish()
                manager.sync(run["collection_run_id"])
                snapshot = runs.ready_input(run["batch_id"])
                payload, counts = convert_input(snapshot, lambda _: None, data_root=root)
                payload = adapt_conversion_identity(payload, snapshot, root)
                row = payload["sheets"]["VLA trajectories"][0]
                row["actions_box"] = "click(bbox=<bbox>[0,0,20,30]</bbox>)"
                payload["columns"]["VLA trajectories"].append("actions_box")
                store = ArtifactStore(root)
                annotation = store.publish(run["batch_id"], "02_annotation", payload,
                    source_refs=[{"kind": "raw_trajectories", "path": snapshot["raw_root"]}],
                    metadata={"raw_root": snapshot["raw_root"], **counts})
                info = resolve_coordinate_info(row, snapshot["raw_root"])
                self.assertEqual(info["space"], "pixel" if engine == "pipelineagent" else "normalized")
                before = manager.artifact_source(run["collection_run_id"], "artifact1")
                with patch("backend.tree_build_service.configure_reviewer_environment", return_value="fake-model"), \
                     patch("backend.tree_build_service.QwenIntermediateStateClassifier", FakeClassifier), \
                     patch("backend.tree_build_service.QwenStateAlignmentReviewer", FakeAlignmentReviewer):
                    build_tree_run(["task-one"], job_id=allocate_id("j", root), progress=lambda _: None,
                        batch_id=run["batch_id"], annotation_version=annotation["version"], data_root=root,
                        env_path=root / "unused.env", quality_builder=partial(build_quality_workbook, summarizer=FakeSummarizer()))
                tree = store.read_payload(store.get(run["batch_id"], "04_tree"))
                steps = tree["quality_input"]["sheets"]["Steps"]
                identity = json.loads(steps[0]["action_input_json"])["source_identity"]
                self.assertEqual(identity["collection_run_id"], run["collection_run_id"])
                self.assertEqual(identity["trajectory_id"], row["trajectory_id"])
                source = tree["quality_input"]["sheets"]["Trajectories"][0]["source_directory"]
                self.assertEqual(Path(source), Path(snapshot["raw_root"]) / Path(row["image"]).parent)
                self.assertNotIn(run["collection_run_id"], source)
                self.assertEqual(manager.artifact_source(run["collection_run_id"], "artifact1"), before)


if __name__ == "__main__":
    unittest.main()
