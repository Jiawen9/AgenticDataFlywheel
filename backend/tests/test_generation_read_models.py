from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.data_store.registry import RecordStore
from backend.task_generation import router as routes
from backend.task_generation import resource_views


class GenerationReadModelsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.store = RecordStore(self.root)
        app = FastAPI()
        app.include_router(routes.router)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        manager = SimpleNamespace(store=self.store, knowledge_base_dir=self.root / "知识库")
        self.patch = patch.object(routes, "manager", manager)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def job(self, identity, kind="task_generation", **extra):
        self.store.put("task_generation.jobs", identity, {
            "job_id": identity, "kind": kind, "status": "succeeded", "stage": "succeeded",
            "created_at": "2026-11-17", "input_filename": "中文.xlsx", "result_count": 100,
            "execution_units": [{"private_detail": "large"}] * 100, "knowledge_base": {"directory": "private"}, **extra})

    def test_history_route_pages_projected_records_without_detail_reads(self):
        for index in range(25):
            self.job(f"job-{index:02}", "augmentation" if index < 5 else "task_generation")
        with patch.object(self.store, "list", side_effect=AssertionError("full list read")), patch.object(self.store, "get", side_effect=AssertionError("detail read")):
            value = self.client.get("/api/task-generation/jobs?view=summary&kind=task_generation&limit=10&offset=10").json()
        self.assertEqual(value["filtered_total"], 20)
        self.assertEqual(len(value["items"]), 10)
        self.assertTrue(all("execution_units" not in item and "knowledge_base" not in item for item in value["items"]))
        filtered = self.client.get("/api/task-generation/jobs?view=summary&kind=augmentation&search=中文").json()
        self.assertEqual(filtered["filtered_total"], 5)

    def test_result_page_preserves_dependency_group_and_global_counts(self):
        self.job("g")
        rows = []
        for index in range(23):
            shared = {"app": "测试", "scene": "场景", "capability": "能力", "sub_capability": "子能力", "dependency_group_id": str(index), "deleted": index == 22}
            rows.extend([{**shared, "result_id": f"pre{index}", "task_uuid": f"pre{index}", "pre_dependency": "pre_node", "task": "准备"},
                         {**shared, "result_id": f"main{index}", "pre_task_uuid": f"pre{index}", "pre_dependency": "weak", "task": "执行"}])
        self.store.put("task_generation.results", "g", {"rows": rows})
        value = self.client.get("/api/task-generation/jobs/g/results?view=groups&offset=10&limit=10").json()
        self.assertEqual(value["total"], 22)
        self.assertEqual(len(value["groups"]), 10)
        self.assertEqual(len(value["results"]), 20)
        self.assertEqual(value["counts"]["active"], 44)
        self.assertEqual({row["dependency_group_id"] for row in value["groups"][0]["results"]}, {"10"})
        # Saving a row changes the same authoritative record; the next page sees it.
        rows[21]["task"] = "保存后的修改"
        self.store.put("task_generation.results", "g", {"rows": rows})
        changed = self.client.get("/api/task-generation/jobs/g/results?view=groups&offset=10&limit=1").json()
        self.assertEqual(changed["results"][1]["task"], "保存后的修改")
        self.assertEqual(len(self.store.get("task_generation.results", "g")["rows"]), 46)

    def test_augmentation_page_filters_by_seed_and_keeps_all_variants(self):
        self.job("a", "augmentation")
        self.store.put("task_generation.results", "a", {"rows": [
            {"result_id": f"r{seed}-{variant}", "seed_id": f"s{seed}", "task": "任务", "app": "示例", "deleted": variant == 2}
            for seed in range(25) for variant in range(3)]})
        value = self.client.get("/api/task-generation/jobs/a/results?view=groups&limit=10&show_deleted=true").json()
        self.assertEqual(value["total"], 25)
        self.assertEqual(len(value["results"]), 30)
        self.assertEqual(value["counts"]["active"], 50)
        located = self.client.get("/api/task-generation/jobs/a/results?view=groups&seed_id=s20&show_deleted=true").json()
        self.assertEqual(located["total"], 1)
        self.assertEqual({row["seed_id"] for row in located["results"]}, {"s20"})

    def test_cold_resource_get_is_nonblocking_and_reuses_version(self):
        started, release = threading.Event(), threading.Event()
        def slow_tree(_):
            started.set()
            release.wait(5)
            return {"version": "one", "scenes": []}
        with patch.object(resource_views, "current_root", side_effect=lambda root: root), patch.object(resource_views, "tree_payload", side_effect=slow_tree) as tree, patch.object(resource_views, "list_knowledge_bases", return_value=[]):
            first = self.client.get("/api/task-generation/tree?view=summary").json()
            self.assertEqual(first["index_state"], "preparing")
            self.assertTrue(started.wait(1))
            self.assertEqual(self.client.get("/api/task-generation/tree?view=summary").json()["index_state"], "preparing")
            release.set()
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                value = self.client.get("/api/task-generation/tree?view=summary").json()
                if value["index_state"] == "ready":
                    break
                time.sleep(.01)
            self.assertEqual(value["tree"]["version"], "one")
            self.client.get("/api/task-generation/tree?view=summary")
            self.assertEqual(tree.call_count, 1)


if __name__ == "__main__":
    unittest.main()
