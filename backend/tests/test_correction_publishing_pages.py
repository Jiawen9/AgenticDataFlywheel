"""Real route contracts: list projections must not traverse evidence or drafts."""
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.data_store import RecordStore
from backend.trajectory_correction import draft_store, service
from backend.trajectory_correction.router import router as correction_router
from backend.data_publishing.router import router as publishing_router
from backend.data_publishing.service import DatasetReleaseRegistry


class ReadPageTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = RecordStore(self.root)
        self.registry = DatasetReleaseRegistry(project_root=self.root, data_root=self.root,
            releases_file=self.root / "releases.json", trajectory_root=self.root / "raw",
            correction_exports_dir=self.root / "exports")
        app = FastAPI()
        app.include_router(correction_router)
        app.include_router(publishing_router)
        self.client = TestClient(app)
        for item in (patch.object(draft_store, "CORRECTION_SESSIONS_DIR", self.root / "sessions"),
                     patch("backend.data_publishing.router.registry", self.registry)):
            item.start()
            self.addCleanup(item.stop)

    def session(self, sid="s261117-001", groups=25):
        payload = {"session_id": sid, "storage_batch_id": "b-one", "tree_run_id": "b-one",
            "created_at": "2026-11-17T00:00:00Z", "updated_at": "2026-11-17T00:00:00Z",
            "selection": {"tasks": []}, "row_edits": {}, "snapshot_payload": {"groups": [], "row_count": groups}}
        for index in range(groups):
            group = {"group_id": f"g{index}", "task_id": f"t{index}", "task": "中文任务", "meta_task": f"tr{index}",
                "quality": "good", "prefix": "", "export": True,
                "rows": [{"excel_row": index + 2, "step": 1, "actions": '{"action":"wait"}',
                    "action": {"action": "wait"}, "image": f"task/tr{index}/step.jpg", "summary": "original"}]}
            payload["snapshot_payload"]["groups"].append(group)
            payload["selection"]["tasks"].append({"task_id": f"t{index}", "trajectory_count": 1})
            payload["row_edits"][str(index + 2)] = {"actions": '{"action":"wait","duration":2}'}
        return self.store.put("correction_sessions", sid, payload)

    def test_session_release_and_candidates_are_projection_only(self):
        self.session()
        self.store.put("dataset_releases", "d-one", {"release_id": "d-one", "name": "发布一",
            "created_at": "2026-11-17", "excel_paths": [{"path": "missing.xlsx", "sha256": "hash"}],
            "trajectory_paths": ["secret/raw/path"] * 2000, "internal_upload": {"status": "failed", "job_id": "u", "file_results": [{"error": "large"}] * 1000}})
        with patch.object(service, "_snapshot", side_effect=AssertionError("list opened snapshot")), \
             patch.object(self.registry, "_with_availability", side_effect=AssertionError("list statted files")), \
             patch.object(self.registry, "candidates", side_effect=AssertionError("list validated export")), \
             patch.object(RecordStore, "list", side_effect=AssertionError("list read full payloads")):
            sessions = self.client.get("/api/correction/sessions?view=summary&limit=10").json()
            releases = self.client.get("/api/dataset-releases?view=summary").json()
            candidates = self.client.get("/api/dataset-releases/candidates?view=summary").json()
        self.assertEqual(sessions["total"], 1)
        self.assertEqual(sessions["items"][0]["group_count"], 25)
        self.assertNotIn("snapshot_payload", sessions["items"][0])
        self.assertEqual(releases["items"][0]["excel_count"], 1)
        self.assertEqual(releases["items"][0]["trajectory_paths"], [])
        self.assertNotIn("file_results", releases["items"][0]["internal_upload"])
        self.assertEqual(candidates["items"][0]["reason"], "尚未导出完整数据集 Excel")

    def test_summary_filters_and_revision_follow_same_transaction(self):
        self.session()
        self.session("s261117-002", 3)
        page = self.client.get("/api/correction/sessions?view=summary&offset=1&limit=1").json()
        self.assertEqual(page["total"], 2)
        self.assertEqual(len(page["items"]), 1)
        self.store.update("correction_sessions", "s261117-001", lambda row: row.update(pending_review={"key": {"task_id": "t0"}}))
        exact = self.client.get("/api/correction/sessions?view=summary&session_id=s261117-001").json()
        self.assertEqual(exact["filtered_total"], 1)
        self.assertEqual(exact["items"][0]["pending_review_count"], 1)
        self.assertEqual(exact["items"][0]["storage_revision"], 2)

    def test_group_page_overlays_only_visible_groups_and_cot_only_selected_group(self):
        self.session()
        original = service._overlay_row
        with patch.object(service, "_overlay_row", wraps=original) as overlay:
            result = self.client.get("/api/correction/sessions/s261117-001/tasks?view=summary&offset=10&limit=10").json()
        self.assertEqual(result["total"], 25)
        self.assertEqual(overlay.call_count, 10)
        self.assertTrue(all("rows" not in group for group in result["items"]))
        with patch.object(service, "_overlay_row", wraps=original) as overlay:
            cot = self.client.get("/api/correction/sessions/s261117-001/cot?group_id=g4").json()
        self.assertEqual([group["group_id"] for group in cot["groups"]], ["g4"])
        self.assertLessEqual(overlay.call_count, 2)
        self.assertEqual(cot["groups"][0]["rows"][0]["excel_row"], 6)

    def test_release_detail_reads_single_record(self):
        self.store.put("dataset_releases", "one", {"release_id": "one", "batch_ids": [], "excel_paths": [], "trajectory_paths": []})
        with patch.object(RecordStore, "list", side_effect=AssertionError("full release registry read")):
            result = self.registry.get("one")
        self.assertEqual(result["release_id"], "one")

    def test_active_cot_lookup_is_scoped_before_pagination(self):
        self.store.put("correction_cot_jobs", "old-active", {"job_id": "old-active", "session_id": "s261117-001", "status": "running", "created_at": "2026-01-01"})
        self.store.put("correction_cot_jobs", "other-active", {"job_id": "other-active", "session_id": "s261117-002", "status": "running", "created_at": "2026-11-17"})
        for index in range(15):
            self.store.put("correction_cot_jobs", f"done-{index}", {"job_id": f"done-{index}", "session_id": "s261117-001", "status": "succeeded", "created_at": "2026-11-17"})
        result = self.client.get("/api/correction/cot-jobs?view=summary&session_id=s261117-001&status=active&limit=10").json()
        self.assertEqual([item["job_id"] for item in result["items"]], ["old-active"])


if __name__ == "__main__":
    unittest.main()
