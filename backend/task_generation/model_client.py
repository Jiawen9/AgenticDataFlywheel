from __future__ import annotations

import json
import logging
import re
import time
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .config import TaskGenerationConfig, load_model_config
from .constants import LOGS_DIR
from .response_parser import final_text, parse_json_value, parse_jsonl_tasks  # Public compatibility imports.


def _content_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(_content_text(item) for item in value if not isinstance(item, dict) or item.get("type", "text") in {"text", "output_text"})
    if isinstance(value, dict):
        if value.get("type", "text") not in {"text", "output_text"}:
            return ""
        return _content_text(value.get("text", value.get("content", "")))
    return ""


def _plain(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return _plain(vars(value)) if hasattr(value, "__dict__") else str(value)


def redact(value: Any, api_key: str = "") -> Any:
    if isinstance(value, dict):
        return {key: "[REDACTED]" if re.search(r"(?i)api.?key|authorization|password|secret|access.?token|headers", key) else redact(item, api_key) for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item, api_key) for item in value]
    if isinstance(value, str):
        if api_key:
            value = value.replace(api_key, "[REDACTED]")
        value = re.sub(r"(?i)Bearer\s+[^\s\"']+", "Bearer [REDACTED]", value)
        value = re.sub(r"(?i)(https?://)[^/\s@]+@", r"\1[REDACTED]@", value)
        value = re.sub(r"(?i)((?:api[_-]?key|access[_-]?token|password|secret)\s*[=:]\s*)[^\s&\"']+", r"\1[REDACTED]", value)
    return value


class ModelText(str):
    def __new__(cls, text: str, trace_id: str | None = None):
        value = super().__new__(cls, text)
        value.trace_id = trace_id
        return value


class TaskGenerationModel:
    def __init__(self, config: TaskGenerationConfig | None = None, call: Callable[[str], str] | None = None, *, diagnostics_dir: Path | None = None) -> None:
        self.config = config or load_model_config()
        self._call_override = call
        self.diagnostics_dir = diagnostics_dir or LOGS_DIR / "model_calls"
        if call is None:
            try:
                import httpx
                from openai import OpenAI
            except ImportError as exc:
                raise RuntimeError("任务生成模型需要安装 openai 和 httpx 依赖") from exc
            if not self.config.model:
                raise RuntimeError("任务生成需要配置 MODEL_NAME 或 TASK_GENERATION_MODEL_NAME")
            self._client = OpenAI(
                api_key=self.config.api_key or "none", base_url=self.config.base_url,
                timeout=self.config.timeout, max_retries=0,
                http_client=httpx.Client(proxy=self.config.proxy or None, verify=self.config.verify,
                                         timeout=self.config.timeout, trust_env=self.config.trust_env),
            )

    def _trace(self, record: dict[str, Any]) -> str | None:
        if not self.config.trace_enabled:
            return None
        try:
            self.diagnostics_dir.mkdir(parents=True, exist_ok=True)
            name = record["trace_id"] + ".json"
            path = self.diagnostics_dir / name
            temporary = path.with_suffix(".tmp")
            temporary.write_text(json.dumps(redact(record, self.config.api_key), ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(path)
            return record["trace_id"]
        except (OSError, TypeError, ValueError):
            logging.warning("无法写入任务生成模型诊断记录，trace_id=%s", record["trace_id"])
            return None

    def complete(self, prompt: str, *, temperature: float = 0.4, max_tokens: int = 8192, stage: str = "generation", item_id: str = "") -> str:
        if self._call_override is not None:
            return self._call_override(prompt)
        request: dict[str, Any] = {
            "model": self.config.model,
            "messages": [
                {"role": "system", "content": "你是 GUI Agent 任务数据生成器。只输出用户要求的完整 JSON 最终答案，不输出思考、分析、草稿、格式示例或占位符。"},
                {"role": "user", "content": prompt},
            ],
            "temperature": temperature, "max_tokens": max_tokens,
        }
        if self.config.json_mode:
            request["response_format"] = {"type": "json_object"}
        if self.config.extra_body:
            request["extra_body"] = self.config.extra_body
        endpoint = urlsplit(self.config.base_url)
        last_error: Exception | None = None
        last_trace: str | None = None
        for attempt in range(self.config.max_retries + 1):
            record: dict[str, Any] = {
                "trace_id": uuid.uuid4().hex, "created_at": datetime.now(timezone.utc).isoformat(),
                "stage": stage, "item_id": item_id, "attempt": attempt + 1,
                "endpoint": f"{endpoint.scheme}://{endpoint.netloc.rsplit('@', 1)[-1]}{endpoint.path}", "request": request,
            }
            try:
                response = self._client.chat.completions.create(**request)
                record["response"] = _plain(response)
                record["request_id"] = getattr(response, "_request_id", None)
                if not response.choices:
                    raise RuntimeError("模型响应没有 choices")
                choice = response.choices[0]
                message = getattr(choice, "message", None)
                finish = getattr(choice, "finish_reason", None)
                record["finish_reason"] = finish
                if finish == "length":
                    raise RuntimeError(f"模型输出达到 token 上限并被截断（finish_reason=length，max_tokens={max_tokens}），请检查推理模式或提高输出预算")
                if finish in {"content_filter", "tool_calls", "function_call"} or getattr(message, "refusal", None):
                    raise RuntimeError(f"模型未返回可用的 JSON 最终答案（finish_reason={finish!r}，可能拒答或请求工具）")
                content = _content_text(getattr(message, "content", None))
                source = "message.content"
                # Only explicitly final text fields are compatible fallbacks. Never reasoning fields.
                if not content.strip():
                    content = _content_text(getattr(message, "output_text", None))
                    source = "message.output_text"
                if not content.strip():
                    content = _content_text(getattr(choice, "text", None))
                    source = "choice.text"
                if not content.strip():
                    reasoning = any(_content_text(getattr(message, field, None)).strip() for field in ("reasoning_content", "reasoning"))
                    raise RuntimeError("模型只返回推理内容，没有最终答案 content" if reasoning else "模型响应没有最终文本内容")
                answer = final_text(content)
                if not answer:
                    raise RuntimeError("模型只返回思考过程，没有最终答案")
                record.update({"selected_source": source, "final_text": answer})
                last_trace = self._trace(record)
                return ModelText(answer, last_trace)
            except Exception as exc:
                last_error = exc
                record["error"] = f"{type(exc).__name__}: {exc}"
                last_trace = self._trace(record)
                if attempt < self.config.max_retries:
                    time.sleep(min(2**attempt, 10))
        detail = redact(str(last_error) or type(last_error).__name__, self.config.api_key)
        suffix = f"；诊断记录 {last_trace}.json" if last_trace else ""
        raise RuntimeError(f"任务生成模型请求失败（共尝试 {self.config.max_retries + 1} 次）：{detail[:800]}{suffix}") from last_error

    def close(self) -> None:
        if self._call_override is None:
            self._client.close()
