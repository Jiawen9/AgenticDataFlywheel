"""Short IDs preserve generated dependencies and immutable processing identities."""
from __future__ import annotations

import json
import tempfile
import unittest
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, AsyncMock, patch

import pandas as pd

from backend.identifiers import allocate_id, batch_uses_short_ids
from backend.preprocessing_service import annotate_input
from backend.quality_input_builder import build_quality_workbook
from backend.task_generation.jobs import TaskGenerationJobManager
from backend.task_generation.service import _dependency_tasks
from backend.task_generation.model_client import TaskGenerationModel
from backend.tree_build_service import build_tree_run
from backend.tests import test_preprocessing_jobs as preprocessing
from backend.tests.test_collection_runs import seed_batch
from backend.tests.test_scene_tree_editing import ImmediateExecutor
from backend.tests.test_task_generation import _write_knowledge_base, _submit_initial
from backend.tests.test_task_generation_responses import SequenceModel, config, response
from backend.tests.test_tree_build_service import FakeClassifier, FakeAlignmentReviewer, FakeSummarizer


class ShortProcessingTests(unittest.TestCase):
    def manager(self, base):
        kb = base / "kb"
        _write_knowledge_base(kb)
        manager = TaskGenerationJobManager(base / "jobs", base / "runs", base / "exports",
            kb, base / "logs", data_root=base / "data", executor=ImmediateExecutor())
        self.addCleanup(manager.shutdown)
        return manager, kb

    def test_production_initial_generation_allocates_batch_and_linked_task_ids(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            manager, kb = self.manager(base)
            model = SequenceModel([
                '{"tasks":[{"task":"查看我的收藏"}]}',
                '{"dependency_relationships":"weak","pre_task":"收藏一条内容"}',
                '{"scene":"场景A","capability":"能力A","sub_capability":"子能力A"}',
            ])
            model.close = lambda: None
            with patch("backend.task_generation.service._model", return_value=model) as model_factory:
                job = _submit_initial(manager, kb)
            self.assertRegex(model_factory.call_args.args[2](), r"^q\d{6}-\d{3,}$")
            self.assertRegex(job["job_id"], r"^b\d{6}-\d{3,}$")
            self.assertTrue(batch_uses_short_ids(job["job_id"], manager.data_root))
            self.assertEqual(manager.get(job["job_id"])["status"], "succeeded")
            pre, main = manager.results(job["job_id"])
            for item in (pre, main):
                self.assertRegex(item["task_uuid"], r"^t\d{6}-\d{3,}$")
                self.assertEqual(item["result_id"], item["task_uuid"])
            self.assertEqual(main["pre_task_uuid"], pre["task_uuid"])
            self.assertEqual(pre["dependency_group_id"], main["task_uuid"])
            tasks = manager.collection_input(job["job_id"])["tasks"]
            self.assertEqual(tasks[1]["pre_task_id"], tasks[0]["task_id"])
            self.assertEqual(manager.results(job["job_id"]), [pre, main])

    def test_augmentation_seed_and_result_ids_survive_preview_confirmation(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            manager, _ = self.manager(base)
            path = base / "seeds.xlsx"
            pd.DataFrame([{"app": "AppA", "task": "失败任务", "scene": "场景A",
                           "capability": "能力A", "sub_capability": "子能力A"}]).to_excel(path, index=False)
            model = SequenceModel(['{"tasks":[{"task":"新变体任务"}]}'])
            model.close = lambda: None
            with patch("backend.task_generation.service._model", return_value=model):
                job = manager.submit_augmentation(path, path.name, 1, auto_start=False)
                seed = manager.augmentation_preview(job["job_id"])["seeds"][0]
                self.assertRegex(seed["seed_id"], r"^t\d{6}-\d{3,}$")
                manager.start_augmentation(job["job_id"])
            result = manager.results(job["job_id"])[0]
            self.assertRegex(result["result_id"], r"^t\d{6}-\d{3,}$")
            self.assertNotEqual(result["result_id"], seed["seed_id"])
            self.assertEqual(result["seed_id"], seed["seed_id"])
            self.assertEqual(manager.collection_input(job["job_id"])["tasks"][0]["task_id"], result["result_id"])
            manager.start_augmentation(job["job_id"])
            self.assertEqual(manager.results(job["job_id"]), [result])
            self.assertEqual(len(model.calls), 1)

    def test_standalone_generation_keeps_legacy_uuid_without_default_data_root(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            model = SequenceModel(['{"dependency_relationships":"zero","pre_task":null}'])
            with patch("backend.identifiers.allocate_id", side_effect=AssertionError("no platform root supplied")):
                result = _dependency_tasks({"app": "AppA", "task": "查看首页"}, kb_root=root, model=model)
            self.assertRegex(result[0]["task_uuid"], r"^[0-9a-f-]{36}$")

    def fixture(self):
        f = preprocessing.PreprocessingJobTests("test_pipeline_keeps_business_columns_identity_and_idempotent_reads")
        f.setUp()
        self.addCleanup(f.doCleanups)
        batch_id = allocate_id("b", f.root)
        payload, workbook = seed_batch(f.root, batch_id)
        f.store.publish(batch_id, "00_collection", payload, workbooks={"result.xlsx": workbook})
        # A legacy collection producer remains supported for a new short-ID batch.
        run = f.complete(batch_id)
        return f, batch_id, run

    def test_preprocessing_short_identity_reuses_saved_conversion_on_retry(self):
        f, batch_id, _ = self.fixture()
        f.manager.annotator = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("mock interruption"))
        queued = f.manager.submit(batch_id)
        f.queue.drain()
        failed = f.manager.get(queued["job_id"])
        self.assertEqual(failed["status"], "failed")
        self.assertRegex(failed["job_id"], r"^j\d{6}-\d{3,}$")
        conversion = f.payload(failed["artifacts"][0])
        identity = conversion["sheets"][preprocessing.SHEET][0]["trajectory_id"]
        self.assertRegex(identity, r"^tr\d{6}-\d{3,}$")
        f.manager.annotator = annotate_input
        with patch("backend.preprocessing_jobs.adapt_conversion_identity", side_effect=AssertionError("must reuse conversion")):
            retry = f.manager.retry(failed["job_id"])
            f.queue.drain()
        done = f.manager.get(retry["job_id"])
        self.assertEqual(done["status"], "succeeded", done.get("error"))
        annotated = f.payload(done["artifacts"][1])["sheets"][preprocessing.SHEET][0]
        self.assertEqual(annotated["trajectory_id"], identity)
        self.assertEqual(annotated["source_trajectory_id"], "original-run")
        caches = list((f.root / "cache/bounding_box" / batch_id).iterdir())
        self.assertEqual(len(caches), 1)
        self.assertRegex(caches[0].name, r"^c\d{6}-\d{3,}$")
        marker = json.loads((caches[0] / "identity.json").read_text(encoding="utf-8"))
        self.assertEqual(len(marker["fingerprint"]), 64)

    def test_new_trajectory_identity_reaches_tree_and_rubric_steps(self):
        f, batch_id, run = self.fixture()
        queued = f.manager.submit(batch_id)
        f.queue.drain()
        done = f.manager.get(queued["job_id"])
        self.assertEqual(done["status"], "succeeded", done.get("error"))
        identity = f.payload(done["artifacts"][1])["sheets"][preprocessing.SHEET][0]["trajectory_id"]
        with patch("backend.tree_build_service.configure_reviewer_environment", return_value="fake-model"), \
             patch("backend.tree_build_service.QwenIntermediateStateClassifier", FakeClassifier), \
             patch("backend.tree_build_service.QwenStateAlignmentReviewer", FakeAlignmentReviewer):
            build_tree_run(["task-one"], job_id=allocate_id("j", f.root), progress=lambda _: None,
                batch_id=batch_id, annotation_version=done["annotation_version"], data_root=f.root,
                env_path=f.root / "unused.env", quality_builder=partial(build_quality_workbook, summarizer=FakeSummarizer()))
        tree = f.store.read_payload(f.store.get(batch_id, "04_tree"))
        row = tree["quality_input"]["sheets"]["Steps"][0]
        self.assertEqual(row["trajectory_id"], identity)
        source = json.loads(row["action_input_json"])["source_identity"]
        self.assertEqual(source["trajectory_id"], identity)
        self.assertEqual(source["collection_run_id"], run["collection_run_id"])

    def test_task_model_diagnostic_factory_is_optional_and_fail_open(self):
        for factory in (lambda: "q261015-001", lambda: "../not-a-file", Mock(side_effect=OSError("private detail")), None):
            with self.subTest(factory=factory), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                model = TaskGenerationModel(config(), call=lambda _: "", diagnostics_dir=root,
                                            diagnostic_id_factory=factory)
                model._call_override = None
                create = Mock(return_value=response('{"task":"有效任务"}'))
                model._client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
                with patch("backend.identifiers.allocate_id", side_effect=AssertionError("no implicit data root")):
                    value = model.complete("offline fixture")
                self.assertEqual(str(value), '{"task":"有效任务"}')
                self.assertTrue((root / (value.trace_id + ".json")).is_file())
                self.assertRegex(value.trace_id, r"^(q261015-001|[0-9a-f]{32})$")
                create.assert_called_once()


class ShortQualityRequestTests(unittest.IsolatedAsyncioTestCase):
    async def test_factory_is_scoped_and_failure_does_not_change_model_request(self):
        from backend.tests import test_quality_request_control as quality
        frames = []
        factory = Mock(side_effect=["q261015-001", OSError("private detail")])
        with quality.observe_requests(frames.append, id_factory=factory):
            for _ in range(2):
                request = AsyncMock(return_value="ok")
                self.assertEqual(await quality.controlled_request(request, **quality.METADATA), "ok")
                request.assert_awaited_once()
        self.assertEqual(frames[0]["local_request_id"], "q261015-001")
        self.assertEqual(frames[1]["local_request_id"], "q261015-001")
        self.assertRegex(frames[2]["local_request_id"], r"^[0-9a-f]{32}$")
        subsequent = []
        with quality.observe_requests(subsequent.append):
            await quality.controlled_request(AsyncMock(return_value="ok"), **quality.METADATA)
        self.assertRegex(subsequent[0]["local_request_id"], r"^[0-9a-f]{32}$")
        self.assertEqual(factory.call_count, 2)


if __name__ == "__main__":
    unittest.main()
