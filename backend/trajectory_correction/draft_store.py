"""Small atomic JSON store for correction drafts and export history."""

from __future__ import annotations

import json
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .constants import CORRECTION_SESSIONS_DIR, ensure_correction_dirs


SESSION_ID_RE = re.compile(r"^[a-f0-9]{12,32}$")
_STORE_LOCK = threading.RLock()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_session_id() -> str:
    return uuid.uuid4().hex[:16]


def _path(session_id: str) -> Path:
    if not SESSION_ID_RE.fullmatch(session_id):
        raise ValueError("无效的修正会话 ID")
    candidate = (CORRECTION_SESSIONS_DIR / f"{session_id}.json").resolve()
    try:
        candidate.relative_to(CORRECTION_SESSIONS_DIR.resolve())
    except ValueError as exc:
        raise ValueError("会话路径无效") from exc
    return candidate


def save_session(session: dict[str, Any]) -> dict[str, Any]:
    ensure_correction_dirs()
    session["updated_at"] = utc_now()
    target = _path(str(session["session_id"]))
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    payload = json.dumps(session, ensure_ascii=False, indent=2)
    with _STORE_LOCK:
        temporary.write_text(payload, encoding="utf-8")
        temporary.replace(target)
    return session


def load_session(session_id: str) -> dict[str, Any] | None:
    path = _path(session_id)
    if not path.is_file():
        return None
    with _STORE_LOCK:
        return json.loads(path.read_text(encoding="utf-8"))


def list_sessions() -> list[dict[str, Any]]:
    ensure_correction_dirs()
    values: list[dict[str, Any]] = []
    with _STORE_LOCK:
        for path in CORRECTION_SESSIONS_DIR.glob("*.json"):
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            values.append(value)
    return sorted(values, key=lambda item: str(item.get("updated_at", "")), reverse=True)
