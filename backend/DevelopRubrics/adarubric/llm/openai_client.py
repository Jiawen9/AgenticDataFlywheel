"""OpenAI-compatible LLM client.

Works with any API that implements the OpenAI chat completions interface
(OpenAI, Azure OpenAI, vLLM with --api-key, LiteLLM proxy, etc.).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, TypeVar, cast

from pydantic import BaseModel, ValidationError

from adarubric.core.exceptions import LLMClientError
from adarubric.llm.base import LLMClient
from adarubric.llm.json_extract import extract_json_candidates

try:
    from openai import (
        APIConnectionError,
        APIError,
        APIStatusError,
        APITimeoutError,
        AsyncOpenAI,
        RateLimitError,
    )
except ImportError as e:
    raise ImportError(
        "openai package is required. Install with: pip install 'adarubric[openai]'"
    ) from e

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

_RETRYABLE = (APIConnectionError, APITimeoutError, RateLimitError)
# 5xx status codes are transient server-side errors and should be retried.
_RETRYABLE_STATUS = frozenset({500, 502, 503, 504})
_EXTRA_BODY_ENV = "ADARUBRIC_EXTRA_BODY_JSON"


def parse_extra_body(value: Any, *, source: str = "extra_body") -> dict[str, Any] | None:
    if value is None or value == "":
        return None
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        raise ValueError(f"{source} must be a JSON object or JSON object string")

    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{source} must be valid JSON") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"{source} must be a JSON object")
    return parsed


def _extra_body_from_env(value: str | None = None) -> dict[str, Any] | None:
    raw = os.environ.get(_EXTRA_BODY_ENV) if value is None else value
    return parse_extra_body(raw, source=_EXTRA_BODY_ENV)


class OpenAIClient(LLMClient):
    """LLM client for OpenAI-compatible APIs.

    Parameters
    ----------
    model : str
        Model identifier (e.g. ``"gpt-4o"``).
    api_key : str | None
        API key. Falls back to ``OPENAI_API_KEY`` env var.
    base_url : str | None
        Override API base URL for compatible providers.
    max_retries : int
        Maximum retry attempts for transient failures.
    extra_body : dict | None
        Optional OpenAI-compatible provider-specific request body fields.
        If omitted, ``ADARUBRIC_EXTRA_BODY_JSON`` is used when set.
    """

    def __init__(
        self,
        model: str = "gpt-4o",
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        max_retries: int = 3,
        extra_body: dict[str, Any] | None = None,
    ) -> None:
        self.model = model
        self._max_retries = max(1, max_retries)
        self._extra_body = extra_body if extra_body is not None else _extra_body_from_env()
        self._client = AsyncOpenAI(api_key=api_key, base_url=base_url)

    async def _chat(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float,
        max_tokens: int,
        json_mode: bool = False,
    ) -> str:
        """Chat completion with exponential backoff on transient errors."""
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        if self._extra_body:
            kwargs["extra_body"] = self._extra_body

        delay = 2.0
        last_exc: Exception | None = None
        for attempt in range(self._max_retries):
            try:
                response = await self._client.chat.completions.create(**kwargs)
                content = response.choices[0].message.content
                if content is None:
                    raise LLMClientError("LLM returned empty content")
                return cast(str, content)
            except _RETRYABLE as exc:
                last_exc = exc
            except APIStatusError as exc:
                # Only 5xx are transient; client errors (4xx) should not be retried.
                if exc.status_code not in _RETRYABLE_STATUS:
                    raise
                last_exc = exc
            except APIError:
                raise

            if attempt + 1 >= self._max_retries:
                break
            logger.warning(
                "OpenAI transient error (%s), retry %d/%d in %.1fs",
                type(last_exc).__name__,
                attempt + 1,
                self._max_retries,
                delay,
            )
            await asyncio.sleep(delay)
            delay = min(delay * 2.0, 60.0)

        assert last_exc is not None
        raise last_exc

    async def generate_structured(
        self,
        messages: list[dict[str, str]],
        response_model: type[T],
        *,
        temperature: float = 0.0,
        max_tokens: int = 4096,
    ) -> T:
        json_instruction = (
            f"\n\nYou MUST respond with valid JSON matching this schema:\n"
            f"{json.dumps(response_model.model_json_schema(), indent=2)}"
        )
        augmented_messages = list(messages)
        if augmented_messages and augmented_messages[-1]["role"] == "user":
            augmented_messages[-1] = {
                **augmented_messages[-1],
                "content": augmented_messages[-1]["content"] + json_instruction,
            }
        else:
            augmented_messages.append({"role": "user", "content": json_instruction})

        try:
            raw = await self._chat(
                augmented_messages,
                temperature=temperature,
                max_tokens=max_tokens,
                json_mode=True,
            )
        except APIError as exc:
            raise LLMClientError(
                f"OpenAI API error: {exc}",
                context={"model": self.model},
            ) from exc

        candidates = [
            candidate
            for candidate in extract_json_candidates(raw)
            if candidate.lstrip().startswith("{")
        ]
        errors: list[dict[str, str]] = []
        for candidate in candidates:
            try:
                return response_model.model_validate_json(candidate)
            except (ValidationError, json.JSONDecodeError) as exc:
                errors.append({"candidate": candidate[:200], "error": str(exc)})

        logger.warning("Failed to parse LLM response, raw output:\n%s", raw)
        raise LLMClientError(
            "Failed to parse structured response: no JSON object candidate matched schema",
            context={
                "raw_response": raw[:1000],
                "candidate_count": len(candidates),
                "candidate_errors": errors[:5],
            },
        )

    async def generate_text(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.0,
        max_tokens: int = 4096,
    ) -> str:
        try:
            return await self._chat(
                messages,
                temperature=temperature,
                max_tokens=max_tokens,
            )
        except APIError as exc:
            raise LLMClientError(
                f"OpenAI API error: {exc}",
                context={"model": self.model},
            ) from exc

    async def close(self) -> None:
        await self._client.close()
