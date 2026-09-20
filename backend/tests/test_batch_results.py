from __future__ import annotations

import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from backend.batch_results import (
    StaleTaskInput, annotation_task_fingerprints, current_quality_payload,
    current_tree_payload, current_tree_batch, invalidate_tasks, merge_quality_results,
    merge_tree_results, tree_task_fingerprints, tree_result_hashes,
    resolve_current_batch_id, invalidation_record_entry, recover_pending_batch_results,
)
from backend.data_store import ArtifactStore, RecordStore


class BatchResultsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = ArtifactStore(self.root)
        self.batch = "batch-one"
        self.annotation = {"schema_version": 1, "columns": {"VLA trajectories": ["task_id", "trajectory_id", "image", "action"]},
                           "sheets": {"VLA trajectories": [
                               {"task_id": task, "trajectory_id": task + "-1", "image": task + "/1.png", "action": "wait"}
                               for task in ("A", "B")]}}
        self.store.publish(self.batch, "02_annotation", self.annotation)
        self.hook = patch("backend.batch_results._notify_invalidated")
        self.hook.start()
        self.addCleanup(self.hook.stop)

    def incoming(self, task):
        annotation = {**self.annotation, "sheets": {"VLA trajectories": [
            row for row in self.annotation["sheets"]["VLA trajectories"] if row["task_id"] == task]}}
        quality = {"schema_version": 1, "columns": {}, "sheets": {
            "Tasks": [{"task_id": task}], "Trajectories": [{"task_id": task, "trajectory_id": task + "-1"}],
            "Steps": [{"trajectory_id": task + "-1", "step_id": 1}]}}
        fingerprints = tree_task_fingerprints(annotation, {"model": "fake"})
        trees = {task: {"task_id": task, "nodes": [annotation["sheets"]["VLA trajectories"][0]["action"]]}}
        return {"schema_version": 2, "trees": trees, "tasks": [{"task_id": task, "trajectory_count": 1}],
                "source_annotation": annotation, "quality_input": quality,
                "source_task_fingerprints": annotation_task_fingerprints(annotation),
                "task_fingerprints": fingerprints, "tree_hashes": tree_result_hashes(trees, quality, fingerprints),
                "raw_root": str(self.root / "raw"), "completed_at": "2026-09-20T00:00:00Z"}

    def build(self, task, incoming=None):
        value = incoming or self.incoming(task)
        return merge_tree_results(self.batch, value,
            {"trajectories": [{"task_id": task, "trajectory_id": task + "-1", "steps": []}]},
            [{"任务编号": task, "轨迹编号": task + "-1"}], self.root)

    def quality(self, task):
        trees = current_tree_payload(self.batch, self.root)
        return merge_quality_results(self.batch, [{"task_id": task, "run_id": self.batch, "evaluations": {}, "average_score": 4.5}],
            {task: trees["tree_hashes"][task]}, {task: "quality-" + task},
            job_id="q-" + task, completed_at="2026-09-20T01:00:00Z", root=self.root)

    def test_a_then_b_preserves_both_in_every_merged_input(self):
        self.build("A")
        self.build("B")
        current = current_tree_payload(self.batch, self.root)
        self.assertEqual(set(current["trees"]), {"A", "B"})
        self.assertEqual({row["task_id"] for row in current["quality_input"]["sheets"]["Tasks"]}, {"A", "B"})
        self.assertEqual(len(current["quality_input"]["sheets"]["Steps"]), 2)
        self.assertEqual(len(current["source_annotation"]["sheets"]["VLA trajectories"]), 2)
        observation = self.store.get(self.batch, "03_observation")
        tree = self.store.get(self.batch, "04_tree")
        self.assertEqual(tree["source_refs"][0]["version"], observation["version"])
        self.assertEqual(len(self.store.list(self.batch, "04_tree")), 1)
        self.assertTrue((self.root / "batches" / self.batch / "04_tree" / "result.json").is_file())

    def test_identical_task_reuses_result_without_new_revision(self):
        self.build("A")
        before = self.store.get(self.batch, "04_tree")
        self.build("A")
        self.assertEqual(self.store.get(self.batch, "04_tree"), before)

    def test_concurrent_disjoint_tasks_do_not_lose_results(self):
        with ThreadPoolExecutor(max_workers=2) as executor:
            list(executor.map(self.build, ["A", "B"]))
        self.assertEqual(set(current_tree_payload(self.batch, self.root)["trees"]), {"A", "B"})

    def test_quality_partial_runs_merge_and_keep_other_task(self):
        self.build("A")
        self.quality("A")
        self.build("B")
        self.assertEqual([item["task_id"] for item in current_quality_payload(self.batch, self.root)["tasks"]], ["A"])
        current_quality_ref = self.store.get(self.batch, "05_quality")
        current_tree_ref = self.store.get(self.batch, "04_tree")
        self.assertEqual(current_quality_ref["source_refs"][0]["version"], current_tree_ref["version"])
        self.store.resolve_file(current_quality_ref["source_refs"][0], "result.json")
        self.quality("B")
        self.assertEqual({item["task_id"] for item in current_quality_payload(self.batch, self.root)["tasks"]}, {"A", "B"})
        quality = self.store.get(self.batch, "05_quality")
        self.assertEqual(quality["source_refs"][0]["version"], self.store.get(self.batch, "04_tree")["version"])

    def test_changing_a_invalidates_only_a_and_late_a_is_rejected(self):
        late_a = self.incoming("A")
        self.build("A")
        self.build("B")
        self.quality("A")
        self.quality("B")
        self.annotation["sheets"]["VLA trajectories"][0]["action"] = "click"
        self.store.publish(self.batch, "02_annotation", self.annotation)
        invalidate_tasks(self.batch, ["A"], self.root)
        self.assertEqual(set(current_tree_payload(self.batch, self.root)["trees"]), {"B"})
        self.assertEqual([item["task_id"] for item in current_quality_payload(self.batch, self.root)["tasks"]], ["B"])
        with self.assertRaises(StaleTaskInput):
            self.build("A", late_a)
        self.build("A")
        self.assertEqual(set(current_tree_payload(self.batch, self.root)["trees"]), {"A", "B"})
        state = RecordStore(self.root).get("batch_task_states", self.batch + ":A")
        self.assertEqual((state["tree_status"], state["quality_status"]), ("succeeded", "pending"))

    def test_late_quality_for_changed_task_cannot_overwrite_current(self):
        self.build("A")
        old = current_tree_payload(self.batch, self.root)["tree_hashes"]["A"]
        self.annotation["sheets"]["VLA trajectories"][0]["action"] = "click"
        self.store.publish(self.batch, "02_annotation", self.annotation)
        self.build("A")
        with self.assertRaises(StaleTaskInput):
            merge_quality_results(self.batch, [{"task_id": "A"}], {"A": old}, {"A": "old"},
                                  job_id="late", completed_at="later", root=self.root)

    def test_alias_is_retired_when_its_task_changes(self):
        self.build("A")
        current = current_tree_payload(self.batch, self.root)
        RecordStore(self.root).put("batch_run_aliases", "old-run", {
            "batch_id": self.batch, "task_fingerprints": current["task_fingerprints"]})
        self.assertEqual(resolve_current_batch_id("old-run", self.root), self.batch)
        self.build("B")
        self.assertEqual(resolve_current_batch_id("old-run", self.root), self.batch)
        self.annotation["sheets"]["VLA trajectories"][0]["action"] = "click"
        self.store.publish(self.batch, "02_annotation", self.annotation)
        self.assertIsNone(resolve_current_batch_id("old-run", self.root))

    def test_committed_annotation_outbox_recovers_invalidation_without_losing_b(self):
        self.build("A")
        self.build("B")
        self.quality("A")
        self.quality("B")
        self.annotation["sheets"]["VLA trajectories"][0]["action"] = "click"
        with self.store.batch_lock(self.batch):
            self.store.publish_many(self.batch, [{"stage": "02_annotation", "payload": self.annotation}],
                record_entries=[invalidation_record_entry(self.store, self.batch, "annotation", ["A"])])
        # Simulate process exit after the annotation committed, before invalidation.
        self.assertEqual(set(self.store.read_payload(self.store.get(self.batch, "04_tree"))["trees"]), {"A", "B"})
        recover_pending_batch_results(self.root)
        self.assertEqual(set(self.store.read_payload(self.store.get(self.batch, "04_tree"))["trees"]), {"B"})
        self.assertEqual([item["task_id"] for item in current_quality_payload(self.batch, self.root)["tasks"]], ["B"])
        self.assertIsNone(RecordStore(self.root).get("batch_invalidations", self.batch))
        current = self.store.get(self.batch, "04_tree")
        recover_pending_batch_results(self.root)
        self.assertEqual(self.store.get(self.batch, "04_tree"), current)

    def test_outbox_survives_notification_failure_and_replays(self):
        self.build("A")
        with patch("backend.batch_results._notify_invalidated", side_effect=RuntimeError("simulated crash")):
            with self.assertRaisesRegex(RuntimeError, "simulated crash"):
                self.quality("A")
        self.assertIsNotNone(RecordStore(self.root).get("batch_invalidations", self.batch))
        self.assertEqual([item["task_id"] for item in current_quality_payload(self.batch, self.root)["tasks"]], ["A"])
        recover_pending_batch_results(self.root)
        self.assertIsNone(RecordStore(self.root).get("batch_invalidations", self.batch))
        self.assertEqual(RecordStore(self.root).get("batch_task_states", self.batch + ":A")["quality_status"], "succeeded")

    def test_failed_stage_commit_does_not_leave_an_invalidation_event(self):
        original = RecordStore._save
        def fail(connection, namespace, key, payload, revision):
            if namespace == "artifacts" and payload.get("stage") == "04_tree":
                raise RuntimeError("commit failure")
            return original(connection, namespace, key, payload, revision)
        with patch.object(RecordStore, "_save", side_effect=fail):
            with self.assertRaisesRegex(RuntimeError, "commit failure"):
                self.build("A")
        self.assertIsNone(RecordStore(self.root).get("batch_invalidations", self.batch))
        self.assertIsNone(self.store.get(self.batch, "03_observation"))
        self.assertIsNone(self.store.get(self.batch, "04_tree"))


class DeferredExecutor:
    def __init__(self):
        self.pending = []

    def submit(self, function, *args, **kwargs):
        self.pending.append((function, args, kwargs))

    def finish(self):
        function, args, kwargs = self.pending.pop(0)
        function(*args, **kwargs)


class BatchJobTests(unittest.TestCase):
    incoming = BatchResultsTests.incoming
    build = BatchResultsTests.build

    def setUp(self):
        BatchResultsTests.setUp(self)
        (self.root / "raw").mkdir()
        self.store.publish(self.batch, "02_annotation", self.annotation,
                           metadata={"raw_root": str(self.root / "raw")})

    def test_pending_b_can_finish_after_only_a_input_changes(self):
        from backend.tree_build_jobs import TreeBuildJobManager
        executor = DeferredExecutor()
        called = []
        def runner(tasks, **kwargs):
            called.append(tasks)
            return self.batch, {}
        manager = TreeBuildJobManager(self.root / "system" / "tree_jobs", runner, executor, self.root)
        job = manager.submit(["B"], batch_id=self.batch)
        self.annotation["sheets"]["VLA trajectories"][0]["action"] = "click"
        self.store.publish(self.batch, "02_annotation", self.annotation,
                           metadata={"raw_root": str(self.root / "raw")})
        executor.finish()
        self.assertEqual(called, [["B"]])
        self.assertEqual(manager.get(job["job_id"])["status"], "succeeded")

    def test_repeated_pending_tree_tasks_share_the_job(self):
        from backend.tree_build_jobs import TreeBuildJobManager
        executor = DeferredExecutor()
        manager = TreeBuildJobManager(self.root / "system" / "tree_jobs", executor=executor, data_root=self.root)
        one = manager.submit(["A"], batch_id=self.batch)
        same = manager.submit(["A"], batch_id=self.batch)
        self.assertEqual(one["job_id"], same["job_id"])
        self.assertEqual(len(executor.pending), 1)

    def test_quality_job_is_reused_and_stale_job_never_calls_worker(self):
        from backend.quality_jobs import QualityJobManager
        self.build("A")
        executor = DeferredExecutor()
        called = []
        manager = QualityJobManager(self.root / "system" / "quality_jobs",
                                   runner=lambda *args, **kwargs: called.append(args) or {},
                                   executor=executor, data_root=self.root)
        one = manager.submit(batch_id=self.batch, task_ids=["A"])
        same = manager.submit(batch_id=self.batch, task_ids=["A"])
        self.assertEqual(one["job_id"], same["job_id"])
        self.annotation["sheets"]["VLA trajectories"][0]["action"] = "click"
        self.store.publish(self.batch, "02_annotation", self.annotation,
                           metadata={"raw_root": str(self.root / "raw")})
        executor.finish()
        self.assertEqual(called, [])
        self.assertEqual(manager.get(one["job_id"])["status"], "stale")

    def test_overlapping_tree_requests_only_reserve_unclaimed_tasks(self):
        from backend.tree_build_jobs import TreeBuildJobManager
        executor = DeferredExecutor()
        manager = TreeBuildJobManager(self.root / "system" / "tree_jobs", executor=executor, data_root=self.root)
        first = manager.submit(["A"], batch_id=self.batch)
        second = manager.submit(["A", "B"], batch_id=self.batch)
        self.assertEqual(first["task_ids"], ["A"])
        self.assertEqual(second["task_ids"], ["B"])
        self.assertEqual(set(second["task_fingerprints"]), {"B"})
        still_waiting = manager.submit(["A", "B"], batch_id=self.batch)
        self.assertEqual(still_waiting["status"], "queued")
        self.assertEqual(len(executor.pending), 2)

    def test_overlapping_quality_requests_never_report_active_work_succeeded(self):
        from backend.quality_jobs import QualityJobManager
        self.build("A")
        self.build("B")
        executor = DeferredExecutor()
        manager = QualityJobManager(self.root / "system" / "quality_jobs", executor=executor, data_root=self.root)
        one = manager.submit(batch_id=self.batch, task_ids=["A"])
        two = manager.submit(batch_id=self.batch, task_ids=["A", "B"])
        self.assertEqual(one["task_ids"], ["A"])
        self.assertEqual(two["task_ids"], ["B"])
        self.assertEqual(set(two["task_fingerprints"]), {"B"})
        again = manager.submit(batch_id=self.batch, task_ids=["A", "B"])
        self.assertEqual(again["status"], "queued")
        self.assertEqual(len(executor.pending), 2)


if __name__ == "__main__":
    unittest.main()
