"""Real controller/service wiring with frozen fixtures and an offline COT model."""
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from backend.batch_results import current_tree_payload, quality_task_fingerprints, tree_task_fingerprints
from backend.data_store import RecordStore
from backend.pipelines import PipelineManager, PipelineRuntime
from backend.tests import test_pipeline_release as release_fixtures
from backend.tests.test_data_publishing import ImmediateExecutor
from backend.training_data_overview.service import TrainingOverviewManager
from backend.trajectory_correction import cot_jobs, draft_store, service


class OfflineCot:
    model = "offline-model"
    calls = []

    def generate(self, **kwargs):
        self.calls.append(kwargs)
        return {"summary": "模拟生成摘要", "thought": "模拟生成思考"}


class RealPipelineIntegrationTests(unittest.TestCase):
    seed = release_fixtures.PipelineReleaseTests.seed

    def setUp(self):
        release_fixtures.PipelineReleaseTests.setUp(self)
        OfflineCot.calls = []
        configuration = {"model": "offline-tree", "version": 1}
        for patcher in (
            patch("backend.tree_build_service.tree_build_config", return_value=configuration),
            patch("backend.trajectories_preprocessing.read_env_file", return_value={}),
            patch.object(draft_store, "CORRECTION_SESSIONS_DIR", self.root / "sessions"),
            patch.object(service, "CORRECTION_INPUTS_DIR", self.root / "system" / "trajectory_correction" / "inputs"),
            patch.object(service, "CORRECTION_EXPORTS_DIR", self.root / "system" / "trajectory_correction" / "exports"),
            patch.object(cot_jobs, "session_asset", return_value=self.raw / "offline.jpg"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        # Compute production fingerprints rather than teaching the adapter that
        # arbitrary fixtures are ready. No worker is permitted to invoke a model.
        ref = self.store.get(self.batch, "04_tree")
        tree = self.store.read_payload(ref)
        tree["task_fingerprints"] = tree_task_fingerprints(tree["source_annotation"], configuration)
        tree_ref = self.store.publish(self.batch, "04_tree", tree,
            source_refs=ref["source_refs"], metadata=ref["metadata"])
        quality = self.store.read_payload(self.store.get(self.batch, "05_quality"))
        quality["task_fingerprints"] = quality_task_fingerprints(current_tree_payload(self.batch, self.root))
        self.store.publish(self.batch, "05_quality", quality, source_refs=[tree_ref])
        self.overview = TrainingOverviewManager(self.registry, executor=ImmediateExecutor())
        self.overview.start()
        self.addCleanup(self.overview.close)
        self.cot = cot_jobs.CotJobManager(self.root / "cot_jobs", generator_factory=OfflineCot, executor=ImmediateExecutor())
        self.addCleanup(self.cot.shutdown)
        forbidden = Mock(side_effect=AssertionError("valid frozen results must not run a real model"))
        self.runtime = PipelineRuntime(self.root,
            preprocessing=SimpleNamespace(config_loader=lambda: {"offline": True}, submit=forbidden),
            tree=SimpleNamespace(submit=forbidden), quality=SimpleNamespace(submit=forbidden), cot=self.cot,
            factory=SimpleNamespace(collection_runs=SimpleNamespace(list_runs=lambda batch: [])), overview=self.overview)
        self.manager = PipelineManager(self.runtime, self.root)

    def create(self, mode):
        return self.manager.create({"request_id": "offline-" + mode, "batch_id": self.batch,
            "name": "实际服务离线集成", "mode": mode, "start_mode": "existing", "threshold": 4})

    def advance(self, pipeline_id, target):
        for _ in range(15):
            self.manager.tick(pipeline_id)
            pipeline = self.manager.get(pipeline_id)
            self.assertNotEqual(pipeline["status"], "failed", pipeline.get("error"))
            if pipeline["status"] == target:
                return pipeline
        self.fail(f"Pipeline did not reach {target}: {pipeline}")

    def test_automatic_real_pipeline_publishes_and_accumulates_once(self):
        pipeline = self.create("automatic")
        completed = self.advance(pipeline["pipeline_id"], "succeeded")
        self.assertEqual(self.overview.query()["overview"]["total_steps"], 3)
        self.assertEqual(self.overview.query()["overview"]["total_trajectories"], 1)
        self.assertTrue(self.overview.workbook().is_file())
        self.assertEqual(self.records.list("correction_sessions"), [])
        self.assertEqual(OfflineCot.calls, [])
        self.manager.tick(pipeline["pipeline_id"])
        self.overview.submit(completed["release_id"])
        self.assertEqual(self.overview.query()["overview"]["total_steps"], 3)
        self.assertEqual(len(self.registry.list_releases()), 1)

    def test_manual_real_editor_cot_confirmation_and_overview(self):
        pipeline = self.create("manual")
        waiting = self.advance(pipeline["pipeline_id"], "waiting_for_correction")
        session = service.get_session(waiting["session_id"])
        self.assertEqual(session["group_count"], 4)
        raw = self.records.get("correction_sessions", waiting["session_id"])
        chosen = next(group for group in raw["snapshot_payload"]["groups"] if group["meta_task"] == "a-first")
        row = chosen["rows"][0]["excel_row"]
        service.patch_row(waiting["session_id"], row,
            {"actions": '{"action":"wait","time":3}', "thought": "人工保留思考", "expected_revision": raw["storage_revision"]})
        current = self.records.get("correction_sessions", waiting["session_id"])
        self.manager.control(pipeline["pipeline_id"], "confirm-correction", waiting["storage_revision"],
                             session_revision=current["storage_revision"])
        completed = self.advance(pipeline["pipeline_id"], "succeeded")
        self.assertEqual(len(OfflineCot.calls), 1)
        self.assertEqual(OfflineCot.calls[0]["task"], "task A")
        self.assertEqual(self.overview.query()["overview"]["total_steps"], 6)
        self.assertEqual(self.overview.query()["overview"]["manual_refine_steps"], 1)
        saved = self.records.get("correction_sessions", waiting["session_id"])
        self.assertEqual(saved["row_edits"][str(row)]["thought"], "人工保留思考")
        self.assertEqual(saved["cot"][str(row)]["summary"], "模拟生成摘要")
        final = self.store.read_payload(self.store.get(self.batch, "07_cot"))
        self.assertEqual(final["cot"]["2"]["summary"], "模拟生成摘要")
        self.assertTrue(completed["release_id"])


class FullCollectionPipelineIntegrationTests(unittest.TestCase):
    def test_collect_http_transfer_preprocess_tree_quality_release_overview(self):
        import base64
        import contextlib
        import io
        import json
        import sys
        import tempfile
        import urllib.error
        from functools import partial
        from fastapi.testclient import TestClient
        from backend import phonefactory_client
        from backend.phone_factory import PhoneFactoryStore
        from backend.phonefactory_manager import create_app
        from backend.collector_service.core import CollectorConfig
        from backend.data_publishing.service import DatasetReleaseRegistry
        from backend.data_store import ArtifactStore
        from backend.data_store.registry import utc_now
        from backend.preprocessing_jobs import PreprocessingJobManager
        from backend.tree_build_jobs import TreeBuildJobManager
        from backend.tree_build_service import build_tree_run
        from backend.quality_jobs import QualityJobManager
        from backend.quality_input_builder import build_quality_workbook
        from backend.batch_results import merge_quality_results
        from backend.tests.test_collector_service import Devices, Execution
        from backend.tests.test_phone_factory_runtime import workbook_bytes
        from backend.tests.test_preprocessing_jobs import CONFIG, ManualQueue
        from backend.tests.test_trajectories_preprocessing import AcceptingReviewer, create_step
        from backend.tests.test_tree_build_service import FakeClassifier, FakeAlignmentReviewer, FakeSummarizer

        with tempfile.TemporaryDirectory() as directory, contextlib.ExitStack() as patches:
            base = Path(directory)
            def trajectory(path):
                path.mkdir(parents=True, exist_ok=True)
                action = {"action": "click", "coordinate": [50, 50]}
                create_step(path, 1, action)
                (path / "_trajectory_for_evaluate.json").write_text(
                    json.dumps({"actions_flat": [{"global_step": 1, "action": action}]}), encoding="utf-8")
            patches.enter_context(patch("backend.tests.test_collector_service.make_trajectory", side_effect=trajectory))
            patches.enter_context(patch("backend.trajectories_preprocessing.read_env_file", return_value={}))
            patches.enter_context(patch("backend.tree_build_service.configure_reviewer_environment", return_value="fake-model"))
            patches.enter_context(patch("backend.tree_build_service.QwenIntermediateStateClassifier", FakeClassifier))
            patches.enter_context(patch("backend.tree_build_service.QwenStateAlignmentReviewer", FakeAlignmentReviewer))
            config = {"model": "offline-tree", "version": 1}
            patches.enter_context(patch("backend.tree_build_service.tree_build_config", return_value=config))
            patches.enter_context(patch("backend.tree_build_jobs.tree_build_config", return_value=config))
            execution = Execution()
            app = create_app(CollectorConfig(base / "collector"), device_adapter=Devices(),
                             execution_adapter=execution, executor=ImmediateExecutor())
            with TestClient(app) as remote:
                def urlopen(request, timeout=None):
                    from urllib.parse import urlsplit
                    url = urlsplit(request.full_url)
                    response = remote.request(request.get_method(), url.path + ("?" + url.query if url.query else ""),
                        content=request.data, headers=dict(request.header_items()))
                    if response.status_code >= 400:
                        raise urllib.error.HTTPError(request.full_url, response.status_code, response.text,
                                                     response.headers, io.BytesIO(response.content))
                    stream = io.BytesIO(response.content)
                    stream.headers = response.headers
                    return stream
                def remote_client(args):
                    output = io.StringIO()
                    with patch.object(sys, "argv", ["phonefactory_client", *args]), patch.object(phonefactory_client.urllib.request,
                            "urlopen", urlopen), contextlib.redirect_stdout(output):
                        try:
                            phonefactory_client.main()
                        except SystemExit as exc:
                            return {"ok": False, "output": str(exc)}
                    return {"ok": True, "output": output.getvalue()}
                platform = PhoneFactoryStore(base / "platform", run_client_fn=remote_client)
                patches.callback(platform.runtime.close)
                platform.initialize_settings({"phones": ["phone-a"], "apps": ["App"],
                    "phoneApps": [{"phone_id": "phone-a", "app": "App"}], "vla": ["http://mock.invalid/vla"]})
                registered = platform.add_task({"filename": "offline.xlsx", "description": "完整自动链路",
                    "content_base64": base64.b64encode(workbook_bytes()).decode()})
                batch = registered["tasks"][0]["source_batch_id"]
                root = platform.root
                queue = ManualQueue()
                preprocessing = PreprocessingJobManager(root, source_store=platform.collection_runs, executor=queue,
                    config_loader=lambda: dict(CONFIG), reviewer_factory=lambda **_: AcceptingReviewer())
                def build(tasks, **kwargs):
                    return build_tree_run(tasks, **kwargs,
                        env_path=base / "no-network.env", quality_builder=partial(build_quality_workbook, summarizer=FakeSummarizer()))
                tree = TreeBuildJobManager(root / "tree-jobs", runner=build, executor=queue, data_root=root)
                def score(batch_id, task_ids, *, job_id, progress):
                    current = current_tree_payload(batch_id, root)
                    rows = current["source_annotation"]["sheets"]["VLA trajectories"]
                    results = [{"task_id": task, "status": "succeeded", "evaluations": {
                        row["trajectory_id"]: {"global_score": 5, "passed_threshold": True}
                        for row in rows if row["task_id"] == task}} for task in task_ids]
                    progress({"stage": "evaluating", "completed_trajectories": len(rows), "percent": 100})
                    return merge_quality_results(batch_id, results, current["tree_hashes"], quality_task_fingerprints(current),
                        job_id=job_id, completed_at=utc_now(), root=root)
                quality = QualityJobManager(root / "quality-jobs", runner=score, executor=queue, data_root=root)
                cot = cot_jobs.CotJobManager(root / "cot-jobs", generator_factory=OfflineCot, executor=queue)
                for manager in (tree, quality, cot):
                    patches.callback(manager.shutdown)
                registry = DatasetReleaseRegistry(data_root=root, releases_file=root / "registry.json")
                overview = TrainingOverviewManager(registry, executor=ImmediateExecutor())
                overview.start()
                patches.callback(overview.close)
                runtime = PipelineRuntime(root, preprocessing=preprocessing, tree=tree, quality=quality,
                    cot=cot, factory=platform, overview=overview)
                manager = PipelineManager(runtime, root)
                pipeline = manager.create({"request_id": "complete-collect", "name": "完整模拟采集发布",
                    "batch_id": batch, "mode": "automatic", "start_mode": "collect", "threshold": 4,
                    "collection_config": {"phone_id": "phone-a", "app": "App", "vla": "http://mock.invalid/vla",
                        "config": {"sampling_enabled": True, "temperature": .3, "top_p": .8, "use_experience_lib": True}}})
                for _ in range(22):
                    manager.tick(pipeline["pipeline_id"])
                    current = manager.get(pipeline["pipeline_id"])
                    self.assertNotEqual(current["status"], "failed", current.get("error"))
                    if current["status"] == "succeeded":
                        break
                    platform.runtime.transfer.recover()
                    queue.drain()
                self.assertEqual(current["status"], "succeeded", current.get("error"))
                self.assertEqual(execution.calls, 1)
                self.assertEqual(current["selection"]["tasks"][0]["goal"], "搜索商品")
                self.assertEqual(len(current["collection_run_ids"]), 1)
                run = platform.collection_runs.get(current["collection_run_ids"][0])
                self.assertEqual((run["status"], run["transfer_status"]), ("completed", "completed"))
                for key in ("preprocessing", "tree", "quality"):
                    step = next(item for item in current["steps"] if item["id"] == key)
                    self.assertEqual(len(step["job_ids"]), 1)
                    self.assertEqual(step["jobs"][0]["status"], "succeeded")
                    self.assertEqual(step["jobs"][0]["pipeline_id"], pipeline["pipeline_id"])
                self.assertEqual(overview.query()["overview"]["total_steps"], 1)
                self.assertEqual(overview.query()["filters"]["apps"], ["App"])
                self.assertEqual(overview.query()["filters"]["scenes"][0]["name"], "购物")
                self.assertTrue(overview.workbook().is_file())
                self.assertEqual(RecordStore(root).list("correction_sessions"), [])


class PipelineInputReadinessTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.runs = []
        self.ready = {"input_digest": "verified-current-input", "trajectories": [{"task_id": "A"}, {"task_id": "B"}]}
        self.sources = SimpleNamespace(list_runs=lambda _: self.runs, ready_input=Mock(side_effect=lambda _: self.ready))
        self.runtime = PipelineRuntime(Path(self.temporary.name), preprocessing=None, tree=None, quality=None,
            cot=None, factory=SimpleNamespace(collection_runs=self.sources), overview=None)

    def pipeline(self, mode):
        return {"batch_id": "batch-recovery", "task_ids": ["A", "B"], "start_mode": mode,
                "collection_run_ids": ["current-run"]}

    def test_existing_continues_after_failed_history_when_completed_results_cover_batch(self):
        self.runs = [
            {"collection_run_id": "old-failed", "status": "failed", "errors": [{"task_id": "A", "error": "old failure"}]},
            {"collection_run_id": "current-run", "status": "completed", "errors": []}]
        pipeline = self.pipeline("existing")
        self.assertTrue(self.runtime.input_ready(pipeline))
        self.assertEqual(pipeline["input_digest"], "verified-current-input")
        self.sources.ready_input.assert_called_once_with("batch-recovery")

    def test_collect_does_not_ignore_its_failed_or_partial_bound_run(self):
        from backend.pipelines import PipelineError
        for status, errors in (("failed", []), ("interrupted", []), ("completed", [{"error": "missing case"}])):
            with self.subTest(status=status, errors=errors):
                self.runs = [
                    {"collection_run_id": "old-success", "status": "completed", "errors": []},
                    {"collection_run_id": "current-run", "status": status, "errors": errors}]
                pipeline = self.pipeline("collect")
                with self.assertRaisesRegex(PipelineError, "本次采集存在失败"):
                    self.runtime.input_ready(pipeline)
                self.assertNotIn("input_digest", pipeline)
        self.sources.ready_input.assert_not_called()

    def test_missing_task_blocks_both_existing_and_collect_despite_successful_run(self):
        from backend.pipelines import PipelineError
        self.runs = [{"collection_run_id": "current-run", "status": "completed", "errors": []}]
        self.ready["trajectories"] = [{"task_id": "A"}]
        for mode in ("existing", "collect"):
            with self.subTest(mode=mode):
                pipeline = self.pipeline(mode)
                with self.assertRaisesRegex(PipelineError, "未覆盖整批任务"):
                    self.runtime.input_ready(pipeline)
                self.assertNotIn("input_digest", pipeline)


if __name__ == "__main__":
    unittest.main()
