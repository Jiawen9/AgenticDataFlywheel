"""Load the repository model configuration without exposing secrets."""

from __future__ import annotations

import os
import sys
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))
from backend.file_io import io_path

DEFAULT_ENV_FILE = REPOSITORY_ROOT / "backend" / ".env"


def load_repository_env(path: Path = DEFAULT_ENV_FILE) -> dict[str, str]:
    if not io_path(path).is_file():
        raise FileNotFoundError(f"model configuration not found: {path}")
    values: dict[str, str] = {}
    for line_number, raw_line in enumerate(io_path(path).read_text(encoding="utf-8").splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ValueError(f"invalid .env entry at {path}:{line_number}")
        name, value = line.split("=", 1)
        values[name.strip()] = value.strip().strip('"').strip("'")
    missing = [key for key in ("YUNAI_API_KEY", "MODEL_URL", "MODEL_NAME") if not values.get(key)]
    if missing:
        raise ValueError(f"missing required model settings: {', '.join(missing)}")
    return values


def configure_model_environment(path: Path = DEFAULT_ENV_FILE) -> dict[str, str]:
    values = load_repository_env(path)
    from backend.model_http import model_http_options
    network = model_http_options(values, prefix="ADARUBRIC")
    os.environ["ADARUBRIC_HTTP_PROXY_URL"] = network["proxy"] or ""
    os.environ["ADARUBRIC_HTTP_TRUST_ENV"] = "true" if network["trust_env"] else "false"
    mapping = {
        "TRAJECTORY_API_KEY": values["YUNAI_API_KEY"],
        "TRAJECTORY_API_BASE_URL": values["MODEL_URL"],
        "TRAJECTORY_MODEL": values["MODEL_NAME"],
        "ADARUBRIC_API_KEY": values["YUNAI_API_KEY"],
        "ADARUBRIC_BASE_URL": values["MODEL_URL"],
        "ADARUBRIC_MODEL": values["MODEL_NAME"],
    }
    for name, value in mapping.items():
        os.environ.setdefault(name, value)
    if values.get("ADARUBRIC_EVAL_MAX_CONCURRENT"):
        os.environ.setdefault(
            "ADARUBRIC_EVAL_MAX_CONCURRENT",
            values["ADARUBRIC_EVAL_MAX_CONCURRENT"],
        )
    return {"model": values["MODEL_NAME"], "base_url": values["MODEL_URL"]}
