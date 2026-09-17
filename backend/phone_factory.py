"""Phone collection API backed by the shared data store.

Current state lives only in SQLite and uploads live under the data root.
GET requests do not create data or import previous collection records.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Callable

from fastapi import APIRouter, Body, Request
from fastapi.responses import JSONResponse

from .data_store import DATA_ROOT, RecordStore, RevisionConflict
from .data_store.paths import contained_path
from .collection_runs import CollectionRunError, CollectionRunStore

DEFAULT_CONFIG = {
    "sampling_enabled": False, "temperature": 0.7,
    "top_p": 0.85, "use_experience_lib": False,
}
CLIENT_SCRIPT = Path(__file__).resolve().with_name("phonefactory_client.py")


class PhoneFactoryError(ValueError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def run_client(args: list[str]) -> dict:
    """Keep the existing client protocol and a bounded subprocess timeout."""
    try:
        result = subprocess.run(
            [sys.executable, str(CLIENT_SCRIPT), *args],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=12, check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return {"ok": False, "output": f"功能不支持或者网络断连：{exc}"}
    return {
        "ok": result.returncode == 0,
        "output": (result.stdout if result.returncode == 0 else result.stderr or result.stdout).strip(),
    }


def _text(value) -> str:
    return str(value if value is not None else "").strip()


def _filename(value) -> str:
    filename = _text(value).replace("\\", "/").rsplit("/", 1)[-1]
    if not filename or filename in {".", ".."}:
        raise PhoneFactoryError("任务文件名不能为空")
    if len(filename) > 180 or re.search(r'[<>:"/\\|?*\x00-\x1f]', filename) or filename.endswith((".", " ")) or re.fullmatch(r"(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(\..*)?", filename, re.I):
        raise PhoneFactoryError("任务文件名无效")
    return filename


def _atomic_write(destination: Path, content: bytes) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("xb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


class PhoneFactoryStore:
    def __init__(
        self, root: Path = DATA_ROOT, *, run_client_fn: Callable | None = None,
        collection_runs: CollectionRunStore | None = None,
    ):
        self.root = contained_path(Path(root))
        self.records = RecordStore(self.root)
        self.upload_dir = contained_path(self.root, "inputs", "phone_factory")
        self.run_client = run_client_fn or run_client
        self.collection_runs = collection_runs or CollectionRunStore(self.root)

    @staticmethod
    def _empty_state() -> dict:
        return {"phones": [], "apps": [], "phoneApps": [], "vla": [], "tasks": [],
                "config": dict(DEFAULT_CONFIG)}

    def initialize_settings(self, settings: dict) -> dict:
        """Explicitly seed devices/configuration once, without task history or files.

        This is an administrative Python entry point, never called by API reads.
        The caller supplies reviewed values; this method reads no old directories.
        """
        allowed = {"phones", "apps", "phoneApps", "vla", "config"}
        if not isinstance(settings, dict) or set(settings) - allowed:
            raise PhoneFactoryError("初始化仅支持设备、App、关联关系、VLA 和配置，不支持任务或运行记录", 409)
        state = self._empty_state()
        for key in ("phones", "apps", "vla"):
            values = settings.get(key, [])
            if not isinstance(values, list) or any(not isinstance(value, str) or not value.strip() for value in values):
                raise PhoneFactoryError(f"初始化 {key} 必须为非空文本列表", 409)
            state[key] = list(dict.fromkeys(value.strip() for value in values))
        associations = settings.get("phoneApps", [])
        if not isinstance(associations, list):
            raise PhoneFactoryError("初始化 phoneApps 必须为列表", 409)
        for row in associations:
            if not isinstance(row, dict) or not isinstance(row.get("phone_id"), str) or not isinstance(row.get("app"), str):
                raise PhoneFactoryError("初始化手机 App 关联格式无效", 409)
            phone_id, app = row["phone_id"].strip(), row["app"].strip()
            if not phone_id or not app:
                raise PhoneFactoryError("初始化手机 App 关联不能为空", 409)
            association = {"phone_id": phone_id, "app": app, "status": "空闲"}
            if association not in state["phoneApps"]:
                state["phoneApps"].append(association)
            for key, value in (("phones", phone_id), ("apps", app)):
                if value not in state[key]:
                    state[key].append(value)
        config = settings.get("config", {})
        if not isinstance(config, dict) or set(config) - set(DEFAULT_CONFIG):
            raise PhoneFactoryError("初始化采集配置格式无效", 409)
        config = {**DEFAULT_CONFIG, **config}
        for key in ("sampling_enabled", "use_experience_lib"):
            if not isinstance(config[key], bool):
                raise PhoneFactoryError(f"初始化 {key} 必须为布尔值", 409)
        for key in ("temperature", "top_p"):
            value = config[key]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise PhoneFactoryError(f"初始化 {key} 必须为有限数字", 409)
        state["config"] = config
        try:
            saved = self.records.put("phone_factory", "state", state, expected_revision=0)
        except RevisionConflict as exc:
            raise PhoneFactoryError("手机采集状态已经初始化，不覆盖现有数据", 409) from exc
        return self._public(saved)

    def _current(self) -> dict:
        current = self.records.get("phone_factory", "state")
        return current if current is not None else self._empty_state()

    @staticmethod
    def _public(state: dict) -> dict:
        return {key: state.get(key, []) for key in ("phones", "apps", "phoneApps", "vla", "tasks")}

    def state(self) -> dict:
        return self._public(self._current())

    def config(self) -> dict:
        return self._current()["config"]

    def _update(self, mutator: Callable[[dict], None]) -> dict:
        return self.records.update("phone_factory", "state", mutator, default=self._empty_state())

    def task_path(self, filename: str) -> Path:
        return contained_path(self.upload_dir, filename)

    def add_task(self, data: dict) -> dict:
        filename = _filename(data.get("filename"))
        description = _text(data.get("description"))
        if not description:
            raise PhoneFactoryError("任务描述不能为空")
        try:
            content = base64.b64decode(_text(data.get("content_base64")), validate=True)
        except (ValueError, binascii.Error) as exc:
            raise PhoneFactoryError("文件内容不是有效的Base64") from exc
        if not content:
            raise PhoneFactoryError("文件内容为空")
        batch = data.get("source_batch_id")
        if "source_batch_id" in data and (not isinstance(batch, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", batch)):
            raise PhoneFactoryError("采集批次编号无效")
        if batch and filename != f"collection-batch-{batch}.xlsx":
            raise PhoneFactoryError("采集批次文件名不匹配", 409)
        if not batch and filename.lower().startswith("collection-batch-"):
            raise PhoneFactoryError("该文件名前缀仅供采集批次使用，请重命名手动上传文件", 409)
        digest = hashlib.sha256(content).hexdigest()
        destination = contained_path(self.upload_dir, filename)

        def mutate(state):
            tasks = state["tasks"]
            existing = next((row for row in tasks if row.get("source_batch_id") == batch), None) if batch else None
            path = self.task_path(filename)
            if existing:
                if existing["filename"] != filename or (existing.get("content_sha256") and existing["content_sha256"] != digest):
                    raise PhoneFactoryError("该采集批次已导入，不能替换为不同内容", 409)
                if path.is_file():
                    if path.read_bytes() != content:
                        raise PhoneFactoryError("该采集批次已导入，不能替换为不同内容", 409)
                elif not existing.get("content_sha256"):
                    raise PhoneFactoryError("已登记批次文件缺失，无法确认原始内容", 409)
                else:
                    _atomic_write(destination, content)
                return
            occupied = next((row for row in tasks if row.get("filename") == filename), None)
            if occupied:
                if batch or occupied.get("source_batch_id"):
                    raise PhoneFactoryError("任务文件名已被其他任务占用", 409)
                if path.is_file() and path.read_bytes() == content:
                    return
                raise PhoneFactoryError("任务文件名已存在，请使用新文件名，已有任务及运行状态不会被覆盖", 409)
            if batch and path.is_file() and path.read_bytes() != content:
                raise PhoneFactoryError("批次文件已存在且内容不同", 409)
            _atomic_write(destination, content)
            row = {"description": description, "filename": filename, "status": "未运行"}
            if batch:
                row.update(source_batch_id=batch, content_sha256=digest)
            tasks.append(row)

        return self._public(self._update(mutate))

    def start_task(self, filename) -> dict:
        filename = _filename(filename)

        def mutate(state):
            task = next((row for row in state["tasks"] if row.get("filename") == filename), None)
            if task is None:
                raise PhoneFactoryError(f"任务 {filename} 不存在", 404)
            task["status"] = "运行中"

        return self._public(self._update(mutate))

    def remove_task(self, filename) -> dict:
        filename = _filename(filename)
        return self._public(self._update(lambda state: state.update(tasks=[row for row in state["tasks"] if row.get("filename") != filename])))

    def _remote_result(self, args: list[str]):
        result = self.run_client(args)
        if not result["ok"]:
            raise PhoneFactoryError(result.get("output") or "功能不支持或者网络断连", 502)
        try:
            return json.loads(result["output"])
        except (TypeError, ValueError):
            return {"ok": True, "message": result.get("output", "")}

    def remote_start(self, data: dict):
        filename = _filename(data.get("filename"))
        state = self._current()
        task = next((row for row in state["tasks"] if row.get("filename") == filename), None)
        if task is None:
            raise PhoneFactoryError(f"任务 {filename} 不存在", 404)
        task_path = self.task_path(filename)
        if not task_path.is_file():
            raise PhoneFactoryError("已登记任务文件缺失", 409)
        if task.get("content_sha256") and hashlib.sha256(task_path.read_bytes()).hexdigest() != task["content_sha256"]:
            raise PhoneFactoryError("采集批次文件内容与登记校验值不一致", 409)
        config = state["config"]
        batch_id = task.get("source_batch_id")
        if data.get("batch_id") is not None and data["batch_id"] != batch_id:
            raise PhoneFactoryError("采集批次与已登记任务不匹配", 409)
        run = None
        if batch_id:
            request_id = data.get("request_id", data.get("dispatch_key"))
            run, created = self.collection_runs.create(
                batch_id, dispatch_key=request_id,
                workbook_sha256=hashlib.sha256(task_path.read_bytes()).hexdigest(),
                metadata={"filename": filename, "phone_id": _text(data.get("phone_id")),
                          "app": _text(data.get("app")), "config": config,
                          "phone_apps": state["phoneApps"]},
            )
            if not created:
                run, created = self.collection_runs.retry_dispatch(run["collection_run_id"])
                if not created:
                    return {**run.get("dispatch_response", {"ok": True}),
                            "batch_id": batch_id, "collection_run_id": run["collection_run_id"],
                            "output_dir": run["output_dir"], "collection_status": run["status"],
                            "dispatch_pending": run["status"] == "dispatching"}
        try:
            temp_dir = contained_path(self.root, "tmp", "phone_factory")
            temp_dir.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(dir=temp_dir) as directory:
                apps_path = Path(directory) / "phone_apps.json"
                apps_path.write_text(json.dumps(state["phoneApps"], ensure_ascii=False), encoding="utf-8")
                args = ["start-run", str(task_path), str(apps_path), _text(data.get("phone_id")), _text(data.get("app"))]
                if config["sampling_enabled"]:
                    args.append("--sampling")
                args.extend(["--temperature", str(config["temperature"]), "--top-p", str(config["top_p"])])
                if config["use_experience_lib"]:
                    args.append("--exp")
                if run is not None:
                    args.extend(["--batch-id", batch_id, "--collection-run-id", run["collection_run_id"],
                                 "--output-dir", run["output_dir"]])
                response = self._remote_result(args)
                if run is None:
                    return response
                if not isinstance(response, dict):
                    raise PhoneFactoryError("手机采集服务返回了无效响应", 502)
                if response.get("ok") is False:
                    raise PhoneFactoryError(str(response.get("error") or response.get("message") or "手机采集服务拒绝运行"), 502)
                saved = self.collection_runs.dispatched(run["collection_run_id"], response)
                return {**response, "batch_id": batch_id, "collection_run_id": run["collection_run_id"],
                        "output_dir": run["output_dir"], "collection_status": saved["status"]}
        except Exception as exc:
            if run is not None:
                self.collection_runs.dispatch_failed(run["collection_run_id"], str(exc))
            raise

    def dispatch(self, action: str, method: str, data: dict):
        if method == "GET" and action == "collection-runs":
            return {"runs": self.collection_runs.list_runs(data.get("batch_id"))}
        if action.startswith("collection-runs/"):
            parts = action.split("/")
            if method == "GET" and len(parts) == 2:
                run = self.collection_runs.get(parts[1])
                if run is None:
                    raise PhoneFactoryError("采集运行不存在", 404)
                return run
            if method == "POST" and len(parts) == 3 and parts[2] == "complete":
                run, _ = self.collection_runs.complete(parts[1], data)
                return run
        if method == "GET" and action == "state":
            return self.state()
        if method == "GET" and action == "config":
            return self.config()
        if method == "POST" and action == "tasks":
            return self.add_task(data)
        if method == "POST" and action == "tasks/start":
            return self.start_task(data.get("filename"))
        if method == "DELETE" and action == "tasks":
            return self.remove_task(data.get("filename"))
        if method == "POST" and action == "remote/start-run":
            return self.remote_start(data)
        if method == "POST" and action == "remote/add-phone":
            phone_id = _text(data.get("phone_id"))
            if not phone_id:
                raise PhoneFactoryError("手机ID不能为空")
            return self._remote_result(["add-phone", phone_id])
        if method == "POST" and action == "config":
            config = {}
            for key in ("temperature", "top_p"):
                try:
                    value = float(data[key])
                except (KeyError, TypeError, ValueError):
                    value = float("nan")
                if not math.isfinite(value) and data.get("sampling_enabled"):
                    raise PhoneFactoryError("temperature 与 top_p 必须是数字")
                config[key] = value if math.isfinite(value) else DEFAULT_CONFIG[key]
            config.update(sampling_enabled=bool(data.get("sampling_enabled")), use_experience_lib=bool(data.get("use_experience_lib")))
            self._update(lambda state: state.update(config=config))
            return config
        if method == "POST" and action in {"phones", "apps", "vla"}:
            key, label = {"phones": ("phone_id", "手机ID"), "apps": ("app", "APP名称"), "vla": ("value", "VLA接口")}[action]
            value = _text(data.get(key))
            if not value:
                raise PhoneFactoryError(f"{label}不能为空")

            def append(state):
                if value in state[action]:
                    raise PhoneFactoryError(f"{label} {value} 已存在")
                state[action].append(value)

            return self._public(self._update(append))
        if action == "phone-apps" and method in {"POST", "DELETE"}:
            phone_id, app = _text(data.get("phone_id")), _text(data.get("app"))
            if method == "POST" and (not phone_id or not app):
                raise PhoneFactoryError("手机ID不能为空" if not phone_id else "运行APP不能为空")

            def associate(state):
                existing = [row for row in state["phoneApps"] if row.get("phone_id") == phone_id and row.get("app") == app]
                if method == "DELETE":
                    state["phoneApps"] = [row for row in state["phoneApps"] if row not in existing]
                    return
                if existing:
                    raise PhoneFactoryError(f"手机 {phone_id} 已关联 APP {app}")
                state["phoneApps"].append({"phone_id": phone_id, "app": app, "status": "空闲"})
                if phone_id not in state["phones"]:
                    state["phones"].append(phone_id)
                if app not in state["apps"]:
                    state["apps"].append(app)

            return self._public(self._update(associate))
        raise PhoneFactoryError(f"Unsupported {method} /{action}", 404)


def create_router(store: PhoneFactoryStore | None = None) -> APIRouter:
    service = store if store is not None else PhoneFactoryStore()
    result = APIRouter(prefix="/api/phone-factory", tags=["phone-factory"])

    @result.api_route("/{action:path}", methods=["GET", "POST", "DELETE"])
    def dispatch(action: str, request: Request, data: dict | None = Body(default=None)):
        try:
            body = dict(request.query_params) if request.method == "GET" else data or {}
            return JSONResponse(service.dispatch(action, request.method, body), headers={"Cache-Control": "no-store"})
        except (PhoneFactoryError, CollectionRunError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=exc.status, headers={"Cache-Control": "no-store"})
        except (ValueError, OSError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=500, headers={"Cache-Control": "no-store"})

    return result


router = create_router()
