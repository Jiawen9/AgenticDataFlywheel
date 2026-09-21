"""Standalone 9011 service. Run: uvicorn backend.phonefactory_manager:app --port 9011."""
from __future__ import annotations

import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from starlette.concurrency import run_in_threadpool

from .collector_service.adapters import AdbAdapter, TemplateExecutor
from .collector_service.core import Collector, CollectorConfig, CollectorError, phone_identifier


def create_app(config: CollectorConfig | None = None, *, device_adapter=None, execution_adapter=None, executor=None):
    config = config or CollectorConfig.from_env()
    creation_lock = threading.Lock()
    def service():
        with creation_lock:
            if app.state.collector is None:
                app.state.collector = Collector(config, device_adapter=device_adapter or AdbAdapter(config),
                                                execution_adapter=execution_adapter or TemplateExecutor(config),
                                                executor=executor)
            return app.state.collector

    @asynccontextmanager
    async def lifespan(_app):
        service()
        yield
        if app.state.collector:
            app.state.collector.close()

    app = FastAPI(title="Phone factory collector", lifespan=lifespan)
    app.state.collector = None

    @app.exception_handler(CollectorError)
    async def collector_error(_request, exc):
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=exc.status)

    @app.get("/capabilities")
    def capabilities():
        return {"protocol_version": 1, "batch_results": True, "persistent_runs": True,
                "run_modes": ["generate", "modeliter"], "report_ids": True}

    @app.post("/add_phone")
    def add_phone(payload: dict):
        return service().add_phone(payload.get("phoneid") or payload.get("phone_id"))

    @app.post("/del_phone")
    def delete_phone(payload: dict):
        return service().delete_phone(payload.get("phoneid") or payload.get("phone_id"))

    @app.post("/status")
    def status(payload: dict):
        phones = payload.get("phones") or []
        if not isinstance(phones, list):
            raise CollectorError("phones 必须为数组")
        return {"ok": True, "statuses": service().statuses(phones)}

    @app.post("/adb_devices")
    def devices():
        return {"ok": True, "devices": service().devices.devices()}

    @app.post("/monitor")
    def monitor(payload: dict):
        phone = phone_identifier(payload.get("phone_id") or payload.get("phoneid"))
        collector = service()
        result = collector.devices.monitor(phone, collector.phone_dir(phone))
        return {"ok": True, **result, "running": collector.statuses([phone])[0]["status"] == "运行中"}

    @app.post("/start_run")
    async def start_run(request: Request):
        form = await request.form(max_files=2, max_fields=24, max_part_size=config.max_upload_bytes)
        try:
            task_file, apps_file = form.get("task_file"), form.get("apps_file")
            if not hasattr(task_file, "read") or not hasattr(apps_file, "read"):
                raise CollectorError("缺少 task_file 或 apps_file")
            task_bytes = await task_file.read(config.max_upload_bytes + 1)
            apps_bytes = await apps_file.read(config.max_upload_bytes + 1)
            params = {k: v for k, v in form.items() if isinstance(v, str)}
            if params.get("launch", "1").lower() in {"0", "false", "no"}:
                raise CollectorError("持久运行协议不支持 launch=0；请直接校验任务表而不要创建运行")
            return await run_in_threadpool(service().submit, task_bytes=task_bytes, apps_bytes=apps_bytes, form=params)
        finally:
            await form.close()

    @app.get("/runs/{run_id}")
    def get_run(run_id: str):
        return service().public(service().get(run_id))

    @app.get("/runs/{run_id}/archive")
    def archive(run_id: str):
        return FileResponse(service().archive(run_id), media_type="application/zip", filename=run_id + ".zip")

    @app.get("/reports")
    def reports(runmode: str = "modeliter"):
        return service().reports(runmode)

    @app.get("/report_download")
    def report_download(runmode: str = "modeliter", folder: str | None = None, name: str | None = None,
                        run_id: str | None = None, file_id: str | None = None):
        path = service().report(mode=runmode, run_id=run_id, file_id=file_id, folder=folder, name=name)
        return FileResponse(path, filename=path.name, media_type="application/octet-stream")

    return app


app = create_app()
