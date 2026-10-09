"""Explicit model proxy settings, independent of Windows/system proxies."""
from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any


def model_http_options(values: Mapping[str, str] | None = None, *, prefix: str = "") -> dict[str, Any]:
    """Module environment > module file > shared environment > shared file.

    Empty settings explicitly clear an override. Standard HTTP(S)_PROXY and
    Windows proxy settings are consulted only when HTTP_TRUST_ENV is enabled.
    """
    values = values or {}

    def setting(key: str) -> tuple[str, str]:
        names = (f"{prefix}_{key}", key) if prefix else (key,)
        for name in names:
            if name in os.environ:
                return name, os.environ[name].strip()
            if name in values:
                return name, values[name].strip()
        return key, ""

    name, raw = setting("HTTP_TRUST_ENV")
    if raw.lower() in {"", "false", "0", "no", "off"}:
        trust_env = False
    elif raw.lower() in {"true", "1", "yes", "on"}:
        trust_env = True
    else:
        # Do not include configuration values, which may contain credentials.
        raise ValueError(f"{name} must be a boolean")
    _, proxy = setting("HTTP_PROXY_URL")
    return {"proxy": proxy or None, "trust_env": trust_env}


def model_http_client(options: Mapping[str, Any] | None = None):
    """Keep the SDK's timeout, connection limits and redirect behavior."""
    from openai import DefaultHttpxClient

    resolved = {"trust_env": False}
    resolved.update(dict(options) if options is not None else model_http_options())
    return DefaultHttpxClient(**resolved)
