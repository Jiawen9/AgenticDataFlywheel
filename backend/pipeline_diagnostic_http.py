"""Passive, sanitized HTTP diagnostics for existing Pipeline model calls."""
from __future__ import annotations

import functools
import inspect
import json
import logging
import re
import sys
import threading
import time
import zlib
from contextvars import ContextVar
from pathlib import Path

import httpx

from . import pipeline_diagnostics as trace

_call = ContextVar("pipeline_model_call", default=None)
_LOG = logging.getLogger(__name__)
_install_lock = threading.RLock()
_SAFE_HEADERS = frozenset({"content-type", "content-length", "date", "server", "retry-after",
                           "x-request-id", "request-id", "x-stainless-retry-count"})


def _safe(function, *args, **kwargs):
    try:
        return function(*args, **kwargs)
    except Exception:
        try:
            _LOG.warning("Pipeline HTTP diagnostic write failed")
        except Exception:
            pass
        return None


def _json(path, value):
    # Explicitly sanitize BEFORE crossing the file writer boundary.
    return trace.write_json(path, trace.sanitize(value))


def _headers(headers):
    # Authentication, cookies, and unknown custom headers never enter the log.
    return trace.sanitize({k: v for k, v in headers.items() if k.lower() in _SAFE_HEADERS})


def _body(value: bytes):
    text = value.decode("utf-8", errors="replace")
    try:
        return trace.sanitize(json.loads(text))
    except (ValueError, TypeError):
        # Incomplete JSON must reach the partial sanitizer before regex-based
        # sanitization can disturb JSON string escapes.
        return text


def _quality_context():
    module = sys.modules.get("adarubric.llm.request_control")
    context = getattr(module, "_context", None)
    return dict(context.get()) if context is not None else {}


class _Call:
    def __init__(self, operation, metadata):
        self.id, self.start = trace.diagnostic_id("q"), time.monotonic()
        context = {k: v for k, v in (trace.current_context() or {}).items() if k != "root"}
        self.data = {**context, "request_id": self.id, "operation": operation,
                     "started_at": trace.utc_now(), "status": "running", **metadata}
        self.attempts = []

    def save(self):
        if self.attempts:
            _json(f"requests/{self.id}/call.json", {**self.data,
                  "attempt_ids": [a.id for a in self.attempts], "http_attempt_count": len(self.attempts)})

    def finish(self, error=None, result=None):
        for attempt in self.attempts:
            if not attempt.data.get("finished_at"):
                attempt.finish(False, error)
        now = trace.utc_now()
        self.data.update(status="failed" if error is not None else "success",
                         validation_finished_at=now, finished_at=now,
                         elapsed_ms=round((time.monotonic() - self.start) * 1000, 3))
        if error is not None:
            self.data["exception"] = trace.format_exception(error)
        if self.attempts:
            self.save()
            trace.log_event("model_request_finished", **trace.sanitize(self.data), http_attempt_count=len(self.attempts))
        else:
            cached = (getattr(result, "cached", False) or isinstance(result, dict) and result.get("cached")
                      or error is None and self.data["operation"] == "trajectory_summary")
            sources = {name: getattr(result, name) for name in ("classification_source", "observation_source")
                       if getattr(result, name, None) is not None}
            trace.log_event("model_cache_reused" if cached else "model_local_operation_finished",
                            operation=self.data["operation"], status=self.data["status"],
                            exception=self.data.get("exception"), http_attempt_count=0,
                            cache_path=self.data.get("cache_path"), cache_key=self.data.get("cache_key"),
                            sources=trace.sanitize(sources))


class _Attempt:
    def __init__(self, call, request, settings):
        self.call, self.id = call, trace.diagnostic_id("h")
        self.path = f"requests/{call.id}/attempts/{self.id}"
        self.start, self.buffer, self.last_save = time.monotonic(), bytearray(), 0.0
        context = {k: v for k, v in (trace.current_context() or {}).items() if k != "root"}
        self.encoding = ""
        self.data = {**context, "request_id": call.id, "attempt_id": self.id,
                     "attempt_number": len(call.attempts) + 1, "started_at": trace.utc_now(),
                     "response_headers_received": False, "response_body_complete": False,
                     "status_code": None, "first_body_at": None, "headers_at": None,
                     "body_finished_at": None, "finished_at": None}
        call.attempts.append(self)
        try:
            body = _body(request.content)
        except httpx.RequestNotRead:
            body = {"unavailable": "Streamed request was not consumed by diagnostics."}
        if isinstance(body, dict):
            for key in ("model", "temperature", "max_tokens", "response_format", "stream"):
                if key in body:
                    call.data[key] = body[key]
            for message in body.get("messages", []):
                content = message.get("content") if isinstance(message, dict) else None
                if isinstance(content, str):
                    match = re.search(r"target step_ids:\s*(\[[0-9,\s]*\])", content)
                    if match:
                        call.data["target_step_ids"] = json.loads(match.group(1))
        _json(f"{self.path}/request.json", {
            "method": request.method, "url": trace.sanitize(str(request.url)),
            "headers": _headers(request.headers), "body": body,
            "http_options": trace.sanitize(settings), "timeout": request.extensions.get("timeout"),
        })
        self.save()
        call.save()
        if len(call.attempts) == 1:
            trace.log_event("model_request_started", **trace.sanitize(call.data))
        trace.log_event("model_http_started", **{**self.data, "operation": call.data["operation"]})

    def save(self):
        _json(f"{self.path}/attempt.json", self.data)

    def headers(self, response):
        self.encoding = response.headers.get("content-encoding", "").lower().strip()
        self.data.update(response_headers_received=True, headers_at=trace.utc_now(),
                         headers_elapsed_ms=round((time.monotonic() - self.start) * 1000, 3),
                         status_code=response.status_code, content_encoding=self.encoding or "identity")
        _json(f"{self.path}/response.json", {"status_code": response.status_code,
              "headers": _headers(response.headers),
              "service_request_id": response.headers.get("x-request-id") or response.headers.get("request-id")})
        self.save()
        trace.log_event("model_http_headers", **trace.sanitize(self.data))

    def chunk(self, data):
        if self.data["first_body_at"] is None:
            self.data.update(first_body_at=trace.utc_now(),
                             first_body_elapsed_ms=round((time.monotonic() - self.start) * 1000, 3))
            self.save()
        self.buffer.extend(data)
        # Whole-buffer sanitization handles secrets/image data split over chunks.
        if time.monotonic() - self.last_save >= 1.0:
            self.save_body()

    def save_body(self):
        self.last_save = time.monotonic()
        if self.buffer:
            body = self.decoded_body()
            if body is None:
                return
            if not self.data.get("response_body_complete") and isinstance(body, str):
                partial_sanitizer = getattr(trace, "sanitize_partial", None)
                if partial_sanitizer is None:
                    return  # Partial secrets must never reach disk.
                body = partial_sanitizer(body)
            if isinstance(body, (dict, list)):
                _json(f"{self.path}/response_body.json", body)
            text = json.dumps(body, ensure_ascii=False, indent=2) if not isinstance(body, str) else body
            trace.write_text(f"{self.path}/response_body.txt", trace.sanitize(text))

    def decoded_body(self):
        data = bytes(self.buffer)
        try:
            if self.encoding == "gzip":
                data = zlib.decompressobj(16 + zlib.MAX_WBITS).decompress(data)
            elif self.encoding == "deflate":
                try:
                    data = zlib.decompressobj().decompress(data)
                except zlib.error:
                    data = zlib.decompressobj(-zlib.MAX_WBITS).decompress(data)
            elif self.encoding not in {"", "identity"}:
                self.data["body_capture_gap"] = "Unsupported response encoding: " + self.encoding
                return None
        except zlib.error:
            self.data["body_capture_gap"] = "Response decompression incomplete or invalid"
            return None
        return _body(data)

    def finish(self, complete, error=None):
        if self.data.get("finished_at"):
            return
        self.data.update(response_body_complete=bool(complete), finished_at=trace.utc_now(),
                         elapsed_ms=round((time.monotonic() - self.start) * 1000, 3), received_bytes=len(self.buffer))
        if complete:
            self.data["body_finished_at"] = self.data["finished_at"]
        if error is not None:
            self.data["exception"] = trace.format_exception(error)
        if not self.data["response_headers_received"]:
            self.data["waiting_at"] = "response_headers"
        elif not complete:
            self.data["waiting_at"] = "response_body"
        parsed = self.decoded_body() if self.buffer else None
        if isinstance(parsed, dict):
            self.data["usage"] = parsed.get("usage")
            choices = parsed.get("choices")
            self.data["finish_reasons"] = [c.get("finish_reason") for c in choices if isinstance(c, dict)] if isinstance(choices, list) else []
            self.data["response_model"] = parsed.get("model")
        self.save_body()
        self.save()
        trace.log_event("model_http_body_finished", **trace.sanitize(self.data))


class _SyncTee(httpx.SyncByteStream):
    def __init__(self, inner, attempt):
        self.inner, self.attempt = inner, attempt

    def __iter__(self):
        try:
            for chunk in self.inner:
                _safe(self.attempt.chunk, chunk)
                yield chunk
        except BaseException as exc:
            _safe(self.attempt.finish, False, exc)
            raise
        else:
            _safe(self.attempt.finish, True)

    def close(self):
        try:
            self.inner.close()
        finally:
            _safe(self.attempt.finish, False)


class _AsyncTee(httpx.AsyncByteStream):
    def __init__(self, inner, attempt):
        self.inner, self.attempt = inner, attempt

    async def __aiter__(self):
        try:
            async for chunk in self.inner:
                _safe(self.attempt.chunk, chunk)
                yield chunk
        except BaseException as exc:
            _safe(self.attempt.finish, False, exc)
            raise
        else:
            _safe(self.attempt.finish, True)

    async def aclose(self):
        try:
            await self.inner.aclose()
        finally:
            _safe(self.attempt.finish, False)


def instrument_http_client(client, *, api_key=None, settings=None):
    """Install passive hooks without changing client options or network bytes."""
    try:
        trace.register_secrets(api_key)
        if getattr(client, "_pipeline_diagnostic_hooks", False):
            return client
        client._pipeline_diagnostic_hooks = True

        def request_hook(request):
            call = _call.get()
            if call is None or trace.current_context() is None:
                return
            for previous in call.attempts:
                if not previous.data.get("finished_at"):
                    previous.data["end_reason"] = "superseded_by_next_http_attempt"
                    previous.data["exception_available"] = False
                    previous.finish(False)
            request.extensions["pipeline_diagnostic_attempt"] = _Attempt(call, request, settings or {})

        def response_hook(response):
            attempt = response.request.extensions.get("pipeline_diagnostic_attempt")
            if attempt is None:
                return
            _safe(attempt.headers, response)
            response.stream = (_AsyncTee(response.stream, attempt) if isinstance(client, httpx.AsyncClient)
                               else _SyncTee(response.stream, attempt))
            if response.is_stream_consumed:
                _safe(attempt.chunk, response.content)
                _safe(attempt.finish, True)

        if isinstance(client, httpx.AsyncClient):
            async def on_request(request):
                _safe(request_hook, request)
            async def on_response(response):
                _safe(response_hook, response)
        else:
            def on_request(request):
                _safe(request_hook, request)
            def on_response(response):
                _safe(response_hook, response)
        client.event_hooks.setdefault("request", []).append(on_request)
        client.event_hooks.setdefault("response", []).append(on_response)
    except Exception:
        _safe(_LOG.warning, "Pipeline HTTP diagnostic hooks unavailable")
    return client


def model_operation(operation):
    """Record a logical call including its existing output validation."""
    def decorate(function):
        signature = inspect.signature(function)

        def start(args, kwargs):
            context = trace.current_context()
            if context is None:
                return None
            fields = {k: v for k, v in _quality_context().items() if v is not None}
            binding = signature.bind_partial(*args, **kwargs)
            binding.apply_defaults()
            bound = binding.arguments
            for source, target in (("trajectory_id", "trajectory_id"), ("trajectory", "trajectory_id"),
                                   ("step", "step"), ("step_index", "step")):
                if source in bound:
                    fields[target] = bound[source]
            model = getattr(args[0], "model", None) if args else None
            image_refs = {}
            for name in ("current_image_path", "next_image_path", "after_image_path", "image_path", "image",
                         "screenshot_path", "candidate_image_paths", "reference_image_paths"):
                value = bound.get(name)
                if isinstance(value, (Path, str)):
                    image_refs[name] = str(value)
                elif isinstance(value, (list, tuple)):
                    image_refs[name] = [str(item) for item in value if isinstance(item, (Path, str))]
            fields.setdefault("operation", operation)
            call = _Call(fields["operation"], {**fields, "model": model,
                "concurrency": context.get("concurrency", "unknown"),
                "image_source_refs": image_refs or context.get("image_source_refs", {}),
                "temperature": bound.get("temperature"), "max_tokens": bound.get("max_tokens"),
                "cache_path": (getattr(args[0], "cache_path", None) or getattr(args[0], "cache_dir", None)) if args else None,
                "cache_key": bound.get("key"),
                "client_attempt_limit": getattr(args[0], "_max_retries", None) if args else None})
            return call, fields

        if inspect.iscoroutinefunction(function):
            @functools.wraps(function)
            async def wrapped(*args, **kwargs):
                value = _safe(start, args, kwargs)
                if value is None:
                    return await function(*args, **kwargs)
                call, fields = value
                token = _call.set(call)
                try:
                    with trace.trace_context(**fields):
                        try:
                            result = await function(*args, **kwargs)
                        except BaseException as exc:
                            _safe(call.finish, exc)
                            raise
                        _safe(call.finish, result=result)
                        return result
                finally:
                    _call.reset(token)
        else:
            @functools.wraps(function)
            def wrapped(*args, **kwargs):
                value = _safe(start, args, kwargs)
                if value is None:
                    return function(*args, **kwargs)
                call, fields = value
                token = _call.set(call)
                try:
                    with trace.trace_context(**fields):
                        try:
                            result = function(*args, **kwargs)
                        except BaseException as exc:
                            _safe(call.finish, exc)
                            raise
                        _safe(call.finish, result=result)
                        return result
                finally:
                    _call.reset(token)
        return wrapped
    return decorate


def install_box_reviewer_logging():
    """Instrument the existing reviewer without changing its hashed source file.

    The constructor still executes exactly once with the original arguments;
    hooks are attached to its actual configured SDK HTTP client afterwards.
    """
    def install():
        from .bounding_box.qwen_reviewer import QwenBoxReviewer
        with _install_lock:
            original_init = QwenBoxReviewer.__init__
            if getattr(original_init, "_pipeline_diagnostic_wrapper", False):
                return

            @functools.wraps(original_init)
            def initialize(self, *args, **kwargs):
                original_init(self, *args, **kwargs)
                def attach():
                    sdk = self.client
                    settings = kwargs.get("client_settings") or {}
                    instrument_http_client(sdk._client, api_key=sdk.api_key, settings={
                        "proxy": settings.get("proxy", "not captured"), "trust_env": getattr(sdk._client, "trust_env", None),
                        "sdk_retries": sdk.max_retries})
                _safe(attach)

            initialize._pipeline_diagnostic_wrapper = True
            QwenBoxReviewer.__init__ = initialize
            QwenBoxReviewer.review = model_operation("bounding_box_review")(QwenBoxReviewer.review)
    _safe(install)
