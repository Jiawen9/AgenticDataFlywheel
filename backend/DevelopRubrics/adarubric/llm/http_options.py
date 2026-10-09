"""HTTP routing options for standalone AdaRubric clients.

This module intentionally has no dependency on the surrounding platform.
"""
from __future__ import annotations

import asyncio
import os
from typing import Any

_TRUE = frozenset({"true", "1", "yes", "on"})
_FALSE = frozenset({"false", "0", "no", "off", ""})
_closing_tasks: set[asyncio.Task[None]] = set()


def _environment_option(name: str) -> tuple[str | None, str]:
    scoped = "ADARUBRIC_" + name
    if scoped in os.environ:
        return os.environ[scoped], scoped
    return os.environ.get(name), name


def _boolean(value: Any, field: str) -> bool:
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in _TRUE:
            return True
        if normalized in _FALSE:
            return False
    # Values can contain credentials; never include them in validation errors.
    raise ValueError(f"{field} must be a boolean")


def resolve_http_options(
    *,
    http_proxy_url: str | None = None,
    http_trust_env: bool | str | None = None,
) -> dict[str, Any]:
    """Resolve explicit options first, then module and shared process settings."""
    proxy, proxy_field = (
        (http_proxy_url, "http_proxy_url")
        if http_proxy_url is not None else _environment_option("HTTP_PROXY_URL")
    )
    trust, trust_field = (
        (http_trust_env, "http_trust_env")
        if http_trust_env is not None else _environment_option("HTTP_TRUST_ENV")
    )
    if proxy is not None and not isinstance(proxy, str):
        raise ValueError(f"{proxy_field} must be a string")
    return {
        "proxy": (proxy.strip() or None) if proxy is not None else None,
        "trust_env": _boolean(trust, trust_field),
    }


def close_failed_http_client(client: Any) -> None:
    """Release an unowned async transport when SDK construction raises.

    Constructors cannot await inside an active event loop. Keep a strong
    reference to scheduled cleanup until it completes; ordinary client closing
    remains owned by the SDK.
    """
    async def close() -> None:
        try:
            await client.aclose()
        except Exception:
            # Retain the original initialization exception, without logging
            # potentially sensitive configuration from a secondary failure.
            pass

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        asyncio.run(close())
    else:
        task = loop.create_task(close())
        _closing_tasks.add(task)
        task.add_done_callback(_closing_tasks.discard)
