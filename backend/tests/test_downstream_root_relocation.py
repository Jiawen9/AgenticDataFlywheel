"""Root relocation at read/dispatch boundaries, without rewriting snapshots."""
from __future__ import annotations

import base64
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from backend import quality_data, quality_jobs
from backend.collection_runs import CollectionRunError, CollectionRunStore
from backend.data_store import RecordStore
from backend.data_publishing.internal_uploader import UploadResult
from backend.data_publishing.service import DatasetReleaseRegistry
from backend.data_publishing.upload_jobs import DatasetUploadJobManager
from backend.phone_factory import PhoneFactoryError, PhoneFactoryStore
from backend.task_generation.jobs import TaskGenerationJobManager
from backend.tests.test_collection_runs import raw_trajectory, seed_batch
from backend.tests.test_correction_storage import DeferredExecutor
from backend.trajectory_correction import cot_jobs


class DownstreamRootRelocationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="downstream-root-")
        self.addCleanup(temporary.cleanup)
        self.project = Path(temporary.name) / "Project"
        self.old = self.project / "data"
        self.root = Path(temporary.name) / "external/backend_workspace"
        self.records = RecordStore(self.root)
        self.records.put("data_root_relocations", "move", {
            "old_root": str(self.old.resolve()), "new_root": str(self.root.resolve()), "status": "applied"})

    def old_path(self, path):
        return self.old / path.relative_to(self.root)

    def test_completed_run_rebases_only_output_and_keeps_manifest_digest(self):
        batch, workbook = seed_batch(self.root)
        store = CollectionRunStore(self.root)
        run, _ = store.create(batch["batch_id"], dispatch_key="once")
        entry = raw_trajectory(run)
        manifest = {"batch_id": batch["batch_id"], "trajectories": [entry]}
        done, _ = store.complete(run["run_id"], manifest)
        before = store.ready_input(batch["batch_id"])
        self.records.update("collection_runs", run["run_id"],
            lambda item: item.update(output_dir=str(self.old_path(Path(item["output_dir"])))))
        recorded = self.records.get("collection_runs", run["run_id"])
        workbook_bytes = workbook.read_bytes()
        current = store.get(run["run_id"])
        self.assertEqual(current["output_dir"], run["output_dir"])
        self.assertEqual(store.list_runs()[0]["output_dir"], run["output_dir"])
        self.assertEqual(current["manifest_sha256"], done["manifest_sha256"])
        self.assertEqual(store.ready_input(batch["batch_id"]), before)
        self.assertFalse(store.complete(run["run_id"], manifest)[1])
        self.assertEqual(self.records.get("collection_runs", run["run_id"]), recorded)
        self.assertEqual(workbook.read_bytes(), workbook_bytes)
        self.records.update("collection_runs", run["run_id"],
            lambda item: item.update(output_dir=str(self.root.parent / "unregistered")))
        with self.assertRaises(CollectionRunError):
            store.get(run["run_id"])

    def test_custom_project_defaults_to_backend_workspace(self):
        expected = self.project / "backend_workspace"
        registry = DatasetReleaseRegistry(project_root=self.project,
            releases_file=expected / "system/releases.json")
        self.assertEqual(registry.data_root, expected)
        self.assertFalse(self.old.exists())

    def test_phone_retry_passes_current_absolute_output_and_reuses_run(self):
        batch, workbook = seed_batch(self.root)
        calls = []
        def remote(args):
            calls.append(args)
            return {"ok": len(calls) > 1, "output": '{"ok":true}' if len(calls) > 1 else "mock timeout"}
        phone = PhoneFactoryStore(self.root, run_client_fn=remote)
        phone.add_task({"filename": batch["filename"], "description": "test batch", "source_batch_id": batch["batch_id"],
                        "content_base64": base64.b64encode(workbook.read_bytes()).decode()})
        request = {"filename": batch["filename"], "request_id": "once"}
        with self.assertRaises(PhoneFactoryError):
            phone.remote_start(request)
        run = phone.collection_runs.list_runs()[0]
        self.records.update("collection_runs", run["run_id"],
            lambda item: item.update(output_dir=str(self.old_path(Path(item["output_dir"])))))
        result = phone.remote_start(request)
        self.assertEqual(result["collection_run_id"], run["run_id"])
        self.assertEqual(result["output_dir"], run["output_dir"])
        self.assertEqual(calls[1][calls[1].index("--output-dir") + 1], run["output_dir"])
        self.assertEqual(phone.remote_start(request)["collection_run_id"], run["run_id"])
        self.assertEqual(len(calls), 2)

    def test_knowledge_bundle_is_located_by_job_without_rewriting_metadata(self):
        runs = self.root / "system/task_generation/runs"
        library = runs / "job-one/KnowledgeBase"
        library.mkdir(parents=True)
        frozen = library / "scene_tree.json"
        frozen.write_bytes(b'{"version":"frozen"}')
        stored = self.records.put("task_generation.jobs", "job-one", {
            "job_id": "job-one", "status": "succeeded", "created_at": "",
            "knowledge_base": {"version": "frozen", "directory": str(self.old_path(library))}})
        manager = TaskGenerationJobManager(
            jobs_dir=self.root / "system/task_generation/jobs", runs_dir=runs,
            exports_dir=self.root / "system/task_generation/exports", logs_dir=self.root / "logs",
            knowledge_base_dir=library, data_root=self.root, executor=DeferredExecutor())
        self.assertEqual(manager._execution_library("job-one"), library)
        self.assertEqual(manager.get("job-one")["knowledge_base"]["directory"], str(library))
        self.assertEqual(manager.list_jobs()[0]["knowledge_base"]["directory"], str(library))
        self.assertEqual(self.records.get("task_generation.jobs", "job-one"), stored)
        self.assertEqual(frozen.read_bytes(), b'{"version":"frozen"}')

    def test_quality_paths_and_worker_environment_use_current_root(self):
        rubric = self.root / "system/rubric_outputs/rubrics/task.json"
        rubric.parent.mkdir(parents=True)
        rubric.write_bytes(b"{}")
        saved = self.records.put("quality_results", "run:task", {
            "run_id": "run", "task_id": "task", "rubric_path": str(self.old_path(rubric))})
        with patch.object(quality_data, "store_root", return_value=self.root):
            self.assertEqual(quality_data.quality_task("run", "task")["rubric_path"], str(rubric))
        self.assertEqual(self.records.get("quality_results", "run:task"), saved)
        process = Mock(stdout=io.StringIO('RESULT {"warnings":[]}\n'))
        process.wait.return_value = 0
        with patch.object(quality_jobs, "DATA_ROOT", self.root), \
             patch.object(quality_jobs, "_env_values", return_value={}), \
             patch.object(quality_jobs.subprocess, "Popen", return_value=process) as launch:
            self.assertEqual(quality_jobs.run_quality_subprocess("run", ["task"], job_id="job", progress=lambda _: None),
                             {"warnings": []})
        self.assertEqual(launch.call_args.kwargs["env"]["ADF_DATA_ROOT"], str(self.root))

    def test_cot_xml_uses_relocated_file_and_ignores_old_directory_sentinel(self):
        image = self.root / "raw/batch/step001_vla_input.jpg"
        image.parent.mkdir(parents=True)
        xml = image.with_name("step001_vla_input_ui.xml")
        xml.write_text("<new-root/>", encoding="utf-8")
        old_xml = self.old_path(xml)
        old_xml.parent.mkdir(parents=True)
        old_xml.write_text("<old-root/>", encoding="utf-8")
        with patch.object(cot_jobs, "storage_root", return_value=self.root):
            self.assertEqual(cot_jobs._xml_text(image, {"xml": str(old_xml)}), "<new-root/>")

    def test_frozen_release_upload_rebases_project_path_but_keeps_sha_and_key(self):
        release_id = "rel_0123456789abcdef"
        excel = self.root / "releases" / release_id / "001/data.xlsx"
        excel.parent.mkdir(parents=True)
        excel.write_bytes(b"frozen Excel bytes")
        old_excel = self.old_path(excel)
        old_excel.parent.mkdir(parents=True)
        old_excel.write_bytes(b"old directory sentinel must not be uploaded")
        digest = hashlib.sha256(excel.read_bytes()).hexdigest()
        token = f"{self.project.name}/data/{excel.relative_to(self.root).as_posix()}"
        release = {"release_id": release_id, "name": "test", "created_at": "",
                   "excel_paths": [{"path": token, "filename": excel.name, "sha256": digest}], "trajectory_paths": []}
        manifest = excel.parents[1] / "manifest.json"
        manifest.write_text(json.dumps(release), encoding="utf-8")
        frozen_bytes = manifest.read_bytes()
        self.records.put("dataset_releases", release_id, release)
        registry = DatasetReleaseRegistry(project_root=self.project, data_root=self.root,
            releases_file=self.root / "system/releases.json", trajectory_root=self.root / "raw",
            correction_exports_dir=self.root / "system/correction/exports")
        self.assertEqual(registry.resolve_project_path(token), excel)
        self.assertEqual(registry.resolve_project_path("@data/" + excel.relative_to(self.root).as_posix()), excel)
        calls = []
        adapter = Mock()
        adapter.is_configured.return_value = True
        def upload(context):
            calls.append(context)
            self.assertEqual(context.file_path, excel)
            self.assertEqual(context.file_path.read_bytes(), b"frozen Excel bytes")
            return UploadResult(success=True, remote_id="confirmed")
        adapter.upload_excel.side_effect = upload
        queue = DeferredExecutor()
        manager = DatasetUploadJobManager(registry, jobs_dir=self.root / "system/uploads",
                                          executor=queue, internal_adapter=adapter)
        submitted = manager.submit(release_id, target="internal")
        queue.run()
        done = manager.get(submitted["job_id"])
        self.assertEqual(done["status"], "succeeded", done.get("error"))
        self.assertEqual(calls[0].idempotency_key, f"{release_id}:0:{digest}")
        self.assertEqual(manager.submit(release_id, target="internal")["job_id"], done["job_id"])
        self.assertEqual(len(calls), 1)
        self.assertEqual(manifest.read_bytes(), frozen_bytes)
        self.assertEqual(hashlib.sha256(excel.read_bytes()).hexdigest(), digest)
        with self.assertRaises(ValueError):
            registry.resolve_project_path("@data/../outside.xlsx")


if __name__ == "__main__":
    unittest.main()
