"""Configurable ADB and isolated template execution, without embedded service credentials."""
from __future__ import annotations

import base64
import json
import os
import shutil
import signal
import struct
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from .core import CollectorError, atomic_json, boundary, identifier, phone_identifier, token


class AdbAdapter:
    def __init__(self, config):
        self.config = config

    def command(self, args, *, text=True):
        try:
            result = subprocess.run([self.config.adb_path, *args], capture_output=True, text=text,
                                    timeout=self.config.adb_timeout)
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
            raise CollectorError(f"ADB 调用失败：{exc}", 503) from exc
        if result.returncode:
            raise CollectorError("ADB 调用失败：" + (result.stderr if text else result.stderr.decode(errors="replace")), 503)
        return result.stdout

    def devices(self):
        lines = self.command(["devices"]).splitlines()
        devices = []
        for line in lines:
            fields = line.split()
            if len(fields) < 2 or fields[1] != "device":
                continue
            serial = phone_identifier(fields[0])
            model, battery = "", None
            for prop in ("ro.product.marketname", "ro.product.model"):
                try:
                    model = self.command(["-s", serial, "shell", "getprop", prop]).strip()
                except CollectorError:
                    pass
                if model:
                    break
            try:
                battery_text = self.command(["-s", serial, "shell", "dumpsys", "battery"])
                for item in battery_text.splitlines():
                    if item.strip().startswith("level:"):
                        battery = int(item.split(":", 1)[1].strip())
                        break
            except (ValueError, CollectorError):
                pass
            devices.append({"serial": serial, "model": model, "battery": battery})
        return devices

    def monitor(self, phone_id, workdir):
        phone_identifier(phone_id)
        screenshot, size = None, None
        try:
            raw = self.command(["-s", phone_id, "exec-out", "screencap", "-p"], text=False)
            if raw[:8] == b"\x89PNG\r\n\x1a\n" and len(raw) >= 24:
                width, height = struct.unpack(">II", raw[16:24])
                size = {"width": width, "height": height}
                screenshot = base64.b64encode(raw).decode()
        except CollectorError:
            pass
        candidates = list((workdir / ".runs").rglob("gui_agent.log")) if (workdir / ".runs").exists() else []
        path = max(candidates, key=lambda p: p.stat().st_mtime) if candidates else workdir / "onephonerun.log"
        log = ""
        if path.is_file():
            boundary(workdir, path)
            with path.open("rb") as f:
                f.seek(max(0, path.stat().st_size - 256 * 1024))
                content = f.read()
            for encoding in ("utf-8", "gb18030"):
                try:
                    log = content.decode(encoding)
                    break
                except UnicodeError:
                    pass
            if not log:
                log = content.decode("utf-8", errors="replace")
            log = "\n".join(log.splitlines()[-500:])
        return {"screenshot": screenshot, "device_size": size, "log": log}


def process_command(pid):
    if os.name == "nt":
        # Avoid launching PowerShell for every finished historical run.
        import ctypes
        kernel = ctypes.windll.kernel32
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel.OpenProcess(0x1000, False, int(pid))
        if not handle:
            if kernel.GetLastError() == 87:  # ERROR_INVALID_PARAMETER: PID no longer exists
                return ""
            raise CollectorError("无法核验采集进程身份", 503)
        kernel.CloseHandle(handle)
        result = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                                 f"(Get-CimInstance Win32_Process -Filter 'ProcessId={int(pid)}').CommandLine"],
                                capture_output=True, text=True, timeout=10)
        if result.returncode:
            raise CollectorError("无法核验采集进程身份", 503)
        return result.stdout.strip()
    try:
        return Path(f"/proc/{int(pid)}/cmdline").read_bytes().replace(b"\x00", b" ").decode(errors="replace")
    except FileNotFoundError:
        return ""


def owned_process(marker):
    pid = int(marker["pid"])
    command = process_command(pid)
    return str(marker["config_path"]) in command and "collector_service.worker" in command


def terminate_owned(marker):
    if not owned_process(marker):
        return
    pid = int(marker["pid"])
    if os.name == "nt":
        result = subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, timeout=20)
        if result.returncode and owned_process(marker):
            raise CollectorError("终止采集进程失败", 503)
    else:
        os.killpg(pid, signal.SIGTERM)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if not owned_process(marker):
                return
            time.sleep(.05)
        if owned_process(marker):
            os.killpg(pid, signal.SIGKILL)


class TemplateExecutor:
    def __init__(self, config):
        self.config = config
        # Single ASGI worker, with many device execution threads. Only launch,
        # PID acknowledgement and cancellation share this short critical section.
        self._launch_lock = threading.RLock()

    def owned_runs(self, phone_id):
        """Find still-owned workers even when their DB run has already failed."""
        phone_identifier(phone_id)
        root = Path(self.config.root).resolve()
        result = []
        try:
            for marker_path in (root / "runs").glob(f"*/execution/{token(phone_id)}/process.json"):
                boundary(root, marker_path)
                run_id = identifier(marker_path.parent.parent.parent.name)
                marker = json.loads(marker_path.read_text(encoding="utf-8"))
                expected_config = marker_path.parent / "worker-config.json"
                if (type(marker.get("pid")) is not int or marker["pid"] <= 0
                        or Path(marker.get("config_path", "")).resolve() != expected_config.resolve()):
                    raise CollectorError("采集进程所有权记录无效", 503)
                if owned_process(marker):
                    result.append(run_id)
        except CollectorError:
            raise
        except (OSError, ValueError, TypeError, subprocess.SubprocessError) as exc:
            raise CollectorError(f"无法核验设备所属采集进程：{exc}", 503) from exc
        return result

    def preflight(self, params):
        template = self.config.template_dir
        if template is None or not template.is_dir():
            raise CollectorError("执行模板未配置：请设置 PHONE_FACTORY_TEMPLATE_DIR", 503)
        for name in ("main.py", "config_trajectory.py", "config_report.py", "run_evaluator_batch.py"):
            if not (template / name).is_file():
                raise CollectorError(f"执行模板缺少 {name}", 503)
        parsed = urlsplit(params["vla"] if "://" in params["vla"] else "http://" + params["vla"])
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise CollectorError("VLA 地址必须为 HTTP(S) 服务地址")

    def prepare_device(self, path):
        if self.config.template_dir is None or not self.config.template_dir.is_dir():
            raise CollectorError("执行模板未配置：请设置 PHONE_FACTORY_TEMPLATE_DIR", 503)
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(self.config.template_dir, path,
                        ignore=shutil.ignore_patterns(".runs", ".git", "__pycache__", "pid.txt", "test.xlsx",
                                                     "onephonerun.log", "evaluator_results_*.xlsx"))

    def execute(self, run, workdirs, output, cancel):
        def one(phone, workdir):
            with self._launch_lock:
                if cancel.is_set() or (output / "cancelled.json").exists():
                    return {"trajectories": [], "reports": [], "errors": [{"phone_id": phone, "error": "运行已取消，未启动执行器"}]}
                device_output = output / token(phone)
                device_output.mkdir(exist_ok=True)
                config_path = device_output / "worker-config.json"
                marker_path = device_output / "process.json"
                completion = device_output / "completion.json"
                launch_journal = device_output / "launch.json"
                if completion.exists():
                    return json.loads(completion.read_text(encoding="utf-8"))
                if launch_journal.exists() and not marker_path.exists():
                    # A crash between spawn and acknowledgement must never launch the same phone twice.
                    deadline = time.monotonic() + 3
                    while time.monotonic() < deadline and not marker_path.exists() and not completion.exists():
                        if cancel.wait(.1):
                            break
                    if completion.exists():
                        return json.loads(completion.read_text(encoding="utf-8"))
                    if not marker_path.exists():
                        atomic_json(device_output / "cancelled.json", {"reason": "launch acknowledgement interrupted"})
                        return {"trajectories": [], "reports": [], "errors": [{"phone_id": phone, "error": "执行器启动确认中断；未重复下发，请检查设备日志后创建新运行"}]}
                if marker_path.exists():
                    marker = json.loads(marker_path.read_text(encoding="utf-8"))
                    if not owned_process(marker):
                        # PID disappearance is an interruption, never successful collection.
                        return {"trajectories": [], "reports": [], "errors": [{"phone_id": phone, "error": "采集执行器中断，未生成完成记录"}]}
                else:
                    params = dict(run["params"])
                    parsed = urlsplit(params["vla"] if "://" in params["vla"] else "http://" + params["vla"])
                    path = parsed.path.rstrip("/")
                    params["vla"] = urlunsplit((parsed.scheme, parsed.netloc, path if path.endswith("/v1") else path + "/v1", "", "")) + "/"
                    config = {"phone_id": phone, "workdir": str(workdir), "output": str(device_output),
                              "task_workbook": str(output.parent / "tasks.xlsx"), "cases": run["phone_tasks"][phone],
                              "run_id": run["run_id"], "params": params, "adb_path": self.config.adb_path,
                              "adb_timeout": self.config.adb_timeout,
                              "execution_timeout": self.config.execution_timeout, "evaluator_timeout": self.config.evaluator_timeout}
                    atomic_json(config_path, config)
                    env = dict(os.environ)
                    env["PYTHONUTF8"], env["PYTHONIOENCODING"] = "1", "utf-8"
                    project = str(Path(__file__).resolve().parents[2])
                    env["PYTHONPATH"] = project + os.pathsep + env.get("PYTHONPATH", "")
                    kwargs = {"start_new_session": True} if os.name != "nt" else {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW}
                    atomic_json(launch_journal, {"config_path": str(config_path), "run_id": run["run_id"]})
                    with (workdir / "onephonerun.log").open("ab") as log:
                        process = subprocess.Popen([sys.executable, "-m", "backend.collector_service.worker", str(config_path)],
                                                   cwd=workdir, stdout=log, stderr=subprocess.STDOUT, env=env, **kwargs)
                    marker = {"pid": process.pid, "config_path": str(config_path)}
                    atomic_json(marker_path, marker)
            deadline = time.monotonic() + self.config.execution_timeout + self.config.evaluator_timeout + 120
            next_process_check = 0
            while not completion.exists():
                if cancel.wait(.2) or (output / "cancelled.json").exists():
                    terminate_owned(marker)
                    return {"trajectories": [], "reports": [], "errors": [{"phone_id": phone, "error": "运行被中断"}]}
                check_process = time.monotonic() >= next_process_check
                next_process_check = time.monotonic() + 2 if check_process else next_process_check
                if check_process and not owned_process(marker):
                    # Complete JSON is replaced atomically before worker exits.
                    if completion.exists():
                        break
                    return {"trajectories": [], "reports": [], "errors": [{"phone_id": phone, "error": "采集进程已退出但没有完成记录"}]}
                if time.monotonic() > deadline:
                    terminate_owned(marker)
                    return {"trajectories": [], "reports": [], "errors": [{"phone_id": phone, "error": "采集执行超时"}]}
            return json.loads(completion.read_text(encoding="utf-8"))
        combined = {"trajectories": [], "reports": [], "errors": []}
        with ThreadPoolExecutor(max_workers=min(50, len(workdirs))) as pool:
            futures = [(phone, pool.submit(one, phone, path)) for phone, path in workdirs.items()]
            for phone, future in futures:
                try:
                    result = future.result()
                    for key in combined:
                        combined[key].extend(result.get(key, []))
                except Exception as exc:
                    combined["errors"].append({"phone_id": phone, "error": str(exc)})
        return combined

    def cancel(self, run, root):
        with self._launch_lock:
            # Commit the fence before inspecting PIDs. Queued devices and a
            # delayed worker from a previous service process must not start.
            atomic_json(root / "execution" / "cancelled.json", {"run_id": run["run_id"], "reason": "device deleted"})
            for marker_path in (root / "execution").glob("*/process.json"):
                terminate_owned(json.loads(marker_path.read_text(encoding="utf-8")))
