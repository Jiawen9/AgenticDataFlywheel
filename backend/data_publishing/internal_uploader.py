"""Server-side adapter for the private, synchronous Yundao directory uploader.

Nothing is transmitted until ADF_YUNDAO_* configuration is supplied. The script
must exit zero only after the directory has finished uploading.
"""
import codecs
from collections import deque
from dataclasses import dataclass
import os
from pathlib import Path
import subprocess
import time
from typing import Callable
import uuid

from .script_progress import parse_progress, redact
from .constants import BACKEND_DIR
from ..file_io import io_path, logical_path, resolve_path

try:
    import psutil
except ImportError:
    psutil = None


class UploadError(RuntimeError):
    """Actionable, credential-free message safe to display to the user."""


@dataclass(frozen=True)
class UploadContext:
    file_path: Path
    filename: str
    dataset_name: str
    release_id: str
    file_index: int
    sha256: str
    idempotency_key: str


@dataclass(frozen=True)
class DirectoryUploadContext:
    source_dir: Path
    bucket_path: str
    region: str
    bucket: str
    release_id: str
    idempotency_key: str


@dataclass(frozen=True)
class UploadResult:
    success: bool
    remote_id: str | None = None
    url: str | None = None


def _configuration() -> dict:
    names = ("python", "script", "app_token", "region", "bucket", "prefix")
    keys = {"ADF_YUNDAO_" + name.upper() for name in names}
    values = {}
    path = io_path(BACKEND_DIR / ".env")
    try:
        if path.is_file():
            for raw in path.read_text(encoding="utf-8-sig").splitlines():
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                if key.strip() in keys:
                    values[key.strip()] = value.strip().strip('"').strip("'")
    except (OSError, UnicodeError):
        raise UploadError("云道上传配置文件无法读取，请检查后端 .env 的权限和编码") from None
    # An explicitly empty environment variable disables its .env fallback.
    return {name: os.environ.get("ADF_YUNDAO_" + name.upper(),
                                 values.get("ADF_YUNDAO_" + name.upper(), "")).strip()
            for name in names}


def get_configuration() -> dict:
    """Safe metadata: never return commands, local configuration or credentials."""
    config = _configuration()
    required = ("python", "script", "app_token", "region", "bucket")
    reason = None
    if not all(config[name] for name in required):
        reason = "云道S3上传尚未配置"
    elif not all(logical_path(config[name]).is_absolute() and io_path(config[name]).is_file()
                 for name in ("python", "script")):
        reason = "云道上传的 Python 或脚本路径无效，请配置现有文件的绝对路径"
    elif psutil is None:
        reason = "云道上传缺少 psutil 依赖，请安装平台后端 requirements.txt"
    return {"configured": reason is None, "reason": reason, "directory_upload": True,
            "default_prefix": config["prefix"], "region": config["region"], "bucket": config["bucket"]}


def is_configured() -> bool:
    return get_configuration()["configured"]


# EOF before GO means the parent disappeared: do not execute an untracked upload.
_BOOTSTRAP = """import runpy, sys
if sys.stdin.readline().strip() != 'GO':
    raise SystemExit(73)
script = sys.argv[2]
sys.argv = sys.argv[2:]
sys.path.insert(0, __import__('os').path.dirname(script))
runpy.run_path(script, run_name='__main__')
"""


def probe_process(record: dict) -> str:
    """Match creation time + executable + nonce, not just a potentially reused PID."""
    if psutil is None:
        return "unknown"
    try:
        pid = int(record["pid"])
        created = float(record["create_time"])
        executable, script, nonce = record["executable"], record["script"], record["nonce"]
        if pid <= 0 or not executable or not script or not nonce:
            return "unknown"
        process = psutil.Process(pid)
        if abs(process.create_time() - created) > 0.01:
            return "dead"
        command = process.cmdline()
        expected = ["-u", "-c", _BOOTSTRAP, nonce, script]
        if (os.path.normcase(process.exe()) != os.path.normcase(executable)
                or command[1:6] != expected):
            return "dead"
        if process.status() == psutil.STATUS_ZOMBIE:
            return "dead"
        return "alive"
    except (psutil.NoSuchProcess, psutil.ZombieProcess):
        return "dead"
    except (KeyError, ValueError, TypeError, OSError, psutil.AccessDenied):
        return "unknown"


def _stop_child(process: subprocess.Popen) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def upload_directory(context: DirectoryUploadContext,
                     on_progress: Callable[[dict], None],
                     on_process: Callable[[dict], None]) -> UploadResult:
    safe_config = get_configuration()
    if not safe_config["configured"]:
        raise UploadError(safe_config["reason"])
    config = _configuration()
    if context.region != config["region"] or context.bucket != config["bucket"]:
        raise UploadError("任务的云道 region/bucket 与当前配置不同，请恢复原配置后重试")
    source = logical_path(context.source_dir)
    if not source.is_absolute() or not io_path(source).is_dir():
        raise UploadError("上传目录不存在或不是绝对路径：" + redact(str(source), config["app_token"]))
    script = str(resolve_path(config["script"]))
    nonce = uuid.uuid4().hex
    command = [str(logical_path(config["python"])), "-u", "-c", _BOOTSTRAP, nonce, script,
               "--local_folder_absolute_path=" + str(source),
               "--app_token=" + config["app_token"], "--region=" + context.region,
               "--bucket_name=" + context.bucket, "--bucket_path=" + context.bucket_path,
               "--show_speed"]
    environment = os.environ.copy()
    environment.update(PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    process = None
    recent = deque(maxlen=6)
    try:
        process = subprocess.Popen(command, cwd=str(Path(script).parent), env=environment,
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, shell=False,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        identity = psutil.Process(process.pid)
        record = {"pid": process.pid, "create_time": identity.create_time(),
                  "executable": identity.exe(), "script": script, "nonce": nonce}
        on_process(record)  # Persist first, then release the child.
        process.stdin.write(b"GO\n")
        process.stdin.flush()
        process.stdin.close()
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        buffer = ""
        dropping = False
        pending = None
        last_update = 0.0

        def accept(line: str, force=False) -> None:
            nonlocal pending, last_update
            if line.strip():
                pending = parse_progress(line, config["app_token"])
                recent.append(pending["message"][-500:])
            now = time.monotonic()
            if pending is not None and (force or now - last_update >= 1.0):
                on_progress({**pending, "recent_output": list(recent)})
                pending = None
                last_update = now

        while True:
            data = process.stdout.read1(4096)
            text = decoder.decode(data, final=not data)
            for char in text:
                if char in "\r\n":
                    accept("[上传脚本输出过长，已省略]" if dropping else buffer)
                    buffer, dropping = "", False
                elif not dropping:
                    buffer += char
                    if len(buffer) > 8192:
                        # Never split/truncate a secret before redaction.
                        buffer, dropping = "", True
            if not data:
                accept("[上传脚本输出过长，已省略]" if dropping else buffer, force=True)
                break
        code = process.wait()
        if code != 0:
            detail = recent[-1] if recent else "没有可用的错误输出"
            raise UploadError(f"云道上传脚本退出码 {code}：{detail}")
        return UploadResult(True)
    except UploadError:
        if process is not None:
            _stop_child(process)
        raise
    except Exception as exc:
        if process is not None:
            _stop_child(process)
        # Popen exception text may contain the entire argv, including token.
        raise UploadError("云道上传进程启动、进度登记或读取失败：" + type(exc).__name__) from None
    finally:
        if process is not None:
            for stream in (process.stdin, process.stdout):
                if stream is not None:
                    stream.close()


def upload_excel(context: UploadContext) -> UploadResult:
    """Legacy custom adapters retain this API; the configured script adapter
    requires durable process registration and uses upload_directory instead.
    """
    raise UploadError("云道S3目录上传请使用支持目标配置及进程登记的新版上传接口")
