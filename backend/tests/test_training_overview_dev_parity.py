"""Compare query-only statistics against the checked-in DEV functions, without Flask."""
from __future__ import annotations

import ast
from copy import deepcopy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import warnings

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pandas as pd

from backend.training_data_overview.router import router
from backend.training_data_overview.service import CATALOG, TrainingOverviewManager, summarize
from backend.tests.test_training_overview_service import Registry


REFERENCE = Path(__file__).resolve().parents[2] / "黄区平台前后端" / "DataVue" / "DataVue" / "backend" / "app.py"


def data_rows():
    def row(trajectory, subtask, steps, app, level1, level2, day, source="数据飞轮", manual=0, actions=None):
        return {"轨迹": trajectory, "子任务轨迹": subtask, "step数量": steps, "APP": app,
                "一级场景": level1, "二级场景": level2, "时间": f"2026-09-{day:02} 08:00:00",
                "生产方式": source, "人工精修步骤数量": manual, "action_box": actions or {},
                "manual_known": False, "app_known": False, "level1_known": False, "level2_known": False}
    return [
        row("shared", "sub-shared", 4, "App A", "场景二", "查询", 17, manual=2, actions={"click": 2, "finish": 2}),
        row("shared", "sub-shared", 3, "App A", "场景二", "查询", 17, manual=None, actions={"click": 1, "swipe": 2}),
        row("shared", "sub-shared", 2, "App B", "场景一", "查询", 18, "数据飞轮-修正", "3", {"finish": 1, "wait": 1}),
        row("legacy", "sub-legacy", 1, "未记录 App", "未分类", "未分类", 19, "历史导入", 99, {"finish": 1}),
        row("zero", "sub-zero", 0, "App B", "场景一", "导航", 18, "其他数据飞轮数据", "invalid"),
        row("collision", "sub-collision", 5, "App C", "A-B", "C", 19, manual=1, actions={"swipe": 5}),
        row("collision", "sub-collision", 1, "App C", "A", "B-C", 19, manual=0, actions={"click": 1}),
        row("repeat-app", "sub-shared", 2, "App A", "场景二", "导航", 20, "模型生成", 42, {"wait": 2}),
    ]


def reference(rows, filters):
    frame = pd.DataFrame(deepcopy(rows))
    if "时间" not in frame:
        frame = pd.DataFrame(columns=list(data_rows()[0]))
    frame["日期"] = frame["时间"].astype(str).str[:10]
    frame["action_box"] = frame["action_box"].map(lambda value: repr(value))
    names = {"get_filtered_df", "get_data", "api_sources", "api_scenes", "api_apps",
             "api_date_range", "api_scene_stats", "api_distributions"}
    tree = ast.parse(REFERENCE.read_text(encoding="utf-8-sig"), filename=str(REFERENCE))
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
    for function in functions:
        function.decorator_list = []
    module = ast.Module(body=functions, type_ignores=[])
    args = {key: value for key, value in filters.items() if value}
    namespace = {"pd": pd, "ast": ast, "get_df": lambda: frame.copy(deep=True),
                 "DATA_FILE": "fixture", "request": SimpleNamespace(args=args), "jsonify": lambda value: value}
    exec(compile(module, str(REFERENCE), "exec"), namespace)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        value = namespace["get_data"](source=filters.get("source") or "all", level1=filters.get("level1"),
            level2=filters.get("level2"), start_date=filters.get("start_date"), end_date=filters.get("end_date"),
            app_filter=filters.get("app") or "all")
        value["scene_stats"] = namespace["api_scene_stats"]()
        value.update(namespace["api_distributions"]())
        value["filters"] = {"sources": namespace["api_sources"](), "scenes": namespace["api_scenes"](),
                            "apps": namespace["api_apps"](), "date_range": namespace["api_date_range"]()}
    return value


class DevParityTests(unittest.TestCase):
    def assert_reference(self, rows, filters):
        before = deepcopy(rows)
        actual = summarize({"rows": rows}, [], filters)
        expected = reference(rows, filters)
        self.assertEqual({key: actual["overview"][key] for key in expected["overview"]}, expected["overview"])
        self.assertEqual(actual["trend"], [{"date": item["date"], "total_trajectories": item["total_trajectories"],
            "subtask_trajectories": item["subtask_trajectories"], "total_steps": item["total_steps_cumulative"]}
            for item in expected["trend"]])
        self.assertEqual(actual["app_stats"], [{"name": item["app"], **{key: item[key] for key in (
            "total_trajectories", "subtask_trajectories", "total_steps")}} for item in expected["app_stats"]])
        self.assertEqual([{key: item[key] for key in ("name", "total_trajectories", "subtask_trajectories", "total_steps")}
                          for item in actual["scene_stats"]],
            [{"name": item["scene_name"], **{key: item[key] for key in (
                "total_trajectories", "subtask_trajectories", "total_steps")}} for item in expected["scene_stats"]])
        self.assertEqual(actual["action_stats"], expected["action_stats"])
        self.assertEqual(actual["step_stats"], expected["step_stats"])
        expected["filters"]["date_range"] = {key: value or None for key, value in expected["filters"]["date_range"].items()}
        self.assertEqual(actual["filters"], expected["filters"])
        self.assertEqual(rows, before)
        return actual

    def test_original_functions_match_cards_groups_trend_distribution_and_facets(self):
        filters = [{}, {"source": "all", "app": "all"}, {"source": "历史导入"}, {"source": "数据飞轮"},
                   {"source": "数据飞轮-修正"}, {"app": "App A"}, {"app": "未记录 App"},
                   {"level1": "场景二", "level2": "查询"}, {"start_date": "2026-09-18", "end_date": "2026-09-19"},
                   {"app": "App A", "start_date": "2026-09-18"}, {"app": "missing"}]
        for selected in filters:
            with self.subTest(filters=selected):
                self.assert_reference(data_rows(), selected)

    def test_intentionally_different_card_and_group_counts_match_dev(self):
        actual = self.assert_reference(data_rows(), {})
        self.assertEqual(actual["overview"]["subtask_trajectories"], 8)
        self.assertEqual(actual["overview"]["total_trajectories"], 5)
        self.assertEqual(actual["trend"][-1]["total_trajectories"], 6)
        self.assertEqual(actual["overview"]["total_apps"], 4)
        self.assertEqual(actual["overview"]["level2_scenes"], 5)
        self.assertEqual(actual["overview"]["manual_refine_steps"], 6)
        self.assertTrue(actual["overview"]["show_manual_refine_steps"])
        self.assertIn({"steps": -1, "count": 1}, actual["step_stats"])
        self.assertEqual(len([item for item in actual["scene_stats"] if item["name"] == "A-B-C"]), 1)

    def test_manual_visibility_empty_missing_and_non_flywheel_rows_match_dev(self):
        one = data_rows()[1]
        self.assertTrue(self.assert_reference([one], {})["overview"]["show_manual_refine_steps"])
        self.assertEqual(self.assert_reference([one], {})["overview"]["manual_refine_steps"], 0)
        one.pop("人工精修步骤数量")
        self.assertFalse(self.assert_reference([one], {})["overview"]["show_manual_refine_steps"])
        historical = self.assert_reference(data_rows(), {"source": "历史导入"})["overview"]
        self.assertEqual(historical["manual_refine_steps"], 0)
        self.assertFalse(historical["show_manual_refine_steps"])
        self.assert_reference([], {})

    def test_cascading_facet_queries_preserve_date_app_and_scene_context(self):
        rows = data_rows()
        selected = {"source": "all", "level1": "场景二", "level2": "查询", "app": "App A", "start_date": "2026-09-18"}
        full = self.assert_reference(rows, selected)
        self.assertEqual(full["filters"]["apps"], [])
        scene_context = self.assert_reference(rows, {**selected, "level2": ""})
        self.assertEqual(scene_context["filters"]["scenes"], [{"name": "场景二", "level2_scenes": ["导航"]}])
        self.assertEqual(scene_context["filters"]["date_range"], {"min_date": "2026-09-20", "max_date": "2026-09-20"})
        app_context = self.assert_reference(rows, {"source": "all", "app": "App A", "start_date": "2026-09-18", "end_date": "2026-09-19"})
        self.assertEqual(app_context["filters"]["apps"], ["App B", "App C", "未记录 App"])
        self.assertEqual(app_context["filters"]["scenes"], [])
        self.assertEqual(app_context["filters"]["sources"], sorted({row["生产方式"] for row in rows}))

    def test_http_keeps_api_shape_and_queries_only_the_committed_catalog(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = TrainingOverviewManager(Registry(Path(directory)))
            try:
                manager.records.put(CATALOG, "current", {"schema_version": 1, "version": "fixture", "rows": data_rows()})
                before = manager.records.get(CATALOG, "current")
                app = FastAPI()
                app.include_router(router)
                with patch("backend.training_data_overview.router.get_manager", return_value=manager), TestClient(app) as client:
                    response = client.get("/api/training-data-overview", params={"source": "all", "app": "all"})
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.headers["cache-control"], "no-store")
                    value = response.json()
                    self.assertEqual(value["overview"]["subtask_trajectories"], 8)
                    self.assertIs(value["overview"]["show_manual_refine_steps"], True)
                    self.assertEqual(value["workbook_url"], "/api/training-data-overview/workbook")
                    self.assertEqual(value["conversions"], [])
                    self.assertTrue(all("name" in item and "level1" in item and "level2" in item for item in value["scene_stats"]))
                self.assertEqual(manager.records.get(CATALOG, "current"), before)
                self.assertFalse((Path(directory) / "system" / "training_data_overview").exists())
            finally:
                manager.close()


if __name__ == "__main__":
    unittest.main()
