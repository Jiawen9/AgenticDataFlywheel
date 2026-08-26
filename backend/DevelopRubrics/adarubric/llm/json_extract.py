"""Extract JSON objects from LLM outputs (markdown fences, surrounding prose)."""

from __future__ import annotations

import re

_THINK_BLOCK_RE = re.compile(r"<think\b[^>]*>.*?</think>", re.IGNORECASE | re.DOTALL)


def strip_thinking(text: str) -> str:
    """Remove model thinking blocks before parsing or saving final answers."""
    stripped = _THINK_BLOCK_RE.sub("", text)
    lower = stripped.lower()
    end_tag = "</think>"
    last_end = lower.rfind(end_tag)
    if last_end != -1:
        stripped = stripped[last_end + len(end_tag) :]
    return stripped.strip()


def _strip_fence(text: str) -> str:
    t = strip_thinking(text)
    for marker in ("```json", "```JSON", "```"):
        if marker in t:
            start = t.index(marker) + len(marker)
            rest = t[start:]
            fence_end = rest.find("```")
            return rest[:fence_end].strip() if fence_end != -1 else rest.strip()
    return t


def extract_json_candidates(text: str) -> list[str]:
    """Return balanced JSON object/array candidates in text order."""
    t = _strip_fence(text)
    candidates: list[tuple[int, str]] = []
    for open_char, close_char in (("{", "}"), ("[", "]")):
        for start, ch in enumerate(t):
            if ch != open_char:
                continue
            depth = 0
            for end in range(start, len(t)):
                current = t[end]
                if current == open_char:
                    depth += 1
                elif current == close_char:
                    depth -= 1
                if depth == 0:
                    candidates.append((start, t[start : end + 1]))
                    break
    return [candidate for _, candidate in sorted(candidates, key=lambda item: item[0])]


def extract_json_substring(text: str) -> str:
    """Return the first JSON object or array substring from ``text``.

    Strips common markdown fences (``json ... ```) then scans for balanced
    ``{...}`` or ``[...]`` using brace depth (strings are not parsed; rare
    false positives if braces appear unescaped in string literals).
    """
    candidates = extract_json_candidates(text)
    return candidates[0] if candidates else _strip_fence(text)
