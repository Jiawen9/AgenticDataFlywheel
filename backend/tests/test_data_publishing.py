from __future__ import annotations

from concurrent.futures import Future
from copy import deepcopy
import json
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
        self.release_root = self.root / "backend_workspace" / "dataset_release"
        self.exports_root = self.root / "backend_workspace" / "trajectory_correction" / "exports"
        self.trajectory_root = self.root / "backend_workspace" / "rollout_trajectories"
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
                {"kind": "full_dataset", "filename": "old.xlsx", "created_at": "2026-01-01T00:00:00+00:00", "sheets": {"Steps": 3}},
                {"kind": "full_dataset", "filename": "latest.xlsx", "created_at": "2026-01-02T00:00:00+00:00", "sheets": {"Steps": 4}},
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
            ["AgenticDataFlywheel/backend_workspace/rollout_trajectories"],
        )
        self.assertEqual(release["step_count"], 4)
        self.assertTrue(self.sessions[session["session_id"]]["published"])
        self.assertEqual(registry.candidates(), [])
        persisted = json.loads((self.release_root / "releases.json").read_text(encoding="utf-8"))
        entry = persisted["releases"][0]
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
