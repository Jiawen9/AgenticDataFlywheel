# -*- coding: utf-8 -*-
"""Evaluator vision input preparation.

Screenshots are large (1176x2400, sometimes ~900KB). Sending them raw (as
``agent.models.llm_client.encode_image_base64`` does) makes every VLM request
big and slow. This module resizes each screenshot with the SAME Qwen smart-resize
rule the framework's orche/VLA paths use, to a bounded per-image token budget
(``config.EVALUATOR_IMAGE_TOKENS``), then re-encodes as JPEG.

Results are memoized per (path, budget) so re-inspecting the same screenshot is
free within a process.

The per-call image count ceiling lives in ``config.EVALUATOR_MAX_IMAGES_PER_CALL``;
inspect tools use :func:`max_images_per_call` to truncate an over-large request.
"""
from __future__ import annotations

import functools
import os

import config


@functools.lru_cache(maxsize=512)
def _resized_b64_cached(abspath: str, target_tokens: int) -> str:
    """Resize+JPEG-encode one screenshot to the token budget; raw fallback."""
    try:
        from agent.media.image_resize import (
            fetch_qwen_resized_image,
            encode_pil_image_base64,
            resolve_qwen_resize_pixels,
        )
        min_px, max_px, factor = resolve_qwen_resize_pixels(
            {"image_patch_size": 16, "spatial_merge_size": 2,
             "factor": 32, "target_image_tokens": target_tokens})
        img = fetch_qwen_resized_image(
            image_path=abspath, factor=factor,
            min_pixels=min_px, max_pixels=max_px)
        return encode_pil_image_base64(img)
    except Exception:
        # Framework resize unavailable (e.g. qwen_vl_utils missing): send raw bytes.
        from agent.models.llm_client import encode_image_base64
        return encode_image_base64(abspath)


def image_token_budget() -> int:
    return max(1, int(getattr(config, "EVALUATOR_IMAGE_TOKENS", 2048) or 2048))


def max_images_per_call() -> int:
    return max(1, int(getattr(config, "EVALUATOR_MAX_IMAGES_PER_CALL", 6) or 6))


def prepare_vision_image(path: str) -> tuple[str, str]:
    """Return (base64_jpeg, mime) for one screenshot, resized to the budget."""
    b64 = _resized_b64_cached(os.path.abspath(str(path)), image_token_budget())
    return b64, "jpeg"
