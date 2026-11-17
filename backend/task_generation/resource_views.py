"""Bounded background views of versioned knowledge workbooks for page reads."""
from __future__ import annotations

import copy
import threading
from functools import lru_cache
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .constants import KNOWLEDGE_BASE_FILES
from .knowledge_base import list_knowledge_bases
from .tree_store import current_root, tree_payload, TREE_FILE

_lock = threading.RLock()
_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="generation-resource-view")
_views: dict[str, dict] = {}


def _signature(root: Path) -> tuple:
    # Constant-sized stat reads only. Excel/JSON parsing happens in the worker.
    values = []
    for name in ("current.json", TREE_FILE, *KNOWLEDGE_BASE_FILES.values()):
        try:
            stat = (root / name).stat()
            values.append((name, stat.st_mtime_ns, stat.st_size))
        except FileNotFoundError:
            values.append((name, None, None))
    return tuple(values)


def resource_view(root: Path) -> dict:
    root = Path(root).resolve()
    key, signature = str(root), _signature(root)
    with _lock:
        state = _views.get(key)
        if state is None:
            # Only configured roots enter this cache. Eviction never cancels work.
            if len(_views) >= 8:
                oldest = next((name for name, value in _views.items() if not value.get("pending")), None)
                if oldest is not None:
                    _views.pop(oldest)
            state = _views[key] = {"signature": None, "pending": False}
        if signature != state["signature"] and not state["pending"]:
            state["pending"] = True
            state.pop("error", None)
            _pool.submit(_build, key, root, state)
        return copy.deepcopy({"index_state": "preparing" if state["pending"] else "failed" if state.get("error") else "ready",
                              "tree": state.get("tree"), "knowledge_bases": state.get("knowledge_bases", []),
                              "error": state.get("error"), "stale": bool(state["pending"] and state.get("tree"))})


def _build(key, root, state):
    try:
        source = current_root(root)
        signature = _signature(root)
        tree = tree_payload(source)
        bases = list_knowledge_bases(source)
        with _lock:
            state.update(tree=tree, knowledge_bases=bases, signature=signature, pending=False)
    except Exception as error:
        with _lock:
            state.update(error=str(error), signature=_signature(root), pending=False)


def invalidate_resource_view(root: Path) -> None:
    with _lock:
        state = _views.get(str(Path(root).resolve()))
        if state and not state.get("pending"):
            state["signature"] = None


@lru_cache(maxsize=8)
def _snapshot_tree(root: str, signature: tuple) -> dict:
    return tree_payload(Path(root))


def snapshot_tree(root: Path) -> dict:
    """Selected immutable augmentation snapshots share their parsed display tree."""
    return copy.deepcopy(_snapshot_tree(str(Path(root).resolve()), _signature(Path(root))))
