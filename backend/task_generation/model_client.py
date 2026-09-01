from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable
from typing import Any

from .config import TaskGenerationConfig, load_model_config


def _strip_thinking(text: str) -> str:
    return text.split("</think>")[-1].strip()


def _content_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = [_content_text(item.get("text", item.get("content", "")) if isinstance(item, dict) else item) for item in value]
        return "".join(part for part in parts if part)
    if isinstance(value, dict):
        return _content_text(value.get("text", value.get("content", "")))
    return ""


def _field_names(value: Any) -> list[str]:
    fields = getattr(value, "model_fields_set", None)
    if fields:
        return sorted(str(item) for item in fields)
    values = getattr(value, "__dict__", None)
    return sorted(str(item) for item in values) if isinstance(values, dict) else []


def parse_json_value(raw: str, expected: type | None = None) -> Any:
    """Parse JSON embedded in plain, fenced, or thinking-model output."""
    text = _strip_thinking(raw).strip()
    fence = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.IGNORECASE | re.DOTALL)
    candidates = [fence.group(1).strip()] if fence else []
    candidates.append(text)
    for opener in ("[", "{"):
        index = text.find(opener)
        if index >= 0:
            try:
                value, _ = json.JSONDecoder().raw_decode(text[index:])
                candidates.append(value)
            except json.JSONDecodeError:
                pass
    for candidate in candidates:
        try:
            value = json.loads(candidate) if isinstance(candidate, str) else candidate
        except (TypeError, json.JSONDecodeError):
            continue
        if expected is None or isinstance(value, expected):
            return value
    raise ValueError(f"模型响应不包含合法 JSON：{raw[:300]!r}")


def parse_jsonl_tasks(raw: str) -> list[dict[str, Any]]:
    text = _strip_thinking(raw).strip()
    line_values: list[dict[str, Any]] = []
    line_parse_failed = False
    for line in text.splitlines():
        line = line.strip().strip("`")
        if not line:
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            line_parse_failed = True
            break
        if isinstance(value, dict):
            line_values.append(value)
    if line_values and not line_parse_failed:
        return line_values
    try:
        value = parse_json_value(text)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        if isinstance(value, dict) and "task" in value:
            return [value]
    except ValueError:
        pass
    result: list[dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip().strip("`")
        if not line:
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            logging.warning("模型返回中存在无法解析的任务行，已跳过：%s", line[:160])
            continue
        if isinstance(value, dict):
            result.append(value)
    return result


class TaskGenerationModel:
    def __init__(self, config: TaskGenerationConfig | None = None, call: Callable[[str], str] | None = None) -> None:
        self.config = config or load_model_config()
        self._call_override = call
        if call is None:
            try:
                import httpx
                from openai import OpenAI
            except ImportError as exc:
                raise RuntimeError("任务生成模型需要安装 openai 和 httpx 依赖") from exc
            if not self.config.model:
                raise RuntimeError("任务生成需要配置 MODEL_NAME 或 TASK_GENERATION_MODEL_NAME")
            self._client = OpenAI(
                api_key=self.config.api_key or "none",
                base_url=self.config.base_url,
                timeout=self.config.timeout,
                max_retries=0,
                http_client=httpx.Client(
                    proxy=self.config.proxy or None,
                    verify=self.config.verify,
                    timeout=self.config.timeout,
                    trust_env=self.config.trust_env,
                ),
            )

    def complete(self, prompt: str, *, temperature: float = 0.7, max_tokens: int = 2048) -> str:
        if self._call_override is not None:
            return self._call_override(prompt)
        last_error: Exception | None = None
        for attempt in range(self.config.max_retries + 1):
            try:
                response = self._client.chat.completions.create(
                    model=self.config.model,
                    messages=[
                        {"role": "system", "content": "You are a helpful assistant."},
                        {"role": "user", "content": prompt},
                    ],
                    temperature=temperature,
                    top_p=0.8,
                    max_tokens=max_tokens,
                    extra_body={"top_k": 500},
                )
                if not response.choices:
                    raise RuntimeError("模型响应没有 choices")
                choice = response.choices[0]
                message = getattr(choice, "message", None)
                content = _content_text(getattr(message, "content", None))
                if not content.strip():
                    # Some OpenAI-compatible reasoning providers put the answer in
                    # reasoning_content when content is null or empty.
                    for field in ("reasoning_content", "reasoning", "output_text"):
                        content = _content_text(getattr(message, field, None))
                        if content.strip():
                            break
                if not content.strip():
                    content = _content_text(getattr(choice, "text", None))
                if not content.strip():
                    finish_reason = getattr(choice, "finish_reason", None)
                    fields = ", ".join(_field_names(message)) or "未知"
                    raise RuntimeError(f"模型响应没有文本内容（finish_reason={finish_reason!r}，message字段={fields}）")
                return _strip_thinking(content)
            except Exception as exc:  # OpenAI SDK exposes provider-specific exception types.
                last_error = exc
                if attempt < self.config.max_retries:
                    time.sleep(min(2**attempt, 10))
        if last_error is None:
            detail = "未知异常"
        else:
            detail = str(last_error).strip() or repr(last_error)
            if self.config.api_key:
                detail = detail.replace(self.config.api_key, "[REDACTED]")
            detail = f"{type(last_error).__name__}: {detail[:800]}"
        raise RuntimeError(f"任务生成模型请求在重试后仍失败（共尝试 {self.config.max_retries + 1} 次）：{detail}") from last_error
