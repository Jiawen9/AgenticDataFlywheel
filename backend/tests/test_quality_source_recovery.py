"""Real collection-conversion/quality input boundary, without model or device calls."""
from __future__ import annotations

import asyncio
import contextlib
import copy
import hashlib
import io
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from PIL import Image

from backend.file_io import io_path, resolve_path
from backend.preprocessing_service import convert_input, SHEET
from backend.quality_checklist import build_task
from backend.quality_input_builder import build_quality_workbook, SYSTEM_PROMPT
from backend.stage_artifacts import load_quality_objects, read_workbook_payload, write_payload_workbook, write_sidecar
from backend.trajectories_tree.tree_builder import load_trajectories


class Summary:
    model = "offline-fixture"

    def summarize_trajectory(self, task, trajectory_id, steps):
        return "已进入目标页面"


class QualitySourceRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.raw = self.root / "raw" / "批次原件"
        self.raw.mkdir(parents=True)
        self.task_id, self.trajectory_id = "t261112-002", "tr261112-010"
        self.task = SimpleNamespace(instruction="打开目标页面", context={})

    def trajectory(self, directory, **metadata):
        return SimpleNamespace(trajectory_id=self.trajectory_id, task_id=self.task_id, steps=[],
            metadata={"source_directory": str(directory), "source_identity": {
                "task_id": self.task_id, "trajectory_id": self.trajectory_id}, **metadata})

    def fixture(self, engine, source_number=1):
        run_id = "cr261112-001"
        relative = Path("original-case") / f"phone__original-trajectory-{source_number}"
        directory = self.raw / "runs" / run_id / relative
        directory.mkdir(parents=True, exist_ok=True)
        action = {"action": "click", "coordinate": [50, 70]}
        contents = {"_trajectory_for_evaluate.json": {"agent_type": engine, "task": "打开目标页面",
            "key_elements": "priority_1: 打开目标页面", "actions_flat": [{"global_step": 1, "action": action}]},
            "step001_vla_model_response.json": {"content": '<tool_call>' + json.dumps(action) +
                '</tool_call><summary>进入目标页面</summary>'}}
        if engine == "pipelineagent":
            contents["step001_vla_model_response.json"].update(agent_type=engine, global_step=1,
                executed_action=action, actual_action_coordinate_system="pixel", parsed_summary="进入目标页面")
        for filename, content in contents.items():
            (directory / filename).write_text(json.dumps(content, ensure_ascii=False), encoding="utf-8")
        Image.new("RGB", (120, 240), "white").save(directory / "step001_vla_input.jpg")
        (directory / "step001_vla_input_ui.xml").write_text("<hierarchy />", encoding="utf-8")
        files = [{"path": (relative / item.name).as_posix(), "size": item.stat().st_size,
                  "sha256": hashlib.sha256(item.read_bytes()).hexdigest()} for item in directory.iterdir()]
        snapshot = {"batch_id": "b261112-004", "raw_root": str(self.raw), "trajectories": [{
            "task_id": self.task_id, "agent_type": engine, "source_trajectory_id": "original-phone-trajectory",
            "collection_case_id": "original-case", "collection_run_id": run_id,
            "relative_dir": relative.as_posix(), "files": files}]}
        payload, _ = convert_input(snapshot, lambda _: None)
        payload["columns"][SHEET].append("actions_box")
        for row in payload["sheets"][SHEET]:
            # Short processing identities are independent of source directory names.
            row["trajectory_id"] = self.trajectory_id
            row["actions_box"] = "click(bbox=<bbox>[40,60,60,80]</bbox>)"
        source = self.root / "annotation.xlsx"
        write_payload_workbook(source, payload)
        write_sidecar(source, payload)
        grouped = load_trajectories(source, None)
        for _, steps in grouped:
            for step in steps:
                step.observation = "可见目标页面"
        output = self.root / (engine + "-quality.xlsx")
        return directory, grouped, output

    def generate(self, grouped, output):
        build_quality_workbook(grouped={self.task_id: grouped}, task_goals={self.task_id: "打开目标页面"},
            trajectory_root=self.raw, output=output, env_path=self.root / "unused.env", summarizer=Summary())
        return load_quality_objects(output.with_suffix(".json"))

    def test_both_engines_real_producer_loader_and_checklist_preserve_evidence(self):
        for engine in ("newagent", "pipelineagent"):
            with self.subTest(engine=engine):
                directory, grouped, output = self.fixture(engine)
                before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in directory.iterdir()}
                tasks, trajectories = self.generate(grouped, output)
                task_before = copy.deepcopy(tasks[self.task_id].model_dump())
                snapshot_before = output.with_suffix(".json").read_bytes()
                workbook_before = output.read_bytes()
                manifest = build_task(self.task_id, tasks[self.task_id], trajectories, raw_root=self.raw)
                self.assertEqual(manifest["task"], "打开目标页面")
                self.assertEqual(manifest["task"], json.loads((directory / "_trajectory_for_evaluate.json").read_text(encoding="utf-8"))["task"])
                self.assertEqual(tasks[self.task_id].instruction, SYSTEM_PROMPT + "\n\nTask:\n打开目标页面")
                self.assertEqual(tasks[self.task_id].context["task_description"], "打开目标页面")
                self.assertEqual(tasks[self.task_id].model_dump(), task_before)
                self.assertEqual(output.with_suffix(".json").read_bytes(), snapshot_before)
                self.assertEqual(output.read_bytes(), workbook_before)
                self.assertEqual(manifest["trajectories"], [{"trajectory_id": self.trajectory_id,
                    "source_directory": str(resolve_path(directory))}])
                self.assertEqual(manifest["key_elements"], "priority_1: 打开目标页面")
                self.assertEqual(manifest['case_id'], 'original-case')
                self.assertEqual(trajectories[0].metadata['collection_case_id'], 'original-case')
                self.assertEqual(trajectories[0].metadata['source_metadata_sha256'], before['_trajectory_for_evaluate.json'])
                identity = trajectories[0].metadata["source_identity"]
                self.assertEqual(identity["trajectory_id"], self.trajectory_id)
                self.assertEqual(identity["agent_type"], engine)
                self.assertEqual(identity["collection_run_id"], "cr261112-001")
                self.assertEqual(identity["source_trajectory_id"], "original-phone-trajectory")
                self.assertEqual(identity['collection_case_id'], 'original-case')
                self.assertEqual(before, {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in directory.iterdir()})

    def test_old_short_id_recovers_original_case_from_frozen_annotation(self):
        directory, grouped, output = self.fixture('newagent')
        tasks, trajectories = self.generate(grouped, output)
        metadata = trajectories[0].metadata
        for key in ('checklist_input_version', 'collection_case_id', 'key_elements', 'source_metadata_sha256'):
            metadata.pop(key, None)
        metadata['source_identity'].pop('collection_case_id', None)
        source = {'sheets': {'VLA trajectories': [{'task_id': self.task_id,
            'trajectory_id': self.trajectory_id, 'collection_case_id': 'original-case'}]}}
        before = copy.deepcopy((source, metadata))
        manifest = build_task(self.task_id, tasks[self.task_id], trajectories, raw_root=self.raw, source_annotation=source)
        self.assertEqual(manifest['case_id'], 'original-case')
        self.assertEqual((source, metadata), before)

    def test_changed_source_conditions_do_not_silently_change_a_frozen_quality_input(self):
        directory, grouped, output = self.fixture('pipelineagent')
        tasks, trajectories = self.generate(grouped, output)
        original = directory / '_trajectory_for_evaluate.json'
        data = json.loads(original.read_text(encoding='utf-8'))
        data['key_elements'] = 'changed requirement'
        original.write_text(json.dumps(data), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'source_changed'):
            build_task(self.task_id, tasks[self.task_id], trajectories, raw_root=self.raw)

    def test_bad_checklist_metadata_does_not_destroy_usable_rubric_input(self):
        directory, grouped, output = self.fixture('newagent')
        original_bytes = (directory / '_trajectory_for_evaluate.json').read_bytes()
        (directory / '_trajectory_for_evaluate.json').write_text('{broken', encoding='utf-8')
        tasks, trajectories = self.generate(grouped, output)
        self.assertTrue(trajectories[0].final_answer)
        self.assertTrue(trajectories[0].metadata['source_metadata_error'])
        with self.assertRaisesRegex(ValueError, 'source_metadata_unreadable'):
            build_task(self.task_id, tasks[self.task_id], trajectories, raw_root=self.raw)
        partial = build_task(self.task_id, tasks[self.task_id], trajectories, raw_root=self.raw, allow_partial=True)
        self.assertIn('source_metadata_unreadable', partial['trajectories'][0]['source_error'])
        self.assertEqual(partial['case_id'], 'original-case')
        frozen_bytes = output.with_suffix('.json').read_bytes()
        (directory / '_trajectory_for_evaluate.json').write_bytes(original_bytes)
        recovered = build_task(self.task_id, tasks[self.task_id], trajectories, raw_root=self.raw, allow_partial=True)
        self.assertNotIn('source_error', recovered['trajectories'][0])
        self.assertEqual(output.with_suffix('.json').read_bytes(), frozen_bytes)

    def test_existing_relative_snapshot_recovers_in_memory_without_changing_inputs(self):
        directory, grouped, output = self.fixture("newagent")
        self.generate(grouped, output)
        snapshot = read_workbook_payload(output)
        row = snapshot["sheets"]["Trajectories"][0]
        metadata = json.loads(row["metadata_json"])
        relative = directory.relative_to(self.raw).as_posix()
        metadata["source_directory"] = relative
        row.update(source_directory=relative, metadata_json=json.dumps(metadata))
        frozen = self.root / "old-quality.json"
        frozen.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
        before = frozen.read_bytes()
        tasks, trajectories = load_quality_objects(frozen)
        model_input_before = copy.deepcopy(trajectories[0].model_dump())
        rubric_task_before = copy.deepcopy(tasks[self.task_id].model_dump())
        manifest = build_task(self.task_id, tasks[self.task_id], trajectories, raw_root=str(self.raw))
        self.assertEqual(manifest["task"], "打开目标页面")
        self.assertEqual(manifest["trajectories"][0]["source_directory"], str(resolve_path(directory)))
        self.assertEqual(tasks[self.task_id].model_dump(), rubric_task_before)
        self.assertEqual(trajectories[0].model_dump(), model_input_before)
        self.assertEqual(frozen.read_bytes(), before)
        # No relative path is resolved against the process working directory.
        with self.assertRaisesRegex(ValueError, "source_root_missing"):
            build_task(self.task_id, tasks[self.task_id], trajectories)

    def test_frozen_context_task_precedes_instruction_without_rewriting_source(self):
        directory, _, _ = self.fixture("newagent")
        original = directory / "_trajectory_for_evaluate.json"
        before = original.read_bytes()
        task = SimpleNamespace(instruction=SYSTEM_PROMPT + "\n\nTask:\n旧的目标说明",
                               context={"task_description": "当前冻结任务，包含 Task: 字样", "extra": {"keep": True}})
        task_before = copy.deepcopy(vars(task))
        manifest = build_task(self.task_id, task, [self.trajectory(directory)], raw_root=self.raw)
        self.assertEqual(manifest["task"], task.context["task_description"])
        # A genuine task mismatch remains visible to the collector; do not silently
        # replace the frozen task with the original trajectory's task to pass it.
        self.assertNotEqual(manifest["task"], json.loads(original.read_text(encoding="utf-8"))["task"])
        self.assertEqual(vars(task), task_before)
        self.assertEqual(original.read_bytes(), before)

    def test_legacy_task_text_only_unwraps_known_instruction_prefix(self):
        goal = "打开目标页面，搜索 Task:\n目标关键词"
        prefix = SYSTEM_PROMPT + "\n\nTask:\n"
        unknown = "Different system instructions\n\nTask:\n" + goal
        cases = [
            (SimpleNamespace(instruction=prefix + goal, context={}), goal),
            (SimpleNamespace(instruction=prefix.replace("\n", "\r\n") + goal, context={}), goal),
            (SimpleNamespace(instruction=prefix + goal, context={"task_description": "   "}), goal),
            (SimpleNamespace(instruction=goal, context={}), goal),
            (SimpleNamespace(instruction=unknown, context={}), unknown),
            (SimpleNamespace(description=goal, context={}), goal),
            (SimpleNamespace(task=goal, context={}), goal),
        ]
        for task, expected in cases:
            with self.subTest(task=vars(task)):
                before = copy.deepcopy(vars(task))
                manifest = build_task(self.task_id, task, [self.trajectory(self.raw)], raw_root=self.raw)
                self.assertEqual(manifest["task"], expected)
                self.assertEqual(vars(task), before)

    def test_missing_invalid_unreadable_and_identity_errors_name_the_trajectory(self):
        outside = self.root / "outside"
        outside.mkdir()
        cases = [("", {}, "source_directory_missing"),
                 ("relative", {}, "source_root_missing"),
                 ("relative", {"raw_root": Path("relative-root")}, "source_directory_invalid"),
                 ("../outside", {"raw_root": self.raw}, "source_directory_invalid"),
                 (str(outside), {"raw_root": self.raw}, "source_directory_invalid"),
                 ("missing", {"raw_root": self.raw}, "source_directory_not_found"),
                 ("broken\x00path", {"raw_root": self.raw}, "source_directory_invalid")]
        for directory, options, code in cases:
            with self.subTest(code=code, directory=directory), self.assertRaisesRegex(ValueError, code) as caught:
                build_task(self.task_id, self.task, [self.trajectory(directory)], **options)
            self.assertIn(self.task_id, str(caught.exception))
            self.assertIn(self.trajectory_id, str(caught.exception))
        with patch("backend.quality_checklist.os.scandir", side_effect=PermissionError("fixture access denied")), \
             self.assertRaisesRegex(ValueError, "source_directory_unreadable"):
            build_task(self.task_id, self.task, [self.trajectory(self.raw)], raw_root=self.raw)
        with self.assertRaisesRegex(ValueError, "source_identity_mismatch"):
            build_task(self.task_id, self.task, [self.trajectory(self.raw, source_identity={"trajectory_id": "other"})])

    def test_absolute_legacy_directory_needs_no_invented_root_and_missing_xml_is_allowed(self):
        source = self.trajectory(self.raw)
        self.assertEqual(build_task(self.task_id, self.task, [source])["trajectories"][0]["source_directory"], str(self.raw))

    def test_metadata_read_failure_is_not_reported_as_missing_directory(self):
        original = self.raw / "_trajectory_for_evaluate.json"
        original.write_text("not JSON", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "source_metadata_unreadable"):
            build_task(self.task_id, self.task, [self.trajectory(self.raw)])

    def test_long_chinese_directory_uses_existing_io_adapter_without_changing_source(self):
        directory = self.raw
        while len(str(directory)) < 285:
            directory /= "中文原件目录1234567890"
        io_path(directory).mkdir(parents=True)
        original = directory / "_trajectory_for_evaluate.json"
        content = '{"key_elements":"长路径原件"}'.encode("utf-8")
        io_path(original).write_bytes(content)
        image = directory / "step001_vla_input.jpg"
        with io_path(image).open("wb") as stream:
            Image.new("RGB", (120, 240), "white").save(stream, format="JPEG")
        step = SimpleNamespace(counted_in_tree=True, observation="页面", action={"action": "wait"},
            image=image.relative_to(self.raw).as_posix(), summary="等待", step_index=1)
        with contextlib.ExitStack() as stack:
            if os.name == "nt":
                from backend.tests.test_file_io import legacy_path_limit
                stack.enter_context(legacy_path_limit())
                with self.assertRaises(FileNotFoundError):
                    original.read_bytes()
            tasks, trajectories = self.generate([(self.trajectory_id, [step])], self.root / "long.xlsx")
            result = build_task(self.task_id, tasks[self.task_id], trajectories, raw_root=self.raw)
        self.assertEqual(result["trajectories"][0]["source_directory"], str(resolve_path(directory)))
        self.assertEqual(result["key_elements"], "长路径原件")
        self.assertEqual(io_path(original).read_bytes(), content)
        self.assertNotIn("\\\\?\\", result["trajectories"][0]["source_directory"])

    def test_worker_recovers_ten_old_relative_sources_and_reuses_rubric_checkpoints(self):
        from backend.DevelopRubrics import quality_job_runner as worker
        from backend.batch_results import quality_task_fingerprints
        from backend.data_store import RecordStore
        from backend.short_paths import short_cache_directory
        E = worker.EVAL
        grouped, directories = [], []
        for index in range(1, 11):
            self.trajectory_id = f"tr261112-{index:03d}"
            directory, items, _ = self.fixture("newagent", index)
            grouped.extend(items)
            directories.append(directory)
        output = self.root / "rubric_trajectories.xlsx"
        tasks, trajectories = self.generate(grouped, output)
        snapshot = read_workbook_payload(output)
        for row in snapshot["sheets"]["Trajectories"]:
            metadata = json.loads(row["metadata_json"])
            relative = Path(metadata["source_directory"]).relative_to(self.raw).as_posix()
            metadata["source_directory"] = relative
            row.update(source_directory=relative, metadata_json=json.dumps(metadata))
        write_sidecar(output, snapshot)
        frozen_bytes = output.with_suffix(".json").read_bytes()
        original_hashes = {str(p): hashlib.sha256(p.read_bytes()).hexdigest()
                           for directory in directories for p in directory.iterdir()}
        config = self.root / "rubric-config.json"
        config.write_text("{}", encoding="utf-8")
        env = self.root / "model.env"
        env.write_text("MODEL_NAME=offline\nMODEL_URL=https://fixture.invalid/v1\n", encoding="utf-8")
        def fingerprints(tree, **kwargs):
            return quality_task_fingerprints(tree, config_path=config, env_path=env, **kwargs)
        tree = {"id": 0, "terminal_trajectories": [t.trajectory_id for t in trajectories], "children": []}
        current = {"raw_root": str(self.raw), "tasks": [{"task_id": self.task_id, "trajectory_count": 10}],
            "trees": {self.task_id: tree}, "tree_hashes": {self.task_id: "frozen-tree-hash"},
            "quality_input": snapshot}
        original_current = copy.deepcopy(current)
        rubric = E.DynamicRubric(task_id=self.task_id, dimensions=[{
            "name": "完成度", "description": "验证目标页面已正确出现并可读取", "scoring_criteria": {i: str(i) for i in range(1, 6)}}])
        rubric_path = self.root / "rubric.json"
        rubric_path.write_text(rubric.model_dump_json(), encoding="utf-8")
        batch, job_id = "b261112-004", "quality-recovery"
        options = {"version": 1, "evaluation_profile_id": "frozen-rules"}
        RecordStore(self.root).put("quality_jobs", job_id, {"job_id": job_id, "batch_id": batch,
            "checklist_options": options, "task_fingerprints": fingerprints(current, checklist_options=options)})
        checkpoint_root = self.root / "checkpoints"
        checkpoint_key = fingerprints(current)[self.task_id]
        checkpoint = short_cache_directory(self.root, batch, "quality_checkpoint", checkpoint_key,
            checkpoint_root / batch) / (self.task_id + ".jsonl")
        E.initialize_evaluations_jsonl(checkpoint, resume=True)
        for trajectory in trajectories:
            evaluation = E.TrajectoryEvaluation(task_id=self.task_id, trajectory_id=trajectory.trajectory_id,
                rubric_used=rubric, step_evaluations=[], global_score=4.0)
            E.append_evaluation_jsonl(path=checkpoint, task=tasks[self.task_id], rubric_path=rubric_path,
                run_number=1, evaluation=evaluation, evaluation_settings=E._evaluation_settings({}))
        checkpoint_bytes = io_path(checkpoint).read_bytes()
        observed = []
        def remote(_root, queued, manifest, input_digest, **kwargs):
            observed.append(copy.deepcopy(manifest))
            self.assertEqual(queued["checklist_options"], options)
            self.assertEqual(manifest["task"], "打开目标页面")
            self.assertEqual({row["source_directory"] for row in manifest["trajectories"]},
                             {str(resolve_path(path)) for path in directories})
            for row in manifest["trajectories"]:
                original = io_path(Path(row["source_directory"]) / "_trajectory_for_evaluate.json")
                self.assertEqual(manifest["task"], json.loads(original.read_text(encoding="utf-8"))["task"])
            return {row["trajectory_id"]: {"status": "complete", "definition_digest": "frozen",
                "source": "llm", "total": 1, "passed": 1, "failed": 0, "unchecked": 0,
                "verdict": "success", "items": [{"id": "one", "checked": True, "result": "passed"}]}
                for row in manifest["trajectories"]}
        @contextlib.asynccontextmanager
        async def evaluation_pipeline(_config):
            yield SimpleNamespace(filter_evaluations=lambda results: results)
        with contextlib.ExitStack() as stack:
            for name, value in {"DATA_ROOT": self.root, "CHECKPOINT_ROOT": checkpoint_root}.items():
                stack.enter_context(patch.object(worker, name, value))
            stack.enter_context(patch.object(worker, "configure_model_environment"))
            stack.enter_context(patch.object(worker.GEN, "load_config", return_value={}))
            stack.enter_context(patch.object(worker, "quality_task_fingerprints", side_effect=fingerprints))
            stack.enter_context(patch.object(worker, "_matching_rubric", return_value=rubric_path))
            stack.enter_context(patch.object(E, "evaluation_pipeline", evaluation_pipeline))
            model = stack.enter_context(patch.object(E, "evaluate_trajectory", new_callable=AsyncMock))
            stack.enter_context(patch("backend.quality_checklist.evaluate_task", side_effect=remote))
            stack.enter_context(patch.object(worker, "merge_quality_results", side_effect=lambda _batch, results, *args, **kwargs: results))
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            result = asyncio.run(worker._run_frozen(batch, [self.task_id], job_id, current, self.root))
        model.assert_not_awaited()
        self.assertEqual(len(observed), 1)
        self.assertEqual(len(observed[0]["trajectories"]), 10)
        self.assertTrue(result[0]["fusion_complete"])
        self.assertEqual(result[0]["fusion_eligible_count"], 10)
        self.assertEqual(current, original_current)
        self.assertEqual(fingerprints(current)[self.task_id], checkpoint_key)
        self.assertEqual(output.with_suffix(".json").read_bytes(), frozen_bytes)
        self.assertEqual(io_path(checkpoint).read_bytes(), checkpoint_bytes)
        self.assertEqual(original_hashes, {str(p): hashlib.sha256(p.read_bytes()).hexdigest()
                           for directory in directories for p in directory.iterdir()})


if __name__ == "__main__":
    unittest.main()
