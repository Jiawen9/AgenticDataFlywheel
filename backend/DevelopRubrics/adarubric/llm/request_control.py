"""Bounded model requests and content-free diagnostics for quality workers.

This module deliberately does not depend on the platform. The observer installed
by a supervised worker may revoke execution before a request starts. ContextVars
keep concurrent trajectory requests separate.
"""
from __future__ import annotations

import asyncio
import logging
import math
import os
import re
import time
import uuid
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from typing import Any, TypeVar
from urllib.parse import urlsplit

import httpx
from openai import APITimeoutError

logger = logging.getLogger(__name__)
TIMEOUT_ENV = "ADARUBRIC_REQUEST_TIMEOUT_SECONDS"
DEFAULT_REQUEST_TIMEOUT_SECONDS = 300.0
_OPERATIONS = frozenset({"rubric_generate", "rubric_repair", "trajectory_score", "model_request", "client_cleanup"})
_IDENTIFIER = re.compile(r"^[A-Za-z0-9_.:/-]{1,160}$")
_SAFE_PATH_PART = re.compile(r"^(?:v[0-9]+(?:\.[0-9]+)?|api|openai|chat|completions|inference)$")
_context: ContextVar[dict[str, str | None]] = ContextVar("quality_request_context", default={})
_observer: ContextVar[Callable[[dict[str, Any]], None] | None] = ContextVar("quality_request_observer", default=None)
_id_factory: ContextVar[Callable[[], str] | None] = ContextVar("quality_request_id_factory", default=None)
_completed_timeouts: ContextVar[dict[tuple, tuple[dict[str, Any], float]]] = ContextVar(
    "quality_completed_timeouts", default={})
T = TypeVar("T")


class RequestConfigurationError(ValueError):
    """Invalid local request control; never includes a configuration value."""


class QualityRequestTimeout(TimeoutError):
    """A local absolute deadline, independent of per-read HTTP timeouts."""

    def __init__(self) -> None:
        super().__init__("Quality model request exceeded its execution deadline")
        self.body = {"code": "quality_request_deadline"}


class QualityClientCleanupTimeout(RuntimeError):
    """Local client shutdown stalled; this is not evidence of a model failure."""

    def __init__(self) -> None:
        super().__init__("Quality model client cleanup exceeded its execution deadline")
        self.body = {"code": "quality_client_cleanup_timeout"}


def request_timeout_seconds() -> float:
    raw = os.environ.get(TIMEOUT_ENV)
    if raw is None:
        return DEFAULT_REQUEST_TIMEOUT_SECONDS
    try:
        value = float(raw)
    except (ValueError, TypeError):
        raise RequestConfigurationError(f"{TIMEOUT_ENV} must be a finite positive number") from None
    if not math.isfinite(value) or value <= 0:
        raise RequestConfigurationError(f"{TIMEOUT_ENV} must be a finite positive number")
    return value


def _identifier(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    return value if _IDENTIFIER.fullmatch(value) and not value.lower().startswith(("sk-", "bearer")) else None


def safe_service_url(value: Any) -> str | None:
    """Keep the endpoint origin and only known API path components.

    Compatible gateways sometimes put credentials in path segments as well as
    userinfo or query strings. Unknown segments are always redacted.
    """
    if (not isinstance(value, str) or len(value) > 4096
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        return None
    try:
        parts = urlsplit(value)
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            return None
        host = parts.hostname
        if not re.fullmatch(r"[A-Za-z0-9.:-]+", host):
            return None
        host = f"[{host}]" if ":" in host else host
        port = f":{parts.port}" if parts.port is not None else ""
        path = "/".join(part if _SAFE_PATH_PART.fullmatch(part) else "[redacted]"
                        for part in parts.path.split("/") if part)
        return f"{parts.scheme}://{host}{port}" + (f"/{path}" if path else "")
    except (ValueError, TypeError):
        return None


@contextmanager
def request_context(operation: str, task_id: str | None = None,
                    trajectory_id: str | None = None) -> Iterator[None]:
    previous = _context.get()
    token = _context.set({
        "operation": operation if operation in _OPERATIONS else "model_request",
        "task_id": _identifier(task_id) if task_id is not None else previous.get("task_id"),
        "trajectory_id": _identifier(trajectory_id) if trajectory_id is not None else previous.get("trajectory_id"),
    })
    try:
        yield
    finally:
        _context.reset(token)


@contextmanager
def observe_requests(callback: Callable[[dict[str, Any]], None], *,
                     id_factory: Callable[[], str] | None = None) -> Iterator[None]:
    token = _observer.set(callback)
    id_token = _id_factory.set(id_factory)
    try:
        yield
    finally:
        _id_factory.reset(id_token)
        _observer.reset(token)


def _request_id() -> str:
    factory = _id_factory.get()
    if factory is not None:
        try:
            identifier = factory()
            if isinstance(identifier, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,96}", identifier):
                return identifier
        except Exception:
            logger.warning("Quality diagnostic ID allocation failed; using a local fallback")
    return uuid.uuid4().hex


def _emit(metadata: dict[str, Any], event: str, started: float) -> None:
    frame = {**metadata, "event": event, "elapsed_ms": max(0, round((time.monotonic() - started) * 1000))}
    callback = _observer.get()
    if callback is not None:
        # Exceptions are intentional: a revoked worker must not start another
        # request or accept a completed result after its lease was revoked.
        callback(dict(frame))
    logger.info("Quality model request %s", frame)


async def controlled_request(factory: Callable[[], Awaitable[T]], *, timeout_seconds: float,
                             messages: list[dict[str, Any]], max_tokens: int, model: str,
                             base_url: str, http_proxy_enabled: bool, http_trust_env: bool,
                             request_owner: object | None = None,
                             deadline_error_factory: Callable[[], BaseException] = QualityRequestTimeout) -> T:
    started = time.monotonic()
    wall_started = datetime.now(timezone.utc)
    context = _context.get()
    owner_key = (request_owner, context.get("operation"), context.get("task_id"), context.get("trajectory_id"))
    # Standalone / old jobs may retry inside a client or at the Rubric generator.
    # Clear only an exited request in this exact execution context, and only when
    # a subsequent call is really about to start. Fatal Pipeline failures retain
    # their timeout until the parent confirms process exit (including hung close).
    previous = _completed_timeouts.get().get(owner_key)
    if previous:
        _emit(previous[0], "settled", previous[1])
        remaining = dict(_completed_timeouts.get())
        remaining.pop(owner_key, None)
        _completed_timeouts.set(remaining)
    metadata = {
        "local_request_id": _request_id(),
        "operation": context.get("operation", "model_request"),
        "task_id": context.get("task_id"),
        "trajectory_id": context.get("trajectory_id"),
        "started_at": wall_started.isoformat(),
        "deadline_at": (wall_started + timedelta(seconds=timeout_seconds)).isoformat(),
        "timeout_seconds": timeout_seconds,
        "input_characters": sum(len(message.get("content", "")) for message in messages
                                if isinstance(message.get("content"), str)),
        "max_tokens": max_tokens,
        "model": _identifier(model),
        "base_url": safe_service_url(base_url),
        "http_proxy_enabled": bool(http_proxy_enabled),
        "http_trust_env": bool(http_trust_env),
    }
    # Check the execution lease before even constructing the request coroutine.
    _emit(metadata, "started", started)
    task: asyncio.Task[T] | None = None
    reported_timeout = False
    try:
        task = asyncio.ensure_future(factory())
        done, _ = await asyncio.wait({task}, timeout=timeout_seconds)
        if not done:
            _emit(metadata, "timeout", started)
            reported_timeout = True
            task.cancel()
            # Never overlap a retry with a request which has not acknowledged
            # cancellation. The parent retains this request's deadline and kills
            # an uncooperative child after its 15-second cleanup grace period.
            try:
                await task
            except BaseException:
                pass
            raise deadline_error_factory()
        result = task.result()
    except BaseException as exc:
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except BaseException:
                pass
        if reported_timeout or isinstance(exc, (TimeoutError, APITimeoutError, httpx.TimeoutException)):
            if not reported_timeout:
                _emit(metadata, "timeout", started)
            _completed_timeouts.set({**_completed_timeouts.get(), owner_key: (metadata, started)})
        else:
            _emit(metadata, "failed", started)
        raise
    _emit(metadata, "finished", started)
    return result
