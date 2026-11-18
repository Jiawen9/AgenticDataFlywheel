import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.batch_results import annotation_task_fingerprints, digest
from backend.data_store import ArtifactStore
from backend.export_markers import annotate_rows
from backend.trajectory_correction import draft_store, router
from backend.trajectory_correction.session_state import identified


class CorrectionTreeWorkspaceTests(unittest.TestCase):
    def setUp(self):
        scratch = TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        self.store = ArtifactStore(self.root)
        patcher = patch.object(draft_store, "CORRECTION_SESSIONS_DIR", self.root / "sessions")
        patcher.start()
        self.addCleanup(patcher.stop)
        app = FastAPI()
        app.include_router(router.router)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        self.batch = "batch-tree"
        self.session_id = "s261118-001"
        groups = [self.group("A", "tr-first", 2), self.group("A", "tr-second", 5),
                  self.group("A", "tr-third", 8), self.group("B", "tr-first", 11)]
        self.snapshot = identified({"groups": groups})
        self.table = {"sheets": {"VLA trajectories": [row for group in groups for row in group["rows"]]}}
        self.inputs = annotation_task_fingerprints(self.table)
        self.tree = {"id": 0, "children": [self.node(10, [("tr-first", 1), ("tr-second", 1), ("tr-third", 1)],
                     [self.node(11, [("tr-first", 3), ("tr-second", 3)]), self.node(12, [("tr-third", 3)])])],
                     "source_trajectories": [], "decisions": []}
        hashes = {"A": digest(self.tree)}
        self.tree_payload = {"trees": {"A": self.tree}, "tree_hashes": hashes,
                             "task_fingerprints": self.inputs, "source_task_fingerprints": self.inputs}
        self.quality = {"source_tree_hashes": hashes, "tasks": [{"task_id": "A",
                        "scoring_version": "checklist_fusion_v1", "node_scores": {
                            "10": self.score(.65, 3), "11": self.score(.82, 2), "12": self.score(.31, 1),
                        }}]}
        self.store.publish(self.batch, "02_annotation", self.table)
        self.tree_ref = self.store.publish(self.batch, "04_tree", self.tree_payload)
        self.quality_ref = self.store.publish(self.batch, "05_quality", self.quality)
        self.draft = draft_store.save_session({
            "session_id": self.session_id, "batch_id": self.batch,
            "task_fingerprints": self.inputs, "source_id": "test",
            "snapshot_payload": self.snapshot, "row_edits": {}, "group_exports": {},
        })
        self.url = f"/api/correction/sessions/{self.session_id}/tasks"

    @staticmethod
    def group(task, trajectory, first):
        return {"group_id": f"{task}-{trajectory}", "task_id": task, "meta_task": trajectory,
                "task": f"任务 {task}", "quality": "", "prefix": "", "export": False,
                "rows": [{"excel_row": first + step - 1, "step": step, "task_id": task,
                          "image": f"{task}/{trajectory}/step{step:03}.jpg", "actions": '{"action":"wait"}',
                          "action": {"action": "wait"}, "sop": ""}
                         for step in (1, 2, 3)]}

    @staticmethod
    def score(q, count):
        return {"q": q, "scored_count": count, "total_count": count, "status": "valid"}

    @staticmethod
    def node(node_id, identities, children=None):
        return {"id": node_id, "occurrences": [
            {"trajectory": trajectory, "step": step, "image": f"A/{trajectory}/step{step:03}.jpg", "excel_row": 99}
            for trajectory, step in identities], "children": children or []}

    def tree_get(self, task="A", **params):
        return self.client.get(self.url, params={"view": "tree", "task_id": task, **params})

    def test_shared_nodes_map_actual_draft_steps_and_match_export_markers(self):
        self.draft["row_edits"] = {"4": {"actions": '{"action":"click"}'}, "7": {"actions": '{ "action": "wait" }'}}
        self.draft = draft_store.save_session(self.draft)
        response = self.tree_get(expected_revision=self.draft["storage_revision"])
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual(result["tree_version"], self.tree_ref["version"])
        self.assertEqual(result["quality_version"], self.quality_ref["version"])
        self.assertEqual(result["key_node_ids"], [11, 12])
        self.assertEqual(result["node_scores"]["11"]["q"], .82)
        self.assertNotIn("source_trajectories", result["tree"])
        self.assertEqual([target["excel_row"] for target in result["targets"]["11"]], [4, 7])
        self.assertEqual([target["edited"] for target in result["targets"]["11"]], [True, False])
        self.assertEqual([target["step_key"] for target in result["targets"]["11"]],
                         [group["rows"][-1]["step_key"] for group in self.snapshot["groups"][:2]])
        self.assertNotIn(2, [target["step"] for entries in result["targets"].values() for target in entries])
        exported = annotate_rows(self.snapshot["groups"][:3], tree_payload=self.tree_payload,
                                 quality_payload=self.quality, row_edits=self.draft["row_edits"])
        self.assertEqual({group["rows"][-1]["source_node_id"] for group in exported
                          if group["rows"][-1]["is_key_step"]}, set(result["key_node_ids"]))
        self.assertEqual(draft_store.load_session(self.session_id), self.draft)

    def test_missing_q_is_navigable_and_unverified_tree_falls_back(self):
        self.quality["source_tree_hashes"] = {"A": "old"}
        self.store.publish(self.batch, "05_quality", self.quality)
        result = self.tree_get().json()
        self.assertTrue(result["tree"])
        self.assertEqual(result["key_node_ids"], [])
        self.assertEqual(result["node_scores"], {})
        self.draft["task_fingerprints"] = {"A": "old"}
        self.draft = draft_store.save_session(self.draft)
        result = self.tree_get().json()
        self.assertIsNone(result["tree"])
        self.assertEqual(result["targets"], {})
        self.assertIn("步骤列表", result["reason"])

    def test_old_draft_without_step_identity_keeps_list_and_has_no_tree_targets(self):
        for group in self.draft["snapshot_payload"]["groups"]:
            for row in group["rows"]:
                row.pop("step_key")
        self.draft = draft_store.save_session(self.draft)
        response = self.tree_get()
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.json()["tree"])
        self.assertEqual(response.json()["targets"], {})
        response = self.client.get(self.url, params={"view": "summary", "unit": "task", "task_id": "A"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(response.json()["items"]), 3)

    def test_save_and_restore_one_shared_step_preserves_other_trajectory(self):
        url = self.url.replace("/tasks", "/rows/4")
        baseline = self.draft["storage_revision"]
        for action, edited in (('{"action":"wait","duration":3}', True), ('{"action":"wait"}', False)):
            saved = self.client.patch(url, json={"actions": action, "expected_revision": baseline})
            self.assertEqual(saved.status_code, 200, saved.text)
            baseline = saved.json()["storage_revision"]
            navigable = self.tree_get(expected_revision=baseline)
            self.assertEqual(navigable.status_code, 200, navigable.text)
            self.assertEqual([value["edited"] for value in navigable.json()["targets"]["11"]], [edited, False])

    def test_revision_and_exact_task_page_do_not_cross_task_boundaries(self):
        stale = self.tree_get(expected_revision=self.draft["storage_revision"] + 1)
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(self.tree_get(task="other").status_code, 404)
        response = self.client.get(self.url, params={"view": "summary", "unit": "task", "task_id": "B"})
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual([group["task_id"] for group in result["items"]], ["B"])
        self.assertEqual(result["total"], 1)


if __name__ == "__main__":
    unittest.main()
