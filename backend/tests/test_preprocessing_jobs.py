from __future__ import annotations

import copy
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from openpyxl import load_workbook

from backend.collection_runs import CollectionRunStore
from backend.data_store import ArtifactStore
from backend.preprocessing_jobs import PreprocessingJobManager
from backend.preprocessing_service import COLUMNS, SHEET, PreprocessingError, annotate_input
from backend import preprocessing_router
from backend.stage_artifacts import fingerprint
from backend.tests.test_collection_runs import seed_batch
from backend.tests.test_trajectories_preprocessing import AcceptingReviewer, create_step
from backend.preprocessing_service import processing_config
from backend import trajectories_preprocessing


class ManualQueue:
    def __init__(self):
        self.calls = []

    def submit(self, function, *args):
        self.calls.append((function, args))

    def drain(self):
        while self.calls:
            function, args = self.calls.pop(0)
            function(*args)


CONFIG = {"model": "mock-model", "base_url": "https://example.invalid/v1", "pipeline_revision": "test",
          "configuration_error": None, "max_review_rounds": 4}


class PreprocessingJobTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "data"
        self.queue = ManualQueue()
        self.config = copy.deepcopy(CONFIG)
        self.sources = CollectionRunStore(self.root)
        self.store = ArtifactStore(self.root)
        self.batch, workbook = seed_batch(self.root)
        self.store.publish("batch-one", "00_collection", self.batch, workbooks={"result.xlsx": workbook})
        self.factory_calls = 0

        def factory(**kwargs):
            self.factory_calls += 1
            return AcceptingReviewer()
        self.factory = factory
        self.manager = self.new_manager()

    def new_manager(self, **kwargs):
        return PreprocessingJobManager(self.root, source_store=self.sources, executor=self.queue,
            config_loader=lambda: copy.deepcopy(self.config), reviewer_factory=self.factory, **kwargs)

    def complete(self, batch_id="batch-one", case_id="task-one", name="original-run"):
        run, _ = self.sources.create(batch_id)
        directory = Path(run["output_dir"]) / case_id / name
        directory.mkdir(parents=True)
        action = {"action": "click", "coordinate": [50, 50]}
        create_step(directory, 1, action)
        (directory / "_trajectory_for_evaluate.json").write_text(
            json.dumps({"actions_flat": [{"global_step": 1, "action": action}]}), encoding="utf-8")
        entry = {"collection_case_id": case_id, "source_trajectory_id": name,
                 "relative_dir": directory.relative_to(Path(run["output_dir"])).as_posix(),
                 "files": [{"path": file.relative_to(Path(run["output_dir"])).as_posix(),
                            "sha256": fingerprint(file)} for file in sorted(directory.iterdir())]}
        return self.sources.complete(run["collection_run_id"],
            {"batch_id": batch_id, "trajectories": [entry]})[0]

    def payload(self, artifact):
        return json.loads(self.store.resolve_file(artifact, "result.json").read_text(encoding="utf-8"))

    def start(self):
        job = self.manager.submit("batch-one")
        self.queue.drain()
        return self.manager.get(job["job_id"])

    def test_pipeline_keeps_business_columns_identity_and_idempotent_reads(self):
        self.complete()
        job = self.start()
        self.assertEqual(job["status"], "succeeded", job.get("error"))
        conversion, annotation = job["artifacts"]
        self.assertEqual(conversion["source_refs"][0]["stage"], "00_collection")
        self.assertEqual(annotation["source_refs"][0]["version"], conversion["version"])
        for artifact, columns in ((conversion, COLUMNS), (annotation, COLUMNS + ["actions_box"])):
            payload = self.payload(artifact)
            row = payload["sheets"][SHEET][0]
            self.assertEqual(row["task_id"], "task-one")
            self.assertEqual(row["source_trajectory_id"], "original-run")
            self.assertEqual(len(row["trajectory_id"]), 67)
            path = self.store.resolve_file(artifact, next(f["name"] for f in artifact["files"] if f["kind"] == "excel"))
            workbook = load_workbook(path, read_only=True)
            try:
                self.assertEqual(list(next(workbook.active.values)), columns)
            finally:
                workbook.close()
        row = self.payload(annotation)["sheets"][SHEET][0]
        self.assertIn("<bbox>", row["actions_box"])
        self.assertEqual(self.manager.submit("batch-one")["job_id"], job["job_id"])
        self.assertEqual(self.factory_calls, 1)
        self.assertNotIn("config", job)
        self.assertNotIn("input_path", job)
        before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.root.rglob("*") if p.is_file()}
        self.assertEqual(self.manager.batches()[0]["annotation_version"], annotation["version"])
        self.manager.get(job["job_id"])
        self.assertEqual(before, {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.root.rglob("*") if p.is_file()})

    def test_failure_retains_conversion_and_retry_uses_frozen_input_despite_new_collection(self):
        self.complete()
        calls = []
        def flaky(*args, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("mock unavailable")
            return annotate_input(*args, **kwargs)
        self.manager = self.new_manager(annotator=flaky)
        failed = self.start()
        self.assertEqual(failed["status"], "failed")
        self.assertEqual([item["stage"] for item in failed["artifacts"]], ["01_conversion"])
        original = failed["artifacts"][0]
        self.complete()
        resumed = self.manager.retry(failed["job_id"])
        self.queue.drain()
        done = self.manager.get(resumed["job_id"])
        self.assertEqual(done["status"], "succeeded", done.get("error"))
        self.assertEqual(done["artifacts"][0]["version"], original["version"])
        self.assertEqual(len(self.payload(done["artifacts"][1])["sheets"][SHEET]), 1)
        prior_trajectory = self.payload(done["artifacts"][1])["sheets"][SHEET][0]["trajectory_id"]
        newest = self.start()
        rows = self.payload(newest["artifacts"][1])["sheets"][SHEET]
        self.assertEqual(len(rows), 2)
        self.assertEqual(len({row["trajectory_id"] for row in rows}), 2)
        self.assertEqual(len({row["文件夹名"] for row in rows}), 1)
        self.assertIn(prior_trajectory, {row["trajectory_id"] for row in rows})
        self.assertIsNone(self.store.get("batch-one", "02_annotation", done["artifacts"][1]["version"]))

    def test_concurrent_start_creates_one_job(self):
        self.complete()
        with ThreadPoolExecutor(max_workers=5) as pool:
            jobs = list(pool.map(lambda _: self.manager.submit("batch-one"), range(5)))
        self.assertEqual(len({job["job_id"] for job in jobs}), 1)
        self.assertEqual(len(self.queue.calls), 1)
        self.queue.drain()
        self.assertEqual(self.factory_calls, 1)

    def test_restart_marks_interrupted_and_retry_has_identical_input(self):
        self.complete()
        queued = self.manager.submit("batch-one")
        self.queue.calls.clear()
        manager = self.new_manager()
        self.assertEqual(manager.get(queued["job_id"])["status"], "interrupted")
        self.complete()
        retried = manager.retry(queued["job_id"])
        self.queue.drain()
        done = manager.get(retried["job_id"])
        self.assertEqual(done["status"], "succeeded", done.get("error"))
        self.assertEqual(done["input_digest"], queued["input_digest"])
        self.assertEqual(len(self.payload(done["artifacts"][1])["sheets"][SHEET]), 1)

    def test_excel_removal_does_not_change_retry_but_missing_json_is_error(self):
        self.complete()
        self.manager = self.new_manager(annotator=lambda *a, **k: (_ for _ in ()).throw(RuntimeError("offline")))
        failed = self.start()
        conversion = failed["artifacts"][0]
        excel_name = next(item["name"] for item in conversion["files"] if item["kind"] == "excel")
        self.store.resolve_file(conversion, excel_name).unlink()
        self.manager.annotator = annotate_input
        retried = self.manager.retry(failed["job_id"])
        self.queue.drain()
        self.assertEqual(self.manager.get(retried["job_id"])["status"], "succeeded")
        self.store.resolve_file(conversion, "result.json").unlink()
        with self.assertRaises((OSError, ValueError)):
            self.manager.retry(failed["job_id"])

    def test_modified_raw_input_and_missing_job_json_fail_explicitly(self):
        run = self.complete()
        job = self.manager.submit("batch-one")
        source = Path(run["output_dir"]) / "task-one/original-run/step001_vla_input_ui.xml"
        source.write_text("changed", encoding="utf-8")
        self.queue.drain()
        self.assertEqual(self.manager.get(job["job_id"])["status"], "failed")
        self.assertEqual(self.store.list("batch-one", "01_conversion"), [])
        with self.assertRaises(PreprocessingError):
            self.manager.retry(job["job_id"])
        private = self.manager._get(job["job_id"])
        (self.root / private["input_path"]).unlink()
        with self.assertRaisesRegex(PreprocessingError, "JSON"):
            self.manager.retry(job["job_id"])

    def test_conversion_survives_unconfigured_model(self):
        self.complete()
        self.manager.reviewer_factory = None
        self.manager.env_file = self.root / "missing.env"
        job = self.start()
        self.assertEqual(job["status"], "failed")
        self.assertEqual(job["artifacts"][0]["stage"], "01_conversion")
        self.assertTrue(self.store.resolve_file(job["artifacts"][0], "result.json").exists())

    def test_success_reuse_does_not_hide_missing_frozen_input(self):
        self.complete()
        job = self.start()
        private = self.manager._get(job["job_id"])
        (self.root / private["input_path"]).unlink()
        for operation in (lambda: self.manager.submit("batch-one"),
                          lambda: self.manager.retry(job["job_id"])):
            with self.assertRaisesRegex(PreprocessingError, "输入 JSON"):
                operation()
        self.assertEqual(self.factory_calls, 1)

    def test_augmentation_maps_collection_case_to_original_task(self):
        batch, workbook = seed_batch(self.root, "aug", "augmentation")
        self.store.publish("aug", "00_collection", batch, workbooks={"result.xlsx": workbook})
        self.complete("aug", "CASE-01")
        job = self.manager.submit("aug")
        self.queue.drain()
        done = self.manager.get(job["job_id"])
        self.assertEqual(done["status"], "succeeded", done.get("error"))
        row = self.payload(done["artifacts"][1])["sheets"][SHEET][0]
        self.assertEqual((row["collection_case_id"], row["task_id"]), ("CASE-01", "task-one"))

    def test_api_progress_errors_and_downloadable_conversion_after_failure(self):
        app = FastAPI()
        app.include_router(preprocessing_router.router)
        previous = preprocessing_router._manager
        self.addCleanup(preprocessing_router.configure_preprocessing_manager, previous)
        preprocessing_router.configure_preprocessing_manager(self.manager)
        with TestClient(app) as client:
            self.assertEqual(client.get("/api/trajectory-preprocessing/jobs/missing").status_code, 404)
            self.assertEqual(client.post("/api/trajectory-preprocessing/jobs", json={"batch_id": "missing"}).status_code, 404)
            result = client.get("/api/trajectory-preprocessing/batches")
            self.assertEqual(result.headers["cache-control"], "no-store")
            self.assertFalse(result.json()["batches"][0]["can_start"])
            self.complete()
            queued = client.post("/api/trajectory-preprocessing/jobs", json={"batch_id": "batch-one"})
            self.assertEqual(queued.status_code, 202)
            self.assertFalse(client.get("/api/trajectory-preprocessing/batches").json()["batches"][0]["can_start"])
            self.queue.drain()
            result = client.get("/api/trajectory-preprocessing/jobs/" + queued.json()["job_id"])
            self.assertEqual(result.json()["status"], "succeeded")
            self.assertEqual(result.json()["percent"], 100)

    def test_rule_change_invalidates_configuration_fingerprint(self):
        env = self.root / "model.env"
        env.write_text("MODEL_NAME=mock\nMODEL_URL=https://example.invalid/v1\nYUNAI_API_KEY=hidden", encoding="utf-8")
        def first(path):
            return "before-" + path.name
        def second(path):
            return "changed" if path.name == "action_box.py" else first(path)
        with patch("backend.preprocessing_service.fingerprint", side_effect=first):
            before = processing_config(env)
        with patch("backend.preprocessing_service.fingerprint", side_effect=second):
            after = processing_config(env)
        self.assertNotEqual(before["pipeline_revision"], after["pipeline_revision"])
        self.assertNotIn("hidden", json.dumps(after))

    def test_cli_default_outputs_are_temporary_and_removed_after_publication(self):
        calls = []
        def fake_pipeline(**kwargs):
            calls.append(kwargs)
            kwargs["export_output"].write_text("temporary conversion")
            kwargs["annotated_output"].write_text("temporary annotation")
        with patch("backend.trajectories_preprocessing.run_pipeline", side_effect=fake_pipeline):
            for _ in range(2):
                with patch("sys.argv", ["preprocess", "--batch-id", "cli-batch", "--data-root", str(self.root)]):
                    self.assertEqual(trajectories_preprocessing.main(), 0)
        for call in calls:
            self.assertEqual(call["export_output"].parent, call["annotated_output"].parent)
            self.assertTrue(call["export_output"].is_relative_to(self.root / "tmp/preprocessing-cli-views"))
            self.assertFalse(call["register_annotation_export"])
            self.assertFalse(call["export_output"].parent.exists())
        self.assertNotEqual(calls[0]["export_output"], calls[1]["export_output"])
        self.assertEqual(list((self.root / "tmp/preprocessing-cli-views").iterdir()), [])
