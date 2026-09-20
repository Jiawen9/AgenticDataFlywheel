"""Isolated summary publication, recovery and API tests; no external services."""
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from openpyxl import load_workbook

from backend.training_data_overview.converter import ALL_DATA_COLUMNS
from backend.training_data_overview.service import CATALOG, CONVERSIONS, TrainingOverviewManager, summarize
from backend.training_data_overview.router import router


def row(release="rel_one", identity="one", steps=3, app="App A", scene="场景一", capability="查询", known=True):
    key = release + ":" + identity
    return {"轨迹": key, "子任务轨迹": key, "step数量": steps, "APP": app,
            "一级场景": scene, "二级场景": capability, "时间": "2026-09-17 08:00:00", "生产方式": "数据飞轮",
            "人工精修步骤数量": 1 if known else None, "action_box": {"click": steps},
            "release_id": release, "source_file_index": 1, "trajectory_identity": identity,
            "manual_known": known, "app_known": app != "未记录 App", "level1_known": scene != "未分类",
            "level2_known": capability != "未分类"}


class Registry:
    def __init__(self, root):
        self.data_root = root
        self.releases = {"rel_one": {"release_id": "rel_one", "name": "发布一"}}

    def list_releases(self):
        return list(self.releases.values())

    def get(self, identifier):
        return self.releases.get(identifier)


class OverviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.registry = Registry(self.root)
        self.calls = []
        def convert(registry, release):
            self.calls.append(release["release_id"])
            return {"release_id": release["release_id"], "rows": [row(release["release_id"])], "warnings": []}
        self.manager = TrainingOverviewManager(self.registry, converter=convert)

    def tearDown(self):
        self.manager.close()
        self.tmp.cleanup()

    def test_consistent_counts_and_scoped_scenes(self):
        rows = [row(), row(identity="two", steps=4, scene="场景二"),
                row(identity="three", steps=2, app="未记录 App", scene="未分类", capability="未分类", known=False)]
        value = summarize({"rows": rows}, [], {})
        self.assertEqual(value["overview"]["total_steps"], 9)
        self.assertEqual(value["overview"]["subtask_trajectories"], 3)
        self.assertEqual(value["overview"]["level2_scenes"], 2)
        self.assertEqual(value["overview"]["total_apps"], 2)
        self.assertEqual(value["overview"]["manual_unknown_steps"], 2)
        self.assertEqual(value["overview"]["manual_refine_steps"], 2)
        self.assertEqual(value["trend"][-1]["total_steps"], 9)
        self.assertEqual(sum(x["total_steps"] for x in value["app_stats"]), 9)
        self.assertEqual(value["step_stats"], [{"steps": 1, "count": 1}, {"steps": 2, "count": 1}, {"steps": 3, "count": 1}])
        empty = summarize({"rows": rows}, [], {"app": "missing"})
        self.assertEqual(empty["overview"]["total_steps"], 0)
        self.assertEqual(empty["trend"], [])
        self.assertIsNone(summarize({"rows": rows}, [], {"start_date": "2026-09-18"})["filters"]["date_range"]["min_date"])

    def test_saved_workbook_matches_json_and_reads_do_not_write(self):
        self.manager.start(); self.manager.wait()
        path = self.manager.workbook()
        wb = load_workbook(path, read_only=True)
        try:
            values = list(wb.active.values)
            self.assertEqual(list(values[0]), ALL_DATA_COLUMNS)
            self.assertEqual(values[1][2], 3)
            self.assertEqual(json.loads(values[1][-1]), {"click": 3})
        finally:
            wb.close()
        payload = json.loads(path.with_name("all_data.json").read_text(encoding="utf-8"))
        self.assertEqual(payload["rows"][0]["轨迹"], values[1][0])
        before = {p.relative_to(self.root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in self.root.rglob("*") if p.is_file()}
        self.manager.query(); self.manager.query(app="App A"); self.manager.workbook()
        self.manager.submit("rel_one")
        after = {p.relative_to(self.root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in self.root.rglob("*") if p.is_file()}
        self.assertEqual(before, after)
        self.assertEqual(self.calls, ["rel_one"])

    def test_concurrent_requests_convert_once_and_accumulate_versions(self):
        self.registry.releases.clear()
        self.manager.start()
        self.registry.releases["rel_two"] = {"release_id": "rel_two", "name": "第二发布"}
        gate = threading.Event()
        original = self.manager.converter
        def convert(*args):
            gate.wait(5)
            return original(*args)
        self.manager.converter = convert
        with ThreadPoolExecutor(max_workers=8) as executor:
            list(executor.map(lambda _: self.manager.submit("rel_two"), range(16)))
        gate.set(); self.manager.wait()
        self.assertEqual(self.calls, ["rel_two"])
        self.registry.releases["rel_three"] = {"release_id": "rel_three", "name": "第三发布"}
        self.manager.submit("rel_three"); self.manager.wait()
        self.assertEqual(self.manager.query()["overview"]["total_trajectories"], 2)

    def test_failure_keeps_committed_catalog_and_retry_succeeds(self):
        self.manager.start(); self.manager.wait()
        version = self.manager.query()["version"]
        self.registry.releases["rel_two"] = {"release_id": "rel_two", "name": "第二发布"}
        converter = self.manager.converter
        self.manager.converter = lambda *_: (_ for _ in ()).throw(ValueError("文件 SHA256 校验失败"))
        self.manager.submit("rel_two"); self.manager.wait()
        query = self.manager.query()
        self.assertEqual(query["version"], version)
        self.assertEqual(query["overview"]["total_trajectories"], 1)
        self.assertEqual(query["conversions"][1]["status"], "failed")
        self.manager.converter = converter
        self.manager.submit("rel_two"); self.manager.wait()
        self.assertEqual(self.manager.query()["overview"]["total_trajectories"], 2)

    def test_write_and_commit_failure_do_not_expose_half_outputs(self):
        self.manager.start(); self.manager.wait()
        original = self.manager.query()["version"]
        self.registry.releases["rel_two"] = {"release_id": "rel_two", "name": "第二发布"}
        with patch("backend.training_data_overview.service._write_tables", side_effect=OSError("disk full")):
            self.manager.submit("rel_two"); self.manager.wait()
        self.assertEqual(self.manager.query()["version"], original)
        with patch.object(self.manager.records, "put_many", side_effect=OSError("commit rejected")):
            self.manager.submit("rel_two"); self.manager.wait()
        self.assertEqual(self.manager.query()["version"], original)
        directories = list((self.root / "system/training_data_overview/exports").iterdir())
        self.assertEqual([p.name for p in directories], [original])

    def test_restart_recovers_interrupted_but_does_not_repeat_success(self):
        self.manager.records.put(CONVERSIONS, "rel_one", {"release_id": "rel_one", "status": "running"})
        self.manager.start(); self.manager.wait(); self.manager.close()
        self.manager = TrainingOverviewManager(self.registry, converter=lambda *_: self.fail("Repeated successful conversion"))
        self.manager.start(); self.manager.wait()
        self.assertEqual(self.manager.query()["overview"]["total_steps"], 3)

    def test_queue_failure_can_retry_without_restart(self):
        self.registry.releases.clear()
        self.manager.start()
        self.registry.releases["rel_one"] = {"release_id": "rel_one", "name": "发布一"}
        with patch.object(self.manager._executor, "submit", side_effect=RuntimeError("queue unavailable")):
            state = self.manager.submit("rel_one")
        self.assertEqual(state["status"], "failed")
        self.manager.submit("rel_one"); self.manager.wait()
        self.assertEqual(self.manager.query()["overview"]["total_steps"], 3)

    def test_retry_rebuilds_damaged_derived_files_without_duplicate_rows(self):
        self.manager.start(); self.manager.wait()
        first = self.manager.query()["version"]
        self.manager.workbook().write_bytes(b"damaged derived artifact")
        self.manager.submit("rel_one"); self.manager.wait()
        self.assertNotEqual(self.manager.query()["version"], first)
        self.assertEqual(self.manager.query()["overview"]["total_trajectories"], 1)
        self.assertEqual(self.calls, ["rel_one", "rel_one"])
        self.assertTrue(self.manager.workbook().is_file())

    def test_publication_route_queues_summary_and_isolates_queue_failure(self):
        from backend.data_publishing import router as publishing
        app = FastAPI(); app.include_router(publishing.router)
        with patch.object(publishing, "overview_manager", self.manager), \
             patch.object(publishing.registry, "create", return_value=self.registry.get("rel_one")), \
             TestClient(app) as client:
            response = client.post("/api/dataset-releases", json={"name": "publication", "session_ids": ["session"]})
            self.assertEqual(response.status_code, 201)
            self.assertEqual(self.manager.records.get(CONVERSIONS, "rel_one")["status"], "queued")
            self.manager.start(); self.manager.wait()
            self.assertEqual(self.manager.query()["overview"]["total_steps"], 3)
            with patch.object(self.manager, "submit", side_effect=OSError("storage unavailable")), \
                 self.assertLogs(publishing.__name__, level="ERROR"):
                response = client.post("/api/dataset-releases", json={"name": "publication", "session_ids": ["session"]})
            self.assertEqual(response.status_code, 201)
            self.assertEqual(response.json()["release"]["release_id"], "rel_one")

    def test_queries_and_http_errors(self):
        app = FastAPI(); app.include_router(router)
        with patch("backend.training_data_overview.router.get_manager", return_value=self.manager), TestClient(app) as client:
            response = client.get("/api/training-data-overview")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers["cache-control"], "no-store")
            self.assertEqual(client.get("/api/training-data-overview/workbook").status_code, 404)
            self.assertEqual(client.get("/api/training-data-overview?start_date=2026-02-30").status_code, 422)
            self.assertEqual(client.get("/api/training-data-overview?start_date=2026-09-19&end_date=2026-09-17").status_code, 422)
            self.assertEqual(client.post("/api/training-data-overview/releases/absent/retry").status_code, 404)
            self.manager.start(); self.manager.wait()
            self.assertEqual(client.post("/api/training-data-overview/releases/rel_one/retry").status_code, 202)
            self.assertEqual(client.get("/api/training-data-overview/workbook?app=absent").status_code, 200)
            self.manager.workbook().write_bytes(b"corrupt")
            self.assertEqual(client.get("/api/training-data-overview/workbook").status_code, 409)


if __name__ == "__main__":
    unittest.main()
