"""Runner + real local Checklist subprocess; only the provider is offline."""
import asyncio
import contextlib
import copy
import hashlib
import io
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from backend.batch_results import quality_task_fingerprints
from backend.ChecklistEvaluator.evaluation_profiles import freeze_profile
from backend.data_store import RecordStore
from backend.file_io import io_path
from backend.quality_fusion import completion_summary
from backend.short_paths import short_cache_directory
from backend.stage_artifacts import read_workbook_payload
from backend.tests import test_quality_checklist_local as native_fixture
from backend.tests import test_quality_source_recovery as source_fixture


class LocalQualityRunnerTests(unittest.TestCase):
    def test_both_engines_retain_partial_results_then_reuse_native_and_rubric_checkpoints(self):
        from backend.DevelopRubrics import quality_job_runner as worker
        from backend import quality_checklist_local as local
        for engine in ("newagent", "pipelineagent"):
            with self.subTest(engine=engine), contextlib.ExitStack() as stack:
                source = source_fixture.QualitySourceRecoveryTests()
                source.setUp(); stack.callback(source.doCleanups)
                native = native_fixture.LocalChecklistTests()
                native.setUp(); stack.callback(native.doCleanups)
                provider = native.fixture / "sitecustomize.py"
                provider.write_text(provider.read_text(encoding="utf-8") +
                    "\nimport faulthandler\nfaulthandler.dump_traceback_later(30, exit=True)\n", encoding="utf-8")
                root, task_id = source.root, source.task_id
                grouped, directories = [], []
                for index in (1, 2):
                    source.trajectory_id = f"tr{index:03d}"
                    directory, rows, _ = source.fixture(engine, index)
                    metadata = directory / "_trajectory_for_evaluate.json"
                    data = json.loads(metadata.read_text(encoding="utf-8"))
                    data["key_elements"] = ""
                    data["final_orch_screenshot"] = "step001_vla_input.jpg"
                    data["actions_flat"][0].update(screenshot_before="step001_vla_input.jpg",
                        screenshot_after="step001_vla_input.jpg", action_desc="点击目标页面")
                    metadata.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
                    grouped.extend(rows); directories.append(directory)
                workbook = root / "rubric_trajectories.xlsx"
                tasks, trajectories = source.generate(grouped, workbook)
                snapshot = read_workbook_payload(workbook)
                snapshot_bytes = workbook.with_suffix(".json").read_bytes()
                originals = {str(path): hashlib.sha256(path.read_bytes()).hexdigest()
                    for directory in directories for path in directory.iterdir()}
                profile_dir = native.base / "profiles" / "runner"
                native.write(profile_dir / "profile.json", {"id":"runner", "name":"本地组合验证",
                    "gold_path":"gold.json", "rules_dir":"rules"})
                native.write(profile_dir / "gold.json", {"original-case": {"task":"打开目标页面",
                    "checklist":[{"id":"chk_01", "description":"目标页面已打开", "check_method":"screenshot"}]}})
                native.write(profile_dir / "rules" / "evidence.json", {"rules":[{"id":"r1", "prompt_text":"Judge observed evidence only."}]})
                profile = freeze_profile(profile_dir.parent, "runner", native.base / "runner-frozen", runtime_root=local.RUNTIME)
                options = {**native.job["checklist_options"], "evaluation_profile_id":"runner",
                    "evaluation_profile_digest":profile["profile_digest"], "profile_snapshot":profile,
                    "runtime_digest":profile["runtime_digest"]}
                config, env = root / "config.json", root / "model.env"
                config.write_text("{}", encoding="utf-8")
                env.write_text("MODEL_NAME=offline\nMODEL_URL=https://fixture.invalid/v1\n", encoding="utf-8")
                def fingerprints(tree, **kwargs):
                    return quality_task_fingerprints(tree, config_path=config, env_path=env, **kwargs)
                current = {"raw_root":str(source.raw), "tasks":[{"task_id":task_id,"trajectory_count":2}],
                    "trees":{task_id:{"id":0,"terminal_trajectories":["tr001","tr002"],"children":[]}},
                    "tree_hashes":{task_id:"frozen-tree"}, "quality_input":snapshot}
                before_current = copy.deepcopy(current)
                batch, job_id = "b-local-contract", "q-local-contract"
                records = RecordStore(root)
                records.put("quality_jobs", job_id, {"job_id":job_id,"batch_id":batch,"status":"running",
                    "task_ids":[task_id],"created_at":"2026-11-13T00:00:00Z", "checklist_options":options,
                    "source_tree_hashes":current["tree_hashes"],"task_fingerprints":fingerprints(current,checklist_options=options)})
                E = worker.EVAL
                rubric = E.DynamicRubric(task_id=task_id, dimensions=[{"name":"完成度",
                    "description":"验证目标页面已正确出现并可读取", "scoring_criteria":{i:str(i) for i in range(1,6)}}])
                rubric_path = root / "rubric.json"
                rubric_path.write_text(rubric.model_dump_json(),encoding="utf-8")
                checkpoint_root = root / "checkpoints"
                checkpoint = short_cache_directory(root,batch,"quality_checkpoint",fingerprints(current)[task_id],
                    checkpoint_root / batch) / (task_id + ".jsonl")
                E.initialize_evaluations_jsonl(checkpoint,resume=True)
                for trajectory in trajectories:
                    evaluation = E.TrajectoryEvaluation(task_id=task_id,trajectory_id=trajectory.trajectory_id,
                        rubric_used=rubric,step_evaluations=[],global_score=4.0)
                    E.append_evaluation_jsonl(path=checkpoint,task=tasks[task_id],rubric_path=rubric_path,
                        run_number=1,evaluation=evaluation,evaluation_settings=E._evaluation_settings({}))
                checkpoint_bytes = io_path(checkpoint).read_bytes()
                events, partials = [], []
                def progress(**event):
                    events.append(event)
                    row = records.get("quality_partial_results", job_id + ":" + task_id)
                    if row: partials.append(copy.deepcopy(row["result"]))
                @contextlib.asynccontextmanager
                async def evaluation_pipeline(_config):
                    yield SimpleNamespace(filter_evaluations=lambda results:results)
                for name,value in {"DATA_ROOT":root,"CHECKPOINT_ROOT":checkpoint_root}.items():
                    stack.enter_context(patch.object(worker,name,value))
                stack.enter_context(patch.object(worker,"configure_model_environment"))
                stack.enter_context(patch.object(worker.GEN,"load_config",return_value={}))
                stack.enter_context(patch.object(worker,"quality_task_fingerprints",side_effect=fingerprints))
                stack.enter_context(patch.object(worker,"_matching_rubric",return_value=rubric_path))
                stack.enter_context(patch.object(E,"evaluation_pipeline",evaluation_pipeline))
                model = stack.enter_context(patch.object(E,"evaluate_trajectory",new_callable=AsyncMock))
                stack.enter_context(patch.object(worker,"merge_quality_results",side_effect=lambda _batch,rows,*a,**kw:{"tasks":rows}))
                stack.enter_context(patch.object(worker,"progress",side_effect=progress))
                stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
                hidden = directories[1].with_name("temporarily-unavailable")
                directories[1].rename(hidden)
                try:
                    first = asyncio.run(worker._run_frozen(batch,[task_id],job_id,current,root))
                finally:
                    hidden.rename(directories[1])
                self.assertFalse(first["tasks"][0]["fusion_complete"],first)
                self.assertEqual(first["tasks"][0]["fusion_eligible_count"],1,first)
                self.assertEqual(first["tasks"][0]["evaluations"]["tr002"]["fusion"]["invalid_reasons"],["checklist_incomplete"])
                self.assertFalse(completion_summary(first)["result_complete"])
                self.assertTrue(completion_summary(first)["partial_results_available"])
                self.assertTrue(any(row.get("fusion_eligible_count")==1 for row in partials))
                self.assertTrue(any(event.get("completed_rubric_trajectories")==2 and event.get("completed_checklist_trajectories")==0
                    and event.get("percent",100)<95 for event in events))
                first_calls = native.log.read_bytes().splitlines()
                events.clear(); partials.clear()
                second = asyncio.run(worker._run_frozen(batch,[task_id],job_id,current,root))
                self.assertTrue(second["tasks"][0]["fusion_complete"],second)
                self.assertEqual(second["tasks"][0]["fusion_eligible_count"],2)
                self.assertEqual(len(native.log.read_bytes().splitlines()),len(first_calls)*2)
                self.assertEqual(first["tasks"][0]["evaluations"]["tr001"],second["tasks"][0]["evaluations"]["tr001"])
                # A previous complete ledger row is not counted until this attempt validates it.
                self.assertTrue(any(event.get("stage")=="checklist_definition" and event.get("completed_checklist_trajectories")==0 for event in events))
                self.assertTrue(any(event.get("completed_checklist_trajectories")==2 and event.get("failed_checklist_trajectories")==0 for event in events))
                model.assert_not_awaited()
                self.assertEqual(io_path(checkpoint).read_bytes(),checkpoint_bytes)
                self.assertEqual(current,before_current)
                self.assertEqual(workbook.with_suffix(".json").read_bytes(),snapshot_bytes)
                self.assertEqual(originals,{str(path):hashlib.sha256(path.read_bytes()).hexdigest()
                    for directory in directories for path in directory.iterdir()})


if __name__ == "__main__":
    unittest.main()
