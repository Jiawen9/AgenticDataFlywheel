from __future__ import annotations

from concurrent.futures import Future
from copy import deepcopy
import json
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.data_publishing.router import router
from backend.data_publishing.service import DatasetReleaseRegistry
from backend.data_publishing.upload_jobs import DatasetUploadJobManager


class ImmediateExecutor:
    def submit(self, function, *args, **kwargs):
        future = Future()
        try:
            future.set_result(function(*args, **kwargs))
        except Exception as exc:  # pragma: no cover - mirrors Executor behavior
            future.set_exception(exc)
        return future


class DatasetPublishingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "AgenticDataFlywheel"
        self.release_root = self.root / "data" / "system" / "dataset_release"
        self.exports_root = self.root / "data" / "system" / "trajectory_correction" / "exports"
        self.trajectory_root = self.root / "data" / "raw" / "rollout_trajectories"
        self.trajectory_root.mkdir(parents=True)
        (self.trajectory_root / "TASK-A" / "TASK-A-1").mkdir(parents=True)
        (self.trajectory_root / "TASK-A" / "TASK-A-1" / "trajectory.jsonl").write_text("{}\n", encoding="utf-8")
        (self.trajectory_root / "TASK-A" / "_eval_queue.txt").write_text("TASK-A-1\n", encoding="utf-8")
        self.sessions: dict[str, dict] = {}

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def add_session(self, session_id: str = "a" * 16, *, ready: bool = True) -> dict:
        export_dir = self.exports_root / session_id
        export_dir.mkdir(parents=True)
        exports = []
        if ready:
            old = export_dir / "old.xlsx"
            latest = export_dir / "latest.xlsx"
            old.write_bytes(b"old")
            latest.write_bytes(b"latest")
            exports = [
                {"kind": "full_dataset", "filename": "old.xlsx", "sha256": hashlib.sha256(b"old").hexdigest(), "created_at": "2026-01-01T00:00:00+00:00", "sheets": {"Steps": 3}},
                {"kind": "full_dataset", "filename": "latest.xlsx", "sha256": hashlib.sha256(b"latest").hexdigest(), "created_at": "2026-01-02T00:00:00+00:00", "sheets": {"Steps": 4}},
            ]
        session = {
            "session_id": session_id,
            "tree_run_id": "run-1",
            "created_at": "2026-01-01T00:00:00+00:00",
            "updated_at": "2026-01-02T00:00:00+00:00",
            "selection": {"tasks": [{"task_id": "TASK-A", "trajectory_count": 2}]},
            "exports": exports,
        }
        self.sessions[session_id] = session
        return session

    def registry(self) -> DatasetReleaseRegistry:
        def loader(session_id: str):
            return self.sessions.get(session_id)

        def lister():
            return list(self.sessions.values())

        def saver(session: dict):
            self.sessions[str(session["session_id"])] = session
            return session

        return DatasetReleaseRegistry(
            releases_file=self.release_root / "releases.json",
            project_root=self.root,
            data_root=self.root / "data",
            trajectory_root=self.trajectory_root,
            correction_exports_dir=self.exports_root,
            session_loader=loader,
            session_lister=lister,
            session_saver=saver,
        )

    def test_release_uses_latest_export_and_hides_session(self):
        session = self.add_session()
        registry = self.registry()

        release = registry.create("GUI 数据集", [session["session_id"]])

        self.assertRegex(release["release_id"], r"^rel_[a-f0-9]{16}$")
        self.assertEqual(release["excel_paths"][0]["filename"], "latest.xlsx")
        self.assertTrue(release["excel_paths"][0]["path"].startswith("AgenticDataFlywheel/"))
        self.assertEqual(
            release["trajectory_paths"],
            ["AgenticDataFlywheel/data/raw/rollout_trajectories"],
        )
        self.assertEqual(release["step_count"], 4)
        self.assertTrue(self.sessions[session["session_id"]]["published"])
        self.assertEqual(registry.candidates(), [])
        entry = registry._records.get("dataset_releases", release["release_id"])
        self.assertFalse(registry.releases_file.exists())
        self.assertNotIn("session_id", entry)
        self.assertNotIn("session_ids", entry)
        self.assertFalse((self.release_root / release["release_id"]).exists())

    def test_missing_full_export_does_not_publish_session(self):
        session = self.add_session(ready=False)
        registry = self.registry()
        candidate = registry.candidates()[0]
        self.assertFalse(candidate["ready"])
        with self.assertRaisesRegex(ValueError, "尚未导出完整数据集"):
            registry.create("不可发布", [session["session_id"]])
        self.assertFalse(session.get("published", False))
        self.assertFalse((self.release_root / "releases.json").exists())

    def test_new_release_freezes_excel_and_records_internal_lineage(self):
        session = self.add_session()
        registry = self.registry()
        release = registry.create("frozen", [session["session_id"]])
        frozen, _ = registry.excel_file(release["release_id"], 0)
        original_bytes = frozen.read_bytes()
        self.assertTrue(frozen.is_relative_to(self.root / "data" / "releases" / release["release_id"]))
        (self.exports_root / session["session_id"] / "latest.xlsx").write_bytes(b"later edits")
        self.assertEqual(frozen.read_bytes(), original_bytes)
        self.assertEqual(release["source_refs"][0]["id"], session["session_id"])
        self.assertTrue((self.root / "data" / "system" / "app.sqlite").is_file())
        # A stale compatibility index cannot hide an authoritative DB release.
        registry.releases_file.write_text('{"schema_version":1,"releases":[]}', encoding="utf-8")
        self.assertIsNotNone(self.registry().get(release["release_id"]))

    def test_legacy_registry_is_invisible_and_untouched(self):
        old_file = self.release_root / "releases.json"
        old_file.parent.mkdir(parents=True, exist_ok=True)
        old_record = {"release_id": "rel_legacy", "excel_paths": [], "trajectory_paths": [], "created_at": "2000"}
        old_file.write_text(json.dumps({"releases": [old_record]}), encoding="utf-8")
        before = old_file.read_bytes()
        registry = self.registry()
        session = self.add_session()
        release = registry.create("new", [session["session_id"]])
        self.assertEqual({item["release_id"] for item in registry.list_releases()}, {release["release_id"]})
        self.assertIsNone(registry.get("rel_legacy"))
        self.assertEqual(old_file.read_bytes(), before)

    def test_export_fingerprint_must_match_before_release_and_on_download(self):
        session = self.add_session()
        registry = self.registry()
        latest = self.exports_root / session["session_id"] / "latest.xlsx"
        latest.write_bytes(b"unregistered edit")
        with self.assertRaisesRegex(ValueError, "SHA256"):
            registry.create("mismatch", [session["session_id"]])
        self.assertEqual(registry.list_releases(), [])
        latest.write_bytes(b"latest")
        release = registry.create("valid", [session["session_id"]])
        frozen, _ = registry.excel_file(release["release_id"], 0)
        frozen.write_bytes(b"changed release")
        with self.assertRaisesRegex(ValueError, "SHA256"):
            registry.excel_file(release["release_id"], 0)

    def test_json_upload_jobs_are_ignored_on_restart(self):
        registry = self.registry()
        jobs = self.release_root / "upload_jobs"
        jobs.mkdir()
        job_id = "a" * 32
        path = jobs / f"{job_id}.json"
        path.write_text(json.dumps({"job_id": job_id, "status": "running"}), encoding="utf-8")
        before = path.read_bytes()
        manager = DatasetUploadJobManager(registry, jobs_dir=jobs, executor=ImmediateExecutor())
        self.assertIsNone(manager.get(job_id))
        self.assertEqual(path.read_bytes(), before)

    def test_failed_freeze_never_publishes_session_or_release(self):
        session = self.add_session()
        registry = self.registry()
        with patch("backend.data_publishing.service.shutil.copyfile", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(OSError, "disk full"):
                registry.create("failed", [session["session_id"]])
        self.assertEqual(registry.list_releases(), [])
        self.assertFalse(self.sessions[session["session_id"]].get("published"))
        self.assertEqual(list((self.root / "data" / "releases").iterdir()), [])

    def test_external_data_root_release_and_image_paths_are_resolved(self):
        from backend.trajectory_correction.assets import resolve_asset
        external = Path(self.temporary.name) / "shared-data"
        self.exports_root = external / "system" / "correction_exports"
        self.trajectory_root = external / "raw" / "rollout_trajectories"
        self.trajectory_root.mkdir(parents=True)
        image = self.trajectory_root / "TASK" / "step.jpg"
        image.parent.mkdir()
        image.write_bytes(b"fake image")
        session = self.add_session()
        registry = DatasetReleaseRegistry(
            releases_file=external / "system" / "releases.json", project_root=self.root,
            data_root=external, trajectory_root=self.trajectory_root, correction_exports_dir=self.exports_root,
            session_loader=self.sessions.get, session_lister=lambda: list(self.sessions.values()),
            session_saver=lambda value: self.sessions.update({value["session_id"]: value}) or value)
        release = registry.create("external", [session["session_id"]])
        self.assertTrue(release["excel_paths"][0]["path"].startswith("@data/"))
        self.assertEqual(registry.excel_file(release["release_id"], 0)[0].read_bytes(), b"latest")
        self.assertEqual(registry.resolve_project_path(release["trajectory_paths"][0]), self.trajectory_root)
        self.assertEqual(resolve_asset(self.trajectory_root, "TASK/step.jpg"), image)
        with self.assertRaises(ValueError):
            registry.resolve_project_path("@data/../outside.xlsx")

    def test_same_name_creates_distinct_append_only_releases(self):
        first = self.add_session("a" * 16)
        second = self.add_session("b" * 16)
        registry = self.registry()
        one = registry.create("同名数据集", [first["session_id"]])
        two = registry.create("同名数据集", [second["session_id"]])
        self.assertNotEqual(one["release_id"], two["release_id"])
        self.assertEqual(len(registry.list_releases()), 2)

    def test_mock_upload_scans_excel_and_whole_trajectory_root(self):
        session = self.add_session()
        registry = self.registry()
        release = registry.create("待上传", [session["session_id"]])
        manager = DatasetUploadJobManager(
            registry,
            jobs_dir=self.release_root / "upload_jobs",
            executor=ImmediateExecutor(),
            step_delay=0,
        )

        submitted = manager.submit(release["release_id"])
        job = manager.get(submitted["job_id"])
        refreshed = registry.get(release["release_id"])

        self.assertEqual(job["status"], "succeeded")
        self.assertEqual(job["total_files"], 3)
        self.assertEqual(job["completed_files"], 3)
        self.assertEqual(job["percent"], 100)
        self.assertEqual(refreshed["upload_status"], "succeeded")
        self.assertTrue(refreshed["s3_uri"].endswith(f"/{release['release_id']}/"))

    def test_missing_source_file_fails_mock_upload(self):
        session = self.add_session()
        registry = self.registry()
        release = registry.create("损坏数据", [session["session_id"]])
        Path(registry.resolve_project_path(release["excel_paths"][0]["path"])).unlink()
        manager = DatasetUploadJobManager(
            registry,
            jobs_dir=self.release_root / "upload_jobs",
            executor=ImmediateExecutor(),
            step_delay=0,
        )
        submitted = manager.submit(release["release_id"])
        self.assertEqual(manager.get(submitted["job_id"])["status"], "failed")
        self.assertEqual(registry.get(release["release_id"])["upload_status"], "failed")

    def test_release_and_upload_api(self):
        session = self.add_session()
        registry = self.registry()
        manager = DatasetUploadJobManager(
            registry,
            jobs_dir=self.release_root / "upload_jobs",
            executor=ImmediateExecutor(),
            step_delay=0,
        )
        app = FastAPI()
        app.include_router(router)
        with (
            patch("backend.data_publishing.router.registry", registry),
            patch("backend.data_publishing.router.upload_manager", manager),
            TestClient(app) as client,
        ):
            candidates = client.get("/api/dataset-releases/candidates")
            self.assertEqual(candidates.status_code, 200)
            self.assertEqual(len(candidates.json()["candidates"]), 1)
            created = client.post(
                "/api/dataset-releases",
                json={"name": "API 数据集", "session_ids": [session["session_id"]]},
            )
            self.assertEqual(created.status_code, 201)
            release = created.json()["release"]
            self.assertEqual(client.get("/api/dataset-releases").status_code, 200)
            self.assertEqual(
                client.get(f"/api/dataset-releases/{release['release_id']}/excels/0").content,
                b"latest",
            )
            upload = client.post(f"/api/dataset-releases/{release['release_id']}/upload")
            self.assertEqual(upload.status_code, 202)
            job_id = upload.json()["job"]["job_id"]
            self.assertEqual(client.get(f"/api/dataset-upload-jobs/{job_id}").json()["job"]["status"], "succeeded")


if __name__ == "__main__":
    unittest.main()
