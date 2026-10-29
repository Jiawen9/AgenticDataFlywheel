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
from fastapi.responses import JSONResponse, Response

from .batch_operations import active_batch_lock
from .batch_lifecycle import is_batch_active
from .data_store import DATA_ROOT, RecordStore, RevisionConflict
from .data_store.paths import contained_path
from .data_store.locking import batch_lock
from .collection_runs import CollectionRunError, CollectionRunStore
from .agent_types import normalize_agent_type
from .pipeline_model_config import default_models, normalize_defaults

DEFAULT_CONFIG = {
    "sampling_enabled": False, "temperature": 0.7,
    "top_p": 0.85, "use_experience_lib": False, "capture_logprobs": False,
}
CLIENT_SCRIPT = Path(__file__).resolve().with_name("phonefactory_client.py")
CLIENT_ERROR_PREFIX = "__PHONE_FACTORY_ERROR__="
CLIENT_TIMEOUT_MESSAGE = "错误：采集服务请求超时，请用同一请求编号重试"


class PhoneFactoryError(ValueError):
    def __init__(self, message: str, status: int = 400, *,
                 remote_kind: str | None = None, remote_status: int | None = None,
                 remote_command: str | None = None):
        super().__init__(message)
        self.status = status
        # Internal metadata is separate from the existing public HTTP status/message.
        self.remote_kind = remote_kind
        self.remote_status = remote_status
        self.remote_command = remote_command


def _client_failure(output: str) -> dict:
    """Read the opt-in stderr envelope; accept only unambiguous legacy errors."""
    output = output.strip()
    details = {}
    first, _, remainder = output.partition("\n")
    if first.startswith(CLIENT_ERROR_PREFIX):
        output = remainder.strip()
        try:
            metadata = json.loads(first[len(CLIENT_ERROR_PREFIX):])
            kind = metadata.get("remote_kind") if isinstance(metadata, dict) else None
            code = metadata.get("remote_status") if isinstance(metadata, dict) else None
            if kind in {"http", "timeout", "connection", "protocol", "client"}:
                details["remote_kind"] = kind
                if kind == "http" and type(code) is int and 100 <= code <= 599:
                    details["remote_status"] = code
        except (TypeError, ValueError):
            pass
    if not details:
        # Do not classify arbitrary messages containing 404/timeout as remote errors.
        match = re.search(r"(?:（HTTP ([1-5]\d{2})）|\(HTTP ([1-5]\d{2})\))$", output)
        if output.startswith("错误：") and match:
            details = {"remote_kind": "http", "remote_status": int(match[1] or match[2])}
        elif output == CLIENT_TIMEOUT_MESSAGE:
            details = {"remote_kind": "timeout"}
    return {"ok": False, "output": output, **details}


def run_client(args: list[str]) -> dict:
    """Keep the existing client protocol and a bounded subprocess timeout."""
    try:
        environment = dict(os.environ, PHONE_FACTORY_CLIENT_ERROR_METADATA="1")
        result = subprocess.run(
            [sys.executable, str(CLIENT_SCRIPT), *args],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=float(os.environ.get("PHONE_FACTORY_TRANSFER_TIMEOUT", "300") if args and args[0] in {"run-archive", "report-download"} else os.environ.get("PHONE_FACTORY_TIMEOUT", "30")) + 5, check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            env=environment,
        )
    except subprocess.TimeoutExpired:
        # TimeoutExpired.__str__ includes the command, which may contain request data.
        return {"ok": False, "output": CLIENT_TIMEOUT_MESSAGE, "remote_kind": "timeout"}
    except OSError:
        return {"ok": False, "output": "功能不支持或者网络断连：采集客户端启动失败", "remote_kind": "client"}
    if result.returncode == 0:
        return {"ok": True, "output": result.stdout.strip()}
    return _client_failure(result.stderr or result.stdout)


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
                "config": dict(DEFAULT_CONFIG), "agent_type": "newagent"}

    def initialize_settings(self, settings: dict) -> dict:
        """Explicitly seed devices/configuration once, without task history or files.

        This is an administrative Python entry point, never called by API reads.
        The caller supplies reviewed values; this method reads no old directories.
        """
        allowed = {"phones", "apps", "phoneApps", "vla", "config", "agent_type"}
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
        for key in ("sampling_enabled", "use_experience_lib", "capture_logprobs"):
            if not isinstance(config[key], bool):
                raise PhoneFactoryError(f"初始化 {key} 必须为布尔值", 409)
        for key in ("temperature", "top_p"):
            value = config[key]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise PhoneFactoryError(f"初始化 {key} 必须为有限数字", 409)
        state["config"] = config
        try:
            state["agent_type"] = normalize_agent_type(settings.get("agent_type", "newagent"))
        except ValueError as exc:
            raise PhoneFactoryError(str(exc)) from exc
        try:
            saved = self.records.put("phone_factory", "state", state, expected_revision=0)
        except RevisionConflict as exc:
            raise PhoneFactoryError("手机采集状态已经初始化，不覆盖现有数据", 409) from exc
        return self._public(saved)

    def _current(self) -> dict:
        current = self.records.get("phone_factory", "state")
        return current if current is not None else self._empty_state()

    @property
    def runtime(self):
        if not hasattr(self, "_runtime"):
            from .phone_factory_runtime import FactoryRuntime
            self._runtime = FactoryRuntime(self)
        return self._runtime

    def _public(self, state: dict) -> dict:
        result = {key: state.get(key, []) for key in ("phones", "apps", "phoneApps", "vla", "tasks")}
        tasks = []
        for item in result["tasks"]:
            batch_id = item.get("source_batch_id")
            batch = self.collection_runs.result_batches.get(batch_id) if batch_id else None
            if batch is not None:
                if not is_batch_active(batch_id, self.root):
                    continue
                item = {**item, "batch_role": "collection_result", "task_source_batch_id": batch["task_source_batch_id"]}
            # Source workbooks are reusable read-only inputs, including sources
            # whose historical results have already been published.
            tasks.append(dict(item))
        result["tasks"] = tasks
        return result

    def state(self, mode="generate") -> dict:
        return self.runtime.public_state(self._public(self._current()), mode)

    def config(self, mode="generate") -> dict:
        state = self._current()
        return {**DEFAULT_CONFIG, **state["config"], "agent_type": state.get("agent_type", "newagent"),
                "model_defaults": state.get("model_defaults", {}).get(mode, default_models())}

    def _update(self, mutator: Callable[[dict], None]) -> dict:
        return self.records.update("phone_factory", "state", mutator, default=self._empty_state())

    def task_path(self, filename: str) -> Path:
        return contained_path(self.upload_dir, filename)

    def add_task(self, data: dict, *, mode="generate") -> dict:
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
        if not data.get("source_batch_id") and mode == "generate":
            data = self.runtime.register_manual(data, filename, content)
            filename = data["filename"]
        batch = data.get("source_batch_id")
        if "source_batch_id" in data and (not isinstance(batch, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", batch)):
            raise PhoneFactoryError("采集批次编号无效")
        if batch and filename != f"collection-batch-{batch}.xlsx":
            raise PhoneFactoryError("采集批次文件名不匹配", 409)
        if not batch and filename.lower().startswith("collection-batch-"):
            raise PhoneFactoryError("该文件名前缀仅供采集批次使用，请重命名手动上传文件", 409)
        digest = hashlib.sha256(content).hexdigest()
        destination = contained_path(self.upload_dir, filename)
        source_registration = False
        if batch and mode == "generate":
            try:
                frozen = self.collection_runs.batch(batch, require_workbook=True)
            except CollectionRunError as exc:
                # Preserve legacy input registration; dispatch still requires a
                # real frozen batch. Never bypass a closed source's write guard.
                if exc.status != 404 or not is_batch_active(batch, self.root):
                    raise
            else:
                source_registration = frozen.get("batch_role") != "collection_result"
                if frozen.get("workbook_sha256") != digest:
                    raise PhoneFactoryError("上传文件与冻结的采集批次 Excel 不一致", 409)

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
            row = {"description": description, "filename": filename, "status": "未运行",
                   "content_sha256": digest}
            for key in ("original_filename", "warnings"):
                if key in data:
                    row[key] = data[key]
            if batch:
                row.update(source_batch_id=batch, content_sha256=digest)
            tasks.append(row)

        # Registering an immutable source only updates the factory's input list;
        # it must not acquire write ownership of that source's historical result.
        lock = batch_lock(self.root, batch) if source_registration else active_batch_lock(batch, self.root)
        with lock:
            from .pipeline_access import ensure_pipeline_write
            if not source_registration:
                ensure_pipeline_write(batch, self.root, action="collect")
            state = self.runtime.public_state(self._public(self._update(mutate)), mode)
            imported = next(row for row in state["tasks"] if row["filename"] == filename)
            return {**state, "imported_task": dict(imported)}

    def start_task(self, filename) -> dict:
        filename = _filename(filename)

        def mutate(state):
            task = next((row for row in state["tasks"] if row.get("filename") == filename), None)
            if task is None:
                raise PhoneFactoryError(f"任务 {filename} 不存在", 404)
            task["status"] = "运行中"

        task = next((row for row in self._current()["tasks"] if row.get("filename") == filename), {})
        with active_batch_lock(task.get("source_batch_id"), self.root):
            from .pipeline_access import ensure_pipeline_write
            ensure_pipeline_write(task.get("source_batch_id"), self.root, action="collect")
            return self._public(self._update(mutate))

    def remove_task(self, filename) -> dict:
        filename = _filename(filename)
        task = next((row for row in self._current()["tasks"] if row.get("filename") == filename), {})
        with active_batch_lock(task.get("source_batch_id"), self.root):
            from .pipeline_access import ensure_pipeline_write
            ensure_pipeline_write(task.get("source_batch_id"), self.root, action="collect")
            return self._public(self._update(lambda state: state.update(tasks=[row for row in state["tasks"] if row.get("filename") != filename])))

    def _remote_result(self, args: list[str]):
        result = self.run_client(args)
        if not result["ok"]:
            failure = _client_failure(result.get("output") or "功能不支持或者网络断连")
            raise PhoneFactoryError(failure["output"], 502,
                remote_kind=result.get("remote_kind") or failure.get("remote_kind"),
                remote_status=result.get("remote_status", failure.get("remote_status")),
                remote_command=args[0] if args else None)
        try:
            return json.loads(result["output"])
        except (TypeError, ValueError):
            return {"ok": True, "message": result.get("output", "")}

    def remote_start(self, data: dict, *, mode="generate"):
        return self.runtime.remote_start(data, mode)

    def dispatch(self, action: str, method: str, data: dict, *, mode="generate"):
        handled, result = self.runtime.dispatch(action, method, data, mode)
        if handled:
            return result
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
            return self.state(mode)
        if method == "GET" and action == "config":
            return self.config(mode)
        if method == "POST" and action == "tasks":
            return self.add_task(data, mode=mode)
        if method == "POST" and action == "tasks/start":
            return self.start_task(data.get("filename"))
        if method == "DELETE" and action == "tasks":
            return self.remove_task(data.get("filename"))
        if method == "POST" and action == "remote/start-run":
            return self.remote_start(data, mode=mode)
        if method == "POST" and action == "remote/add-phone":
            phone_id = _text(data.get("phone_id"))
            if not phone_id:
                raise PhoneFactoryError("手机ID不能为空")
            return self._remote_result(["add-phone", phone_id])
        if method == "POST" and action == "config":
            config = {}
            for key in ("sampling_enabled", "use_experience_lib", "capture_logprobs"):
                value = data.get(key, DEFAULT_CONFIG[key])
                if not isinstance(value, bool):
                    raise PhoneFactoryError(f"{key} 必须是布尔值")
                config[key] = value
            for key in ("temperature", "top_p"):
                try:
                    value = float(data[key])
                except (KeyError, TypeError, ValueError):
                    value = float("nan")
                if not math.isfinite(value) and data.get("sampling_enabled"):
                    raise PhoneFactoryError("temperature 与 top_p 必须是数字")
                config[key] = value if math.isfinite(value) else DEFAULT_CONFIG[key]
            try:
                agent_type = normalize_agent_type(data.get("agent_type", self._current().get("agent_type", "newagent")))
                models = normalize_defaults(data["model_defaults"]) if "model_defaults" in data else None
            except ValueError as exc:
                raise PhoneFactoryError(str(exc)) from exc
            def update_config(state):
                state.update(config=config, agent_type=agent_type)
                if models is not None:
                    state.setdefault("model_defaults", {})[mode] = models
            self._update(update_config)
            return self.config(mode)
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


def create_router(store: PhoneFactoryStore | None = None, *, prefix="/api/phone-factory", mode="generate") -> APIRouter:
    service = store if store is not None else PhoneFactoryStore()
    result = APIRouter(prefix=prefix, tags=["phone-factory"])

    @result.api_route("/{action:path}", methods=["GET", "POST", "PUT", "DELETE"])
    def dispatch(action: str, request: Request, data: dict | None = Body(default=None)):
        try:
            body = dict(request.query_params) if request.method == "GET" else data or {}
            value = service.dispatch(action, request.method, body, mode=mode)
            return value if isinstance(value, Response) else JSONResponse(value, headers={"Cache-Control": "no-store"})
        except (PhoneFactoryError, CollectionRunError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=exc.status, headers={"Cache-Control": "no-store"})
        except (ValueError, OSError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=500, headers={"Cache-Control": "no-store"})

    return result


store = PhoneFactoryStore()
router = create_router(store)
model_iter_router = create_router(store, prefix="/api/model-iter", mode="modeliter")


def start_phone_factory():
    store.runtime.start()


def close_phone_factory():
    store.runtime.close()
