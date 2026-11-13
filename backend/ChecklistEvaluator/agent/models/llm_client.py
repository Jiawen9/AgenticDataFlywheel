import base64
import copy
import hashlib
import json
import logging
import re
import time
from datetime import datetime
from typing import Any
from uuid import uuid4
import httpx
from openai import OpenAI
from runtime_events import request as request_event

import config
from agent.utils.redaction import redact_text
from agent.models.usage_ledger import record_usage

logger = logging.getLogger(__name__)


def _attach_call_timing(
    result: dict,
    *,
    call_id: str,
    started_at: datetime,
    started_perf: float,
    attempt_count: int,
) -> float:
    ended_at = datetime.now().astimezone()
    elapsed_sec = time.perf_counter() - started_perf
    result["timing"] = {
        "call_id": call_id,
        "started_at": started_at.isoformat(timespec="milliseconds"),
        "ended_at": ended_at.isoformat(timespec="milliseconds"),
        "elapsed_ms": int(round(elapsed_sec * 1000)),
        "attempt_count": attempt_count,
    }
    return elapsed_sec


def build_chat_extra_body() -> dict[str, Any]:
    """Provider extras for chat.completions (e.g. Qwen enable_thinking)."""
    enable_thinking = bool(getattr(config, "ENABLE_THINKING", False))
    style = str(
        getattr(config, "THINKING_EXTRA_BODY_STYLE", "chat_template_kwargs") or ""
    ).strip()
    if style == "top_level":
        return {"enable_thinking": enable_thinking}
    if style and style != "chat_template_kwargs":
        logger.warning(
            "[LLM EXTRA BODY] invalid THINKING_EXTRA_BODY_STYLE=%s, fallback to chat_template_kwargs",
            style,
        )
    return {"chat_template_kwargs": {"enable_thinking": enable_thinking}}


def _extra_body_thinking_enabled(extra_body: dict[str, Any] | None) -> Any:
    if not isinstance(extra_body, dict):
        return None
    if "enable_thinking" in extra_body:
        return extra_body.get("enable_thinking")
    chat_template_kwargs = extra_body.get("chat_template_kwargs")
    if isinstance(chat_template_kwargs, dict):
        return chat_template_kwargs.get("enable_thinking")
    return None


def _coerce_bool_or_path(value: Any, default: bool = True) -> bool | str:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip()
    if not text:
        return default
    lowered = text.lower()
    if lowered in {"1", "true", "yes", "on"}:
        return True
    if lowered in {"0", "false", "no", "off"}:
        return False
    return text


def _coerce_timeout(value: Any) -> float | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return float(text)


def _overlay_provider_config(base: dict[str, Any], raw: dict[str, Any] | None) -> dict[str, Any]:
    merged = dict(base)
    if not isinstance(raw, dict):
        return merged
    aliases = {
        "base_url": ("base_url", "api_base_url"),
        "api_key": ("api_key",),
        "http_proxy_url": ("http_proxy_url", "proxy_url", "proxy"),
        "http_verify": ("http_verify", "verify"),
        "http_timeout": ("http_timeout", "timeout"),
        "http_connect_timeout": ("http_connect_timeout", "connect_timeout"),
        "http_read_timeout": ("http_read_timeout", "read_timeout"),
        "http_trust_env": ("http_trust_env", "trust_env"),
    }
    for target_key, source_keys in aliases.items():
        for source_key in source_keys:
            if source_key not in raw:
                continue
            value = raw[source_key]
            if value is None:
                continue
            if isinstance(value, str) and not value.strip():
                continue
            merged[target_key] = value
            break
    return merged


def resolve_provider_config(provider_name: str | None = None) -> tuple[str, dict[str, Any]]:
    requested = str(provider_name or "default").strip() or "default"
    providers = getattr(config, "PROVIDERS", {})
    if not isinstance(providers, dict):
        providers = {}

    default_provider = _overlay_provider_config(
        {},
        providers.get("default") if isinstance(providers.get("default"), dict) else None,
    )
    if requested == "default":
        return "default", default_provider
    return requested, _overlay_provider_config(
        default_provider,
        providers.get(requested) if isinstance(providers.get(requested), dict) else None,
    )


def resolve_provider_route(
    *,
    role: str,
    phase: str | None = None,
    provider: str | None = None,
) -> str:
    explicit = str(provider or "").strip()
    if explicit:
        return explicit
    routes = getattr(config, "PROVIDER_ROUTES", {})
    if not isinstance(routes, dict):
        routes = {}
    role_key = str(role or "default").strip() or "default"
    phase_key = str(phase or "").strip()
    if phase_key:
        routed = str(routes.get(f"{role_key}.{phase_key}", "") or "").strip()
        if routed:
            return routed
    routed = str(routes.get(role_key, "") or "").strip()
    if routed:
        return routed
    if role_key in {"orchestrator", "vla"}:
        return role_key
    return "default"


def build_openai_http_timeout(provider_config: dict[str, Any] | None = None) -> httpx.Timeout | None:
    """Build separate connection/read timeouts for OpenAI-compatible calls."""
    provider_config = provider_config or {}
    legacy_timeout = _coerce_timeout(
        provider_config.get("http_timeout", getattr(config, "HTTP_TIMEOUT", None))
    )
    connect_timeout = _coerce_timeout(
        provider_config.get("http_connect_timeout", getattr(config, "HTTP_CONNECT_TIMEOUT", None))
    ) or legacy_timeout
    read_timeout = _coerce_timeout(
        provider_config.get("http_read_timeout", getattr(config, "HTTP_READ_TIMEOUT", None))
    ) or legacy_timeout
    if connect_timeout is None and read_timeout is None:
        return None
    return httpx.Timeout(
        connect=connect_timeout,
        read=read_timeout,
        write=read_timeout,
        pool=connect_timeout,
    )


def build_openai_http_client(provider_config: dict[str, Any] | None = None) -> httpx.Client | None:
    provider_config = provider_config or {}
    proxy_url = str(provider_config.get("http_proxy_url", getattr(config, "HTTP_PROXY_URL", "")) or "").strip()
    raw_verify = provider_config.get("http_verify", getattr(config, "HTTP_VERIFY", True))
    raw_trust_env = provider_config.get("http_trust_env", getattr(config, "HTTP_TRUST_ENV", True))

    verify = _coerce_bool_or_path(raw_verify, default=True)
    trust_env = bool(_coerce_bool_or_path(raw_trust_env, default=True))
    timeout = build_openai_http_timeout(provider_config)

    if not proxy_url and verify is True and timeout is None and trust_env is True:
        return None

    kwargs: dict[str, Any] = {
        "verify": verify,
        "trust_env": trust_env,
    }
    if proxy_url:
        kwargs["proxy"] = proxy_url
    if timeout is not None:
        kwargs["timeout"] = timeout
    return httpx.Client(**kwargs)


def _thinking_enabled() -> bool:
    return bool(getattr(config, "ENABLE_THINKING", False))


def extract_message_reasoning(msg: Any, content: str = "") -> str:
    """Collect model reasoning/thinking text from a chat completion message."""
    parts: list[str] = []
    seen: set[str] = set()

    def _add(text: Any) -> None:
        if text is None:
            return
        normalized = str(text).strip()
        if not normalized or normalized in seen:
            return
        seen.add(normalized)
        parts.append(normalized)

    for attr in ("reasoning_content", "reasoning", "thinking"):
        _add(getattr(msg, attr, None))

    model_extra = getattr(msg, "model_extra", None)
    if isinstance(model_extra, dict):
        for key in ("reasoning_content", "reasoning", "thinking"):
            _add(model_extra.get(key))

    if hasattr(msg, "model_dump"):
        try:
            dumped = msg.model_dump(exclude_none=True)
        except TypeError:
            dumped = msg.model_dump()
        if isinstance(dumped, dict):
            for key in ("reasoning_content", "reasoning", "thinking"):
                _add(dumped.get(key))

    if content:
        for pattern in (
            r"<think>(.*?)</think>",
            r"<\s*think\s*>(.*?)</\s*think\s*>",
        ):
            for block in re.findall(pattern, content, flags=re.DOTALL | re.IGNORECASE):
                _add(block)

    return "\n\n".join(parts)


def _log_llm_response(
    result: dict,
    *,
    call_id: str,
    role: str,
    provider: str,
    phase: str | None,
    latency_sec: float,
) -> None:
    logger.info(
        "[LLM RESPONSE] call_id=%s role=%s provider=%s phase=%s latency_sec=%.3f finish_reason=%s",
        call_id,
        role,
        provider,
        phase or "n/a",
        latency_sec,
        result.get("finish_reason"),
    )
    reasoning = str(result.get("reasoning_content") or "").strip()
    if _thinking_enabled():
        if reasoning:
            logger.info(
                "[LLM THINKING] chars=%s\n%s",
                len(reasoning),
                redact_text(reasoning, key_hint="llm.reasoning"),
            )
        else:
            logger.info(
                "[LLM THINKING] enable_thinking=true but provider returned no reasoning_content"
            )
    logger.debug("[LLM RESPONSE CONTENT]\n%s", result.get("content", ""))
    if result.get("tool_calls"):
        logger.debug(
            "[LLM TOOL CALLS]\n%s",
            json.dumps(result["tool_calls"], ensure_ascii=False, indent=2),
        )


def _format_messages_for_log(messages: list[dict]) -> str:
    """Format messages for logging, replacing base64 image data with placeholder."""
    parts = []
    for msg in messages:
        role = msg.get("role", "?")
        if role == "assistant":
            content = msg.get("content", "")
            if isinstance(content, str) and not content.strip():
                continue
        content = msg.get("content", "")
        if isinstance(content, str):
            parts.append(f"[{role}]\n{redact_text(content, key_hint=f'message.{role}.content')}")
        elif isinstance(content, list):
            texts = []
            for item in content:
                if isinstance(item, dict):
                    if item.get("type") == "text":
                        texts.append(redact_text(item.get("text", ""), key_hint=f"message.{role}.content"))
                    elif item.get("type") == "image_url":
                        url = item.get("image_url", {}).get("url", "")
                        b64_len = len(url.split(",", 1)[1]) if url.startswith("data:") and "," in url else len(url)
                        texts.append(f"[IMAGE base64 len={b64_len}]")
            parts.append(f"[{role}]\n" + "\n".join(texts))
        else:
            parts.append(f"[{role}]\n{redact_text(str(content), key_hint=f'message.{role}.content')}")
    return "\n---\n".join(parts)


class LLMClient:
    def __init__(self):
        self.max_retries = 3
        self._prompt_cache_slots: dict[tuple[str, str, str], str] = {}
        self._clients: dict[str, Any] = {}

    def _client_for_provider(self, provider_name: str | None = None):
        if not hasattr(self, "_clients"):
            existing_client = getattr(self, "client", None)
            if existing_client is not None:
                return existing_client
            self._clients = {}

        resolved_name, provider_config = resolve_provider_config(provider_name)
        if resolved_name in self._clients:
            return self._clients[resolved_name]

        api_key = str(provider_config.get("api_key", "") or "").strip()
        if not api_key:
            raise RuntimeError(
                f"API key is not configured for provider '{resolved_name}'. "
                "Configure PROVIDERS in config.py or set GUI_AGENT_API_KEY through config_example.py."
            )
        base_url = str(provider_config.get("base_url", "") or "").strip()
        if not base_url:
            raise RuntimeError(
                f"Base URL is not configured for provider '{resolved_name}'. "
                "Configure PROVIDERS in config.py or set GUI_AGENT_API_BASE_URL through config_example.py."
            )
        http_client = build_openai_http_client(provider_config)
        client_kwargs: dict[str, Any] = {
            "base_url": base_url,
            "api_key": api_key,
        }
        if http_client is not None:
            client_kwargs["http_client"] = http_client
            logger.info(
                "[OPENAI HTTP CLIENT] provider=%s custom=true proxy=%s verify=%s timeout=%s trust_env=%s",
                resolved_name,
                "set" if provider_config.get("http_proxy_url") else "unset",
                _coerce_bool_or_path(provider_config.get("http_verify", True), default=True),
                _coerce_timeout(provider_config.get("http_timeout")),
                _coerce_bool_or_path(provider_config.get("http_trust_env", True), default=True),
            )
        # Thread http_timeout into the OpenAI SDK itself.  The SDK's own default
        # request timeout (600s) overrides whatever the custom httpx.Client carries,
        # so GUI_AGENT_HTTP_TIMEOUT was previously a no-op for bounding a stalled
        # request -> a hung call silently ate the full 600s before any retry.  Passing
        # timeout here makes the configured value the per-request bound (all attempts).
        _sdk_timeout = build_openai_http_timeout(provider_config)
        if _sdk_timeout is not None:
            client_kwargs["timeout"] = _sdk_timeout
        client_kwargs["max_retries"] = 0  # The outer loop owns observable, bounded retries.
        client = OpenAI(**client_kwargs)
        self._clients[resolved_name] = client
        if resolved_name == "default":
            self.client = client
        return client

    def _system_cache_key(self, system: str) -> str:
        return hashlib.sha256(system.encode("utf-8")).hexdigest()[:12]

    def _resolve_prompt_cache_strategy(self) -> str:
        strategy = str(getattr(config, "PROMPT_CACHE_STRATEGY", "none")).strip().lower()
        if strategy in {"none", "system_only", "system_and_last3"}:
            return strategy
        logger.warning("[PROMPT CACHE] invalid PROMPT_CACHE_STRATEGY=%s, fallback to none", strategy)
        return "none"

    def _resolve_provider_mode(self, model: str, provider_name: str | None = None) -> str:
        mode = str(getattr(config, "PROMPT_CACHE_PROVIDER_MODE", "auto")).strip().lower()
        if mode in {"none", "anthropic_ephemeral"}:
            return mode
        if mode != "auto":
            logger.warning("[PROMPT CACHE] invalid PROMPT_CACHE_PROVIDER_MODE=%s, fallback to none", mode)
            return "none"

        model_l = model.lower()
        _, provider_config = resolve_provider_config(provider_name)
        base_l = str(provider_config.get("base_url") or "").lower()
        if "claude" in model_l or "anthropic" in base_l:
            return "anthropic_ephemeral"
        return "none"

    def _select_cache_breakpoints(self, messages: list[dict], strategy: str) -> list[int]:
        if not messages or strategy == "none":
            return []
        points: list[int] = []
        if messages[0].get("role") == "system":
            points.append(0)
        if strategy != "system_and_last3":
            return points
        non_system_indices = [i for i, m in enumerate(messages) if m.get("role") != "system"]
        points.extend(non_system_indices[-3:])
        return sorted(set(points))

    def _apply_cache_marker_to_message(self, message: dict, marker: dict[str, str]) -> bool:
        if not isinstance(message, dict):
            return False
        message["cache_control"] = dict(marker)
        content = message.get("content")
        if isinstance(content, str):
            message["content"] = [{"type": "text", "text": content, "cache_control": dict(marker)}]
            return True
        if isinstance(content, list):
            for idx in range(len(content) - 1, -1, -1):
                block = content[idx]
                if isinstance(block, dict) and block.get("type") == "text":
                    block["cache_control"] = dict(marker)
                    return True
        return True

    def _apply_provider_prompt_cache(
        self,
        model: str,
        messages: list[dict],
        strategy: str,
        provider_name: str | None = None,
    ) -> tuple[list[dict], dict[str, Any]]:
        mode = self._resolve_provider_mode(model, provider_name=provider_name)
        breakpoints = self._select_cache_breakpoints(messages, strategy)
        info: dict[str, Any] = {
            "strategy": strategy,
            "mode": mode,
            "provider": str(provider_name or "default"),
            "breakpoints": breakpoints,
            "applied": False,
        }
        if mode == "none" or strategy == "none" or not breakpoints:
            return messages, info

        cached_messages = copy.deepcopy(messages)
        marker = {"type": "ephemeral"}
        applied = 0
        for idx in breakpoints:
            if 0 <= idx < len(cached_messages):
                if self._apply_cache_marker_to_message(cached_messages[idx], marker):
                    applied += 1
        info["applied"] = applied > 0
        info["applied_count"] = applied
        return cached_messages, info

    @staticmethod
    def _looks_like_cache_marker_error(err: Exception) -> bool:
        text = str(err).lower()
        signals = [
            "cache_control",
            "ephemeral",
            "invalid content type",
            "invalid type for 'messages",
        ]
        return any(s in text for s in signals)

    def _extract_response_usage(self, resp) -> dict[str, Any]:
        usage = getattr(resp, "usage", None)
        if usage is None:
            return {}
        if isinstance(usage, dict):
            usage_dict = dict(usage)
        elif hasattr(usage, "model_dump"):
            usage_dict = usage.model_dump()
        elif hasattr(usage, "__dict__"):
            usage_dict = dict(usage.__dict__)
        else:
            return {}

        cache_metrics: dict[str, Any] = {}
        prompt_details = usage_dict.get("prompt_tokens_details")
        if isinstance(prompt_details, dict) and prompt_details.get("cached_tokens") is not None:
            cache_metrics["cached_tokens"] = prompt_details.get("cached_tokens")
        for key in (
            "cache_creation_input_tokens",
            "cache_read_input_tokens",
            "input_cached_tokens",
            "cache_creation_tokens",
            "cache_read_tokens",
        ):
            if usage_dict.get(key) is not None:
                cache_metrics[key] = usage_dict.get(key)
        if cache_metrics:
            usage_dict["cache_metrics"] = cache_metrics
        return usage_dict

    def chat(
        self,
        model: str,
        system: str,
        messages: list[dict],
        tools: list[dict] | None = None,
        tool_choice: str = "auto",
        temperature: float = 0.0,
        max_tokens: int = 4096,
        phase: str | None = None,
        active_skills: list[str] | None = None,
        provider: str | None = None,
        role: str = "orchestrator",
    ) -> dict:
        full_messages = [{"role": "system", "content": system}] + messages
        provider_name = resolve_provider_route(
            role=role,
            phase=phase,
            provider=provider,
        )
        client = self._client_for_provider(provider_name)
        prompt_cache_strategy = self._resolve_prompt_cache_strategy()
        raw_messages = full_messages
        cached_messages, provider_cache_info = self._apply_provider_prompt_cache(
            model=model,
            messages=full_messages,
            strategy=prompt_cache_strategy,
            provider_name=provider_name,
        )
        effective_messages = cached_messages if provider_cache_info.get("applied") else raw_messages
        cache_slot = (provider_name, model, "chat")
        cache_key = self._system_cache_key(system)
        cache_hit = self._prompt_cache_slots.get(cache_slot) == cache_key
        self._prompt_cache_slots[cache_slot] = cache_key
        kwargs = dict(
            model=model,
            messages=effective_messages,
            temperature=temperature,
            max_tokens=max_tokens,
            extra_body=build_chat_extra_body(),
        )
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = tool_choice

        prompt_length_estimate = len(system) + len(_format_messages_for_log(messages))
        exposed_tools_count = len(tools) if tools else 0
        call_id = uuid4().hex[:12]
        logger.info(f"{'='*60}")
        logger.info(
            "[LLM CALL] call_id=%s role=%s provider=%s model=%s temperature=%s max_tokens=%s enable_thinking=%s phase=%s active_skills=%s exposed_tools_count=%s prompt_length_estimate=%s",
            call_id,
            role,
            provider_name,
            model,
            temperature,
            max_tokens,
            _extra_body_thinking_enabled(kwargs["extra_body"]),
            phase or "n/a",
            active_skills or [],
            exposed_tools_count,
            prompt_length_estimate,
        )
        logger.info(
            "[PROMPT CACHE] slot=%s cache_key=%s hit=%s",
            cache_slot,
            cache_key,
            cache_hit,
        )
        logger.info(
            "[PROMPT CACHE PROVIDER] mode=%s strategy=%s applied=%s breakpoints=%s ttl=%s",
            provider_cache_info.get("mode"),
            prompt_cache_strategy,
            provider_cache_info.get("applied", False),
            provider_cache_info.get("breakpoints", []),
            getattr(config, "PROMPT_CACHE_TTL", "5m"),
        )
        logger.debug("[LLM SYSTEM PROMPT]\n%s", redact_text(system, key_hint="llm.system_prompt"))
        logger.debug(f"[LLM USER MESSAGES]\n{_format_messages_for_log(messages)}")
        if tools:
            logger.debug(
                "[LLM TOOLS]\n%s",
                redact_text(
                    json.dumps(tools, ensure_ascii=False, indent=2),
                    key_hint="llm.tools",
                ),
            )

        provider_fallback_done = False
        call_started_wall = datetime.now().astimezone()
        call_started_at = time.perf_counter()
        for attempt in range(self.max_retries):
            try:
                with request_event(role=role, model=model, call_id=call_id, attempt=attempt + 1, stage=phase or ""):
                    resp = client.chat.completions.create(**kwargs)
                result = self._parse_response(resp)
                latency_sec = _attach_call_timing(
                    result,
                    call_id=call_id,
                    started_at=call_started_wall,
                    started_perf=call_started_at,
                    attempt_count=attempt + 1,
                )
                record_usage(
                    call_id=call_id, attempt=attempt + 1, model=model, role=role,
                    provider=provider_name, usage=result.get("usage"), status="success",
                    phase=phase or "", output_chars=len(str(result.get("content") or "")),
                    latency_seconds=latency_sec,
                )
                _log_llm_response(
                    result,
                    call_id=call_id,
                    role=role,
                    provider=provider_name,
                    phase=phase,
                    latency_sec=latency_sec,
                )
                cache_metrics = result.get("usage", {}).get("cache_metrics")
                if cache_metrics:
                    logger.info("[PROMPT CACHE PROVIDER] usage=%s", cache_metrics)
                else:
                    logger.info("[PROMPT CACHE PROVIDER] usage=unavailable")
                logger.info(f"{'='*60}")
                return result
            except Exception as e:
                record_usage(
                    call_id=call_id, attempt=attempt + 1, model=model, role=role,
                    provider=provider_name, usage=None, status="failed",
                    phase=phase or "", error_type=type(e).__name__,
                )
                if (
                    not provider_fallback_done
                    and provider_cache_info.get("applied")
                    and self._looks_like_cache_marker_error(e)
                ):
                    provider_fallback_done = True
                    kwargs["messages"] = raw_messages
                    logger.warning(
                        "[PROMPT CACHE PROVIDER] provider rejected cache markers, fallback to plain messages: %s",
                        e,
                    )
                    continue
                logger.warning(f"LLM call attempt {attempt+1} failed: {e}")
                if attempt < self.max_retries - 1:
                    time.sleep(2 ** (attempt + 1))
                else:
                    raise

    def chat_with_vision(
        self,
        model: str,
        system: str,
        messages: list[dict],
        temperature: float = 0.0,
        top_p: float | None = None,
        max_tokens: int = 4096,
        extra_body: dict[str, Any] | None = None,
        request_kwargs: dict[str, Any] | None = None,
        phase: str | None = None,
        active_skills: list[str] | None = None,
        provider: str | None = None,
        role: str = "vla",
    ) -> dict:
        full_messages = [{"role": "system", "content": system}] + messages
        provider_name = resolve_provider_route(
            role=role,
            phase=phase,
            provider=provider,
        )
        client = self._client_for_provider(provider_name)
        resolved_extra_body = (
            build_chat_extra_body()
            if extra_body is None
            else dict(extra_body)
        )
        # Some providers only accept certain params via extra_body (e.g. top_k).
        # Move them from request_kwargs into extra_body to avoid top-level rejection.
        _extra_body_keys = {"top_k"}
        if request_kwargs:
            for _k in _extra_body_keys:
                _v = request_kwargs.get(_k)
                if _v is not None:
                    resolved_extra_body[_k] = _v
        kwargs = dict(
            model=model,
            messages=full_messages,
            temperature=temperature,
            max_tokens=max_tokens,
            extra_body=resolved_extra_body,
        )
        if top_p is not None:
            kwargs["top_p"] = top_p
        if request_kwargs:
            kwargs.update({k: v for k, v in request_kwargs.items()
                           if v is not None and k not in _extra_body_keys})

        prompt_length_estimate = len(system) + len(_format_messages_for_log(messages))
        call_id = uuid4().hex[:12]
        logger.info(f"{'='*60}")
        logger.info(
            "[LLM CALL] call_id=%s role=%s provider=%s model=%s (vision) temperature=%s top_p=%s max_tokens=%s enable_thinking=%s phase=%s active_skills=%s exposed_tools_count=%s prompt_length_estimate=%s",
            call_id,
            role,
            provider_name,
            model,
            temperature,
            top_p,
            max_tokens,
            _extra_body_thinking_enabled(resolved_extra_body),
            phase or "n/a",
            active_skills or [],
            0,
            prompt_length_estimate,
        )
        logger.debug("[LLM SYSTEM PROMPT]\n%s", redact_text(system, key_hint="llm.system_prompt"))
        logger.debug(f"[LLM USER MESSAGES]\n{_format_messages_for_log(messages)}")
        call_started_wall = datetime.now().astimezone()
        call_started_at = time.perf_counter()
        for attempt in range(self.max_retries):
            try:
                with request_event(role=role, model=model, call_id=call_id, attempt=attempt + 1, stage=phase or ""):
                    resp = client.chat.completions.create(**kwargs)
                result = self._parse_response(resp)
                latency_sec = _attach_call_timing(
                    result,
                    call_id=call_id,
                    started_at=call_started_wall,
                    started_perf=call_started_at,
                    attempt_count=attempt + 1,
                )
                record_usage(
                    call_id=call_id, attempt=attempt + 1, model=model, role=role,
                    provider=provider_name, usage=result.get("usage"), status="success",
                    phase=phase or "", output_chars=len(str(result.get("content") or "")),
                    latency_seconds=latency_sec,
                )
                _log_llm_response(
                    result,
                    call_id=call_id,
                    role=role,
                    provider=provider_name,
                    phase=phase,
                    latency_sec=latency_sec,
                )
                logger.info(f"{'='*60}")
                return result
            except Exception as e:
                record_usage(
                    call_id=call_id, attempt=attempt + 1, model=model, role=role,
                    provider=provider_name, usage=None, status="failed",
                    phase=phase or "", error_type=type(e).__name__,
                )
                logger.warning(f"Vision call attempt {attempt+1} failed: {e}")
                if attempt < self.max_retries - 1:
                    time.sleep(2 ** (attempt + 1))
                else:
                    raise

    def _parse_response(self, resp) -> dict:
        choice = resp.choices[0]
        msg = choice.message
        content = msg.content or ""
        result = {
            "finish_reason": choice.finish_reason,
            "content": content,
            "reasoning_content": extract_message_reasoning(msg, content=content),
            "tool_calls": [],
            "usage": self._extract_response_usage(resp),
        }
        if msg.tool_calls:
            for tc in msg.tool_calls:
                raw_arguments = tc.function.arguments
                try:
                    parsed = json.loads(raw_arguments)
                    if isinstance(parsed, dict):
                        parsed_arguments = parsed
                    else:
                        parsed_arguments = {
                            "__arguments_parse_error__": "arguments must decode to object",
                            "__raw_arguments__": raw_arguments,
                        }
                except Exception as exc:
                    parsed_arguments = {
                        "__arguments_parse_error__": str(exc),
                        "__raw_arguments__": raw_arguments,
                    }
                result["tool_calls"].append({
                    "id": tc.id,
                    "name": tc.function.name,
                    "arguments": parsed_arguments,
                })
        return result


def encode_image_base64(image_path: str) -> str:
    with open(image_path, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")


def make_image_content(b64_data: str, mime: str = "jpeg") -> dict:
    safe_mime = str(mime or "jpeg").strip().lower() or "jpeg"
    return {
        "type": "image_url",
        "image_url": {"url": f"data:image/{safe_mime};base64,{b64_data}"},
    }
