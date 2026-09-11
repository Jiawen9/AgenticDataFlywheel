from __future__ import annotations

import copy
import json
import tempfile
import threading
import unittest
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.task_generation.jobs import AugmentationStateError, TaskGenerationJobManager
from backend.task_generation.knowledge_base import tree_payload
from backend.task_generation.router import router
from backend.task_generation.service import run_augmentation_classification, run_augmentation_generation
from backend.task_generation.tree_store import save_tree
from backend.tests.test_task_generation import _write_knowledge_base


class DeferredExecutor:
    def __init__(self):
        self.pending = []
        self.submissions = 0

    def submit(self, fn, *args, **kwargs):
        self.submissions += 1
        self.pending.append((fn, args, kwargs))
        return Future()

    def run_next(self):
        fn, args, kwargs = self.pending.pop(0)
        fn(*args, **kwargs)

    def run_all(self):
        while self.pending:
            self.run_next()


class LocalModel:
    """All inference in this suite is deterministic and stays in this process."""
    config = SimpleNamespace(max_concurrent=2, validation_retries=0,
                             classification_max_tokens=200, generation_max_tokens=200)

    def __init__(self):
        self.calls = []
        self.responses = {}
        self.lock = threading.Lock()

    def complete(self, prompt, *, stage, item_id, **kwargs):
        with self.lock:
            self.calls.append((stage, item_id, prompt))
        value = self.responses.get((stage, item_id))
        if isinstance(value, Exception):
            raise value
        if value is None:
            value = ({"scene": "场景A", "capability": "能力A", "sub_capability": "子能力A", "reason": "模拟匹配"}
                     if stage == "classifying" else [{"task": f"请在 AppA 完成第 {item_id} 行的变体任务"}])
        return json.dumps(value, ensure_ascii=False)


class AugmentationPreviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.kb = self.base / "kb"
        _write_knowledge_base(self.kb)
        self.executor = DeferredExecutor()
        self.model = LocalModel()
        self.manager = self.make_manager()

    def make_manager(self, **kwargs):
        manager = TaskGenerationJobManager(
            self.base / "jobs", self.base / "runs", self.base / "exports", self.kb, self.base / "logs",
            executor=kwargs.pop("executor", self.executor),
            classification_runner=lambda seeds, **options: run_augmentation_classification(seeds, model=self.model, **options),
            generation_runner=lambda seeds, count, **options: run_augmentation_generation(seeds, count, model=self.model, **options),
            **kwargs,
        )
        self.addCleanup(manager.shutdown)
        return manager

    def workbook(self, rows=None):
        path = self.base / "seeds.xlsx"
        pd.DataFrame(rows or [{"任务": "失败任务", "涉及APP": "AppA", "任务结果": False}]).to_excel(path, index=False)
        return path

    def submit(self, rows=None, *, count=1, auto_start=False):
        return self.manager.submit_augmentation(self.workbook(rows), "seeds.xlsx", count, auto_start=auto_start)

    def ready(self, rows=None, *, count=1):
        job = self.submit(rows, count=count)
        self.executor.run_next()
        self.assertEqual(self.manager.get(job["job_id"])["status"], "awaiting_confirmation")
        return job["job_id"]

    def test_preview_stops_after_classification_and_uses_four_snapshot_ids(self):
        job = self.submit()
        before = self.manager.augmentation_preview(job["job_id"])
        self.assertTrue(before["available"])
        self.assertEqual(before["seeds"][0]["classification_status"], "pending")
        with self.assertRaises(AugmentationStateError):
            self.manager.start_augmentation(job["job_id"])
        self.executor.run_next()
        preview = self.manager.augmentation_preview(job["job_id"])
        self.assertEqual(preview["stats"], {"total": 1, "matched": 1, "unmatched": 0, "classification_failed": 0, "eligible": 1})
        seed = preview["seeds"][0]
        scene = preview["tree"]["scenes"][0]
        capability = scene["children"][0]
        task_type = capability["children"][0]
        app = task_type["children"][0]
        self.assertEqual(seed["node_path_ids"], [scene["id"], capability["id"], task_type["id"], app["id"]])
        self.assertEqual(seed["classification_source"], "model")
        self.assertEqual(seed["reason"], "模拟匹配")
        self.assertEqual(seed["generation_status"], "waiting")
        self.assertEqual([call[0] for call in self.model.calls], ["classifying"])
        self.assertEqual(self.manager.results(job["job_id"]), [])
        self.assertEqual(self.executor.pending, [])
        self.assertNotIn("tree", self.manager.augmentation_preview(job["job_id"], include_tree=False))

    def test_start_reuses_classification_and_snapshot_after_live_tree_edit(self):
        job_id = self.ready()
        preview = self.manager.augmentation_preview(job_id)
        current = tree_payload(self.kb)
        scenes = copy.deepcopy(current["scenes"])
        scenes[0]["label"] = "后来修改的场景"
        save_tree(scenes, current["version"], root=self.kb)
        job = self.manager.start_augmentation(job_id)
        self.assertEqual(job["status"], "queued")
        self.assertEqual(self.manager.augmentation_preview(job_id)["seeds"][0]["generation_status"], "waiting")
        self.executor.run_next()
        result = self.manager.results(job_id)[0]
        self.assertEqual(result["scene"], "场景A")
        self.assertEqual(result["seed_id"], preview["seeds"][0]["seed_id"])
        self.assertEqual(self.manager.augmentation_preview(job_id)["tree"]["version"], preview["tree"]["version"])
        self.assertEqual([call[0] for call in self.model.calls], ["classifying", "generating"])
        self.assertIn("操控描述", self.model.calls[-1][2])
        self.assertEqual(self.manager.augmentation_preview(job_id)["seeds"][0]["generation_status"], "succeeded")
        self.assertEqual(self.manager.get(job_id)["status"], "succeeded")

    def test_restart_restores_ready_preview_without_model_calls(self):
        job_id = self.ready()
        expected = self.manager.augmentation_preview(job_id)
        restored = self.make_manager(executor=DeferredExecutor())
        self.assertEqual(restored.get(job_id)["status"], "awaiting_confirmation")
        self.assertEqual(restored.augmentation_preview(job_id), expected)
        self.assertEqual(len(self.model.calls), 1)
        restored.start_augmentation(job_id)
        restored._executor.run_next()
        self.assertEqual(restored.get(job_id)["status"], "succeeded")
        self.assertEqual(len(self.model.calls), 2)

    def test_restart_recovers_final_classification_checkpoint_before_ready_update(self):
        job_id = self.ready()
        self.manager._progress(job_id, {"status": "running", "stage": "classifying", "classification_completed": False})
        restored = self.make_manager(executor=DeferredExecutor())
        self.assertEqual(restored.get(job_id)["status"], "awaiting_confirmation")
        self.assertTrue(restored.get(job_id)["classification_completed"])
        self.assertEqual(len(self.model.calls), 1)

    def test_restart_interrupts_unfinished_classification_and_queued_generation(self):
        matching = self.submit()
        restored = self.make_manager(executor=DeferredExecutor())
        self.assertEqual(restored.get(matching["job_id"])["status"], "interrupted")
        self.assertTrue(restored.augmentation_preview(matching["job_id"])["available"])
        self.assertEqual(self.model.calls, [])
        # The old simulated process will not be resumed.
        self.executor.pending.clear()
        job_id = self.ready()
        self.manager.start_augmentation(job_id)
        restored = self.make_manager(executor=DeferredExecutor())
        self.assertEqual(restored.get(job_id)["status"], "interrupted")
        self.assertEqual(restored.augmentation_preview(job_id)["stats"]["matched"], 1)
        self.assertEqual(restored.augmentation_preview(job_id)["seeds"][0]["generation_status"], "failed")
        with self.assertRaises(AugmentationStateError):
            restored.start_augmentation(job_id)
        self.assertEqual(len(self.model.calls), 1)

    def test_restart_preserves_completed_seed_results_and_stops_active_seeds(self):
        job_id = self.ready([{"任务": f"失败任务 {index}", "涉及APP": "AppA"} for index in range(2)])
        self.manager.start_augmentation(job_id)
        self.manager._progress(job_id, {"status": "running", "stage": "generating"})
        seeds = self.manager._read_seeds(job_id)
        row = {"result_id": "completed-row", "seed_id": seeds[0]["seed_id"], "source_row": seeds[0]["source_row"], "task": "完成的变体任务"}
        first = {**seeds[0], "generation_status": "succeeded", "result_count": 1}
        self.manager._checkpoint_seed(job_id, first, [row])
        self.manager._checkpoint_seed(job_id, {**seeds[1], "generation_status": "generating"}, None)
        with self.assertRaises(AugmentationStateError):
            self.manager.patch_result(job_id, row["result_id"], {"task": "运行中不应覆盖"})
        restored = self.make_manager(executor=DeferredExecutor())
        self.assertEqual(restored.get(job_id)["status"], "interrupted")
        self.assertEqual(restored.get(job_id)["interrupted_stage"], "generating")
        self.assertEqual(restored.results(job_id), [row])
        states = restored.augmentation_preview(job_id)["seeds"]
        self.assertEqual(states[0], first)
        self.assertEqual(states[1]["generation_status"], "failed")
        self.assertIn("服务重启", states[1]["error"])
        restored.patch_result(job_id, row["result_id"], {"task": "结束后可审核"})
        self.assertEqual(restored.results(job_id)[0]["task"], "结束后可审核")
        self.assertTrue(all(call[0] == "classifying" for call in self.model.calls))

    def test_fatal_generation_error_keeps_checkpoints_and_finishes_row_states(self):
        job_id = self.ready([{"任务": f"失败任务 {index}", "涉及APP": "AppA"} for index in range(2)])

        def fatal_runner(seeds, count, *, on_seed, **kwargs):
            row = {"result_id": "saved", "seed_id": seeds[0]["seed_id"], "task": "已保存的变体任务"}
            on_seed({**seeds[0], "generation_status": "succeeded", "result_count": 1}, [row])
            on_seed({**seeds[1], "generation_status": "generating"}, None)
            raise RuntimeError("模拟作业级故障")

        self.manager.generation_runner = fatal_runner
        self.manager.start_augmentation(job_id)
        self.executor.run_next()
        self.assertEqual(self.manager.get(job_id)["status"], "failed")
        self.assertEqual(len(self.manager.results(job_id)), 1)
        seeds = self.manager.augmentation_preview(job_id)["seeds"]
        self.assertEqual([seed["generation_status"] for seed in seeds], ["succeeded", "failed"])
        self.assertIn("模拟作业级故障", seeds[1]["error"])

    def test_concurrent_and_repeated_start_submit_generation_only_once(self):
        job_id = self.ready()
        with ThreadPoolExecutor(max_workers=6) as executor:
            jobs = list(executor.map(lambda _index: self.manager.start_augmentation(job_id), range(12)))
        self.assertTrue(all(job["job_id"] == job_id for job in jobs))
        self.assertEqual(self.executor.submissions, 2)
        self.assertEqual(len(self.executor.pending), 1)
        self.executor.run_next()
        self.manager.start_augmentation(job_id)
        self.assertEqual(self.executor.submissions, 2)
        self.assertEqual(len(self.manager.results(job_id)), 1)

    def test_unclassified_and_invalid_excel_paths_are_preserved_and_generated(self):
        rows = [
            {"app": "AppA", "task": "已有无匹配任务", "scene": "旧场景", "capability": "旧能力", "sub_capability": "旧类型"},
            {"app": "AppA", "task": "已有未分类任务", "scene": "Unclassified", "capability": "Unclassified", "sub_capability": "Unclassified"},
        ]
        job_id = self.ready(rows)
        preview = self.manager.augmentation_preview(job_id)
        self.assertEqual(preview["stats"], {"total": 2, "matched": 0, "unmatched": 2, "classification_failed": 0, "eligible": 2})
        self.assertEqual([seed["mapping_status"] for seed in preview["seeds"]], ["not_found", "unclassified"])
        self.assertTrue(all(seed["node_path_ids"] == [] and seed["classification_source"] == "excel" for seed in preview["seeds"]))
        self.assertEqual(preview["seeds"][0]["scene"], "旧场景")
        self.assertEqual(self.model.calls, [])
        self.manager.start_augmentation(job_id)
        self.executor.run_next()
        self.assertEqual(len(self.manager.results(job_id)), 2)
        self.assertTrue(all(call[0] == "generating" for call in self.model.calls))

    def test_model_unclassified_is_eligible_and_not_a_classification_failure(self):
        self.model.responses[("classifying", "2")] = {"scene": "Unclassified", "capability": "Unclassified", "sub_capability": "Unclassified"}
        job_id = self.ready()
        seed = self.manager.augmentation_preview(job_id)["seeds"][0]
        self.assertEqual(seed["classification_status"], "classified")
        self.assertEqual(seed["mapping_status"], "unclassified")
        self.assertEqual(seed["node_path_ids"], [])

    def test_duplicate_tasks_keep_distinct_source_rows_and_seed_ids(self):
        job_id = self.ready([
            {"任务": "重复失败任务", "涉及APP": "AppA", "任务结果": False},
            {"任务": "不应入选", "涉及APP": "AppA", "任务结果": True},
            {"任务": "重复失败任务", "涉及APP": "AppA", "任务结果": False},
        ])
        seeds = self.manager.augmentation_preview(job_id)["seeds"]
        self.assertEqual([seed["source_row"] for seed in seeds], [2, 4])
        self.assertNotEqual(seeds[0]["seed_id"], seeds[1]["seed_id"])
        self.manager.start_augmentation(job_id)
        self.executor.run_next()
        self.assertEqual({row["seed_id"] for row in self.manager.results(job_id)}, {seed["seed_id"] for seed in seeds})

    def test_classification_and_generation_failures_keep_partial_results(self):
        self.model.responses[("classifying", "3")] = ValueError("模拟分类失败")
        self.model.responses[("generating", "4")] = ValueError("模拟扩增失败")
        rows = [{"任务": f"失败任务 {index}", "涉及APP": "AppA"} for index in range(3)]
        job_id = self.ready(rows, count=2)
        before = self.manager.augmentation_preview(job_id)
        self.assertEqual(before["stats"], {"total": 3, "matched": 2, "unmatched": 0, "classification_failed": 1, "eligible": 2})
        self.assertEqual(before["seeds"][1]["generation_status"], "skipped")
        self.manager.start_augmentation(job_id)
        self.executor.run_next()
        self.assertEqual(self.manager.get(job_id)["status"], "partial")
        seeds = self.manager.augmentation_preview(job_id)["seeds"]
        self.assertEqual([seed["generation_status"] for seed in seeds], ["partial", "skipped", "failed"])
        self.assertEqual([seed["result_count"] for seed in seeds], [1, 0, 0])
        self.assertEqual(len(self.manager.get(job_id)["errors"]), 3)
        result = self.manager.results(job_id)[0]
        self.assertEqual(result["seed_id"], seeds[0]["seed_id"])
        exported = self.manager.export(job_id)
        self.assertEqual(len(pd.read_excel(exported["path"])), 1)
        restored = self.make_manager(executor=DeferredExecutor())
        self.assertEqual(restored.augmentation_preview(job_id)["seeds"], seeds)
        self.assertEqual(restored.results(job_id), [result])

    def test_all_classification_failures_do_not_enable_start(self):
        self.model.responses[("classifying", "2")] = ValueError("模拟分类失败")
        job = self.submit(auto_start=True)
        self.executor.run_all()
        self.assertEqual(self.manager.get(job["job_id"])["status"], "failed")
        preview = self.manager.augmentation_preview(job["job_id"])
        self.assertEqual(preview["stats"]["eligible"], 0)
        self.assertEqual(preview["stats"]["classification_failed"], 1)
        with self.assertRaises(AugmentationStateError):
            self.manager.start_augmentation(job["job_id"])
        self.assertEqual([call[0] for call in self.model.calls], ["classifying"])

    def test_legacy_automatic_submission_still_generates_without_confirmation(self):
        job = self.submit(auto_start=True)
        self.executor.run_all()
        self.assertEqual(self.manager.get(job["job_id"])["status"], "succeeded")
        self.assertEqual(len(self.manager.results(job["job_id"])), 1)
        self.assertTrue(self.manager.augmentation_preview(job["job_id"])["available"])

    def test_old_history_has_no_preview_and_reading_never_reclassifies(self):
        job = self.manager._new_job("augmentation", total_items=1, generate_n=1)
        self.manager._finish(job["job_id"], {"results": [{"result_id": "legacy", "task": "原结果"}], "errors": []})
        preview = self.manager.augmentation_preview(job["job_id"])
        self.assertFalse(preview["available"])
        self.assertEqual(preview["seeds"], [])
        self.assertNotIn("tree", preview)
        self.assertEqual(self.manager.results(job["job_id"])[0]["task"], "原结果")
        with self.assertRaises(AugmentationStateError):
            self.manager.start_augmentation(job["job_id"])
        self.assertEqual(self.model.calls, [])

    def test_executor_rejection_is_failed_and_cannot_start_twice(self):
        job_id = self.ready()
        with patch.object(self.executor, "submit", side_effect=RuntimeError("模拟执行器已停止")):
            with self.assertRaises(AugmentationStateError):
                self.manager.start_augmentation(job_id)
        self.assertEqual(self.manager.get(job_id)["status"], "failed")
        self.assertEqual(self.manager.start_augmentation(job_id)["status"], "failed")
        self.assertEqual(self.executor.submissions, 1)

    def test_incomplete_or_missing_seed_file_never_starts_generation(self):
        job_id = self.ready()
        seeds = self.manager._read_seeds(job_id)
        seeds[0]["classification_status"] = "pending"
        self.manager._write_json(self.manager._seeds_path(job_id), seeds)
        with self.assertRaises(AugmentationStateError):
            self.manager.start_augmentation(job_id)
        self.assertEqual(self.executor.submissions, 1)

    def test_api_preview_start_errors_and_auto_start_default(self):
        app = FastAPI()
        app.include_router(router)
        workbook = self.workbook().read_bytes()
        with patch("backend.task_generation.router.manager", self.manager), TestClient(app) as client:
            response = client.post("/api/task-generation/augmentation-jobs", data={"generate_n": 1, "auto_start": "false"},
                                   files={"file": ("seeds.xlsx", workbook)})
            self.assertEqual(response.status_code, 202)
            job_id = response.json()["job_id"]
            prefix = f"/api/task-generation/jobs/{job_id}"
            self.assertEqual(client.post(prefix + "/start-augmentation").status_code, 409)
            self.executor.run_next()
            preview = client.get(prefix + "/augmentation-preview").json()
            self.assertEqual(preview["stats"]["matched"], 1)
            self.assertIn("tree", preview)
            self.assertNotIn("tree", client.get(prefix + "/augmentation-preview?include_tree=false").json())
            self.assertEqual(client.post(prefix + "/start-augmentation").status_code, 202)
            self.executor.run_next()
            self.assertEqual(client.get(prefix).json()["status"], "succeeded")
            self.assertEqual(client.post(prefix + "/start-augmentation").status_code, 202)
            self.assertEqual(client.get("/api/task-generation/jobs/missing/augmentation-preview").status_code, 404)
            self.assertEqual(client.post("/api/task-generation/jobs/missing/start-augmentation").status_code, 404)
            automatic = client.post("/api/task-generation/augmentation-jobs", data={"generate_n": 1}, files={"file": ("seeds.xlsx", workbook)})
            self.executor.run_all()
            self.assertEqual(self.manager.get(automatic.json()["job_id"])["status"], "succeeded")
            invalid = client.post("/api/task-generation/augmentation-jobs", data={"generate_n": 0}, files={"file": ("seeds.xlsx", workbook)})
            self.assertEqual(invalid.status_code, 422)


if __name__ == "__main__":
    unittest.main()
