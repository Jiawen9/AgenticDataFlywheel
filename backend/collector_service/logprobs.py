"""Extract token traces in a private snapshot before its archive is frozen."""
from __future__ import annotations

import json
import math
import re
from pathlib import Path

from .core import atomic_json, boundary

_RESPONSE = re.compile(r"^step(\d+)_vla_model_response\.json$", re.IGNORECASE)


def _trace_problem(trace: dict) -> str | None:
    if trace.get("status") != "ok":
        return str(trace.get("error") or trace.get("status") or "missing_status")
    tokens = trace.get("response_token_ids")
    logprobs = trace.get("behavior_logprobs")
    mask = trace.get("response_loss_mask")
    if not isinstance(tokens, list) or not tokens or any(type(value) is not int for value in tokens):
        return "invalid_response_token_ids"
    if not isinstance(logprobs, list) or len(logprobs) != len(tokens):
        return "invalid_behavior_logprobs_length"
    try:
        if any(type(value) not in (int, float) or not math.isfinite(value) for value in logprobs):
            return "invalid_behavior_logprobs"
    except OverflowError:
        return "invalid_behavior_logprobs"
    if not isinstance(mask, list) or len(mask) != len(tokens) or any(type(value) is not int or value not in (0, 1) for value in mask):
        return "invalid_response_loss_mask"
    return None


def extract_logprob_sidecars(trajectory: Path, *, required: bool = False) -> list[str]:
    """Copy each top-level response's behavior_trace without touching the response.

    This function must only receive an unfrozen execution snapshot. Nested prefetch
    and rejected candidates are intentionally never traversed. Missing/invalid
    capture data is reported separately so the ordinary trajectory can be kept.
    """
    errors: list[str] = []
    for response in sorted(trajectory.iterdir()):
        match = _RESPONSE.fullmatch(response.name)
        if not match or not response.is_file():
            continue
        label = response.name
        try:
            payload = json.loads(boundary(trajectory, response).read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as exc:
            if required:
                errors.append(f"{label}: logprobs 响应不可读取：{exc}")
            continue
        trace = payload.get("behavior_trace") if isinstance(payload, dict) else None
        if not isinstance(trace, dict):
            if required:
                errors.append(f"{label}: missing_behavior_trace")
            continue
        try:
            target = trajectory / f"step{int(match.group(1)):03d}_logprobs.json"
            atomic_json(boundary(trajectory, target), trace)
        except (OSError, ValueError) as exc:
            errors.append(f"{label}: logprobs JSON 写入失败：{exc}")
            continue
        problem = _trace_problem(trace)
        if required and problem:
            errors.append(f"{label}: logprobs {problem}")
    return errors
