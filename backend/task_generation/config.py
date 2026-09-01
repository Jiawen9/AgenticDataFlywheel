"""Configuration shared by the task-generation model client and jobs."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .constants import ENV_FILE


def load_env_values(path: Path = ENV_FILE) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _value(values: dict[str, str], module: str, key: str, default: str = "") -> str:
    prefix = module.upper() + "_"
    return os.environ.get(prefix + key) or values.get(prefix + key) or os.environ.get(key) or values.get(key) or default


def _bool(value: str, default: bool) -> bool:
    if not value:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _int(value: str, default: int, *, minimum: int = 0) -> int:
    if not value:
        return default
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ValueError(f"任务生成配置必须是整数，收到 {value!r}") from exc
    if parsed < minimum:
        raise ValueError(f"任务生成配置必须大于等于 {minimum}，收到 {parsed}")
    return parsed


def _float(value: str, default: float) -> float:
    if not value:
        return default
    try:
        parsed = float(value)
    except ValueError as exc:
        raise ValueError(f"任务生成超时必须是数字，收到 {value!r}") from exc
    if parsed <= 0:
        raise ValueError(f"任务生成超时必须为正数，收到 {parsed}")
    return parsed


@dataclass(frozen=True)
class TaskGenerationConfig:
    api_key: str
    base_url: str
    model: str
    proxy: str
    verify: bool | str
    timeout: float
    trust_env: bool
    max_retries: int
    max_concurrent: int


def load_model_config() -> TaskGenerationConfig:
    values = load_env_values()
    api_key = _value(values, "task_generation", "API_KEY")
    base_url = _value(values, "task_generation", "MODEL_URL", "https://yunai.chat/v1").rstrip("/")
    model = _value(values, "task_generation", "MODEL_NAME")
    proxy = _value(values, "task_generation", "HTTP_PROXY_URL")
    verify_raw = _value(values, "task_generation", "HTTP_VERIFY", "true")
    verify: bool | str = _bool(verify_raw, True) if verify_raw.lower() in {"1", "0", "true", "false", "yes", "no", "on", "off"} else verify_raw
    return TaskGenerationConfig(
        api_key=api_key,
        base_url=base_url,
        model=model,
        proxy=proxy,
        verify=verify,
        timeout=_float(_value(values, "task_generation", "HTTP_TIMEOUT", "120"), 120.0),
        trust_env=_bool(_value(values, "task_generation", "HTTP_TRUST_ENV", "false"), False),
        max_retries=_int(_value(values, "task_generation", "MAX_RETRIES", "2"), 2, minimum=0),
        max_concurrent=_int(_value(values, "task_generation", "MAX_CONCURRENT", "4"), 4, minimum=1),
    )
