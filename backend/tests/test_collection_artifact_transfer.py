from __future__ import annotations

import copy
import hashlib
import io
import tempfile
import unittest
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from backend.collection_artifact_transfer import digest
from backend.collection_runs import CollectionRunError, CollectionRunStore
from backend.collection_transfer import CollectionTransferManager
from backend.tests.test_collection_runs import raw_trajectory, seed_batch


class StreamRemote:
    def __init__(self, run, directory):
        self.run, self.directory = run, directory
        self.items, self.manifests, self.archives = [], {}, {}
        self.visible = 0
        self.closed = False
        self.status = "running"
        self.fail = set()
        self.downloads = []
        self.errors = []
        self.mode = run["request"]["metadata"]["run_mode"]

    def envelope(self):
        return {"schema_version": 1, "collection_artifacts_version": 1,
                "collection_run_id": self.run["collection_run_id"], "batch_id": self.run["batch_id"],
                "run_mode": self.mode, "agent_type": "newagent"}

    def add(self, phone="phone-one", case="task-one", index=1, diagnostic=False, status=None):
        artifact_id = f"artifact{len(self.items) + 1}"
        if diagnostic:
            entry = {"collection_case_id": case, "source_trajectory_id": artifact_id,
                     "relative_dir": artifact_id, "files": [], "error": "ui_reset_failed"}
        else:
            entry = raw_trajectory({"output_dir": str(self.directory)}, case_id=case, name=artifact_id)
        entry.update(phone_id=phone, repeat_times=self.run["request"]["metadata"]["repeat_times"],
                     repeat_index=index, expanded_case_id=f"{case}-{phone}-{index:03}",
                     execution_status=status or ("failed" if diagnostic else "completed"))
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for item in entry["files"]:
                archive.write(self.directory / item["path"], item["path"])
        payload = buffer.getvalue()
        archive_info = {"size": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}
        kind = "diagnostic" if diagnostic else "trajectory"
        manifest = {**self.envelope(), "artifact_id": artifact_id, "kind": kind,
                    "archive": archive_info, kind: entry}
        self.archives[artifact_id] = payload
        self.manifests[artifact_id] = manifest
        self.items.append({"artifact_id": artifact_id, "sequence": len(self.items) + 1, "kind": kind,
                           "manifest_sha256": digest(manifest), "archive": archive_info})
        self.visible = len(self.items)
        return artifact_id

    def planned(self):
        options = self.run["request"]["metadata"]
        return {(row["phone_id"], case, index)
                for row in options["phone_apps"]
                for case, task in self.run["batch_tasks"].items() if task["app"] == row["app"]
                for index in range(1, options["repeat_times"] + 1)}

    def summary(self):
        entries = [m[m["kind"]] for m in self.manifests.values()]
        return {"original_task_count": len({case for _, case, _ in self.planned()}),
                "planned_trajectory_count": len(self.planned()),
                "collection_completed_count": sum(e["execution_status"] == "completed" for e in entries),
                "collection_failed_count": sum(e["execution_status"] not in {"completed", "cancelled"} for e in entries),
                "collection_cancelled_count": sum(e["execution_status"] == "cancelled" for e in entries),
                "sealed_trajectory_count": sum(m["kind"] == "trajectory" for m in self.manifests.values()),
                "diagnostic_count": sum(m["kind"] == "diagnostic" for m in self.manifests.values())}

    def get_collection_artifacts(self, run_id, cursor=0, limit=100):
        items = self.items[cursor:min(cursor + limit, self.visible)]
        return {**self.envelope(), "collection_status": self.status, "closed": self.closed,
                "summary": self.summary(),
                "items": copy.deepcopy(items), "next_cursor": cursor + len(items),
                "has_more": cursor + len(items) < self.visible}

    def get_collection_artifact(self, run_id, artifact_id):
        return copy.deepcopy(self.manifests[artifact_id])

    def download_collection_artifact(self, run_id, artifact_id, destination):
        self.downloads.append(artifact_id)
        if artifact_id in self.fail:
            raise OSError("simulated disconnected network")
        Path(destination).write_bytes(self.archives[artifact_id])

    def get_collection_manifest(self, run_id):
        return {**self.envelope(), "collection_status": self.status, "summary": self.summary(),
                "trajectories": [copy.deepcopy(m["trajectory"]) for m in self.manifests.values() if m["kind"] == "trajectory"],
                "diagnostics": [copy.deepcopy(m["diagnostic"]) for m in self.manifests.values() if m["kind"] == "diagnostic"],
                "artifacts": copy.deepcopy(self.items), "errors": copy.deepcopy(self.errors)}

    def finish(self, status="completed", fill_missing=True):
        existing = {(m[m["kind"]]["phone_id"], m[m["kind"]]["collection_case_id"], m[m["kind"]]["repeat_index"]) for m in self.manifests.values()}
        missing = self.planned() - existing
        if fill_missing:
            for phone, case, index in sorted(missing):
                key = self.add(phone, case, index, diagnostic=True, status="cancelled")
                self.manifests[key]["diagnostic"]["error"] = "not_executed"
                self.items[-1]["manifest_sha256"] = digest(self.manifests[key])
                self.errors.append({"collection_case_id": case, "phone_id": phone, "repeat_index": index,
                                    "source_trajectory_id": key, "error": "not_executed"})
        self.closed = True
        self.status = "partial" if status == "completed" and fill_missing and missing else status


class ArtifactTransferTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "platform"
        seed_batch(self.root)
        self.runs = CollectionRunStore(self.root)
        self.run, _ = self.runs.create("batch-one", dispatch_key="stream", metadata={
            "run_mode": "generate", "result_transport": "http", "repeat_times": 3,
            "collection_artifacts_version": 1, "phone_ids": ["phone-one", "phone-two"],
            "phone_apps": [{"phone_id": "phone-one", "app": "Demo"}, {"phone_id": "phone-two", "app": "Demo"}]})
        self.run_id = self.run["collection_run_id"]
        self.remote = StreamRemote(self.run, Path(self.temp.name) / "remote")
        self.manager = self.make_manager()

    def make_manager(self):
        manager = CollectionTransferManager(self.root, self.runs, self.remote)
        self.addCleanup(manager.close)
        return manager

    def drain(self, manager=None):
        manager = manager or self.manager
        for _ in range(5):
            result = manager.sync(self.run_id)
            if result.get("transfer_status") in {"completed", "remote_failed"}:
                return result
        self.fail("complete inventory did not settle within the per-poll budget")

    def test_first_case_persists_while_collection_and_evaluation_still_running(self):
        self.remote.add()
        result = self.manager.sync(self.run_id)
        self.assertEqual(result["transferred_trajectory_count"], 1)
        self.assertNotEqual(result["status"], "completed")
        self.assertTrue((Path(result["output_dir"]) / "task-one/artifact1/step001_vla_input.jpg").is_file())
        with self.assertRaises(CollectionRunError):
            self.runs.ready_input("batch-one")
        self.remote.add(index=2)
        self.remote.finish()
        result = self.drain()
        self.assertEqual(result["status"], "completed")
        self.assertEqual([t["repeat_index"] for t in result["trajectories"]], [1, 2])
        self.assertEqual(len(self.runs.ready_input("batch-one")["trajectories"]), 2)

    def test_twelve_instances_are_distinct_and_budgeted_over_multiple_polls(self):
        for phone in ("phone-one", "phone-two"):
            for case in ("task-one", "task-two"):
                for index in range(1, 4):
                    self.remote.add(phone, case, index)
        self.remote.finish()
        first = self.manager.sync(self.run_id)
        self.assertEqual(first["transferred_trajectory_count"], 10)
        final = self.manager.sync(self.run_id)
        self.assertEqual(final["status"], "completed")
        self.assertEqual(final["trajectory_count"], 12)
        self.assertEqual(len({entry["relative_dir"] for entry in final["trajectories"]}), 12)

    def test_one_failed_download_does_not_block_other_cases_and_restart_only_downloads_missing(self):
        first, second = self.remote.add(), self.remote.add(index=2)
        self.remote.fail.add(first)
        result = self.manager.sync(self.run_id)
        self.assertEqual(result["transfer_status"], "failed")
        self.assertEqual(self.runs.get(self.run_id)["transferred_trajectory_count"], 1)
        self.remote.fail.clear()
        self.remote.finish()
        result = self.drain(self.make_manager())
        self.assertEqual(result["status"], "completed")
        self.assertEqual(self.remote.downloads.count(second), 1)

    def test_acknowledgement_loss_after_rename_does_not_redownload(self):
        artifact = self.remote.add()
        original = self.manager.records.put
        lost = [False]
        def write(namespace, key, payload, **kwargs):
            if (namespace == "collection_artifact_transfers" and not lost[0]
                    and payload["items"].get(artifact, {}).get("status") == "completed"):
                lost[0] = True
                raise OSError("lost acknowledgement")
            return original(namespace, key, payload, **kwargs)
        with patch.object(self.manager.records, "put", side_effect=write):
            self.manager.sync(self.run_id)
        self.remote.finish()
        self.assertEqual(self.drain(self.make_manager())["status"], "completed")
        self.assertEqual(self.remote.downloads.count(artifact), 1)
        self.assertEqual(len(self.remote.downloads), 12)

    def test_cancelled_collection_keeps_data_and_diagnostics(self):
        self.remote.add()
        self.remote.add(index=2, diagnostic=True)
        self.remote.errors = [{"collection_case_id": "task-one", "phone_id": "phone-one", "repeat_index": 2, "error": "ui_reset_failed"}]
        self.remote.finish("cancelled")
        result = self.drain()
        self.assertEqual((result["status"], result["collection_status"]), ("completed", "cancelled"))
        self.assertEqual(result["transferred_diagnostic_count"], 11)
        self.assertTrue((self.runs.raw_root("batch-one") / "diagnostics" / self.run_id / "artifact2").is_dir())
        self.assertEqual(result["errors"][0]["repeat_index"], 2)
        self.assertEqual(len(self.runs.ready_input("batch-one")["trajectories"]), 1)

    def test_only_diagnostics_never_enable_preprocessing(self):
        self.remote.add(diagnostic=True)
        self.remote.finish("failed")
        result = self.drain()
        self.assertEqual((result["status"], result["transfer_status"]), ("failed", "remote_failed"))
        self.assertEqual(result["transferred_diagnostic_count"], 12)
        with self.assertRaises(CollectionRunError):
            self.runs.ready_input("batch-one")

    def test_final_inventory_must_exactly_match_sealed_items(self):
        self.remote.add()
        self.manager.sync(self.run_id)
        self.remote.finish()
        with patch.object(self.remote, "get_collection_manifest", wraps=self.remote.get_collection_manifest) as final:
            changed = self.remote.get_collection_manifest(self.run_id)
            changed["trajectories"][0]["repeat_index"] = 2
            final.return_value = changed
            final.side_effect = None
            with self.assertRaisesRegex(CollectionRunError, "修改或遗漏"):
                self.drain()

    def test_modified_manifest_or_archive_is_rejected(self):
        first = self.remote.add()
        self.remote.manifests[first]["trajectory"]["phone_id"] = "foreign-phone"
        result = self.manager.sync(self.run_id)
        self.assertEqual(result["transfer_status"], "failed")
        self.assertEqual(self.remote.downloads, [])

    def test_final_inventory_cannot_omit_frozen_repeat_instances(self):
        self.remote.add()
        self.remote.finish(fill_missing=False)
        with self.assertRaisesRegex(CollectionRunError, "冻结计划"):
            self.manager.sync(self.run_id)
        self.assertNotEqual(self.runs.get(self.run_id)["status"], "completed")
        with self.assertRaises(CollectionRunError):
            self.runs.ready_input("batch-one")

    def test_final_summary_must_match_actual_inventory_and_frozen_plan(self):
        self.remote.add()
        self.remote.finish()
        final = self.remote.get_collection_manifest(self.run_id)
        final["summary"]["collection_completed_count"] += 1
        with patch.object(self.remote, "get_collection_manifest", return_value=final):
            with self.assertRaisesRegex(CollectionRunError, "统计"):
                self.drain()
        self.assertNotEqual(self.runs.get(self.run_id)["status"], "completed")

    def test_distinct_repeat_instances_cannot_reuse_source_trajectory_id(self):
        first, second = self.remote.add(), self.remote.add(index=2)
        self.remote.manifests[second]["trajectory"]["source_trajectory_id"] = first
        self.remote.items[-1]["manifest_sha256"] = digest(self.remote.manifests[second])
        result = self.manager.sync(self.run_id)
        self.assertEqual(result["transfer_status"], "failed")
        self.assertEqual(result["transferred_trajectory_count"], 1)
        self.assertEqual(self.remote.downloads, [first])
        self.assertIn("重复", result["transfer_error"])

    def test_failed_item_backoff_does_not_delay_new_items_and_force_retries_immediately(self):
        failed = self.remote.add()
        self.remote.fail.add(failed)
        self.manager.sync(self.run_id)
        state = self.manager.sync_policy.view("collection", self.run_id)
        self.assertEqual((state["state"], state["failure_count"]), ("active", 0))
        self.remote.add(index=2)
        self.manager.sync_policy.reset("collection", self.run_id)
        result = self.manager.sync(self.run_id, force=False)
        self.assertEqual(result["transferred_trajectory_count"], 1)
        self.assertEqual(self.remote.downloads.count(failed), 1)
        self.manager.sync(self.run_id, force=True)
        self.assertEqual(self.remote.downloads.count(failed), 2)
        self.assertEqual(self.manager.sync_policy.view("collection", self.run_id)["failure_count"], 0)

    def test_direct_completion_endpoint_cannot_bypass_stream_inventory_reconciliation(self):
        self.remote.add()
        self.manager.sync(self.run_id)
        with self.assertRaisesRegex(CollectionRunError, "最终清单对账"):
            self.runs.complete(self.run_id, self.remote.get_collection_manifest(self.run_id))
        self.assertNotEqual(self.runs.get(self.run_id)["status"], "completed")

    def test_concurrent_sync_and_finished_replay_are_idempotent(self):
        self.remote.add()
        self.remote.finish()
        with ThreadPoolExecutor(max_workers=3) as pool:
            results = list(pool.map(lambda _: self.manager.sync(self.run_id), range(3)))
        self.assertTrue(all(result["transferred_trajectory_count"] == 1 for result in results))
        self.assertEqual(len(self.remote.downloads), len(set(self.remote.downloads)))
        self.assertEqual(self.drain()["status"], "completed")
        self.assertEqual(len(self.remote.downloads), 12)
        self.manager.sync(self.run_id)
        self.assertEqual(len(self.remote.downloads), 12)

    def test_modeliter_transfer_does_not_read_evaluation_status(self):
        self.manager.records.update("collection_runs", self.run_id,
                                    lambda value: value["request"]["metadata"].update(run_mode="modeliter", repeat_times=1))
        self.run["request"]["metadata"].update(run_mode="modeliter", repeat_times=1)
        self.remote.mode = "modeliter"
        self.remote.add()
        self.remote.finish()
        self.assertTrue(any(run["collection_run_id"] == self.run_id for run in self.manager._candidates()))
        self.assertEqual(self.drain()["transfer_status"], "completed")


if __name__ == "__main__":
    unittest.main()
