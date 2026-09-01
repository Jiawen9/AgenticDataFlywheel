#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
手机工厂采集 - client 端

向 server 端(phone_factory/phonefactory_manager.py, 默认 http://localhost:9011) 发送请求：

  1) add-phone <phoneid>
     新增手机：向 server 发送 json  {"phoneid": "<phoneid>"}

  2) start-run <task.xlsx> <phone_apps.json> [<phone_id> <app>] [--sampling] [--temperature X] [--top-p Y] [--exp]
     轨迹生产开始运行：把 任务文件 与 手机ID/运行APP 关联文件 发送到 server 端，
     由 server 解析过滤并下发到各手机。

调试阶段单机运行使用 localhost；生产环境用环境变量 PHONE_FACTORY_SERVER 填写真实 IP。
延迟 10 秒无响应，提示「功能不支持或者网络断连」。
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import uuid
import urllib.error
import urllib.request
from pathlib import Path

SERVER = os.environ.get("PHONE_FACTORY_SERVER", "http://localhost:9011")
TIMEOUT = 10
UNREACHABLE_MSG = "功能不支持或者网络断连"


def _request(req: urllib.request.Request) -> dict:
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            raw = resp.read().decode("utf-8") or "{}"
            return json.loads(raw)
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = json.loads(exc.read().decode("utf-8") or "{}").get("error", "")
        except Exception:  # noqa: BLE001
            pass
        if detail:
            sys.exit(f"错误：{detail}")
        sys.exit(f"错误：{UNREACHABLE_MSG}（HTTP {exc.code}）")
    except socket.timeout:
        sys.exit(f"错误：{UNREACHABLE_MSG}（请求超时 {TIMEOUT} 秒）")
    except urllib.error.URLError as exc:
        sys.exit(f"错误：{UNREACHABLE_MSG}（{exc.reason}）")
    except Exception as exc:  # noqa: BLE001
        sys.exit(f"错误：{UNREACHABLE_MSG}（{exc}）")


def post_json(path: str, payload: dict) -> dict:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        SERVER + path,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    return _request(req)


def post_multipart(path: str, fields: dict, file_fields: list) -> dict:
    boundary = "----phonefactory" + uuid.uuid4().hex
    chunks: list[bytes] = []
    for key, value in fields.items():
        chunks.append(
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{key}"\r\n\r\n'
                f"{value}\r\n"
            ).encode("utf-8")
        )
    for key, filename, content in file_fields:
        chunks.append(
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{key}"; filename="{filename}"\r\n'
                f"Content-Type: application/octet-stream\r\n\r\n"
            ).encode("utf-8")
        )
        chunks.append(content)
        chunks.append(b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode("utf-8"))
    req = urllib.request.Request(
        SERVER + path,
        data=b"".join(chunks),
        method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    return _request(req)


def cmd_add_phone(phoneid: str) -> dict:
    return post_json("/add_phone", {"phoneid": phoneid})


def cmd_start_run(args: argparse.Namespace) -> dict:
    task_path = Path(args.task)
    apps_path = Path(args.apps)
    if not task_path.is_file():
        sys.exit(f"错误：任务文件不存在 {task_path}")
    if not apps_path.is_file():
        sys.exit(f"错误：关联文件不存在 {apps_path}")
    fields = {
        "phone_id": args.phone or "",
        "app": args.app_name or "",
        "sampling_enabled": "true" if args.sampling else "false",
        "temperature": str(args.temperature),
        "top_p": str(args.top_p),
        "use_experience_lib": "true" if args.exp else "false",
    }
    file_fields = [
        ("task_file", task_path.name, task_path.read_bytes()),
        ("apps_file", apps_path.name, apps_path.read_bytes()),
    ]
    return post_multipart("/start_run", fields, file_fields)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="手机工厂采集 client")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p1 = sub.add_parser("add-phone", help="向 server 登记手机")
    p1.add_argument("phoneid", help="手机ID，如 3B65AB01LBl00000")

    p2 = sub.add_parser("start-run", help="发送任务文件与手机/APP关联文件到 server")
    p2.add_argument("task", help="任务文件路径（xlsx）")
    p2.add_argument("apps", help="手机ID/运行APP 关联文件路径（json）")
    p2.add_argument("phone", nargs="?", default="", help="定制运行的手机ID（可选）")
    p2.add_argument("app_name", nargs="?", default="", help="定制运行的APP（可选）")
    p2.add_argument("--sampling", action="store_true", default=False, help="是否打开采样")
    p2.add_argument("--temperature", type=float, default=0.7, help="temperature（默认 0.7）")
    p2.add_argument("--top-p", dest="top_p", type=float, default=0.85, help="top_p（默认 0.85）")
    p2.add_argument("--exp", action="store_true", default=False, help="是否使用经验库")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if args.cmd == "add-phone":
        result = cmd_add_phone(args.phoneid)
    else:
        result = cmd_start_run(args)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
