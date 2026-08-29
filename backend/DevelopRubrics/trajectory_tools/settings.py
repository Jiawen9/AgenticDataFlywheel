"""Load the repository model configuration without exposing secrets."""

from __future__ import annotations

import os
from pathlib import Path

try:
    from ...model_config import (
        DEFAULT_ENV_FILE,
        load_env_values,
        load_model_config,
    )
except ImportError:  # Support quality scripts imported directly from DevelopRubrics/.
    import sys

    BACKEND_DIR = Path(__file__).resolve().parents[2]
    if str(BACKEND_DIR) not in sys.path:
        sys.path.insert(0, str(BACKEND_DIR))
    from model_config import DEFAULT_ENV_FILE, load_env_values, load_model_config


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]


def load_repository_env(path: Path = DEFAULT_ENV_FILE, module: str = "quality") -> dict[str, str]:
    """Return raw settings plus resolved legacy aliases for old AdaRubric code."""
    values = load_env_values(path)
    config = load_model_config(path, module=module)
    values["YUNAI_API_KEY"] = config.api_key
    values["MODEL_URL"] = config.base_url
    values["MODEL_NAME"] = config.model
    return values


def configure_model_environment(
    path: Path = DEFAULT_ENV_FILE,
    module: str = "quality",
) -> dict[str, str | float | int | bool]:
    """Apply one resolved profile to compatibility environment variables."""
    config = load_model_config(path, module=module)
    values = load_repository_env(path, module=module)
    os.environ["MODEL_CONFIG_PATH"] = str(Path(path).expanduser().resolve())
    if module.lower() in {"tree", "bbox"}:
        prefix = "TRAJECTORY"
    else:
        prefix = "ADARUBRIC"
    mapping = {
        f"{prefix}_API_KEY": config.api_key,
        f"{prefix}_API_BASE_URL": config.base_url,
        f"{prefix}_MODEL": config.model,
        f"{prefix}_HTTP_TIMEOUT": str(config.timeout),
        f"{prefix}_HTTP_VERIFY": str(config.verify).lower(),
        f"{prefix}_HTTP_TRUST_ENV": str(config.trust_env).lower(),
        f"{prefix}_MAX_RETRIES": str(config.max_retries),
    }
    if config.proxy:
        mapping[f"{prefix}_HTTP_PROXY_URL"] = config.proxy
    for name, value in mapping.items():
<<<<<<< Updated upstream
        os.environ.setdefault(name, value)
    return {"model": values["MODEL_NAME"], "base_url": values["MODEL_URL"]}

=======
        os.environ[name] = value
    if values.get("ADARUBRIC_EVAL_MAX_CONCURRENT"):
        os.environ[
            "ADARUBRIC_EVAL_MAX_CONCURRENT",
        ] = values["ADARUBRIC_EVAL_MAX_CONCURRENT"]
    return {
        "model": config.model,
        "base_url": config.base_url,
        "api_key": config.api_key,
        "timeout": config.timeout,
        "max_retries": config.max_retries,
        "verify": config.verify,
        "proxy": config.proxy,
        "trust_env": config.trust_env,
    }
>>>>>>> Stashed changes
