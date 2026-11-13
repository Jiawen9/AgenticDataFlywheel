"""Process-safe, credential-free usage events for evolve experiments.

Each process writes its own JSONL file.  The coordinator aggregates files after
workers settle, so neither device workers nor evaluator threads contend on one
shared output file.  Missing provider usage is explicitly unknown, not zero.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from threading import Lock
from typing import Any, Iterator


_context: ContextVar[dict[str, str]] = ContextVar("usage_context", default={})
_write_lock = Lock()


@contextmanager
def usage_context(**fields: str) -> Iterator[None]:
    """Attach task/run context to calls in the current evaluator thread."""
    merged = {**_context.get(), **{key: str(value) for key, value in fields.items() if value}}
    token = _context.set(merged)
    try:
        yield
    finally:
        _context.reset(token)


def _count(usage: dict[str, Any], *names: str) -> int | None:
    for name in names:
        value = usage.get(name)
        if value is not None:
            try:
                return int(value)
            except (TypeError, ValueError):
                return None
    return None


def record_usage(
    *, call_id: str, attempt: int, model: str, role: str, provider: str,
    usage: dict[str, Any] | None, status: str, phase: str = "",
    output_chars: int | None = None, latency_seconds: float | None = None,
    error_type: str = "",
) -> None:
    """Write one actual request attempt when a ledger directory is configured."""
    root = os.environ.get("GUI_AGENT_USAGE_LEDGER_DIR", "").strip()
    if not root:
        return
    raw = usage if isinstance(usage, dict) else {}
    ctx = _context.get()
    event = {
        "schema_version": "gui-agent-usage-event.v1",
        "event_id": f"{os.getpid()}:{call_id}:{attempt}",
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "round_id": ctx.get("round_id") or os.environ.get("GUI_AGENT_USAGE_ROUND_ID", ""),
        "stage": ctx.get("stage") or os.environ.get("GUI_AGENT_USAGE_STAGE", "unknown"),
        "model_phase": phase,
        "task_id": ctx.get("task_id") or os.environ.get("GUI_AGENT_USAGE_TASK_ID", ""),
        "run_id": ctx.get("run_id") or os.environ.get("GUI_AGENT_USAGE_RUN_ID", ""),
        "device_id": os.environ.get("GUI_AGENT_USAGE_DEVICE_ID", ""),
        "skill_version": os.environ.get("GUI_AGENT_SKILLS_VERSION", ""),
        "model": str(model),
        "role": str(role),
        "provider": str(provider),
        "call_id": str(call_id),
        "attempt": int(attempt),
        "status": str(status),
        "input_tokens": _count(raw, "prompt_tokens", "input_tokens"),
        "output_tokens": _count(raw, "completion_tokens", "output_tokens"),
        "cached_input_tokens": (
            _count(raw["prompt_tokens_details"], "cached_tokens")
            if isinstance(raw.get("prompt_tokens_details"), dict)
            and raw["prompt_tokens_details"].get("cached_tokens") is not None
            else _count(raw, "cache_read_input_tokens", "input_cached_tokens")
        ),
        "usage_available": bool(raw),
        "output_chars": output_chars,
        "latency_seconds": latency_seconds,
        "error_type": str(error_type),
    }
    # No prompt, response, endpoint or credentials enter this ledger.
    try:
        directory = Path(root)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"usage-{os.getpid()}.jsonl"
        with _write_lock, path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
    except OSError:
        # Accounting must never make an otherwise healthy agent run fail.
        return


def summarize_usage(directory: Path) -> dict[str, Any]:
    """Aggregate unique request attempts by stage and model."""
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    seen: set[str] = set()
    for path in sorted(directory.glob("usage-*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            event_id = str(row.get("event_id") or "")
            if not event_id or event_id in seen:
                continue
            seen.add(event_id)
            key = (str(row.get("stage") or "unknown"), str(row.get("model") or "unknown"))
            group = groups.setdefault(key, {
                "stage": key[0], "model": key[1], "calls": 0,
                "input_tokens": 0, "output_tokens": 0,
                "unknown_usage_calls": 0, "failed_calls": 0,
                "output_token_samples": [], "output_char_samples": [],
            })
            group["calls"] += 1
            if row.get("status") != "success":
                group["failed_calls"] += 1
            input_tokens, output_tokens = row.get("input_tokens"), row.get("output_tokens")
            if input_tokens is None or output_tokens is None:
                group["unknown_usage_calls"] += 1
            else:
                group["input_tokens"] += int(input_tokens)
                group["output_tokens"] += int(output_tokens)
                group["output_token_samples"].append(int(output_tokens))
            if row.get("output_chars") is not None:
                group["output_char_samples"].append(int(row["output_chars"]))

    def percentile(values: list[int], q: float) -> int | None:
        if not values:
            return None
        values.sort()
        return values[min(len(values) - 1, int((len(values) - 1) * q + 0.999999))]

    result = []
    for group in groups.values():
        tokens = group.pop("output_token_samples")
        chars = group.pop("output_char_samples")
        group["output_tokens_p50"] = percentile(tokens, 0.5)
        group["output_tokens_p95"] = percentile(tokens, 0.95)
        group["output_chars_p50"] = percentile(chars, 0.5)
        group["output_chars_p95"] = percentile(chars, 0.95)
        result.append(group)
    return {"schema_version": "gui-agent-usage-summary.v1", "events": len(seen),
            "groups": sorted(result, key=lambda row: (row["stage"], row["model"]))}
