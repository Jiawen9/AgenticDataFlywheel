"""Best-effort, append-only Pipeline diagnostics, independent of business state.

No files are created on import. A trusted execution context is required to write.
Diagnostics must never grant execution authority or alter a business exception.
"""
from __future__ import annotations

import base64
import hashlib
import importlib.metadata
import json
import logging
import math
import os
import re
import sys
import threading
import traceback
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path, PurePosixPath
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .file_io import io_path, is_link_or_junction, resolve_path

SCHEMA_VERSION = 1
_context = ContextVar("pipeline_diagnostic_context", default=None)
_lock = threading.RLock()
_secrets: set[str] = set()
_writer_pid = os.getpid()
_writer_id = None
_writer_root = None
_sequence = 0
_warned: set[str] = set()
_LOG = logging.getLogger(__name__)
_IDENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,191}$")
_IMAGE_DATA = re.compile(r"data:(image/[A-Za-z0-9.+-]+);base64,([A-Za-z0-9+/=_-]+)", re.I)
_URL = re.compile(r"https?:(?:/|\\/){2}[^\s\"'<>]+", re.I)
_AUTH_TEXT = re.compile(r"\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]+", re.I)
_SECRET_NAMES = {
    "authorization", "proxyauthorization", "cookie", "setcookie", "apikey", "xapikey",
    "yunaiapikey", "openaiapikey", "password", "passwd", "secret", "clientsecret",
    "token", "accesstoken", "refreshtoken", "idtoken", "executiontoken",
    "adfqualityexecutiontoken", "phonefactorytoken", "awssecretaccesskey",
    "awsaccesskeyid", "xamzsignature", "xamzcredential", "xamzsecuritytoken",
}
_SECRET_TEXT = re.compile(
    r"(?i)([\"']?(?:[A-Za-z_]*api[_-]?key|authorization|proxy[_-]?authorization|"
    r"[A-Za-z_]*execution[_-]?token|[A-Za-z_]*password|access[_-]?token|refresh[_-]?token|"
    r"cookie|set-cookie|client[_-]?secret)[\"']?\s*[:=]\s*)"
    r"(?:\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\s,;}\]]+)"
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def register_secrets(*values) -> None:
    """Known credentials are also scrubbed when embedded in error strings."""
    with _lock:
        for value in values:
            if isinstance(value, str) and len(value) >= 4 and value not in {"EMPTY", "[REDACTED]"}:
                _secrets.add(value)


def _sensitive(name) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", str(name).lower())
    return (normalized in _SECRET_NAMES or normalized.endswith(("apikey", "password", "executiontoken"))
            or normalized.startswith("xamz") and any(x in normalized for x in ("signature", "credential", "token")))


def _url(value: str) -> str:
    try:
        parts = urlsplit(value.replace("\\/", "/"))
        netloc = parts.netloc.rsplit("@", 1)[-1]
        query = [(key, "[REDACTED]" if _sensitive(key) or key.lower() in {"sig", "signature", "key"} else val)
                 for key, val in parse_qsl(parts.query, keep_blank_values=True)]
        return urlunsplit((parts.scheme, netloc, parts.path, urlencode(query) if parts.query else "", parts.fragment))
    except (TypeError, ValueError):
        return "[INVALID_URL]"


def _image_descriptor(mime: str, encoded: str) -> dict:
    try:
        raw = base64.b64decode(encoded, altchars=b"-_", validate=True)
        return {"image_body_omitted": True, "mime_type": mime, "size_bytes": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest()}
    except (ValueError, TypeError):
        return {"image_body_omitted": True, "mime_type": mime, "invalid_base64": True,
                "encoded_characters": len(encoded), "encoded_sha256": hashlib.sha256(encoded.encode()).hexdigest()}


def _text(value: str) -> str:
    # Serialized JSON occurs inside exception strings and response text files.
    # Decode escapes before sanitizing, instead of matching escaped credentials.
    if value.lstrip().startswith(("{", "[")):
        try:
            parsed = json.loads(value)
        except (ValueError, TypeError):
            pass
        else:
            cleaned = sanitize(parsed)
            return value if cleaned == parsed else json.dumps(cleaned, ensure_ascii=False)
    # Worker output and exception messages may prefix a JSON response with text.
    # Parse those spans as structures as well; regex alone cannot find image data
    # or URL credentials hidden behind JSON escapes.
    candidates = list(re.finditer(r'[\[{]\s*"', value))
    if candidates:
        chunks, consumed = [], 0
        decoder = json.JSONDecoder()
        for match in candidates:
            if match.start() < consumed:
                continue
            start = match.start()
            chunks.append(value[consumed:start])
            try:
                parsed, count = decoder.raw_decode(value[start:])
            except ValueError:
                chunks.append(_partial_json_strings(value[start:]))
                consumed = len(value)
                break
            cleaned = sanitize(parsed)
            chunks.append(value[start:start + count] if cleaned == parsed else json.dumps(cleaned, ensure_ascii=False))
            consumed = start + count
        chunks.append(value[consumed:])
        value = "".join(chunks)
    value = _IMAGE_DATA.sub(lambda m: json.dumps(_image_descriptor(m[1], m[2]), ensure_ascii=False), value)
    value = _URL.sub(lambda m: _url(m[0]), value)
    value = _AUTH_TEXT.sub(lambda m: m[1] + " [REDACTED]", value)
    value = _SECRET_TEXT.sub(lambda m: m[1] + '"[REDACTED]"', value)
    with _lock:
        secrets = sorted(_secrets, key=len, reverse=True)
    for secret in secrets:
        value = value.replace(secret, "[REDACTED]")
    return value


def sanitize(value):
    """Keep business text intact, excluding credentials and binary/image bodies."""
    seen: set[int] = set()
    def walk(item, field=""):
        if _sensitive(field):
            return "[REDACTED]"
        if item is None or isinstance(item, (bool, int)):
            return item
        if isinstance(item, float):
            return item if math.isfinite(item) else str(item)
        if isinstance(item, str):
            match = _IMAGE_DATA.fullmatch(item)
            if match:
                return _image_descriptor(match[1], match[2])
            key = re.sub(r"[^a-z0-9]", "", field.lower())
            if key in {"base64", "imagebase64", "contentbase64", "imagedata", "screenshotbase64", "imagedatab64"}:
                return _image_descriptor("application/octet-stream", item)
            return _text(item)
        if isinstance(item, (bytes, bytearray, memoryview)):
            raw = bytes(item)
            return {"binary_body_omitted": True, "size_bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
        if isinstance(item, Path):
            return _text(str(item))
        if isinstance(item, (dict, list, tuple, set)):
            if id(item) in seen:
                return "[CIRCULAR_REFERENCE]"
            seen.add(id(item))
            try:
                if isinstance(item, dict):
                    # Anthropic-style image source objects also carry bare base64 in data.
                    image_data = item.get("type") == "base64" or str(item.get("media_type", "")).startswith("image/")
                    return {_text(str(k)): walk(v, "imagebase64" if image_data and k == "data" else str(k))
                            for k, v in item.items()}
                if isinstance(item, (list, tuple)) and len(item) == 2 and isinstance(item[0], str) and _sensitive(item[0]):
                    return [item[0], "[REDACTED]"]
                return [walk(v) for v in item]
            finally:
                seen.remove(id(item))
        return _text(str(item))
    return walk(value)


def sanitize_partial(value: str) -> str:
    """Sanitize an unfinished text buffer without exposing a credential prefix."""
    if value.lstrip().startswith(("{", "[")):
        value = _partial_json_strings(value)
    # A stream may stop halfway through a credential; the completed token is not
    # available to ordinary replacement yet. Withhold any ambiguous trailing URL.
    urls = list(_URL.finditer(value))
    if urls and urls[-1].end() == len(value):
        value = value[:urls[-1].start()] + "[INCOMPLETE_URL_OMITTED]"
    with _lock:
        secrets = tuple(_secrets)
    withheld = 0
    for secret in secrets:
        for count in range(min(len(value), len(secret) - 1), withheld, -1):
            if value.endswith(secret[:count]):
                withheld = count
                break
    if withheld:
        value = value[:-withheld] + "[REDACTED_PARTIAL]"
    return _text(value)


def _partial_json_strings(value: str) -> str:
    """Scrub complete tokens and withhold unfinished strings in truncated JSON.

    Raw bytes stay in memory only. An incomplete JSON string can hide an escaped
    credential or bare image data, so it is not safe to publish its partial value.
    """
    token = re.compile(r'"(?:\\.|[^"\\])*"|"(?:\\.|[^"\\])*(?:\\)?\Z', re.S)
    pending_key = ""
    image_source = bool(re.search(r'"(?:type|media_type)"\s*:\s*"(?:base64|image/)', value))
    result, position = [], 0
    for match in token.finditer(value):
        result.append(value[position:match.start()])
        raw = match.group(0)
        try:
            decoded = json.loads(raw)
        except ValueError:
            result.append('"[INCOMPLETE_JSON_STRING_OMITTED]"')
            position = match.end()
            continue
        is_key = value[match.end():].lstrip().startswith(":")
        if is_key:
            pending_key = decoded
            cleaned = _text(decoded)
        elif _sensitive(pending_key):
            cleaned = "[REDACTED]"
        elif (pending_key == "data" and image_source) or re.sub(r"[^a-z0-9]", "", pending_key.lower()) in {
                "base64", "imagebase64", "contentbase64", "imagedata", "screenshotbase64", "imagedatab64"}:
            cleaned = _image_descriptor("application/octet-stream", decoded)
        else:
            cleaned = sanitize(decoded)
        result.append(json.dumps(cleaned, ensure_ascii=False))
        position = match.end()
        if not is_key:
            pending_key = ""
    result.append(value[position:])
    return "".join(result)


def _format_exception(exc: BaseException) -> dict:
    result = {"type": type(exc).__name__, "module": type(exc).__module__, "message": str(exc),
              "traceback": "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))}
    chain, seen = [], set()
    current = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        entry = {"type": type(current).__name__, "message": str(current)}
        for name in ("context", "status_code", "code", "request_id"):
            if getattr(current, name, None) is not None:
                entry[name] = getattr(current, name)
        chain.append(entry)
        current = current.__cause__ or (None if current.__suppress_context__ else current.__context__)
    result["chain"] = chain
    return sanitize(result)


def format_exception(exc: BaseException) -> dict:
    """Never mask the original failure, including unusual third-party errors."""
    try:
        return _format_exception(exc)
    except BaseException:
        return {"type": type(exc).__name__, "diagnostic_exception_unavailable": True}


def _identifier(value) -> str:
    value = str(value)
    if not _IDENT.fullmatch(value) or value in {".", ".."} or ":" in value:
        raise ValueError("Invalid diagnostic identity")
    return value


def current_context() -> dict | None:
    value = _context.get()
    return dict(value) if value else None


@contextmanager
def trace_context(_reset=False, **fields):
    value = {} if _reset else dict(_context.get() or {})
    if fields.get("pipeline_id") and value.get("pipeline_id") != fields["pipeline_id"]:
        value = {}
    value.update({key: item for key, item in fields.items() if item is not None})
    if value.get("pipeline_id"):
        try:
            _identifier(value["pipeline_id"])
            value["root"] = Path(value.get("root") or _default_root())
        except (ValueError, TypeError):
            value = {}
    token = _context.set(value if value.get("pipeline_id") else None)
    try:
        yield current_context()
    finally:
        _context.reset(token)


def _default_root() -> Path:
    from .data_store.paths import DATA_ROOT
    return DATA_ROOT


def _diagnostic_root(context: dict) -> Path:
    root = resolve_path(context["root"])
    path = root
    for part in ("logs", "pipelines", _identifier(context["pipeline_id"])):
        path = path / part
        if is_link_or_junction(path):
            raise ValueError("Diagnostic paths may not be links")
    return path


def _path(relative_path) -> Path:
    context = current_context()
    if context is None:
        raise ValueError("No Pipeline diagnostic context")
    raw = str(relative_path).replace("\\", "/")
    relative = PurePosixPath(raw)
    if relative.is_absolute() or ":" in raw or not relative.parts or any(p in {".", ".."} for p in relative.parts):
        raise ValueError("Diagnostic path must be relative and contained")
    base = _diagnostic_root(context)
    path = base
    for part in relative.parts:
        path = path / part
        if is_link_or_junction(path):
            raise ValueError("Diagnostic paths may not be links")
    if not resolve_path(path).is_relative_to(resolve_path(base)):
        raise ValueError("Diagnostic path escapes Pipeline directory")
    return path


def _warning(exc):
    # Never recurse into our own writer or include a filename/content/credential.
    kind = type(exc).__name__
    with _lock:
        if kind in _warned:
            return
        _warned.add(kind)
    try:
        _LOG.warning("Pipeline diagnostics unavailable (%s); business execution is unchanged", kind)
    except Exception:
        pass


def _atomic(path: Path, raw: bytes) -> None:
    io_path(path.parent).mkdir(parents=True, exist_ok=True)
    from .short_paths import short_temporary_file
    with short_temporary_file(path) as temporary:
        with io_path(temporary).open("wb") as handle:
            handle.write(raw)
            handle.flush()
        io_path(temporary).replace(io_path(path))


def diagnostic_id(prefix: str, root=None) -> str:
    """Logging allocations are best effort, just like logging itself."""
    try:
        context = current_context() or {}
        data_root = root or context.get("root")
        if data_root is not None and io_path(Path(data_root) / "system" / "app.sqlite").is_file():
            from .identifiers import allocate_id
            return allocate_id(prefix, Path(data_root))
    except Exception as exc:
        _warning(exc)
    # Never let database/log disk failure prevent a model request or job.
    # This fallback is diagnostic-only, with 96 random bits and no business use.
    import secrets
    return prefix + "_" + secrets.token_hex(12)


def write_json(relative_path, value) -> bool:
    if current_context() is None:
        return False
    try:
        raw = json.dumps(sanitize(value), ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8")
        with _lock:
            _atomic(_path(relative_path), raw)
        return True
    except Exception as exc:
        _warning(exc)
        return False


def write_text(relative_path, value: str) -> bool:
    if current_context() is None:
        return False
    try:
        with _lock:
            _atomic(_path(relative_path), str(sanitize(value)).encode("utf-8"))
        return True
    except Exception as exc:
        _warning(exc)
        return False


def append_text(relative_path, value: str) -> bool:
    if current_context() is None:
        return False
    try:
        raw = str(sanitize(value)).encode("utf-8")
        with _lock:
            _append_bytes(relative_path, raw)
        return True
    except Exception as exc:
        _warning(exc)
        return False


def _append_bytes(relative_path, raw: bytes) -> None:
    path = _path(relative_path)
    io_path(path.parent).mkdir(parents=True, exist_ok=True)
    with io_path(path).open("ab") as handle:
        handle.write(raw)
        handle.flush()


def log_event(event: str, **fields) -> None:
    global _writer_id, _writer_pid, _writer_root, _sequence
    context = current_context()
    if context is None:
        return
    try:
        with _lock:
            if os.getpid() != _writer_pid or _writer_id is None or _writer_root != str(context["root"]):
                _writer_pid, _writer_id, _sequence = os.getpid(), diagnostic_id("w"), 0
                _writer_root = str(context["root"])
            _sequence += 1
            payload = {**{k: v for k, v in context.items() if k != "root"}, **fields,
                       "schema_version": SCHEMA_VERSION, "event": event, "at": utc_now(),
                       "writer_id": _writer_id, "sequence": _sequence}
            # Association cannot be overridden by an arbitrary model response.
            payload["pipeline_id"] = context["pipeline_id"]
            raw = json.dumps(sanitize(payload), ensure_ascii=False, allow_nan=False) + "\n"
            _append_bytes(f"events/{_writer_id}.jsonl", raw.encode("utf-8"))
    except Exception as exc:
        _warning(exc)


@lru_cache(maxsize=1)
def _runtime() -> dict:
    dependencies = {}
    for name in ("openai", "httpx", "pydantic"):
        try:
            dependencies[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            dependencies[name] = "unknown"
    # No shell/git subprocess: repositories may be deployment copies or worktrees.
    repo = Path(__file__).resolve().parents[1]
    revision = "unknown"
    try:
        git = repo / ".git"
        if io_path(git).is_file():
            entry = io_path(git).read_text(encoding="utf-8").strip()
            if entry.startswith("gitdir: "):
                git = (repo / entry[8:]).resolve()
        head = io_path(git / "HEAD").read_text(encoding="utf-8").strip()
        if re.fullmatch(r"[a-f0-9]{40,64}", head):
            revision = head
        elif head.startswith("ref: "):
            candidate = io_path(git / head[5:]).read_text(encoding="utf-8").strip()
            if re.fullmatch(r"[a-f0-9]{40,64}", candidate):
                revision = candidate
    except (OSError, ValueError):
        pass
    try:
        code_hash = hashlib.sha256(io_path(__file__).read_bytes()).hexdigest()
    except OSError:
        code_hash = "unknown"
    return {"python": sys.version.split()[0], "dependencies": dependencies, "git_revision": revision,
            "dirty_worktree_status": "not_checked", "diagnostic_code_sha256": code_hash}


def record_pipeline(payload: dict, root: Path, event="pipeline_snapshot", message=None) -> None:
    try:
        if not payload.get("pipeline_id"):
            return
        with trace_context(_reset=True, root=root, pipeline_id=payload["pipeline_id"], batch_id=payload.get("batch_id")):
            index = {"schema_version": SCHEMA_VERSION, "updated_at": utc_now(), "runtime": _runtime(),
                     "pipeline_id": payload["pipeline_id"], "batch_id": payload.get("batch_id"),
                     "pipeline": payload, "coverage": "recorded_from_logging_deployment",
                     "remote_model_queue_time": "unknown"}
            linked = []
            for step in payload.get("steps", []):
                ids = list(step.get("job_ids") or []) + list(step.get("previous_job_ids") or [])
                if step.get("retry_job_id"):
                    ids.append(step["retry_job_id"])
                linked.extend((step.get("id"), ident) for ident in ids)
            linked.extend(("collection", ident) for ident in payload.get("collection_run_ids", []))
            if payload.get("release_id"):
                linked.append(("publication/overview", payload["release_id"]))
            index["associated_jobs"] = []
            for stage, ident in dict.fromkeys(linked):
                try:
                    relative = f"jobs/{_identifier(ident)}/state.json"
                    reference = {"stage": stage, "job_id": ident, "snapshot": relative}
                    path = _path(relative)
                    if io_path(path).is_file():
                        state = json.loads(io_path(path).read_text(encoding="utf-8"))
                        reference["executions"] = sorted(child.name for child in io_path(path.parent).iterdir()
                                                         if child.is_dir() and not is_link_or_junction(child))
                        job = state.get("job", {})
                        reference["artifact_refs"] = {key: job[key] for key in (
                            "artifact", "artifacts", "source_refs", "source_ref", "annotation_ref",
                            "output", "output_path", "manifest_path", "files", "excel_paths", "run_id") if key in job}
                    else:
                        reference["detail_missing"] = True
                    index["associated_jobs"].append(reference)
                except (OSError, TypeError, ValueError):
                    index["associated_jobs"].append({"stage": stage, "job_id": ident, "detail_missing": True})
            write_json("index.json", index)
            log_event(event, status=payload.get("status"), step_id=payload.get("current_step"),
                      storage_revision=payload.get("storage_revision"), message=message,
                      steps=payload.get("steps", []), collection_run_ids=payload.get("collection_run_ids", []),
                      session_id=payload.get("session_id"), release_id=payload.get("release_id"))
    except Exception as exc:
        _warning(exc)


def record_job_snapshot(payload: dict, stage: str, root: Path) -> None:
    try:
        context = current_context() or {}
        pipeline_id = payload.get("pipeline_id") or context.get("pipeline_id")
        job_id = payload.get("job_id") or payload.get("collection_run_id") or payload.get("release_id")
        if not pipeline_id or not job_id:
            return
        job_id = _identifier(job_id)
        same_execution = context.get("pipeline_id") == pipeline_id and context.get("job_id") == job_id
        if same_execution:
            for source, target in (("current_task", "task_id"), ("current_trajectory", "trajectory_id"), ("current_step", "step")):
                if source in payload:
                    if payload[source] is None:
                        context.pop(target, None)
                    else:
                        context[target] = payload[source]
            _context.set(context)
        with trace_context(_reset=not same_execution, root=root, pipeline_id=pipeline_id,
                           batch_id=payload.get("batch_id") or context.get("batch_id"), step_id=stage, job_id=job_id):
            snapshot = {"schema_version": SCHEMA_VERSION, "captured_at": utc_now(),
                        "context": {k: v for k, v in (current_context() or {}).items() if k != "root"}, "job": payload}
            write_json(f"jobs/{job_id}/state.json", snapshot)
            execution_id = context.get("execution_id") if same_execution else None
            if execution_id:
                write_json(f"jobs/{job_id}/{_identifier(execution_id)}/snapshot.json", snapshot)
            log_event("job_snapshot", status=payload.get("status"), stage=payload.get("stage"),
                      percent=payload.get("percent"), failure=payload.get("failure"), error=payload.get("error"),
                      reused=payload.get("reused"), reused_task_ids=payload.get("reused_task_ids"),
                      current_task=payload.get("current_task"), current_trajectory=payload.get("current_trajectory"))
    except Exception as exc:
        _warning(exc)


@contextmanager
def job_context(payload: dict, stage: str, root: Path):
    inherited = current_context() or {}
    pipeline_id = payload.get("pipeline_id") or inherited.get("pipeline_id")
    job_id = payload.get("job_id") or payload.get("collection_run_id") or payload.get("release_id")
    if not pipeline_id or not job_id:
        yield None
        return
    execution_id = diagnostic_id("x", root)
    with trace_context(_reset=True, root=root, pipeline_id=pipeline_id,
                       batch_id=payload.get("batch_id") or inherited.get("batch_id") or payload.get("run_id"),
                       step_id=stage, job_id=job_id, execution_id=execution_id):
        record_job_snapshot(payload, stage, root)
        log_event("execution_started")
        try:
            yield current_context()
        except BaseException as exc:
            try:
                error = format_exception(exc)
                write_json(f"jobs/{_identifier(job_id)}/{execution_id}/exception.json", error)
                log_event("execution_exception", error=error)
            except Exception as diagnostic_error:
                _warning(diagnostic_error)
            raise
        finally:
            log_event("execution_exited")
