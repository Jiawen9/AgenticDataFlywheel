"""Credential-free evaluator configuration shared with the 9011 scheduler."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
try:
    from .file_access import io_path, logical_path
except ImportError:
    from file_access import io_path, logical_path
from typing import Mapping
from urllib.parse import urlsplit, urlunsplit

SNAPSHOT_ENV = "PHONE_FACTORY_EVALUATOR_CONFIG_SNAPSHOT"
_KEY_ENV = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def normalize_base_url(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("evaluator base_url is required")
    raw = value.strip()
    if any(c.isspace() or ord(c) < 32 for c in raw) or "\\" in raw:
        raise ValueError("invalid evaluator base_url")
    parts = urlsplit(raw if "://" in raw else "http://" + raw)
    try:
        port = parts.port
    except ValueError as exc:
        raise ValueError("invalid evaluator URL port") from exc
    if (parts.scheme not in {"http", "https"} or not parts.hostname
            or parts.username is not None or parts.password is not None
            or parts.query or parts.fragment or port == 0):
        raise ValueError("evaluator base_url must be an HTTP(S) API base without credentials/query/fragment")
    path = parts.path.rstrip("/") + "/" if parts.path.strip("/") else "/v1/"
    if path.rstrip("/").endswith("/chat/completions"):
        raise ValueError("evaluator base_url must be an API base, not /chat/completions")
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def normalize_config(raw: object) -> dict:
    if not isinstance(raw, dict) or set(raw) != {"text", "vision"}:
        raise ValueError("evaluator config requires exactly text and vision")
    result = {}
    for role in ("text", "vision"):
        item = raw[role]
        if not isinstance(item, dict) or not {"base_url", "model"} <= set(item):
            raise ValueError(f"evaluator {role} requires base_url and model")
        if set(item) - {"base_url", "model", "api_key_env"}:
            raise ValueError(f"evaluator {role} accepts base_url/model/api_key_env only; never store a key value")
        model = item["model"]
        if not isinstance(model, str) or not model.strip() or any(ord(c) < 32 for c in model):
            raise ValueError(f"invalid evaluator {role} model")
        key_env = item.get("api_key_env", f"PHONE_FACTORY_EVALUATOR_{role.upper()}_API_KEY")
        if not isinstance(key_env, str) or not _KEY_ENV.fullmatch(key_env):
            raise ValueError(f"invalid evaluator {role} api_key_env name")
        result[role] = {"base_url": normalize_base_url(item["base_url"]),
                        "model": model.strip(), "api_key_env": key_env}
    return result


def config_digest(raw: object) -> str:
    payload = json.dumps(normalize_config(raw), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def load_config(path: str | Path) -> dict:
    return normalize_config(json.loads(Path(path).read_text(encoding="utf-8-sig")))


def build_child_environment(raw: object, parent: Mapping[str, str]) -> dict[str, str]:
    """Return a full child snapshot; never mutate the daemon's environment."""
    normalized = normalize_config(raw)
    secrets = {}
    for role, item in normalized.items():
        name = item["api_key_env"]
        value = parent.get(name, "")
        if not value.strip():
            raise ValueError(f"missing evaluator {role} credential environment variable: {name}")
        secrets[name] = value
    child = {k: v for k, v in parent.items()
             if not k.startswith("GUI_AGENT_") and k != SNAPSHOT_ENV}
    child.update(secrets)
    child.update({SNAPSHOT_ENV: json.dumps(normalized, sort_keys=True, ensure_ascii=False),
                  "GUI_AGENT_VLA_CAPTURE_LOGPROBS": "false", "PYTHONIOENCODING": "utf-8",
                  "PYTHONDONTWRITEBYTECODE": "1"})
    return child


def runtime_settings(raw: object, environment: Mapping[str, str]) -> dict:
    normalized = normalize_config(raw)
    providers = {}
    for role, item in normalized.items():
        key = environment.get(item["api_key_env"], "").strip()
        if not key:
            raise ValueError(f"missing evaluator {role} credential environment variable: {item['api_key_env']}")
        providers[f"evaluator_{role}"] = {
            "base_url": item["base_url"], "api_key": key,
            "http_connect_timeout": 15.0, "http_read_timeout": 120.0,
            "http_verify": True, "http_trust_env": False,
        }
    return {
        "PROVIDERS": providers,
        "PROVIDER_ROUTES": {"evaluator.text": "evaluator_text", "evaluator.vision": "evaluator_vision"},
        "EVALUATOR_TEXT_MODEL": normalized["text"]["model"],
        "EVALUATOR_VISION_MODEL": normalized["vision"]["model"],
        "EVALUATOR_CONFIG_DIGEST": config_digest(normalized),
        "VLA_CAPTURE_LOGPROBS": False,
    }
