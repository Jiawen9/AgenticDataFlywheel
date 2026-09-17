"""Current correction drafts are stored only in SQLite."""
from __future__ import annotations

import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from ..data_store import RecordStore
from ..data_store.paths import DATA_ROOT
from .constants import CORRECTION_SESSIONS_DIR

SESSION_ID_RE = re.compile(r"^[a-f0-9]{12,32}$")
_STORE_LOCK = threading.RLock()
_DEFAULT_SESSIONS_DIR = CORRECTION_SESSIONS_DIR
NAMESPACE = "correction_sessions"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_session_id() -> str:
    return uuid.uuid4().hex[:16]


def ensure_correction_dirs() -> None:
    CORRECTION_SESSIONS_DIR.mkdir(parents=True, exist_ok=True)


def storage_root() -> Path:
    # Explicit temporary paths used by tests/embedded callers remain isolated.
    return DATA_ROOT if CORRECTION_SESSIONS_DIR == _DEFAULT_SESSIONS_DIR else CORRECTION_SESSIONS_DIR.parent


def _store() -> RecordStore:
    return RecordStore(storage_root())


def _path(session_id: str) -> Path:
    if not SESSION_ID_RE.fullmatch(session_id):
        raise ValueError("无效的修正会话 ID")
    return CORRECTION_SESSIONS_DIR / f"{session_id}.json"


def save_session(session: dict[str, Any]) -> dict[str, Any]:
    """CAS-save loaded records; never overwrite a newer SQLite revision."""
    key = str(session["session_id"])
    _path(key)
    with _STORE_LOCK:
        session["updated_at"] = utc_now()
        value = _store().put(NAMESPACE, key, session, expected_revision=session.get("storage_revision"))
        session.update(value)
    return session


def load_session(session_id: str) -> dict[str, Any] | None:
    _path(session_id)
    with _STORE_LOCK:
        return _store().get(NAMESPACE, session_id)


def update_session(session_id: str, mutator: Callable[[dict[str, Any]], Any]) -> dict[str, Any]:
    with _STORE_LOCK:
        original = load_session(session_id)
        if original is None:
            raise FileNotFoundError("修正会话不存在")
        def apply(current: dict[str, Any]) -> dict[str, Any]:
            if current.get("published"):
                raise ValueError("修正会话已发布，不能继续修改")
            mutator(current)
            current["updated_at"] = utc_now()
            return current
        return _store().update(NAMESPACE, session_id, apply)


def list_sessions() -> list[dict[str, Any]]:
    with _STORE_LOCK:
        values = _store().list(NAMESPACE)
    return sorted(values, key=lambda item: str(item.get("updated_at", "")), reverse=True)
