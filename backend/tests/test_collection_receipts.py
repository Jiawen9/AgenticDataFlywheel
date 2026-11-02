from __future__ import annotations

import copy
import hashlib
import io
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
import zipfile
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from backend.batch_operations import active_batch_lock
from backend.collection_artifact_transfer import digest
from backend.collection_receipts import ReceiptStore
from backend.collection_runs import CollectionRunError, CollectionRunStore
from backend.collection_transfer import CollectionTransferManager
from backend.tests.test_collection_artifact_transfer import StreamRemote
from backend.tests.test_collection_runs import seed_batch


class V2Remote(StreamRemote):
    def envelope(self):
        return {**super().envelope(), "schema_version": 2, "collection_artifacts_version": 2}

    def add_outcome(self, case, index=1, phone="phone-one", status="not_run"):
        artifact_id = f"artifact{len(self.items) + 1}"
        entry = {"collection_case_id": case, "phone_id": phone, "source_trajectory_id": artifact_id,
                 "relative_dir": artifact_id, "files": [], "error": "not executed",
                 "repeat_times": self.run["request"]["metadata"]["repeat_times"], "repeat_index": index,
                 "expanded_case_id": f"{case}-{phone}-{index:03}", "execution_status": status}
        manifest = {**self.envelope(), "artifact_id": artifact_id, "kind": "outcome", "archive": None, "outcome": entry}
        self.manifests[artifact_id] = manifest
        self.items.append({"artifact_id": artifact_id, "sequence": len(self.items) + 1, "kind": "outcome",
            "archive": None, "manifest_sha256": digest(manifest), "manifest": manifest})
        self.visible = len(self.items)
        return artifact_id

    def summary(self):
        result = super().summary()
        entries = [m[m["kind"]] for m in self.manifests.values()]
        result.update(collection_not_run_count=sum(e["execution_status"] == "not_run" for e in entries),
                      outcome_count=sum(m["kind"] == "outcome" for m in self.manifests.values()),
                      evaluation_eligible_count=0, preprocessing_eligible_count=0)
        result["collection_failed_count"] -= result["collection_not_run_count"]
        return result

    def finish(self, status="partial", fill_missing=True):
        present = {(m[m["kind"]]["phone_id"], m[m["kind"]]["collection_case_id"], m[m["kind"]]["repeat_index"]) for m in self.manifests.values()}
        if fill_missing:
            for phone, case, index in sorted(self.planned() - present):
                self.add_outcome(case, index, phone)
        self.closed, self.status = True, status

    def get_collection_manifest(self, run_id):
        return {**self.envelope(), "collection_status": self.status, "summary": self.summary(),
                "artifact_count": len(self.items), "last_sequence": len(self.items),
                "artifacts_digest": digest([{k: item[k] for k in ("artifact_id", "sequence", "kind", "manifest_sha256")} for item in self.items]),
                "plan_digest": digest(sorted([list(item) for item in self.planned()]))}


class ReceiptTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "platform"
        self.remote_root = Path(temporary.name) / "remote"
        seed_batch(self.root)
        self.runs = CollectionRunStore(self.root)
        self.run, _ = self.runs.create("batch-one", dispatch_key="receipts", metadata={
            "run_mode": "generate", "result_transport": "http", "repeat_times": 1,
            "collection_artifacts_version": 2, "phone_ids": ["phone-one"],
            "phone_apps": [{"phone_id": "phone-one", "app": "Demo"}]})
        self.run_id = self.run["collection_run_id"]
        self.remote = V2Remote(self.run, self.remote_root)
        self.manager = CollectionTransferManager(self.root, self.runs, self.remote)
        self.addCleanup(self.manager.close)

    def rewrite(self, artifact_id, *, missing_xml=False, zero_actions=False, bad_metadata=False):
        manifest = self.remote.manifests[artifact_id]
        entry = manifest[manifest["kind"]]
        directory = self.remote_root / entry["relative_dir"]
        if missing_xml:
            for path in directory.glob("*.xml"):
                path.unlink()
        metadata = {"task": "Open settings", "actions_flat": [] if zero_actions else [
            {"global_step": 1, "action": {"action": "click", "coordinate": [10, 10]},
             "screenshot_before": "step001_vla_input.jpg", "screenshot_after": "missing-done.jpg"}],
            "final_orch_screenshot": "step001_vla_input.jpg"}
        (directory / "_trajectory_for_evaluate.json").write_text(json.dumps({} if bad_metadata else metadata), encoding="utf-8")
        entry["files"] = [{"path": path.relative_to(self.remote_root).as_posix(), "size": path.stat().st_size,
                           "sha256": hashlib.sha256(path.read_bytes()).hexdigest()} for path in sorted(directory.iterdir())]
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            for item in entry["files"]:
                archive.write(self.remote_root / item["path"], item["path"])
        self.remote.archives[artifact_id] = buffer.getvalue()
        manifest["archive"] = {"size": len(buffer.getvalue()), "sha256": hashlib.sha256(buffer.getvalue()).hexdigest()}
        item = next(i for i in self.remote.items if i["artifact_id"] == artifact_id)
        item.update(archive=manifest["archive"], manifest_sha256=digest(manifest))

    def test_missing_xml_is_received_and_evaluable_but_never_training(self):
        bad = self.remote.add()
        self.rewrite(bad, missing_xml=True)
        good = self.remote.add(case="task-two")
        self.rewrite(good)
        self.remote.finish("completed")
        original = copy.deepcopy(self.remote.get_collection_manifest(self.run_id))
        saved = self.manager.sync(self.run_id)
        self.assertTrue(saved["receipt_closed"])
        self.assertEqual(saved["transfer_status"], "completed")
        self.assertEqual(saved["collection_summary"]["received_artifact_count"], 2)
        self.assertEqual(saved["collection_summary"]["evaluation_eligible_count"], 2)
        self.assertEqual(saved["collection_summary"]["preprocessing_eligible_count"], 1)
        self.assertEqual([t["collection_case_id"] for t in self.runs.ready_input("batch-one")["trajectories"]], ["task-two"])
        self.assertEqual(self.remote.get_collection_manifest(self.run_id), original)
        self.assertEqual(self.manager.records.get("collection_source_final_manifests", self.run_id)["manifest"], original)
        self.assertEqual(self.manager.artifact_rows(self.run_id)[0]["execution_status"], "completed")
        self.assertTrue((Path(saved["output_dir"]) / "task-one/artifact1/_trajectory_for_evaluate.json").is_file())

    def test_zero_action_final_image_is_evaluable_not_training(self):
        key = self.remote.add()
        self.rewrite(key, zero_actions=True)
        self.remote.finish()
        saved = self.manager.sync(self.run_id)
        self.assertEqual(saved["transfer_status"], "completed")
        self.assertEqual(saved["collection_summary"]["evaluation_eligible_count"], 1)
        with self.assertRaises(CollectionRunError):
            self.runs.ready_input("batch-one")

    def test_invalid_metadata_keeps_received_raw_evidence_and_terminal_outcome_needs_no_zip(self):
        key = self.remote.add()
        self.rewrite(key, bad_metadata=True)
        outcome = self.remote.add_outcome("task-two")
        self.remote.finish()
        with patch.object(self.remote, "get_collection_artifact", wraps=self.remote.get_collection_artifact) as get:
            saved = self.manager.sync(self.run_id)
        self.assertEqual(saved["collection_summary"]["received_artifact_count"], 2)
        self.assertEqual(saved["collection_summary"]["received_outcome_count"], 1)
        self.assertEqual(saved["collection_summary"]["evaluation_eligible_count"], 0)
        self.assertNotIn(outcome, self.remote.downloads)
        self.assertTrue(all(call.args[1] != outcome for call in get.call_args_list))

    def test_history_unfinished_v1_preserves_request_and_source_kind(self):
        self.manager.records.update("collection_runs", self.run_id, lambda value: value["request"]["metadata"].update(collection_artifacts_version=1))
        self.run["request"]["metadata"]["collection_artifacts_version"] = 1
        remote = StreamRemote(self.run, self.remote_root)
        self.remote = self.manager.remote = remote
        key = remote.add()
        self.rewrite(key, missing_xml=True)
        remote.finish()
        frozen = copy.deepcopy(self.runs.get(self.run_id)["request"])
        self.manager.records.put("collection_artifact_transfers", self.run_id, {"collection_run_id": self.run_id,
            "batch_id": "batch-one", "cursor": 1, "items": {key: {"item": copy.deepcopy(remote.items[0]),
                "manifest": copy.deepcopy(remote.manifests[key]), "status": "pending", "error": "old missing XML"}}})
        saved = self.manager.sync(self.run_id)
        self.assertEqual(saved["request"], frozen)
        self.assertEqual(saved["receipt_policy_version"], 2)
        self.assertEqual(self.manager.artifact_rows(self.run_id)[0]["kind"], "trajectory")
        self.assertTrue(self.manager.artifact_rows(self.run_id)[0]["eligibility"]["evaluation"])
        self.assertNotIn("items", self.manager.records.get("collection_artifact_transfers", self.run_id))

    def test_corrupt_archive_retries_bounded_then_quarantines_without_losing_other_files(self):
        key = self.remote.add()
        self.rewrite(key)
        good = self.remote.add(case="task-two")
        self.rewrite(good)
        self.remote.archives[key] = b"corrupt"
        self.remote.finish("completed")
        for _ in range(3):
            saved = self.manager.sync(self.run_id)
        self.assertTrue(saved["receipt_closed"])
        self.assertEqual(saved["transfer_status"], "partial")
        self.assertEqual(self.remote.downloads.count(key), 3)
        self.assertEqual(self.remote.downloads.count(good), 1)
        self.assertEqual(saved["collection_summary"]["received_artifact_count"], 1)
        self.assertEqual(saved["collection_summary"]["quarantined_count"], 1)
        self.assertEqual(len(self.runs.ready_input("batch-one")["trajectories"]), 1)

    def test_published_batch_is_read_only(self):
        self.remote.add()
        self.manager.records.put("batch_lifecycle", "batch-one", {"batch_id": "batch-one", "status": "published"})
        with patch.object(self.remote, "get_collection_artifacts", side_effect=AssertionError("remote read")):
            from backend.batch_lifecycle import BatchPublishedError
            with self.assertRaises(BatchPublishedError):
                self.manager.sync(self.run_id)
        self.assertEqual(self.runs.get(self.run_id)["request"], self.run["request"])
        self.assertEqual(self.manager.artifact_page(self.run_id)["total"], 0)

    def test_invalid_inline_identity_is_quarantined_without_blocking_valid_subset(self):
        bad = self.remote.add_outcome("task-one")
        good = self.remote.add(case="task-two")
        self.rewrite(good)
        self.remote.finish()
        summary = self.remote.summary()
        final = self.remote.get_collection_manifest(self.run_id)
        self.remote.manifests[bad]["outcome"].pop("collection_case_id")
        self.remote.items[0]["manifest_sha256"] = digest(self.remote.manifests[bad])
        final["artifacts_digest"] = digest([{k: item[k] for k in
            ("artifact_id", "sequence", "kind", "manifest_sha256")} for item in self.remote.items])
        with patch.object(self.remote, "summary", return_value=summary), patch.object(
                self.remote, "get_collection_manifest", return_value=final):
            saved = self.manager.sync(self.run_id)
        self.assertTrue(saved["receipt_closed"])
        self.assertEqual(saved["transfer_status"], "partial")
        self.assertEqual(saved["collection_summary"]["quarantined_count"], 1)
        self.assertEqual(saved["collection_summary"]["received_artifact_count"], 1)
        self.assertEqual(len(self.runs.ready_input("batch-one")["trajectories"]), 1)

    def test_download_does_not_hold_publication_lock(self):
        self.remote.add()
        download = self.remote.download_collection_artifact
        def probe(*args):
            def lock():
                with active_batch_lock("batch-one", self.root):
                    return True
            with ThreadPoolExecutor(max_workers=1) as pool:
                self.assertTrue(pool.submit(lock).result(timeout=2))
            return download(*args)
        with patch.object(self.remote, "download_collection_artifact", side_effect=probe):
            self.manager.sync(self.run_id)
        self.assertEqual(self.manager.artifact_page(self.run_id)["items"][0]["receipt_status"], "received")

    def test_four_global_downloads_two_per_run(self):
        managers, remotes, runs = [], [], []
        for number in range(2):
            run, _ = self.runs.create("batch-one", dispatch_key=f"parallel{number}", metadata={
                **self.run["request"]["metadata"], "repeat_times": 2})
            remote = V2Remote(run, self.remote_root / str(number))
            remote.add(index=1)
            remote.add(index=2)
            manager = CollectionTransferManager(self.root, self.runs, remote)
            self.addCleanup(manager.close)
            managers.append(manager); remotes.append(remote); runs.append(run)
        guard, release = threading.Lock(), threading.Event()
        active, maximum, per_run, per_run_max = [0], [0], {}, {}
        originals = [remote.download_collection_artifact for remote in remotes]
        def download(number, *args):
            with guard:
                active[0] += 1
                per_run[number] = per_run.get(number, 0) + 1
                maximum[0] = max(maximum[0], active[0])
                per_run_max[number] = max(per_run_max.get(number, 0), per_run[number])
                if active[0] == 4:
                    release.set()
            try:
                release.wait(3)
                return originals[number](*args)
            finally:
                with guard:
                    active[0] -= 1
                    per_run[number] -= 1
        for number, remote in enumerate(remotes):
            remote.download_collection_artifact = lambda *args, number=number: download(number, *args)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda number: managers[number].sync(runs[number]["collection_run_id"]), range(2)))
        self.assertEqual(maximum[0], 4)
        self.assertEqual(per_run_max, {0: 2, 1: 2})
        self.assertTrue(all(result["collection_summary"]["received_artifact_count"] == 2 for result in results))

    def test_stop_control_observation_cannot_be_undone_by_old_artifact_snapshot(self):
        self.remote.add()
        self.remote.get_run = lambda run_id: {"run_id": run_id, "run_mode": "generate", "agent_type": "newagent",
            "status": "cancelled", "collection_status": "cancelled", "state_revision": 7, "phone_states": {}}
        saved = self.manager.sync(self.run_id)
        self.assertEqual(saved["collection_status"], "cancelled")
        self.assertEqual(saved["remote_state_revision"], 7)
        self.assertTrue(saved["control_terminal"])
        self.assertEqual(saved["collection_summary"]["received_artifact_count"], 1)

    def test_publication_during_download_prevents_late_raw_commit(self):
        key = self.remote.add()
        original = self.remote.download_collection_artifact
        def download(*args):
            original(*args)
            self.manager.records.put("batch_lifecycle", "batch-one", {"batch_id": "batch-one", "status": "published"})
        with patch.object(self.remote, "download_collection_artifact", side_effect=download):
            with self.assertRaises(Exception):
                self.manager.sync(self.run_id)
        destination = self.runs.run_root("batch-one", self.run_id) / "task-one" / key
        self.assertFalse(destination.exists())
        self.assertFalse(self.runs.get(self.run_id).get("receipt_closed"))

    def test_5440_receipts_are_independent_rows_with_bounded_page_and_header(self):
        ledger = ReceiptStore(self.manager.records, self.run_id)
        def items(begin, end):
            return [{"artifact_id": f"artifact{i}", "sequence": i, "kind": "outcome",
                     "archive": None, "manifest_sha256": "a" * 64} for i in range(begin, end)]
        ledger.register(items(1, 101))
        first = ledger.get("artifact1")
        for begin in range(101, 5441, 100):
            ledger.register(items(begin, min(begin + 100, 5441)))
        self.assertEqual(ledger.get("artifact1"), first)
        self.assertEqual(ledger.counts()["total"], 5440)
        page = self.manager.artifact_page(self.run_id, 5430, 10)
        self.assertEqual((page["total"], len(page["items"]), page["has_more"]), (5440, 10, False))
        filtered = self.manager.artifact_page(self.run_id, search="artifact5440", status="pending")
        self.assertEqual(filtered["total"], 1)
        self.assertNotIn("manifest", json.dumps(page))
        self.assertIsNone(self.manager.records.get("collection_artifact_transfers", self.run_id))

    def test_final_digest_mismatch_never_admits_training(self):
        self.remote.add()
        self.remote.finish()
        final = self.remote.get_collection_manifest(self.run_id)
        final["artifacts_digest"] = "f" * 64
        with patch.object(self.remote, "get_collection_manifest", return_value=final):
            with self.assertRaisesRegex(CollectionRunError, "摘要"):
                self.manager.sync(self.run_id)
        with self.assertRaises(CollectionRunError):
            self.runs.ready_input("batch-one")


if __name__ == "__main__":
    unittest.main()
