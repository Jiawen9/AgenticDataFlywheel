"""Source reconstruction diagnostics never mutate business data or call models."""
from __future__ import annotations

from contextlib import ExitStack, redirect_stdout
import asyncio
import copy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from backend.batch_results import annotation_task_fingerprints, quality_task_fingerprints
from backend.data_store import ArtifactStore, RecordStore


REPO = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("quality_diagnostic_source_test_target", REPO / "scripts" / "diagnose_quality_job.py")
DIAGNOSTIC = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(DIAGNOSTIC)


class QualityDiagnosticSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from backend.DevelopRubrics import quality_job_runner
        cls.generator = quality_job_runner.GEN
        cls.evaluator = quality_job_runner.EVAL

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "data"
        self.output = Path(self.temp.name) / "diagnostic"
        self.output.mkdir()
        self.records = RecordStore(self.root)
        self.store = ArtifactStore(self.root)
        self.batch, self.task_id, self.trajectory_id = "batch-one", "TASK-A", "tr-one"
        self.annotation = {"schema_version": 1, "columns": {}, "sheets": {"VLA trajectories": [
            {"task_id": self.task_id, "trajectory_id": self.trajectory_id, "image": "TASK-A/step001.jpg",
             "action": "click"}]}}
        self.quality = {"schema_version": 1, "columns": {}, "sheets": {
            "Tasks": [{"task_id": self.task_id, "instruction": "打开目标页面并查看搜索结果",
                       "domain": "mobile GUI", "complexity": "moderate",
                       "context_json": '{"app":"模拟应用"}', "expected_tools_json": '["click"]'}],
            "Trajectories": [{"task_id": self.task_id, "trajectory_id": self.trajectory_id,
                              "final_answer": "成功打开页面并展示搜索结果", "metadata_json": '{}'}],
            "Steps": [{"trajectory_id": self.trajectory_id, "step_id": 1, "action": "click",
                       "action_input_json": '{"target":"search"}', "observation": "搜索结果页面出现"}],
        }}
        self.tree = {"schema_version": 2, "batch_id": self.batch, "run_id": self.batch,
                     "trees": {self.task_id: {"terminal_trajectories": [self.trajectory_id]}},
                     "tasks": [{"task_id": self.task_id, "trajectory_count": 1}],
                     "source_task_fingerprints": annotation_task_fingerprints(self.annotation),
                     "tree_hashes": {self.task_id: "tree-source-hash"},
                     "task_fingerprints": {self.task_id: "task-source-hash"}, "quality_input": self.quality}
        self.annotation_ref = self.store.publish(self.batch, "02_annotation", self.annotation)
        self.tree_ref = self.store.publish(self.batch, "04_tree", self.tree, source_refs=[self.annotation_ref])
        self.env_path = Path(self.temp.name) / "mock.env"
        self.env_path.write_text("MODEL_NAME=mock-model\nMODEL_URL=https://model.invalid/v1\n", encoding="utf-8")
        self.job = {"job_id": "quality-one", "batch_id": self.batch, "run_id": self.batch,
                    "task_ids": [self.task_id], "current_task": self.task_id, "status": "failed",
                    "created_at": "2026-10-14T01:00:00Z", "stage": "generating_rubric",
                    "source_tree_hashes": dict(self.tree["tree_hashes"]),
                    "task_fingerprints": quality_task_fingerprints(self.tree, env_path=self.env_path),
                    "failure": {"category": "timeout", "message": "模型请求超时"}}
        self.records.put("quality_jobs", self.job["job_id"], self.job)
        self.environment = {"ADARUBRIC_MODEL": "mock-model", "ADARUBRIC_BASE_URL": "https://model.invalid/v1",
                            "ADARUBRIC_API_KEY": "test-only-not-a-real-key",
                            "ADARUBRIC_HTTP_PROXY_URL": "", "ADARUBRIC_HTTP_TRUST_ENV": "false"}

    def snapshots(self):
        return {path.relative_to(self.root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in self.root.rglob("*") if path.is_file() and "logs" not in path.relative_to(self.root).parts}

    def readonly_guards(self):
        stack = ExitStack()
        for method in ("_recover", "batch_lock", "read_payload", "read_file", "resolve_file"):
            stack.enter_context(patch.object(ArtifactStore, method,
                                             side_effect=AssertionError(f"diagnostic must not call {method}")))
        for method in ("put", "put_many", "update", "delete"):
            stack.enter_context(patch.object(RecordStore, method,
                                             side_effect=AssertionError(f"diagnostic must not write records: {method}")))
        return stack

    def configured(self):
        stack = ExitStack()
        stack.enter_context(patch.dict(os.environ, self.environment, clear=True))
        configure = stack.enter_context(patch("trajectory_tools.settings.configure_model_environment"))
        stack.enter_context(patch("backend.batch_results.quality_task_fingerprints",
                                  side_effect=lambda trees: quality_task_fingerprints(trees, env_path=self.env_path)))
        return stack, configure

    def test_read_artifact_verifies_snapshot_without_recovery_or_database_writes(self):
        before = self.snapshots()
        with self.readonly_guards():
            actual = DIAGNOSTIC.read_artifact(self.root, self.records, self.batch, "04_tree")
        self.assertEqual(actual, self.tree)
        self.assertEqual(self.snapshots(), before)

    def test_bad_sha256_blocks_read_even_when_size_matches(self):
        entry = next(item for item in self.tree_ref["files"] if item["name"] == "result.json")
        path = self.root / entry["path"]
        original = path.read_bytes()
        path.write_bytes(b"[" + original[1:])
        with self.readonly_guards(), self.assertRaisesRegex(ValueError, "校验失败"):
            DIAGNOSTIC.read_artifact(self.root, self.records, self.batch, "04_tree")

    def test_revision_change_during_read_is_rejected(self):
        original = self.records.get
        reads = []
        def get(namespace, key):
            result = original(namespace, key)
            if namespace == "artifacts" and key == f"{self.batch}/04_tree":
                reads.append(True)
                if len(reads) > 1:
                    return {**result, "storage_revision": result["storage_revision"] + 1}
            return result
        with patch.object(self.records, "get", side_effect=get), self.readonly_guards(), \
             self.assertRaisesRegex(ValueError, "修订发生变化"):
            DIAGNOSTIC.read_artifact(self.root, self.records, self.batch, "04_tree")

    def test_prepare_single_trajectory_request_equals_real_generator_messages(self):
        from backend.stage_artifacts import load_quality_objects
        before = self.snapshots()
        setup, configure = self.configured()
        with setup, self.readonly_guards():
            options = DIAGNOSTIC.prepare_request(self.root, self.records, self.job, None, self.output)
            tasks, trajectories = load_quality_objects(self.output / "quality_input.json")
            config = self.generator.load_config(DIAGNOSTIC.RUBRICS / "examples" / "jiawen_rubric_config.json")
            config["validate_rubric"] = False
            dimensions = self.generator._int_setting(config, "num_dimensions", "ADARUBRIC_NUM_DIMENSIONS", default=5)
            expected = self.generator.build_messages(tasks[self.task_id], trajectories,
                                                     config=config, num_dimensions=dimensions)
        configure.assert_called_once()
        self.assertEqual(options["messages"], expected)
        self.assertEqual(options["model"], "mock-model")
        self.assertEqual(options["base_url"], "https://model.invalid/v1")
        self.assertEqual(options["api_key"], "test-only-not-a-real-key")
        self.assertEqual(options["max_tokens"], 4096)
        self.assertEqual(options["http_options"], {"proxy": None, "trust_env": False})
        context = json.loads((self.output / "probe_context.json").read_text(encoding="utf-8"))
        self.assertEqual(context["trajectory_count"], 1)
        self.assertTrue(context["current_quality_fingerprint_matches_job"])
        copied = json.loads((self.output / "quality_input.json").read_text(encoding="utf-8"))
        self.assertNotIn("source_ref", copied)
        self.assertEqual(self.snapshots(), before)

    def test_job_tree_hash_change_refuses_to_probe_current_data_as_old_input(self):
        job = {**self.job, "source_tree_hashes": {self.task_id: "old-tree-source-hash"}}
        setup, configure = self.configured()
        with setup, self.readonly_guards(), self.assertRaisesRegex(ValueError, "树已经变化"):
            DIAGNOSTIC.prepare_request(self.root, self.records, job, None, self.output)
        configure.assert_not_called()
        self.assertEqual(list(self.output.iterdir()), [])

    def test_changed_annotation_refuses_stale_tree_before_reading_model_configuration(self):
        changed = copy.deepcopy(self.annotation)
        changed["sheets"]["VLA trajectories"][0]["action"] = "swipe"
        self.store.publish(self.batch, "02_annotation", changed)
        setup, configure = self.configured()
        with setup, self.readonly_guards(), self.assertRaisesRegex(ValueError, "标框与轨迹树不匹配"):
            DIAGNOSTIC.prepare_request(self.root, self.records, self.job, None, self.output)
        configure.assert_not_called()

    def test_without_probe_exports_existing_records_without_model_or_configuration_access(self):
        before = self.snapshots()
        with self.readonly_guards(), patch.object(DIAGNOSTIC, "prepare_request") as prepare, \
             patch.object(DIAGNOSTIC, "perform_request", new_callable=AsyncMock) as request, \
             patch("trajectory_tools.settings.configure_model_environment") as configure, \
             patch("httpx.AsyncClient.send", side_effect=AssertionError("network prohibited")), \
             patch("httpx.Client.send", side_effect=AssertionError("network prohibited")), \
             redirect_stdout(io.StringIO()):
            code = DIAGNOSTIC.main(["--data-root", str(self.root), "--job-id", self.job["job_id"]])
        self.assertEqual(code, 0)
        prepare.assert_not_called()
        request.assert_not_awaited()
        configure.assert_not_called()
        exports = list((self.root / "logs" / "quality_diagnostics").iterdir())
        self.assertEqual(len(exports), 1)
        job = json.loads((exports[0] / "job.json").read_text(encoding="utf-8"))
        self.assertEqual(job["job_id"], self.job["job_id"])
        self.assertEqual({path.name for path in exports[0].iterdir()}, {"job.json", "recent_jobs.json"})
        self.assertEqual(self.snapshots(), before)

    def test_running_job_probe_is_rejected_before_input_loading(self):
        with patch.object(DIAGNOSTIC, "read_artifact") as read, self.assertRaisesRegex(ValueError, "仍在执行"):
            DIAGNOSTIC.prepare_request(self.root, self.records, {**self.job, "status": "running"}, None, self.output)
        read.assert_not_called()

    def score_fixture(self, *, count=6):
        self.annotation["sheets"]["VLA trajectories"] = [
            {"task_id": self.task_id, "trajectory_id": self.trajectory_id,
             "image": f"TASK-A/step{step:03d}.jpg", "action": "click"}
            for step in range(1, count + 1)]
        self.quality["sheets"]["Steps"] = [
            {"trajectory_id": self.trajectory_id, "step_id": step, "action": "click",
             "action_input_json": json.dumps({"target": f"button-{step}"}),
             "observation": f"第{step}步的页面内容"} for step in range(1, count + 1)]
        self.tree["source_task_fingerprints"] = annotation_task_fingerprints(self.annotation)
        self.annotation_ref = self.store.publish(self.batch, "02_annotation", self.annotation)
        self.tree_ref = self.store.publish(self.batch, "04_tree", self.tree, source_refs=[self.annotation_ref])
        self.job.update(stage="preparing", current_task=None, current_trajectory=None, percent=2,
                        completed_trajectories=0, failure={"category": "unknown",
                            "trajectory_id": self.trajectory_id, "step": 3})
        self.job["task_fingerprints"] = quality_task_fingerprints(self.tree, env_path=self.env_path)
        self.records.put("quality_jobs", self.job["job_id"], self.job)
        rubric = self.evaluator.DynamicRubric(task_id=self.task_id, dimensions=[{
            "name": "任务完成度", "description": "检查每一步的页面与用户目标是否一致",
            "scoring_criteria": {number: f"目标完成度等级{number}" for number in range(1, 6)},
        }])
        path = self.root / "system" / "rubric_outputs" / "rubrics" / "jiawen_gui_initial_rubric__TASK-A.json"
        path.parent.mkdir(parents=True)
        path.write_text(rubric.model_dump_json(), encoding="utf-8")
        return rubric, path

    def test_score_prepare_matches_real_structured_scoring_http_payload(self):
        import httpx
        from adarubric.llm import openai_client as client_module
        from adarubric.evaluator.trajectory_evaluator import LLMTrajectoryEvaluator
        from backend.stage_artifacts import load_quality_objects

        rubric, _ = self.score_fixture()
        before = self.snapshots()
        setup, configure = self.configured()
        with setup, self.readonly_guards(), \
             patch.object(client_module.OpenAIClient, "generate_text", new_callable=AsyncMock) as generation, \
             patch.object(client_module.OpenAIClient, "generate_structured", new_callable=AsyncMock) as scoring:
            options = DIAGNOSTIC.prepare_request(self.root, self.records, self.job, None, self.output,
                                                 operation="score")
            generation.assert_not_awaited()
            scoring.assert_not_awaited()
        configure.assert_called_once()
        self.assertEqual(self.snapshots(), before)
        tasks, trajectories = load_quality_objects(self.output / "quality_input.json")
        config = self.generator.load_config(DIAGNOSTIC.RUBRICS / "examples" / "jiawen_rubric_config.json")
        E = self.evaluator
        trajectory = E._trajectory_for_evaluation(trajectories[0], include_action=True, include_action_input=True)
        chunk = next(chunk for chunk in E._trajectory_chunks(trajectory, lookahead_steps=1)
                     if chunk.core_step_ids == {3})
        captured = []
        def respond(request):
            captured.append(json.loads(request.content))
            response = {"trajectory_id": self.trajectory_id, "task_id": self.task_id,
                        "step_evaluations": [{"step_id": 3, "dimension_scores": [{
                            "dimension_name": "任务完成度", "score": 4, "confidence": 1,
                            "rationale": "模拟评分"}], "step_quality_summary": "目标页面符合要求"}]}
            return httpx.Response(200, json={"id": "mock-score", "object": "chat.completion", "created": 1,
                "model": "mock-model", "choices": [{"index": 0, "message": {"role": "assistant",
                "content": json.dumps(response, ensure_ascii=False)}, "finish_reason": "stop"}]})
        async def production_request():
            transport = httpx.AsyncClient(transport=httpx.MockTransport(respond), trust_env=False)
            with patch.object(client_module, "DefaultAsyncHttpxClient", return_value=transport):
                client = client_module.OpenAIClient(model="mock-model", api_key="test-only-not-a-real-key",
                    base_url="https://model.invalid/v1", max_retries=1,
                    extra_body=self.generator._extra_body_setting(config))
            try:
                evaluator = LLMTrajectoryEvaluator(client, aggregator=E._build_aggregator(config))
                return await evaluator.evaluate(chunk.trajectory, rubric, task_instruction=tasks[self.task_id].instruction,
                    target_step_ids=chunk.core_step_ids, temperature=float(config.get("evaluation_temperature", 0)),
                    max_tokens=int(config.get("evaluation_max_tokens", 8192)))
            finally:
                await client.close()
        with patch.dict(os.environ, self.environment, clear=True):
            asyncio.run(production_request())
        self.assertEqual(len(captured), 1)
        self.assertEqual(options["messages"], captured[0]["messages"])
        self.assertEqual(options["response_format"], captured[0]["response_format"])
        self.assertEqual(options["response_format"], {"type": "json_object"})
        self.assertEqual(options["max_tokens"], captured[0]["max_tokens"])
        self.assertEqual(options["max_tokens"], 8190)
        self.assertEqual(options["temperature"], captured[0]["temperature"])
        self.assertIn("You MUST respond with valid JSON matching this schema:", options["messages"][-1]["content"])
        self.assertIn("target step_ids: [3]", options["messages"][-1]["content"])

    def test_score_missing_rubric_never_generates_one(self):
        from adarubric.llm.openai_client import OpenAIClient
        _, rubric_path = self.score_fixture()
        rubric_path.unlink()
        before = self.snapshots()
        setup, _ = self.configured()
        with setup, self.readonly_guards(), \
             patch.object(OpenAIClient, "generate_text", new_callable=AsyncMock) as generation, \
             patch.object(OpenAIClient, "generate_structured", new_callable=AsyncMock) as scoring:
            with self.assertRaises((ValueError, FileNotFoundError)):
                DIAGNOSTIC.prepare_request(self.root, self.records, self.job, None, self.output,
                                           operation="score")
        generation.assert_not_awaited()
        scoring.assert_not_awaited()
        self.assertEqual(self.snapshots(), before)

    def test_score_missing_target_step_refuses_guessing_or_model_calls(self):
        from adarubric.llm.openai_client import OpenAIClient
        self.score_fixture()
        for step in (99, None):
            job = copy.deepcopy(self.job)
            if step is None:
                job["failure"].pop("step")
            setup, _ = self.configured()
            with self.subTest(step=step), setup, self.readonly_guards(), \
                 patch.object(OpenAIClient, "generate_structured", new_callable=AsyncMock) as scoring:
                with self.assertRaises(ValueError):
                    DIAGNOSTIC.prepare_request(self.root, self.records, job, None, self.output,
                                               operation="score", step=step)
            scoring.assert_not_awaited()

    def test_score_changed_tree_source_blocks_before_any_request(self):
        self.score_fixture()
        job = {**self.job, "source_tree_hashes": {self.task_id: "previous-source-hash"}}
        setup, configure = self.configured()
        with setup, self.readonly_guards(), self.assertRaisesRegex(ValueError, "树已经变化"):
            DIAGNOSTIC.prepare_request(self.root, self.records, job, None, self.output, operation="score")
        configure.assert_not_called()

    def test_score_wrong_trajectory_is_rejected_before_request(self):
        self.score_fixture()
        setup, _ = self.configured()
        with setup, self.readonly_guards(), self.assertRaises(ValueError):
            DIAGNOSTIC.prepare_request(self.root, self.records, self.job, None, self.output,
                                       operation="score", trajectory_id="another-trajectory", step=3)


if __name__ == "__main__":
    unittest.main()
