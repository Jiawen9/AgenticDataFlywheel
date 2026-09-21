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


def _headers():
    token = os.environ.get("PHONE_FACTORY_TOKEN", "")
    return {"Authorization": "Bearer " + token} if token else {}


def _error(exc):
    if isinstance(exc, urllib.error.HTTPError):
        try:
            body = json.loads(exc.read(8192).decode("utf-8"))
            message = body.get("error") or body.get("message") or body.get("detail")
            if isinstance(message, dict):
                message = message.get("message")
        except Exception:
            message = None
        sys.exit(f"错误：{message or UNREACHABLE_MSG}（HTTP {exc.code}）")
    if isinstance(exc, (socket.timeout, TimeoutError)):
        sys.exit("错误：采集服务请求超时，请用同一请求编号重试")
    sys.exit(f"错误：{UNREACHABLE_MSG}（{type(exc).__name__}）")


def _request(req):
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as response:
            raw = response.read(32 * 1024**2 + 1)
            if len(raw) > 32 * 1024**2:
                raise ValueError("采集服务响应过大")
            return json.loads(raw.decode("utf-8") or "{}")
    except (OSError, ValueError) as exc:
        _error(exc)


def post_json(path, payload):
    return _request(urllib.request.Request(SERVER + path,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        method="POST", headers={**_headers(), "Content-Type": "application/json"}))


def get_json(path):
    return _request(urllib.request.Request(SERVER + path, headers=_headers()))


def post_multipart(path, fields, file_fields):
    boundary = "----phonefactory" + uuid.uuid4().hex
    chunks = []
    for key, value in fields.items():
        chunks.append((f"--{boundary}\r\nContent-Disposition: form-data; name=\"{key}\""
                       f"\r\n\r\n{value}\r\n").encode("utf-8"))
    for key, filename, content in file_fields:
        safe_name = filename.replace('"', "_").replace("\r", "_").replace("\n", "_")
        chunks.append((f"--{boundary}\r\nContent-Disposition: form-data; name=\"{key}\"; filename=\"{safe_name}\""
                       "\r\nContent-Type: application/octet-stream\r\n\r\n").encode("utf-8"))
        chunks.extend((content, b"\r\n"))
    chunks.append(f"--{boundary}--\r\n".encode())
    return _request(urllib.request.Request(SERVER + path, data=b"".join(chunks), method="POST",
        headers={**_headers(), "Content-Type": f"multipart/form-data; boundary={boundary}"}))


def download_file(path, destination):
    output = Path(destination)
    temporary = output.with_name(output.name + "." + uuid.uuid4().hex + ".part")
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(urllib.request.Request(SERVER + path, headers=_headers()),
                                    timeout=TRANSFER_TIMEOUT) as response, temporary.open("xb") as stream:
            total = 0
            while chunk := response.read(1024 * 1024):
                total += len(chunk)
                if total > MAX_DOWNLOAD:
                    raise ValueError("采集归档超过配置的下载上限")
                stream.write(chunk)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(output)
        return {"ok": True, "local_file": str(output), "size": total}
    except (OSError, ValueError) as exc:
        _error(exc)
    finally:
        temporary.unlink(missing_ok=True)


def cmd_add_phone(phoneid):
    return post_json("/add_phone", {"phoneid": phoneid})


def cmd_start_run(args):
    task, apps = Path(args.task), Path(args.apps)
    if not task.is_file() or not apps.is_file():
        sys.exit("错误：任务文件或手机关联文件不存在")
    fields = {"phone_id": args.phone or "", "app": args.app_name or "",
        "sampling_enabled": str(args.sampling).lower(), "temperature": str(args.temperature),
        "top_p": str(args.top_p), "use_experience_lib": str(args.exp).lower(),
        "vla": getattr(args, "vla", "") or "", "runmode": getattr(args, "runmode", "generate")}
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
    if manifest:
        fields["task_manifest"] = Path(manifest).read_text(encoding="utf-8")
    return post_multipart("/start_run", fields, [
        ("task_file", task.name, task.read_bytes()), ("apps_file", apps.name, apps.read_bytes())])


def build_parser():
    parser = argparse.ArgumentParser(description="独立手机工厂服务客户端")
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("add-phone", "del-phone", "monitor"):
        sub.add_parser(name).add_argument("phoneid")
    run = sub.add_parser("start-run")
    run.add_argument("task")
    run.add_argument("apps")
    run.add_argument("phone", nargs="?", default="")
    run.add_argument("app_name", nargs="?", default="")
    run.add_argument("--sampling", action="store_true")
    run.add_argument("--temperature", type=float, default=0.7)
    run.add_argument("--top-p", type=float, default=0.85)
    run.add_argument("--exp", action="store_true")
    run.add_argument("--vla", default="")
    run.add_argument("--runmode", choices=["generate", "modeliter"], default="generate")
    run.add_argument("--batch-id")
    run.add_argument("--collection-run-id")
    run.add_argument("--output-dir")
    run.add_argument("--task-manifest")
    sub.add_parser("status").add_argument("phones")
    sub.add_parser("adb-devices")
    sub.add_parser("capabilities")
    sub.add_parser("run").add_argument("run_id")
    archive = sub.add_parser("run-archive")
    archive.add_argument("run_id")
    archive.add_argument("--out", required=True)
    reports = sub.add_parser("reports")
    reports.add_argument("--runmode", choices=["generate", "modeliter"], default="modeliter")
    download = sub.add_parser("report-download")
    download.add_argument("--runmode", choices=["generate", "modeliter"], default="modeliter")
    for key in ("name", "folder", "run-id", "file-id"):
        download.add_argument("--" + key, default="")
    download.add_argument("--out", required=True)
    return parser


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    args = build_parser().parse_args()
    if args.cmd == "start-run":
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
    elif args.cmd == "run":
        result = get_json("/runs/" + urllib.parse.quote(args.run_id, safe=""))
    elif args.cmd == "run-archive":
        result = download_file("/runs/" + urllib.parse.quote(args.run_id, safe="") + "/archive", args.out)
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
