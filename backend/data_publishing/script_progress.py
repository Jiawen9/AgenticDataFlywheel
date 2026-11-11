"""Bounded, credential-free parsing of uploader output (never inferred progress)."""
import re


_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_CREDENTIAL = re.compile(
    r'''(?ix)(["']?(?:app[_-]?token|api[_-]?key|access[_-]?key(?:[_-]?id)?|secret(?:[_-]?key)?|password|authorization|token)["']?\s*[:=]\s*)(?:"[^"]*"|'[^']*'|\S+)'''
)
_SPEED = re.compile(r"(?i)(\d+(?:\.\d+)?\s*(?:[KMGT]i?B|B)\s*/\s*s)\b")
_BYTES = re.compile(r"(?i)(\d+(?:\.\d+)?)\s*([KMGT]?i?B)\s*/\s*(\d+(?:\.\d+)?)\s*([KMGT]?i?B)\b")
_BYTE_COUNT = re.compile(r"(?i)(\d+)\s*/\s*(\d+)\s+bytes\b")


def redact(text: str, token: str = "") -> str:
    """Remove secrets before shortening output, including literal configured token."""
    text = _ANSI.sub("", str(text))
    if token:
        text = text.replace(token, "[REDACTED]")
    text = re.sub(r"(?i)\bBearer\s+[^\s,;\"']+", "Bearer [REDACTED]", text)
    text = _CREDENTIAL.sub(lambda m: m.group(1) + "[REDACTED]", text)
    text = re.sub(r"(?i)(https?://)[^\s/@]+:[^\s/@]+@", r"\1[REDACTED]@", text)
    return "".join(c for c in text if c >= " " or c == "\t")


def _size(value: str, unit: str) -> int:
    exponent = "BKMGT".index(unit[0].upper()) if unit[0].upper() != "B" else 0
    base = 1024 if "i" in unit.lower() else 1000
    return int(float(value) * base ** exponent)


def parse_progress(line: str, token: str = "") -> dict:
    safe = redact(line, token)
    result = {"message": safe[-1000:], "speed_text": None,
              "transferred_bytes": None, "total_bytes": None, "percent": None}
    speed = _SPEED.search(safe)
    if speed:
        result["speed_text"] = speed.group(1)
    sizes = _BYTES.search(safe)
    counts = _BYTE_COUNT.search(safe)
    if sizes:
        transferred, total = _size(sizes[1], sizes[2]), _size(sizes[3], sizes[4])
    elif counts:
        transferred, total = int(counts[1]), int(counts[2])
    else:
        return result
    if total > 0 and 0 <= transferred <= total:
        result.update(transferred_bytes=transferred, total_bytes=total,
                      percent=round(transferred * 100 / total, 2))
    return result
