from __future__ import annotations

import json
import re
from typing import Any

_API_KEY_RE = re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b")
_IMAGE_DATA_RE = re.compile(r"data:image/[^;]+;base64,([A-Za-z0-9+/=]+)")


def redact_text(text: str, *, key_hint: str = "") -> str:
    if not isinstance(text, str) or not text:
        return text

    redacted = _API_KEY_RE.sub("[REDACTED_API_KEY]", text)
    redacted = _IMAGE_DATA_RE.sub(
        lambda m: f"[IMAGE_BASE64_OMITTED len={len(m.group(1))}]",
        redacted,
    )
    return redacted


def sanitize_payload(value: Any, *, key_path: tuple[str, ...] = ()) -> Any:
    key_hint = ".".join(key_path)
    if isinstance(value, dict):
        return {
            k: sanitize_payload(v, key_path=(*key_path, str(k)))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [sanitize_payload(v, key_path=key_path) for v in value]
    if isinstance(value, str):
        return redact_text(value, key_hint=key_hint)
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return redact_text(json.dumps(value, ensure_ascii=False, default=str), key_hint=key_hint)
