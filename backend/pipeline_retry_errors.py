"""Small, safe failure records for background model jobs.

Provider response bodies and exception strings can contain prompts or credentials.
Only known error categories, status codes, provider codes and request IDs cross the
worker boundary or enter persistent job records.
"""

from __future__ import annotations

import re
from typing import Any


_CATEGORIES = {
    "timeout", "connection", "rate_limit", "server_error", "quota", "auth",
    "bad_request", "input", "config", "stale", "published", "service_restart", "unknown",
}
_RETRYABLE = {"timeout", "connection", "rate_limit", "server_error", "service_restart"}
_TRANSIENT_STATUSES = {408, 429, 500, 502, 503, 504}
_IDENTIFIER = re.compile(r"^[A-Za-z0-9_.:-]{1,96}$")
_REQUEST_ID = re.compile(r"request[_ -]?id[\s:：=]+([A-Za-z0-9_.:-]{1,96})", re.I)
_TRAJECTORY = re.compile(r"trajectory[=:]\s*([A-Za-z0-9_.:-]{1,96})", re.I)
_STEP = re.compile(r"step[=:]\s*(\d{1,7})", re.I)
_QUOTA_TERMS = ("insufficient_quota", "token_quota", "quota_not_enough", "quota is not enough",
                "quota exceeded", "pre_consume_token_quota_failed", "billing_hard_limit")


class ModelConfigurationError(ValueError):
    """Invalid local model settings, separate from trajectory input failures."""


def _identifier(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value if _IDENTIFIER.fullmatch(value) and not value.lower().startswith(("sk-", "bearer")) else None


def _status(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if 100 <= number <= 599 else None


def _body_details(exc: BaseException) -> tuple[str | None, str | None, str | None]:
    body = getattr(exc, "body", None)
    if not isinstance(body, dict):
        response = getattr(exc, "response", None)
        if response is not None:
            try:
                body = response.json()
            except Exception:
                body = None
    if isinstance(body, dict):
        error = body.get("error", body)
        if isinstance(error, dict):
            return (_identifier(error.get("code")),
                    error.get("message") if isinstance(error.get("message"), str) else None,
                    _identifier(error.get("request_id") or body.get("request_id")))
    return None, None, None


def _cause_chain(exc: BaseException):
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        current = current.__cause__ or current.__context__


def safe_error_message(category: str, http_status: int | None = None,
                       request_id: str | None = None, code: str | None = None) -> str:
    description = {
        "timeout": "模型请求超时",
        "connection": "模型服务连接中断",
        "rate_limit": "模型服务限流",
        "server_error": "模型服务临时故障",
        "quota": "模型额度不足",
        "auth": "模型服务鉴权失败",
        "bad_request": "模型请求被拒绝，请检查输入与配置",
        "input": "处理输入无效",
        "config": "处理配置无效",
        "stale": "处理输入已变化，请重新提交",
        "published": "批次已发布，处理已结束",
        "service_restart": "服务重启导致作业中断",
        "unknown": "处理失败，原因未判定",
    }.get(category, "处理失败，原因未判定")
    details = []
    if http_status is not None:
        details.append(f"HTTP {http_status}")
    if code:
        details.append(f"代码 {code}")
    if request_id:
        details.append(f"请求编号 {request_id}")
    return description + ("（" + "，".join(details) + "）" if details else "")


def failure_from_payload(payload: Any) -> dict[str, Any]:
    """Validate a child-process failure; never persist its raw message."""
    source = payload if isinstance(payload, dict) else {}
    category = source.get("category") or ("stale" if source.get("kind") == "stale" else "unknown")
    category = category if category in _CATEGORIES else "unknown"
    http_status = _status(source.get("http_status"))
    code = _identifier(source.get("code"))
    request_id = _identifier(source.get("request_id"))
    if category == "rate_limit" and (code and "quota" in code.lower()):
        category = "quota"
    if category == "server_error" and http_status not in {500, 502, 503, 504}:
        category = "unknown"
    if category == "rate_limit" and http_status not in {429, None}:
        category = "unknown"
    result = {
        "category": category,
        "retryable": category in _RETRYABLE,
        "http_status": http_status,
        "code": code,
        "request_id": request_id,
    }
    result["message"] = safe_error_message(category, http_status, request_id, code)
    for key in ("trajectory_id", "task_id"):
        value = _identifier(source.get(key))
        if value:
            result[key] = value
    try:
        step = int(source.get("step"))
    except (TypeError, ValueError):
        step = None
    if step is not None and 0 <= step <= 9999999:
        result["step"] = step
    return result


def failure_from_exception(exc: BaseException) -> dict[str, Any]:
    """Classify typed transport/API errors, including causes wrapped by AdaRubric."""
    chain = list(_cause_chain(exc))
    location: dict[str, Any] = {}
    for cause in chain:
        context = getattr(cause, "context", None)
        if isinstance(context, dict):
            trajectory_id = _identifier(context.get("trajectory_id"))
            if trajectory_id:
                location.setdefault("trajectory_id", trajectory_id)
            step_ids = context.get("core_step_ids") or context.get("step_ids")
            if isinstance(step_ids, list) and step_ids:
                try:
                    location.setdefault("step", min(int(value) for value in step_ids))
                except (TypeError, ValueError):
                    pass
        # Extract only constrained identifiers. The rest of the wrapper string may
        # contain an entire provider response and is never persisted.
        text = str(cause)[:512]
        trajectory_match = _TRAJECTORY.search(text)
        step_match = _STEP.search(text)
        if trajectory_match:
            location.setdefault("trajectory_id", trajectory_match.group(1))
        if step_match:
            location.setdefault("step", int(step_match.group(1)))

    def classified(category: str, status: int | None = None, code: str | None = None,
                   request_id: str | None = None) -> dict[str, Any]:
        return failure_from_payload({"category": category, "http_status": status,
                                     "code": code, "request_id": request_id, **location})

    if any(isinstance(cause, ModelConfigurationError) for cause in chain):
        return classified("config")
    if type(exc).__name__ == "StaleTaskInput":
        return classified("stale")
    if type(exc).__name__ == "BatchPublishedError":
        return classified("published")
    for cause in chain:
        name = type(cause).__name__
        status = _status(getattr(cause, "status_code", None))
        code, body_message, body_request_id = _body_details(cause)
        request_id = _identifier(getattr(cause, "request_id", None)) or body_request_id
        if request_id is None:
            text = body_message or str(cause)
            match = _REQUEST_ID.search(text[:500])
            request_id = _identifier(match.group(1)) if match else None
        details = f"{code or ''} {body_message or ''}".lower()
        if status is not None:
            if any(term in details for term in _QUOTA_TERMS):
                category = "quota"
            elif status == 429:
                category = "rate_limit"
            elif status in {500, 502, 503, 504}:
                category = "server_error"
            elif status == 408:
                category = "timeout"
            elif status in {401, 403}:
                category = "auth"
            elif 400 <= status < 500:
                category = "bad_request"
            else:
                category = "unknown"
            return classified(category, status, code, request_id)
        if name == "RateLimitError":
            return classified("rate_limit", 429, code, request_id)
        if name in {"APITimeoutError", "TimeoutException", "ConnectTimeout", "ReadTimeout", "WriteTimeout", "PoolTimeout"} or isinstance(cause, TimeoutError):
            return classified("timeout", None, code, request_id)
        if name in {"APIConnectionError", "RemoteProtocolError", "ConnectError", "ReadError", "WriteError", "CloseError"} or isinstance(cause, (ConnectionError, EOFError, BrokenPipeError)):
            return classified("connection", None, code, request_id)
    if isinstance(exc, (ValueError, TypeError, FileNotFoundError)):
        return classified("input")
    return classified("unknown")
