"""Isolated evaluator configuration. Never imports a phone's collection config."""
from pathlib import Path
import json
import os

from runtime_config import SNAPSHOT_ENV, runtime_settings

PROJECT_ROOT = Path(__file__).resolve().parent
UI_DUMPS_DIR = PROJECT_ROOT / ".unused-ui-dumps"
if not os.environ.get(SNAPSHOT_ENV):
    raise RuntimeError("Use run_evaluation.py --config-json; no evaluator config snapshot was supplied")
globals().update(runtime_settings(json.loads(os.environ[SNAPSHOT_ENV]), os.environ))

ENABLE_THINKING = False
THINKING_EXTRA_BODY_STYLE = "chat_template_kwargs"
PROMPT_CACHE_STRATEGY = "none"
PROMPT_CACHE_PROVIDER_MODE = "none"
EVALUATOR_MAX_IMAGES_PER_CALL = 6
EVALUATOR_IMAGE_TOKENS = 2048
EVALUATOR_STEP_VISION_CONCURRENCY = 3
SCREENSHOT_JPEG_QUALITY = 85
