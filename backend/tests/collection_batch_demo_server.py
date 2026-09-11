"""Disposable UI acceptance server; every phone operation stays in memory.

Run: python -m backend.tests.collection_batch_demo_server --port 8793
"""
from __future__ import annotations

import argparse
import base64
import io
import tempfile
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from openpyxl import load_workbook

from backend.task_generation.constants import PROJECT_ROOT
from backend.task_generation.jobs import TaskGenerationJobManager
from backend.task_generation.router import configure_job_manager, router
from backend.tests.test_task_generation import _write_knowledge_base


def disabled_runner(*args, **kwargs):
    raise RuntimeError("此演示仅使用预置结果，不调用模型")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8793)
    args = parser.parse_args()
    dist = PROJECT_ROOT / "frontend" / "dist"
    with tempfile.TemporaryDirectory(prefix="collection-batch-browser-") as temporary:
        base = Path(temporary)
        kb = base / "kb"
        _write_knowledge_base(kb)
        manager = TaskGenerationJobManager(
            base / "jobs", base / "runs", base / "exports", kb, base / "logs",
            initial_runner=disabled_runner, augmentation_runner=disabled_runner,
            classification_runner=disabled_runner, generation_runner=disabled_runner,
        )
        shared = {"app": "AppA", "scene": "场景A", "capability": "能力A", "sub_capability": "子能力A", "deleted": False}
        initial = manager._new_job("task_generation", total_items=1, generate_n=3)
        manager._finish(initial["job_id"], {"results": [
            {**shared, "result_id": "pre-demo", "task_uuid": "pre-demo", "task": "在 AppA 中收藏一条内容", "pre_dependency": "pre_node", "dependency_group_id": "group-demo"},
            {**shared, "result_id": "main-demo", "task_uuid": "main-demo", "task": "在 AppA 中查看收藏内容", "pre_dependency": "weak", "pre_task_uuid": "pre-demo", "dependency_group_id": "group-demo"},
            {**shared, "result_id": "strong-demo", "task_uuid": "strong-demo", "task": "在 AppA 中查询已有订单", "pre_dependency": "strong", "status": "-2"},
            {**shared, "result_id": "zero-demo", "task_uuid": "zero-demo", "task": "在 AppA 中搜索周末活动", "pre_dependency": "zero"},
        ], "errors": []})
        augmented = manager._new_job("augmentation", total_items=1, generate_n=1, input_filename="模拟失败用例.xlsx")
        manager._finish(augmented["job_id"], {"results": [
            {**shared, "result_id": "variant-demo", "task": "在 AppA 中搜索附近展览", "用例编号": "DEMO-APP-001", "source_row": 2, "source_task": "搜索活动", "seed_id": "seed-demo"},
        ], "errors": [{"error": "另一条模拟种子生成失败", "stage": "generating"}], "warnings": ["模拟部分完成"]})
        configure_job_manager(manager)
        app = FastAPI(title="Collection batch acceptance: no real model or phone calls")
        app.include_router(router)
        state = {"phones": ["DEMO-PHONE"], "apps": ["AppA"], "phoneApps": [{"phone_id": "DEMO-PHONE", "app": "AppA", "status": "未运行"}], "vla": ["模拟接口"], "tasks": []}
        config = {"sampling_enabled": False, "temperature": 0.7, "top_p": 0.85, "use_experience_lib": False}
        files: dict[str, bytes] = {}
        starts = []

        @app.get("/api/phone-factory/state")
        def phone_state():
            return state

        @app.get("/api/phone-factory/config")
        def phone_config():
            return config

        @app.post("/api/phone-factory/tasks")
        def import_task(data: dict):
            filename = data["filename"]
            content = base64.b64decode(data["content_base64"], validate=True)
            current = next((task for task in state["tasks"] if task["filename"] == filename), None)
            if current:
                if files[filename] != content:
                    raise HTTPException(409, "模拟批次内容冲突")
            else:
                files[filename] = content
                state["tasks"].append({"description": data["description"], "filename": filename, "status": "未运行", "source_batch_id": data.get("source_batch_id")})
            return state

        @app.post("/api/phone-factory/remote/start-run")
        def start_run(data: dict):
            if data["filename"] not in files:
                raise HTTPException(404, "模拟任务尚未上传")
            starts.append(data)
            return {"ok": True, "message": "模拟采集已接收；未向真实手机下发"}

        @app.post("/api/phone-factory/tasks/start")
        def mark_started(data: dict):
            for task in state["tasks"]:
                if task["filename"] == data["filename"]:
                    task["status"] = "运行中"
            return state

        @app.get("/demo/audit")
        def audit():
            sheets = {}
            for filename, content in files.items():
                workbook = load_workbook(io.BytesIO(content), read_only=True, data_only=False)
                sheets[filename] = list(workbook.active.values)
                workbook.close()
            return {"initial_job": initial["job_id"], "augmentation_job": augmented["job_id"], "tasks": state["tasks"], "starts": starts, "workbooks": sheets}

        app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

        @app.get("/{path:path}")
        def frontend(path: str):
            if path.startswith("api/"):
                raise HTTPException(404)
            return FileResponse(dist / "index.html")

        try:
            uvicorn.run(app, host="127.0.0.1", port=args.port)
        finally:
            manager._executor.shutdown(wait=True, cancel_futures=True)


if __name__ == "__main__":
    main()
