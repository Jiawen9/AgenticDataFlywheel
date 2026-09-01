from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

import pandas as pd

from backend.task_generation.jobs import TaskGenerationJobManager
from backend.task_generation.knowledge_base import merged_nodes, node_id, replace_knowledge_base, tree_payload, validate_workbook
from backend.task_generation.model_client import parse_json_value, parse_jsonl_tasks
from backend.task_generation.service import _dependency_tasks, _read_seed_workbook, run_augmentation, run_initial_generation
from backend.task_generation.tree_store import current_root, flatten


def _submit_initial(manager, root):
    tree = tree_payload(root)
    leaf = flatten(tree["scenes"])[0][0]
    return manager.submit_initial([{"node_id": leaf["id"], "apps": ["AppA"]}], 1, version=tree["version"])


def _write_knowledge_base(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    pd.DataFrame([
        {"scene": "场景A", "capability": "能力A", "sub_capability": "子能力A", "target_app": "[AppA, AppB]", "use_resource_prior": True, "reference_example": "示例"},
    ]).to_excel(root / "VLA场景树.xlsx", index=False)
    pd.DataFrame([
        {"scene": "场景A", "capability": "能力A", "sub_capability": "子能力A", "target_app": "AppA", "sub_capability_desc": "操控描述"},
    ]).to_excel(root / "APP操控先验知识库.xlsx", index=False)
    with pd.ExcelWriter(root / "APP资源先验知识库.xlsx") as writer:
        pd.DataFrame([{"实体": "实体1"}, {"实体": "实体2"}]).to_excel(writer, sheet_name="AppA", index=False)
        pd.DataFrame().to_excel(writer, sheet_name="AppB", index=False)


class _FakeModel:
    def __init__(self, responses: list[str]) -> None:
        self.responses = iter(responses)
        self.lock = threading.Lock()

    def complete(self, _prompt: str, **_kwargs) -> str:
        with self.lock:
            return next(self.responses)


class KnowledgeBaseTests(unittest.TestCase):
    def test_expands_apps_merges_control_prior_and_samples_resources(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _write_knowledge_base(root)
            self.assertTrue(validate_workbook(root / "VLA场景树.xlsx", "scene_tree")["valid"])
            nodes = merged_nodes(root, sample_num=2)
            self.assertEqual([item["app"] for item in nodes], ["AppA", "AppB"])
            self.assertEqual(nodes[0]["sub_capability_desc"], "操控描述")
            self.assertEqual(len(nodes[0]["resource_prior"]), 2)
            self.assertEqual(nodes[1]["resource_prior"], [])
            tree = tree_payload(root)
            self.assertEqual(tree["leaf_count"], 1)
            self.assertEqual(tree["execution_unit_count"], 2)
            self.assertEqual(tree["scenes"][0]["label"], "场景A")
            self.assertEqual(len(flatten(tree["scenes"])[0][0]["app_configs"]), 2)

    def test_knowledge_base_replacement_validates_before_replacing_and_keeps_backup(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _write_knowledge_base(root)
            invalid = root / "invalid.xlsx"
            pd.DataFrame([{"wrong": "column"}]).to_excel(invalid, index=False)
            with self.assertRaises(ValueError):
                replace_knowledge_base("control_prior", invalid, root=root)
            self.assertTrue(validate_workbook(root / "APP操控先验知识库.xlsx", "control_prior")["valid"])
            replacement = root / "replacement.xlsx"
            original_version = current_root(root)
            pd.DataFrame([{"scene": "场景B", "capability": "能力B", "sub_capability": "子能力B", "target_app": "AppB", "sub_capability_desc": "新描述"}]).to_excel(replacement, index=False)
            replace_knowledge_base("control_prior", replacement, root=root)
            self.assertNotEqual(current_root(root), original_version)
            self.assertTrue((original_version / "APP操控先验知识库.xlsx").exists())

    def test_parser_accepts_thinking_fence_json_and_jsonl(self):
        self.assertEqual(parse_json_value("analysis </think> ```json\n{\"x\": 1}\n```", dict), {"x": 1})
        self.assertEqual(parse_jsonl_tasks('{"task":"a"}\n{"task":"b"}'), [{"task": "a"}, {"task": "b"}])


class ServiceTests(unittest.TestCase):
    def test_weak_dependency_creates_linked_pre_node(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _write_knowledge_base(root)
            model = _FakeModel([
                '{"dependency_relationships":"weak","pre_task":"AppA中准备一个收藏内容","reason":"需要历史数据"}',
                '{"scene":"场景A","capability":"能力A","sub_capability":"子能力A","reason":"匹配"}',
            ])
            result = _dependency_tasks({"app": "AppA", "task": "查看我的收藏", "scene": "场景A", "capability": "能力A", "sub_capability": "子能力A"}, kb_root=root, model=model)
            self.assertEqual(len(result), 2)
            self.assertEqual(result[0]["pre_dependency"], "pre_node")
            self.assertEqual(result[1]["pre_dependency"], "weak")
            self.assertEqual(result[1]["pre_task_uuid"], result[0]["task_uuid"])
            self.assertEqual(result[0]["dependency_group_id"], result[1]["dependency_group_id"])

    def test_initial_generation_preserves_node_order_and_reports_progress(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _write_knowledge_base(root)
            model = _FakeModel([
                '{"app":"AppA","scene":"场景A","capability":"能力A","sub_capability":"子能力A","task":"请用 AppA 完成任务"}',
                '{"dependency_relationships":"zero","pre_task":null,"reason":"无历史依赖"}',
            ])
            progress = []
            result = run_initial_generation([node_id("AppA", "场景A", "能力A", "子能力A")], 1, kb_root=root, progress=progress.append, model=model)
            self.assertEqual(len(result["results"]), 1)
            self.assertEqual(result["results"][0]["pre_dependency"], "zero")
            self.assertEqual(progress[-1]["percent"], 100)

    def test_augmentation_filters_successful_seeds_and_accepts_classified_sheet(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            raw = root / "raw.xlsx"
            pd.DataFrame([
                {"任务": "失败任务", "涉及APP": "AppA", "任务结果": "FALSE"},
                {"任务": "成功任务", "涉及APP": "AppA", "任务结果": "TRUE"},
            ]).to_excel(raw, index=False)
            self.assertEqual(len(_read_seed_workbook(raw)), 1)
            classified = root / "classified.xlsx"
            pd.DataFrame([{"app": "AppA", "task": "已分类任务", "scene": "场景A", "capability": "能力A", "sub_capability": "子能力A"}]).to_excel(classified, index=False)
            self.assertEqual(_read_seed_workbook(classified)[0]["scene"], "场景A")

    def test_augmentation_classifies_and_generates_variant_records(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            kb = root / "kb"
            _write_knowledge_base(kb)
            input_file = root / "raw.xlsx"
            pd.DataFrame([{"任务": "失败任务", "涉及APP": "AppA", "任务结果": "FALSE"}]).to_excel(input_file, index=False)
            model = _FakeModel([
                '{"scene":"场景A","capability":"能力A","sub_capability":"子能力A","reason":"匹配"}',
                '[{"task":"AppA 的变体任务"}]',
            ])
            result = run_augmentation(input_file, 1, kb_root=kb, progress=lambda _value: None, model=model)
            self.assertEqual(len(result["results"]), 1)
            self.assertEqual(result["results"][0]["生成的变体任务"], "AppA 的变体任务")


class JobManagerTests(unittest.TestCase):
    def test_job_persists_results_supports_group_delete_and_export(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            kb = base / "kb"
            _write_knowledge_base(kb)

            def runner(_nodes, _count, *, kb_root, progress):
                progress({"stage": "generating", "completed_items": 1, "total_items": 1, "percent": 100})
                return {"results": [
                    {"result_id": "pre", "task_uuid": "pre", "dependency_group_id": "main", "pre_dependency": "pre_node", "app": "AppA", "scene": "场景A", "capability": "能力A", "sub_capability": "子能力A", "task": "前置", "deleted": False},
                    {"result_id": "main", "task_uuid": "main", "pre_task_uuid": "pre", "dependency_group_id": "main", "pre_dependency": "weak", "app": "AppA", "scene": "场景A", "capability": "能力A", "sub_capability": "子能力A", "task": "主任务", "deleted": False},
                ], "errors": [], "warnings": [], "total_items": 1}

            manager = TaskGenerationJobManager(base / "jobs", base / "runs", base / "exports", kb, initial_runner=runner)
            job = _submit_initial(manager, kb)
            for _ in range(100):
                current = manager.get(job["job_id"])
                if current and current["status"] == "succeeded":
                    break
                time.sleep(0.01)
            self.assertEqual(current["status"], "succeeded")
            manager.patch_result(job["job_id"], "main", {"deleted": True})
            self.assertTrue(all(item["deleted"] for item in manager.results(job["job_id"])))
            manager.patch_result(job["job_id"], "main", {"deleted": False})
            exported = manager.export(job["job_id"])
            self.assertTrue(Path(exported["path"]).is_file())
            manager.shutdown()

    def test_startup_marks_running_job_interrupted(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            jobs = base / "jobs"
            jobs.mkdir()
            (jobs / "old.json").write_text(json.dumps({"job_id": "old", "status": "running"}), encoding="utf-8")
            manager = TaskGenerationJobManager(jobs, base / "runs", base / "exports", base / "kb")
            self.assertEqual(manager.get("old")["status"], "interrupted")
            manager.shutdown()

    def test_partial_result_is_reported_as_partial(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            kb = base / "kb"
            _write_knowledge_base(kb)

            def runner(_nodes, _count, *, kb_root, progress):
                return {"results": [{"result_id": "one", "task": "任务", "deleted": False}], "errors": [{"item_id": "second", "error": "模型失败"}], "warnings": [], "total_items": 2}

            manager = TaskGenerationJobManager(base / "jobs", base / "runs", base / "exports", kb, initial_runner=runner)
            job = _submit_initial(manager, kb)
            for _ in range(100):
                current = manager.get(job["job_id"])
                if current and current["status"] == "partial":
                    break
                time.sleep(0.01)
            self.assertEqual(current["status"], "partial")
            self.assertEqual(current["result_count"], 1)
            manager.shutdown()


if __name__ == "__main__":
    unittest.main()
