#!/usr/bin/env python3
"""Bounded HTTP client for the independently deployed phone collector."""
from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import uuid
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

SERVER = os.environ.get("PHONE_FACTORY_SERVER", "http://localhost:9011").rstrip("/")
TIMEOUT = float(os.environ.get("PHONE_FACTORY_TIMEOUT", "30"))
TRANSFER_TIMEOUT = float(os.environ.get("PHONE_FACTORY_TRANSFER_TIMEOUT", "300"))
MAX_DOWNLOAD = int(os.environ.get("PHONE_FACTORY_MAX_ARCHIVE_BYTES", str(10 * 1024**3)))
UNREACHABLE_MSG = "功能不支持或者网络断连"
CLIENT_ERROR_PREFIX = "__PHONE_FACTORY_ERROR__="
LEGACY_MANIFEST_MAX_BYTES = 256 * 1024


def _headers():
    token = os.environ.get("PHONE_FACTORY_TOKEN", "")
    return {"Authorization": "Bearer " + token} if token else {}


def _exit_error(message, kind, status=None, *, dispatch_not_sent=False):
    # The CLI's human-readable error stays unchanged. Only the platform parent
    # opts in to machine-readable metadata, on a separate stderr line.
    if os.environ.get("PHONE_FACTORY_CLIENT_ERROR_METADATA") == "1":
        metadata = {"remote_kind": kind}
        if status is not None:
            metadata["remote_status"] = status
        if dispatch_not_sent:
            metadata["dispatch_not_sent"] = True
        print(CLIENT_ERROR_PREFIX + json.dumps(metadata), file=sys.stderr, flush=True)
    sys.exit(message)


def _error(exc, *, dispatch_not_sent=False):
    if isinstance(exc, urllib.error.HTTPError):
        try:
            body = json.loads(exc.read(8192).decode("utf-8"))
            message = body.get("error") or body.get("message") or body.get("detail")
            if isinstance(message, dict):
                message = message.get("message")
        except Exception:
            message = None
        fallback = "请求超过采集服务接收限制" if exc.code == 413 else UNREACHABLE_MSG
        _exit_error(f"错误：{message or fallback}（HTTP {exc.code}）", "http", exc.code,
                    dispatch_not_sent=dispatch_not_sent)
    reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
    if isinstance(reason, (socket.timeout, TimeoutError)):
        _exit_error("错误：采集服务请求超时，请用同一请求编号重试", "timeout",
                    dispatch_not_sent=dispatch_not_sent)
    kind = "protocol" if isinstance(exc, ValueError) else "connection"
    if isinstance(exc, (PermissionError, FileNotFoundError, FileExistsError)):
        kind = "client"
    _exit_error(f"错误：{UNREACHABLE_MSG}（{type(exc).__name__}）", kind,
                dispatch_not_sent=dispatch_not_sent)


def _request(req, *, legacy_statuses=(), dispatch_not_sent=False, require_json=False):
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as response:
            raw = response.read(32 * 1024**2 + 1)
            if len(raw) > 32 * 1024**2:
                raise ValueError("采集服务响应过大")
            text = raw.decode("utf-8")
            return json.loads(text if require_json else text or "{}")
    except urllib.error.HTTPError as exc:
        if exc.code in legacy_statuses:
            exc.close()
            return {}
        _error(exc, dispatch_not_sent=dispatch_not_sent)
    except (OSError, ValueError) as exc:
        _error(exc, dispatch_not_sent=dispatch_not_sent)


def post_json(path, payload):
    return _request(urllib.request.Request(SERVER + path,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        method="POST", headers={**_headers(), "Content-Type": "application/json"}))


def get_json(path):
    return _request(urllib.request.Request(SERVER + path, headers=_headers()))


def cmd_quality_checklist(args):
    """Dedicated asynchronous quality evaluation; never invokes phone collection."""
    base = "/quality-checklist-jobs"
    if args.cmd == "quality-checklist-start":
        payload = json.loads(args.payload)
        if not isinstance(payload, dict):
            raise ValueError("质检请求必须为 JSON 对象")
        manifest = Path(args.manifest).read_bytes()
        if len(manifest) > 32 * 1024**2:
            raise ValueError("质检轨迹清单超过 32 MiB")
        if not isinstance(json.loads(manifest.decode("utf-8")), dict):
            raise ValueError("质检轨迹清单必须为 UTF-8 JSON 对象")
        capabilities = get_json("/capabilities")
        if not isinstance(capabilities, dict) or capabilities.get("quality_checklist_jobs", {}).get("version") != 1:
            raise ValueError("采集端不支持质检 Checklist 后台任务，请先升级采集端")
        return post_multipart(base, {"request": json.dumps(payload, ensure_ascii=False)},
                              [("manifest_file", "trajectories.json", manifest, "application/json")])
    base += "/" + urllib.parse.quote(args.job_id, safe="")
    if args.cmd == "quality-checklist-results":
        return get_json(base + "/results?" + urllib.parse.urlencode({"offset": args.offset, "limit": args.limit}))
    if args.cmd == "quality-checklist-report":
        return get_json(base + "/reports/" + urllib.parse.quote(args.report_id, safe=""))
    if args.cmd in {"quality-checklist-cancel", "quality-checklist-retry"}:
        payload = json.loads(args.payload)
        if not isinstance(payload, dict):
            raise ValueError("质检控制请求必须为 JSON 对象")
        return post_json(base + "/" + args.cmd.rsplit("-", 1)[1], payload)
    return get_json(base)


def post_multipart(path, fields, file_fields, *, max_body_bytes=None):
    boundary = "----phonefactory" + uuid.uuid4().hex
    chunks = []
    for key, value in fields.items():
        chunks.append((f"--{boundary}\r\nContent-Disposition: form-data; name=\"{key}\""
                       f"\r\n\r\n{value}\r\n").encode("utf-8"))
    for item in file_fields:
        key, filename, content = item[:3]
        content_type = item[3] if len(item) == 4 else "application/octet-stream"
        safe_name = filename.replace('"', "_").replace("\r", "_").replace("\n", "_")
        chunks.append((f"--{boundary}\r\nContent-Disposition: form-data; name=\"{key}\"; filename=\"{safe_name}\""
                       f"\r\nContent-Type: {content_type}\r\n\r\n").encode("utf-8"))
        chunks.extend((content, b"\r\n"))
    chunks.append(f"--{boundary}--\r\n".encode())
    body_bytes = sum(len(chunk) for chunk in chunks)
    if max_body_bytes is not None and body_bytes > max_body_bytes:
        _exit_error(f"错误：启动请求为 {body_bytes} 字节，超过采集服务接收上限 {max_body_bytes} 字节，未下发任务",
                    "client", dispatch_not_sent=True)
    return _request(urllib.request.Request(SERVER + path, data=b"".join(chunks), method="POST",
        headers={**_headers(), "Content-Type": f"multipart/form-data; boundary={boundary}"}))


def _manifest_upload_limits():
    """Negotiate only with the target collector; uncertain failures never downgrade."""
    capability = _request(urllib.request.Request(SERVER + "/capabilities", headers=_headers()),
                          legacy_statuses=(404, 405), dispatch_not_sent=True, require_json=True)
    if not isinstance(capability, dict):
        _exit_error("错误：采集服务能力响应必须为 JSON 对象，未下发任务", "protocol", dispatch_not_sent=True)
    if "task_manifest_file_version" not in capability:
        return None
    version = capability["task_manifest_file_version"]
    limits = capability.get("start_run_limits")
    if (type(version) is not int or version != 1 or not isinstance(limits, dict)
            or any(type(limits.get(key)) is not int or limits[key] <= 0
                   for key in ("task_manifest_bytes", "request_bytes"))):
        _exit_error("错误：采集服务任务清单上传能力或大小限制声明无效，请检查采集端，未下发任务",
                    "protocol", dispatch_not_sent=True)
    return limits


def download_file(path, destination):
    output = Path(destination)
    temporary = output.with_name(output.name + "." + uuid.uuid4().hex + ".part")
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
        try:
            response = urllib.request.urlopen(urllib.request.Request(SERVER + path, headers=_headers()),
                                              timeout=TRANSFER_TIMEOUT)
        except OSError as exc:
            _error(exc)
        with response, temporary.open("xb") as stream:
            total = 0
            while True:
                try:
                    chunk = response.read(1024 * 1024)
                except OSError as exc:
                    _error(exc)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_DOWNLOAD:
                    raise ValueError("采集归档超过配置的下载上限")
                stream.write(chunk)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(output)
        return {"ok": True, "local_file": str(output), "size": total}
    except OSError as exc:
        _exit_error(f"错误：平台存储不可写，回传保留为待恢复（{type(exc).__name__}）", "storage")
    except ValueError as exc:
        _error(exc)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass  # Preserve the original storage failure, including its category.


def cmd_add_phone(phoneid):
    return post_json("/add_phone", {"phoneid": phoneid})


def cmd_start_run(args):
    task, apps = Path(args.task), Path(args.apps)
    if not task.is_file() or not apps.is_file():
        sys.exit("错误：任务文件或手机关联文件不存在")
    fields = {"phone_id": args.phone or "", "app": args.app_name or "",
        "sampling_enabled": str(args.sampling).lower(), "temperature": str(args.temperature),
        "top_p": str(args.top_p), "use_experience_lib": str(args.exp).lower(),
        "capture_logprobs": str(getattr(args, "capture_logprobs", False)).lower(),
        "vla": getattr(args, "vla", "") or "", "runmode": getattr(args, "runmode", "generate"),
        "agent_type": getattr(args, "agent_type", "newagent")}
    for key in ("execution_model", "planner_config"):
        if getattr(args, key, None):
            fields[key] = getattr(args, key)
    for key in ("repeat_times", "collection_artifacts_version"):
        if getattr(args, key, None) is not None:
            fields[key] = str(getattr(args, key))
    evaluator_config = getattr(args, "evaluator_config", None)
    if fields["runmode"] == "modeliter" and evaluator_config:
        fields.update(evaluator_config=evaluator_config, checklist_source=args.checklist_source,
                      evaluation_mode=args.evaluation_mode)
        for key in ("evaluation_profile_id", "evaluation_profile_digest"):
            if getattr(args, key, None):
                fields[key] = getattr(args, key)
    batch = getattr(args, "batch_id", None)
    run = getattr(args, "collection_run_id", None)
    output = getattr(args, "output_dir", None)
    if fields["runmode"] == "generate" and any((batch, run, output)) and not all((batch, run, output)):
        sys.exit("错误：batch_id、collection_run_id 和 output_dir 必须一起提供")
    if batch:
        fields["batch_id"] = batch
    if run:
        fields["collection_run_id"] = run
    if output:
        fields["output_dir"] = output
    manifest = getattr(args, "task_manifest", None)
    files = [("task_file", task.name, task.read_bytes()), ("apps_file", apps.name, apps.read_bytes())]
    if manifest:
        try:
            # Preserve the previous text-file newline handling and exact logical
            # contents. Encoding, not JSON reserialization, changes the transport.
            text = Path(manifest).read_text(encoding="utf-8")
            content = text.encode("utf-8")
        except (OSError, UnicodeError) as exc:
            _exit_error(f"错误：无法读取 UTF-8 任务清单（{type(exc).__name__}），未下发任务",
                        "client", dispatch_not_sent=True)
        limits = _manifest_upload_limits()
        if limits is None:
            if len(content) > LEGACY_MANIFEST_MAX_BYTES:
                _exit_error(f"错误：采集服务尚不支持任务清单文件上传；当前清单为 {len(content)} 字节，"
                            f"超过旧文本兼容上限 {LEGACY_MANIFEST_MAX_BYTES} 字节，请升级采集端后使用同一请求编号重试，未下发任务",
                            "client", dispatch_not_sent=True)
            fields["task_manifest"] = text
        else:
            if len(content) > limits["task_manifest_bytes"]:
                _exit_error(f"错误：任务清单文件为 {len(content)} 字节，超过采集服务清单上限 "
                            f"{limits['task_manifest_bytes']} 字节，未下发任务",
                            "client", dispatch_not_sent=True)
            files.append(("task_manifest_file", "tasks.json", content, "application/json"))
            return post_multipart("/start_run", fields, files, max_body_bytes=limits["request_bytes"])
    return post_multipart("/start_run", fields, files)


def build_parser():
    parser = argparse.ArgumentParser(description="独立手机工厂服务客户端")
    sub = parser.add_subparsers(dest="cmd", required=True)
    for command in ("quality-checklist-start", "quality-checklist-status", "quality-checklist-results",
                    "quality-checklist-cancel", "quality-checklist-retry", "quality-checklist-report"):
        item = sub.add_parser(command)
        item.add_argument("--server")
        if command == "quality-checklist-start":
            item.add_argument("--payload", required=True)
            item.add_argument("--manifest", required=True)
        else:
            item.add_argument("job_id")
        if command == "quality-checklist-results":
            item.add_argument("--offset", type=int, default=0)
            item.add_argument("--limit", type=int, default=50)
        if command in {"quality-checklist-cancel", "quality-checklist-retry"}:
            item.add_argument("--payload", default="{}")
        if command == "quality-checklist-report":
            item.add_argument("report_id")
    for name in ("add-phone", "del-phone", "monitor"):
        sub.add_parser(name).add_argument("phoneid")
    run = sub.add_parser("start-run")
    run.add_argument("task")
    run.add_argument("apps")
    run.add_argument("phone", nargs="?", default="")
    run.add_argument("app_name", nargs="?", default="")
    run.add_argument("--sampling", action="store_true")
    run.add_argument("--capture-logprobs", action="store_true")
    run.add_argument("--temperature", type=float, default=0.7)
    run.add_argument("--top-p", type=float, default=0.85)
    run.add_argument("--exp", action="store_true")
    run.add_argument("--agent-type", choices=("newagent", "pipelineagent"), default="newagent")
    run.add_argument("--vla", default="")
    run.add_argument("--execution-model", default="")
    run.add_argument("--planner-config")
    run.add_argument("--repeat-times", type=int)
    run.add_argument("--collection-artifacts-version", type=int, choices=[1, 2])
    run.add_argument("--runmode", choices=["generate", "modeliter"], default="generate")
    run.add_argument("--batch-id")
    run.add_argument("--collection-run-id")
    run.add_argument("--output-dir")
    run.add_argument("--task-manifest")
    run.add_argument("--evaluator-config")
    run.add_argument("--checklist-source", choices=["llm", "auto"], default="llm")
    run.add_argument("--evaluation-mode", choices=["checklist"], default="checklist")
    run.add_argument("--evaluation-profile-id")
    run.add_argument("--evaluation-profile-digest")
    sub.add_parser("status").add_argument("phones")
    sub.add_parser("adb-devices")
    sub.add_parser("capabilities").add_argument("--server")
    sub.add_parser("evaluation-profiles")
    sub.add_parser("evaluation-sources")
    for command in ("storage-scan", "storage-cleanup", "storage-archive"):
        item = sub.add_parser(command)
        item.add_argument("run_id")
        item.add_argument("--payload", required=True)
        item.add_argument("--server")
    storage = sub.add_parser("storage-operation")
    storage.add_argument("operation_id")
    storage.add_argument("--server")
    storage = sub.add_parser("storage-archive-download")
    storage.add_argument("run_id")
    storage.add_argument("--archive-kind", choices=["collection", "run"], required=True)
    storage.add_argument("--out", required=True)
    storage.add_argument("--server")
    for command in ("evaluation-preview", "start-evaluation"):
        item = sub.add_parser(command)
        item.add_argument("--payload", required=True)
        item.add_argument("--server")
    preview = sub.add_parser("evaluation-preview-status")
    preview.add_argument("preview_id")
    preview.add_argument("--offset", type=int, default=0)
    preview.add_argument("--limit", type=int, default=50)
    preview.add_argument("--server")
    for command in ("evaluation-preview-cancel", "evaluation-preview-retry", "evaluation-preparation-retry"):
        item = sub.add_parser(command)
        item.add_argument("identifier")
        item.add_argument("--payload", required=True)
        item.add_argument("--server")
    for command in ("run", "run-evaluations", "cancel-run"):
        item = sub.add_parser(command)
        item.add_argument("run_id")
        item.add_argument("--server")
    for command in ("run-diagnostics", "collection-executions", "resume-preview", "resume-collection", "executions-cancel",
                    "resume-operation", "collection-snapshot"):
        item = sub.add_parser(command)
        item.add_argument("run_id")
        if command in {"resume-preview", "resume-collection", "executions-cancel"}:
            item.add_argument("--payload", required=True)
        elif command in {"resume-operation", "collection-snapshot"}:
            item.add_argument("identifier")
    for command in ("phone-control-capabilities", "phone-ownership", "phone-reconcile", "phone-stop", "phone-control-operation"):
        item = sub.add_parser(command)
        item.add_argument("--server")
        if command in {"phone-reconcile", "phone-stop"}:
            item.add_argument("run_id")
            item.add_argument("phone_id")
            item.add_argument("--payload", required=True)
        elif command == "phone-ownership":
            item.add_argument("phone_id")
        elif command == "phone-control-operation":
            item.add_argument("request_id")
    corrections = sub.add_parser("result-corrections")
    corrections.add_argument("run_id")
    corrections.add_argument("--server")
    corrections.add_argument("--cursor", type=int, default=0)
    corrections.add_argument("--limit", type=int, default=100)
    sub.add_parser("retry-collection-artifacts").add_argument("run_id")
    pressure = sub.add_parser("receipt-storage")
    pressure.add_argument("run_id")
    pressure.add_argument("--payload", required=True)
    archive = sub.add_parser("run-archive")
    archive.add_argument("run_id")
    archive.add_argument("--out", required=True)
    for command in ("collection-artifacts", "collection-artifact", "collection-artifact-archive",
                    "collection-manifest", "collection-archive"):
        item = sub.add_parser(command)
        item.add_argument("run_id")
        if command == "collection-artifacts":
            item.add_argument("--cursor", type=int, default=0)
            item.add_argument("--limit", type=int, default=100)
        if command in {"collection-artifact", "collection-artifact-archive"}:
            item.add_argument("artifact_id")
        if command.endswith("archive"):
            item.add_argument("--out", required=True)
    reports = sub.add_parser("reports")
    reports.add_argument("--runmode", choices=["generate", "modeliter"], default="modeliter")
    download = sub.add_parser("report-download")
    download.add_argument("--runmode", choices=["generate", "modeliter"], default="modeliter")
    for key in ("name", "folder", "run-id", "file-id"):
        download.add_argument("--" + key, default="")
    download.add_argument("--out", required=True)
    return parser


def main():
    global SERVER
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    args = build_parser().parse_args()
    if getattr(args, "server", None):
        parsed = urllib.parse.urlsplit(args.server)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
            sys.exit("错误：采集服务地址无效")
        SERVER = args.server.rstrip("/")
    if args.cmd.startswith("quality-checklist-"):
        try:
            result = cmd_quality_checklist(args)
        except (OSError, ValueError) as exc:
            sys.exit("错误：" + str(exc))
    elif args.cmd in {"storage-scan", "storage-cleanup", "storage-archive"}:
        payload = json.loads(args.payload)
        if not isinstance(payload, dict):
            sys.exit("错误：存储请求必须为 JSON 对象")
        suffix = {"storage-scan": "storage-scan", "storage-cleanup": "cleanup", "storage-archive": "archive"}[args.cmd]
        result = post_json("/runs/" + urllib.parse.quote(args.run_id, safe="") + "/" + suffix, payload)
    elif args.cmd == "storage-operation":
        result = get_json("/storage-operations/" + urllib.parse.quote(args.operation_id, safe=""))
    elif args.cmd == "storage-archive-download":
        suffix = "collection-archive" if args.archive_kind == "collection" else "archive"
        result = download_file("/runs/" + urllib.parse.quote(args.run_id, safe="") + "/" + suffix, args.out)
    elif args.cmd == "phone-control-capabilities":
        result = get_json("/capabilities")
    elif args.cmd == "phone-ownership":
        result = get_json("/phones/" + urllib.parse.quote(args.phone_id, safe="") + "/ownership")
    elif args.cmd == "phone-control-operation":
        result = get_json("/phone-control-operations/" + urllib.parse.quote(args.request_id, safe=""))
    elif args.cmd in {"phone-reconcile", "phone-stop"}:
        payload = json.loads(args.payload)
        if not isinstance(payload, dict):
            sys.exit("错误：手机控制请求必须为 JSON 对象")
        result = post_json("/runs/" + urllib.parse.quote(args.run_id, safe="") + "/phones/" +
            urllib.parse.quote(args.phone_id, safe="") + "/" + args.cmd.removeprefix("phone-"), payload)
    elif args.cmd == "start-run":
        result = cmd_start_run(args)
    elif args.cmd in {"add-phone", "del-phone"}:
        result = post_json("/" + args.cmd.replace("-", "_"), {"phoneid": args.phoneid})
    elif args.cmd == "monitor":
        result = post_json("/monitor", {"phone_id": args.phoneid})
    elif args.cmd == "status":
        phones = json.loads(args.phones)
        if not isinstance(phones, list):
            sys.exit("错误：phones 必须为数组")
        result = post_json("/status", {"phones": phones})
    elif args.cmd == "adb-devices":
        result = post_json("/adb_devices", {})
    elif args.cmd == "capabilities":
        result = get_json("/capabilities")
    elif args.cmd in {"run-diagnostics", "collection-executions", "resume-preview", "resume-collection", "executions-cancel",
                      "resume-operation", "collection-snapshot"}:
        suffix = {"run-diagnostics": "diagnostics", "collection-executions": "executions",
                  "resume-preview": "resume-preview", "resume-collection": "resume",
                  "resume-operation": "resume-operations", "collection-snapshot": "collection-snapshots",
                  "executions-cancel":"executions/cancel"}[args.cmd]
        path = "/runs/" + urllib.parse.quote(args.run_id, safe="") + "/" + suffix
        if args.cmd in {"resume-operation", "collection-snapshot"}:
            path += "/" + urllib.parse.quote(args.identifier, safe="")
        if args.cmd in {"resume-preview", "resume-collection", "executions-cancel"}:
            payload = json.loads(args.payload)
            if not isinstance(payload, dict):
                sys.exit("错误：续采请求必须为 JSON 对象")
            result = post_json(path, payload)
        else:
            result = get_json(path)
    elif args.cmd == "result-corrections":
        result = get_json("/runs/" + urllib.parse.quote(args.run_id, safe="") + "/result-corrections?" + urllib.parse.urlencode({"cursor": args.cursor, "limit": args.limit}))
    elif args.cmd == "retry-collection-artifacts":
        result = post_json("/runs/" + urllib.parse.quote(args.run_id, safe="") + "/collection-artifacts/retry", {})
    elif args.cmd == "receipt-storage":
        payload = json.loads(args.payload)
        if not isinstance(payload, dict) or type(payload.get("paused")) is not bool:
            sys.exit("错误：存储背压请求必须包含 paused 布尔值")
        result = post_json("/runs/" + urllib.parse.quote(args.run_id, safe="") + "/receipt-storage", payload)
    elif args.cmd == "evaluation-preview-status":
        result = get_json("/evaluation_previews/" + urllib.parse.quote(args.preview_id, safe="") + "?" +
                          urllib.parse.urlencode({"offset": args.offset, "limit": args.limit}))
    elif args.cmd in {"evaluation-profiles", "evaluation-sources"}:
        result = get_json("/" + args.cmd.replace("-", "_"))
    elif args.cmd in {"evaluation-preview", "start-evaluation", "evaluation-preview-cancel", "evaluation-preview-retry", "evaluation-preparation-retry"}:
        try:
            payload = json.loads(args.payload)
        except (ValueError, TypeError):
            sys.exit("错误：评测请求必须为 JSON 对象")
        if not isinstance(payload, dict):
            sys.exit("错误：评测请求必须为 JSON 对象")
        if args.cmd == "evaluation-preparation-retry":
            path = "/runs/" + urllib.parse.quote(args.identifier, safe="") + "/evaluation-preparation/retry"
        elif args.cmd in {"evaluation-preview-cancel", "evaluation-preview-retry"}:
            path = "/evaluation_previews/" + urllib.parse.quote(args.identifier, safe="") + "/" + args.cmd.rsplit("-", 1)[1]
        else:
            path = "/" + args.cmd.replace("-", "_")
        result = post_json(path, payload)
    elif args.cmd == "run":
        result = get_json("/runs/" + urllib.parse.quote(args.run_id, safe=""))
    elif args.cmd == "run-evaluations":
        result = get_json("/runs/" + urllib.parse.quote(args.run_id, safe="") + "/evaluations")
    elif args.cmd == "cancel-run":
        result = post_json("/runs/" + urllib.parse.quote(args.run_id, safe="") + "/cancel", {})
    elif args.cmd == "run-archive":
        result = download_file("/runs/" + urllib.parse.quote(args.run_id, safe="") + "/archive", args.out)
    elif args.cmd.startswith("collection-"):
        base = "/runs/" + urllib.parse.quote(args.run_id, safe="") + "/"
        if args.cmd == "collection-artifacts":
            result = get_json(base + "collection-artifacts?" + urllib.parse.urlencode(
                {"cursor": args.cursor, "limit": args.limit}))
        elif args.cmd in {"collection-artifact", "collection-artifact-archive"}:
            path = base + "collection-artifacts/" + urllib.parse.quote(args.artifact_id, safe="")
            result = download_file(path + "/archive", args.out) if args.cmd.endswith("archive") else get_json(path)
        else:
            result = download_file(base + args.cmd, args.out) if args.cmd.endswith("archive") else get_json(base + args.cmd)
    elif args.cmd == "reports":
        result = get_json("/reports?" + urllib.parse.urlencode({"runmode": args.runmode}))
    else:
        query = {"runmode": args.runmode}
        for key in ("name", "folder", "run_id", "file_id"):
            if getattr(args, key):
                query[key] = getattr(args, key)
        result = download_file("/report_download?" + urllib.parse.urlencode(query), args.out)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
