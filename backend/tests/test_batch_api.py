from __future__ import annotations

from copy import deepcopy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend import batch_api, data_registry_api
from backend.batch_results import annotation_task_fingerprints, invalidate_tasks
from backend.data_store import ArtifactStore


class BatchApiTests(unittest.TestCase):
    """Exercise current batch routes without importing the application managers."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "data"
        self.raw = self.root / "raw" / "batch-one"
        self.raw.mkdir(parents=True)
        self.store = ArtifactStore(self.root)
        for module in (batch_api, data_registry_api):
            patcher = patch.object(module, "store", self.store)
            patcher.start()
            self.addCleanup(patcher.stop)
        hook = patch("backend.batch_results._notify_invalidated")
        hook.start()
        self.addCleanup(hook.stop)
        app = FastAPI()
        app.include_router(batch_api.router)
        app.include_router(data_registry_api.router)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        self.batch = "batch-one"
        rows = [{"task_id": task, "trajectory_id": task + "-1", "image": task + "/step1.png",
                 "xml": task + "/step1.xml", "action": '{"action":"wait"}',
                 "summary": "等待", "actions_box": ""} for task in ("A", "B")]
        self.annotation = {"schema_version": 1, "columns": {"Steps": list(rows[0])}, "sheets": {"Steps": rows}}
        self.annotation_ref = self.store.publish(self.batch, "02_annotation", self.annotation,
                                                 metadata={"raw_root": "raw/batch-one"})

    def get(self, suffix):
        response = self.client.get(f"/api/data-batches/{self.batch}/{suffix}")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["cache-control"], "no-store")
        return response.json()

    def tree(self, tasks):
        fingerprints = annotation_task_fingerprints(self.annotation)
        rows = {task: {"id": 0, "task_id": task, "label": "桌面", "children": []} for task in tasks}
        summaries = [{"task_id": task, "goal": "任务 " + task, "trajectory_count": 1,
                      "original_step_count": 1, "tree_step_count": 1,
                      "ignored_step_count": 0, "action_node_count": 1} for task in tasks]
        payload = {"schema_version": 2, "trees": rows, "tasks": summaries,
                   "source_annotation": deepcopy(self.annotation), "quality_input": {"sheets": {}},
                   "source_task_fingerprints": {task: fingerprints[task] for task in tasks},
                   "task_fingerprints": {task: "fingerprint-" + task for task in tasks},
                   "tree_hashes": {task: "hash-" + task for task in tasks},
                   "completed_at": "2026-09-20T00:00:00Z", "model_name": "local-fixture"}
        self.store.publish_many(self.batch, [
            {"stage": "03_observation", "payload": {"trajectories": [{"task_id": task} for task in tasks],
                "rows": [{"任务编号": task} for task in tasks]}},
            {"stage": "04_tree", "payload": payload, "source_stages": ["03_observation"]},
        ])
        return rows

    def quality(self, tasks):
        values = [{"task_id": task, "run_id": self.batch, "status": "succeeded", "average_score": 4.5,
                   "passed_count": 1, "trajectory_count": 1, "rubric": {"dimensions": [{"dimension_id": "correctness", "dimension_name": "正确性"}]},
                   "evaluations": {task + "-1": {"global_score": 4.5, "passed_threshold": True}},
                   "completed_at": "2026-09-20T01:00:00Z"} for task in tasks]
        self.store.publish(self.batch, "05_quality", {"tasks": values,
            "source_tree_hashes": {task: "hash-" + task for task in tasks},
            "task_fingerprints": {task: "quality-" + task for task in tasks},
            "completed_at": "2026-09-20T01:00:00Z"})
        return values

    def test_collection_task_status_is_derived_from_effective_current_results(self):
        self.tree(["A"])
        self.quality(["A"])
        tasks = {item["task_id"]: item for item in batch_api.task_status_summaries(self.batch)}
        self.assertEqual((tasks["A"]["tree_status"], tasks["A"]["quality_status"]), ("succeeded", "succeeded"))
        self.assertEqual((tasks["B"]["tree_status"], tasks["B"]["quality_status"]), ("pending", "pending"))
        invalidate_tasks(self.batch, ["A"], self.root)
        tasks = {item["task_id"]: item for item in batch_api.task_status_summaries(self.batch)}
        self.assertEqual((tasks["A"]["tree_status"], tasks["A"]["quality_status"]), ("stale", "stale"))

    def test_annotation_without_any_tree_returns_all_pending_tasks_and_frontend_fields(self):
        value = self.get("tree")
        self.assertEqual((value["batch_id"], value["run_id"]), (self.batch, self.batch))
        self.assertEqual(value["task_ids"], ["A", "B"])
        self.assertEqual(value["task_count"], 2)
        self.assertEqual(value["revision"], 0)
        self.assertEqual(value["completed_at"], "")
        self.assertEqual(value["total_original_steps"], 2)
        self.assertEqual(value["total_tree_steps"], 0)
        required = {"task_id", "goal", "tree_file", "trajectory_count", "original_step_count",
                    "tree_step_count", "ignored_step_count", "action_node_count", "status", "tree_status", "quality_status"}
        for task in value["tasks"]:
            self.assertTrue(required.issubset(task))
            self.assertEqual((task["status"], task["tree_status"], task["quality_status"]), ("pending", "pending", "pending"))
        reviewed = self.get("quality")
        self.assertEqual([task["task_id"] for task in reviewed["tasks"]], ["A", "B"])
        self.assertTrue(all(task["status"] == "pending" and not task["rubric_ready"] for task in reviewed["tasks"]))
        self.assertEqual(self.client.get(f"/api/data-batches/{self.batch}/tasks/A/tree").status_code, 409)

    def test_partial_build_keeps_unprocessed_tasks_in_the_same_batch(self):
        trees = self.tree(["A"])
        value = self.get("tree")
        self.assertEqual(value["task_count"], 2)
        self.assertEqual({task["task_id"]: task["tree_status"] for task in value["tasks"]}, {"A": "succeeded", "B": "pending"})
        self.assertEqual(self.get("tasks/A/tree"), trees["A"])
        self.assertEqual(self.client.get(f"/api/data-batches/{self.batch}/tasks/B/tree").status_code, 409)
        self.tree(["A", "B"])
        self.assertEqual([task["tree_status"] for task in self.get("tree")["tasks"]], ["succeeded", "succeeded"])
        self.assertEqual(len(self.store.list(self.batch, "04_tree")), 1)

    def test_partial_quality_is_a_summary_plus_current_task_detail(self):
        self.tree(["A", "B"])
        results = self.quality(["A"])
        value = self.get("quality")
        tasks = {task["task_id"]: task for task in value["tasks"]}
        self.assertEqual(value["batch_id"], self.batch)
        self.assertEqual(tasks["A"]["status"], "succeeded")
        self.assertEqual(tasks["A"]["average_score"], 4.5)
        self.assertTrue(tasks["A"]["rubric_ready"])
        self.assertNotIn("rubric", tasks["A"])
        self.assertNotIn("evaluations", tasks["A"])
        self.assertEqual(tasks["B"]["status"], "pending")
        self.assertNotIn("average_score", tasks["B"])
        self.assertEqual(self.get("tasks/A/quality"), results[0])
        self.assertEqual(self.client.get(f"/api/data-batches/{self.batch}/tasks/B/quality").status_code, 409)

    def test_changing_a_invalidates_its_current_reads_but_keeps_b_complete(self):
        self.tree(["A", "B"])
        self.quality(["A", "B"])
        before_b = self.get("tasks/B/tree"), self.get("tasks/B/quality")
        self.annotation["sheets"]["Steps"][0]["actions_box"] = "new-box"
        self.store.publish(self.batch, "02_annotation", self.annotation, metadata={"raw_root": "raw/batch-one"})
        invalidate_tasks(self.batch, ["A"], self.root)
        tree = {task["task_id"]: task for task in self.get("tree")["tasks"]}
        quality = {task["task_id"]: task for task in self.get("quality")["tasks"]}
        self.assertEqual((tree["A"]["tree_status"], quality["A"]["status"]), ("stale", "stale"))
        self.assertEqual((tree["B"]["tree_status"], quality["B"]["status"]), ("succeeded", "succeeded"))
        self.assertNotIn("average_score", quality["A"])
        self.assertFalse(quality["A"]["rubric_ready"])
        for suffix in ("tree", "quality"):
            self.assertEqual(self.client.get(f"/api/data-batches/{self.batch}/tasks/A/{suffix}").status_code, 409)
        self.assertEqual((self.get("tasks/B/tree"), self.get("tasks/B/quality")), before_b)

    def test_all_invalidated_tasks_remain_visible_even_without_a_current_tree(self):
        self.tree(["A", "B"])
        self.quality(["A", "B"])
        invalidate_tasks(self.batch, ["A", "B"], self.root)
        tree = self.get("tree")
        self.assertEqual(tree["task_count"], 2)
        self.assertEqual([task["tree_status"] for task in tree["tasks"]], ["stale", "stale"])
        self.assertEqual([task["status"] for task in self.get("quality")["tasks"]], ["stale", "stale"])

    def test_current_download_follows_same_stage_and_old_token_is_gone(self):
        self.tree(["A"])
        first = self.store.get(self.batch, "04_tree")
        self.tree(["A", "B"])
        current = self.store.get(self.batch, "04_tree")
        value = self.get("artifacts/04_tree/files/result.json")
        self.assertEqual(set(value["trees"]), {"A", "B"})
        self.assertEqual(current["files"][0]["path"], first["files"][0]["path"])
        old = self.client.get(f'/api/data-batches/{self.batch}/artifacts/04_tree/{first["version"]}/files/result.json')
        self.assertEqual(old.status_code, 410)

    def test_unknown_batch_and_unsafe_identifier_do_not_select_another_batch(self):
        for suffix in ("tree", "quality"):
            response = self.client.get("/api/data-batches/unknown/" + suffix)
            self.assertEqual(response.status_code, 404, response.text)
        response = self.client.get("/api/data-batches/bad%5Cid/tree")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.store.list("unknown"), [])

    def test_corrupt_current_payload_reports_conflict_instead_of_empty_success(self):
        path = self.store.resolve_file(self.annotation_ref, "result.json")
        path.write_text('{}', encoding="utf-8")
        for suffix in ("tree", "quality"):
            response = self.client.get(f"/api/data-batches/{self.batch}/" + suffix)
            self.assertEqual(response.status_code, 409, response.text)
            self.assertIn("checksum", response.json()["detail"])


if __name__ == "__main__":
    unittest.main()
