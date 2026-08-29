"""Central model configuration for all backend model callers.

The repository deliberately does not depend on ``python-dotenv``.  This module
parses the small ``backend/.env`` file used by the existing application and
exposes one typed configuration object to each model-using module.

Configuration precedence for a field is:

1. module-specific setting (for example ``QUALITY_MODEL``);
2. common setting (for example ``MODEL_BASE_URL``);
3. legacy setting kept for compatibility (for example ``MODEL_URL``).

Values from the .env file take precedence over process environment variables.
That prevents a previous web request's compatibility variables from silently
overriding the repository configuration.  Process variables are still used
when the corresponding setting is absent from the file.
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable


BACKEND_DIR = Path(__file__).resolve().parent
DEFAULT_ENV_FILE = BACKEND_DIR / ".env"


@dataclass(frozen=True)
class ModelConfig:
    """Resolved connection settings for one logical model module."""

    module: str
    model: str
    api_key: str
    base_url: str
    timeout: float = 120.0
    max_retries: int = 2
    verify: bool = True
    proxy: str = ""
    trust_env: bool = False

    def to_dict(self) -> dict[str, object]:
        """Return settings for existing clients without exposing them in logs."""

        return asdict(self)


def load_env_values(path: Path = DEFAULT_ENV_FILE) -> dict[str, str]:
    """Read a dotenv-like file and fill missing keys from process variables."""

    path = Path(path).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"model configuration not found: {path}")

    values: dict[str, str] = {}
    for line_number, raw_line in enumerate(
        path.read_text(encoding="utf-8-sig").splitlines(), 1
    ):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        name, separator, value = line.partition("=")
        name = name.strip()
        if not separator or not name:
            raise ValueError(f"invalid .env entry at {path}:{line_number}")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[name] = value

    # A value explicitly present in backend/.env wins over a stale value left
    # in the shell by a previous invocation.  This is also convenient for
    # subprocesses launched by the quality-job manager.
    for name, value in os.environ.items():
        values.setdefault(name, value)
    return values


def _first_nonempty(values: dict[str, str], names: Iterable[str]) -> str | None:
    for name in names:
        value = values.get(name)
        if value is not None and value.strip():
            return value.strip()
    return None


def _parse_float(values: dict[str, str], names: Iterable[str], default: float, label: str) -> float:
    raw = _first_nonempty(values, names)
    if raw is None:
        return default
    try:
        parsed = float(raw)
    except ValueError as exc:
        raise ValueError(f"{label} must be a positive number, got {raw!r}") from exc
    if parsed <= 0:
        raise ValueError(f"{label} must be a positive number, got {raw!r}")
    return parsed


def _parse_int(values: dict[str, str], names: Iterable[str], default: int, label: str) -> int:
    raw = _first_nonempty(values, names)
    if raw is None:
        return default
    try:
        parsed = int(raw)
    except ValueError as exc:
        raise ValueError(f"{label} must be a non-negative integer, got {raw!r}") from exc
    if parsed < 0:
        raise ValueError(f"{label} must be a non-negative integer, got {raw!r}")
    return parsed


def _parse_bool(values: dict[str, str], names: Iterable[str], default: bool, label: str) -> bool:
    raw = _first_nonempty(values, names)
    if raw is None:
        return default
    normalized = raw.lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{label} must be true/false, got {raw!r}")


def _module_prefixes(module: str) -> tuple[str, ...]:
    normalized = module.strip().lower().replace("-", "_")
    if not normalized:
        raise ValueError("model module must not be empty")
    prefix = normalized.upper()
    prefixes = [prefix]
    # Text/VL task-generation callers may be split later without forcing a
    # second endpoint.  They fall back to the task-generation profile.
    if normalized.startswith("task_generation_"):
        prefixes.append("TASK_GENERATION")
    return tuple(prefixes)


def load_model_config(
    path: Path = DEFAULT_ENV_FILE,
    module: str = "default",
) -> ModelConfig:
    """Resolve one module's model settings from the shared env file.

    Supported canonical names include ``MODEL_BASE_URL``, ``MODEL_API_KEY``,
    ``MODEL_TIMEOUT_SECONDS`` and ``MODEL_VERIFY_TLS``.  A module can override
    them with names such as ``TREE_MODEL`` or ``QUALITY_BASE_URL``.
    """

    values = load_env_values(path)
    prefixes = _module_prefixes(module)
    common_prefix = prefixes[-1] if len(prefixes) > 1 else None

    def names(suffix: str, *legacy: str) -> tuple[str, ...]:
        candidates: list[str] = []
        for prefix in prefixes:
            candidates.append(f"{prefix}_{suffix}")
        if common_prefix is not None:
            candidates.append(f"{common_prefix}_{suffix}")
        candidates.extend(legacy)
        return tuple(candidates)

    legacy_endpoint = {
        "tree": ("TRAJECTORY_API_BASE_URL", "TRAJECTORY_BASE_URL"),
        "bbox": (
            "TRAJECTORY_VLA_API_BASE_URL",
            "TRAJECTORY_API_BASE_URL",
        ),
        "quality": ("ADARUBRIC_BASE_URL",),
    }.get(module.lower(), ())
    legacy_key = {
        "tree": ("TRAJECTORY_API_KEY",),
        "bbox": ("TRAJECTORY_VLA_API_KEY", "TRAJECTORY_API_KEY"),
        "quality": ("ADARUBRIC_API_KEY",),
    }.get(module.lower(), ())
    legacy_model = {
        "tree": ("TRAJECTORY_MODEL",),
        "bbox": ("TRAJECTORY_MODEL",),
        "quality": ("ADARUBRIC_MODEL",),
    }.get(module.lower(), ())

    model = _first_nonempty(
        values,
        names("MODEL", "MODEL_NAME", *legacy_model),
    )
    # Bbox review normally shares the tree model, while still allowing an
    # explicit BBOX_MODEL override.
    if model is None and module.lower() == "bbox":
        model = _first_nonempty(values, ("TREE_MODEL", "TRAJECTORY_MODEL", "MODEL_NAME"))
    base_url = _first_nonempty(
        values,
        names(
            "BASE_URL",
            "MODEL_BASE_URL",
            "MODEL_URL",
            *legacy_endpoint,
        ),
    )
    api_key = _first_nonempty(
        values,
        names("API_KEY", "MODEL_API_KEY", "YUNAI_API_KEY", *legacy_key),
    ) or ""

    missing: list[str] = []
    if not model:
        missing.append(f"{prefixes[0]}_MODEL or MODEL_NAME")
    if not base_url:
        missing.append(f"{prefixes[0]}_BASE_URL or MODEL_URL")
    if missing:
        raise ValueError(
            f"missing required model settings for {module!r}: {', '.join(missing)}"
        )

    timeout = _parse_float(
        values,
        names(
            "TIMEOUT_SECONDS",
            "MODEL_TIMEOUT_SECONDS",
            "TRAJECTORY_VLA_HTTP_TIMEOUT",
            "TRAJECTORY_HTTP_TIMEOUT",
        ),
        120.0,
        f"{module} timeout",
    )
    max_retries = _parse_int(
        values,
        names("MAX_RETRIES", "MODEL_MAX_RETRIES"),
        2,
        f"{module} max retries",
    )
    verify = _parse_bool(
        values,
        names(
            "VERIFY_TLS",
            "MODEL_VERIFY_TLS",
            "TRAJECTORY_VLA_HTTP_VERIFY",
            "TRAJECTORY_HTTP_VERIFY",
        ),
        True,
        f"{module} TLS verification",
    )
    proxy = _first_nonempty(
        values,
        names(
            "HTTP_PROXY",
            "MODEL_HTTP_PROXY",
            "TRAJECTORY_VLA_HTTP_PROXY_URL",
            "TRAJECTORY_HTTP_PROXY_URL",
        ),
    ) or ""
    trust_env = _parse_bool(
        values,
        names(
            "TRUST_ENV",
            "MODEL_TRUST_ENV",
            "TRAJECTORY_VLA_HTTP_TRUST_ENV",
            "TRAJECTORY_HTTP_TRUST_ENV",
        ),
        False,
        f"{module} proxy environment inheritance",
    )

    return ModelConfig(
        module=module,
        model=model,
        api_key=api_key,
        base_url=base_url.rstrip("/"),
        timeout=timeout,
        max_retries=max_retries,
        verify=verify,
        proxy=proxy,
        trust_env=trust_env,
    )
