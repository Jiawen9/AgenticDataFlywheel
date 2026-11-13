from __future__ import annotations

import base64
from io import BytesIO
from pathlib import Path
from typing import Any

import config


def prompt_image_mime() -> str:
    return "jpeg"


def encode_pil_image_base64(image) -> str:
    buffer = BytesIO()
    quality = max(1, min(95, int(getattr(config, "SCREENSHOT_JPEG_QUALITY", 85))))
    image.convert("RGB").save(
        buffer,
        format="JPEG",
        quality=quality,
        optimize=True,
    )
    return base64.b64encode(buffer.getvalue()).decode("utf-8")


def resolve_qwen_resize_pixels(
    resize_config: dict[str, Any],
) -> tuple[int | None, int | None, int]:
    factor = qwen_resize_factor(resize_config)
    min_pixels = resize_pixels(
        resize_config,
        pixel_key="min_pixels",
        token_key="min_image_tokens",
        factor=factor,
    )
    max_pixels = resize_pixels(
        resize_config,
        pixel_key="max_pixels",
        token_key="max_image_tokens",
        factor=factor,
    )
    if resize_config.get("target_image_tokens") is not None:
        target_pixels = image_tokens_to_pixels(
            resize_config["target_image_tokens"],
            factor=factor,
        )
        min_pixels = target_pixels
        max_pixels = target_pixels
    return min_pixels, max_pixels, factor


def qwen_resize_factor(resize_config: dict[str, Any]) -> int:
    return int(
        resize_config.get(
            "factor",
            int(resize_config.get("image_patch_size", 16))
            * int(resize_config.get("spatial_merge_size", 2)),
        )
    )


def resize_pixels(
    resize_config: dict[str, Any],
    *,
    pixel_key: str,
    token_key: str,
    factor: int,
) -> int | None:
    if resize_config.get(pixel_key) is not None:
        return int(resize_config[pixel_key])
    if resize_config.get(token_key) is not None:
        return image_tokens_to_pixels(resize_config[token_key], factor=factor)
    return None


def image_tokens_to_pixels(value: Any, *, factor: int) -> int:
    return int(value) * int(factor) * int(factor)


def qwen_image_patch_size(factor: int) -> int:
    from qwen_vl_utils.vision_process import SPATIAL_MERGE_SIZE

    spatial_merge_size = int(SPATIAL_MERGE_SIZE)
    if int(factor) % spatial_merge_size != 0:
        raise ValueError(
            "qwen_smart_resize factor must be divisible by Qwen "
            f"SPATIAL_MERGE_SIZE={spatial_merge_size}."
        )
    return int(factor) // spatial_merge_size


def smart_resize(
    height: int,
    width: int,
    *,
    factor: int,
    min_pixels: int | None = None,
    max_pixels: int | None = None,
) -> tuple[int, int]:
    from qwen_vl_utils.vision_process import smart_resize as qwen_smart_resize

    return qwen_smart_resize(
        int(height),
        int(width),
        int(factor),
        min_pixels=min_pixels,
        max_pixels=max_pixels,
    )


def fetch_qwen_resized_image(
    *,
    image_path: str,
    factor: int,
    min_pixels: int | None = None,
    max_pixels: int | None = None,
):
    from qwen_vl_utils.vision_process import fetch_image as qwen_fetch_image

    return qwen_fetch_image(
        {
            "image": str(Path(image_path).expanduser().resolve()),
            "min_pixels": min_pixels,
            "max_pixels": max_pixels,
        },
        image_patch_size=qwen_image_patch_size(factor),
    )
