"""Qwen vision classifier for transient GUI states that should not branch a tree."""

from __future__ import annotations

import base64
import hashlib
import io
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import httpx
from openai import OpenAI
from PIL import Image

try:
    from ..bounding_box.qwen_reviewer import qwen_settings
except ImportError:  # Keep direct script imports working.
    from bounding_box.qwen_reviewer import qwen_settings


CATEGORIES = {
    "advertisement",
    "loading",
    "update_popup",
    "permission_privacy_popup",
    "transient_error",
    "system_overlay",
    "none",
}
PROMPT_VERSION = "trajectory-intermediate-state-v2"


@dataclass
class IntermediateStateResult:
    is_intermediate: bool
    category: str
    confidence: float
    reason: str
    raw_response: str
    cached: bool = False

    def to_dict(self, confidence_threshold: float) -> dict[str, Any]:
        value = asdict(self)
        value["effective_intermediate"] = bool(
            self.is_intermediate and self.confidence >= confidence_threshold
        )
        value["uncertain"] = bool(
            self.is_intermediate and self.confidence < confidence_threshold
        )
        value["confidence_threshold"] = confidence_threshold
        return value


def parse_classification_response(raw: str) -> IntermediateStateResult:
    text = raw.strip()
    object_start = text.find("{")
    if object_start < 0:
        raise ValueError(f"Qwen response does not contain JSON: {raw[:300]!r}")
    try:
        value, _ = json.JSONDecoder().raw_decode(text[object_start:])
    except json.JSONDecodeError as exc:
        raise ValueError(f"Qwen response contains invalid JSON: {raw[:300]!r}") from exc
    if not isinstance(value, dict):
        raise ValueError("Qwen classification must be a JSON object")

    is_intermediate = value.get("is_intermediate")
    if not isinstance(is_intermediate, bool):
        raise ValueError(f"is_intermediate must be boolean, got {is_intermediate!r}")
    category = str(value.get("category", "")).strip().lower()
    if category not in CATEGORIES:
        raise ValueError(f"invalid intermediate-state category: {category!r}")
    if is_intermediate and category == "none":
        raise ValueError("intermediate state must use a non-none category")
    if not is_intermediate and category != "none":
        raise ValueError("stable state must use category='none'")
    confidence = float(value.get("confidence", 0.0))
    if not 0.0 <= confidence <= 1.0:
        raise ValueError(f"confidence must be between 0 and 1, got {confidence!r}")
    reason = str(value.get("reason", "")).strip()
    if not reason:
        raise ValueError("classification reason must not be empty")
    return IntermediateStateResult(
        is_intermediate=is_intermediate,
        category=category,
        confidence=confidence,
        reason=reason,
        raw_response=raw,
    )


def _image_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _image_data_url(path: Path) -> str:
    with Image.open(path) as opened:
        image = opened.convert("RGB")
    image.thumbnail((1080, 1920), Image.Resampling.LANCZOS)
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=88, optimize=True)
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f"data:image/jpeg;base64,{encoded}"


class QwenIntermediateStateClassifier:
    def __init__(self, model: str, cache_path: Path) -> None:
        settings = qwen_settings()
        http_client = httpx.Client(
            proxy=settings["proxy"] or None,
            verify=settings["verify"],
            timeout=settings["timeout"],
            trust_env=settings["trust_env"],
        )
        self.client = OpenAI(
            api_key=settings["api_key"],
            base_url=settings["base_url"],
            timeout=settings["timeout"],
            max_retries=0,
            http_client=http_client,
        )
        self.model = model
        self.cache_path = cache_path
        self.cache: dict[str, str] = {}
        if cache_path.exists():
            payload = json.loads(cache_path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError(f"classification cache must be a JSON object: {cache_path}")
            self.cache = {str(key): str(value) for key, value in payload.items()}

    def _save_cache(self) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = self.cache_path.with_name(f".{self.cache_path.name}.tmp")
        temporary_path.write_text(
            json.dumps(self.cache, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temporary_path.replace(self.cache_path)

    def classify(
        self,
        *,
        trajectory: str,
        step_index: int,
        current_image_path: Path,
        next_image_path: Path | None,
        action: dict[str, Any],
        summary: str,
        previous_summary: str,
        next_summary: str,
    ) -> IntermediateStateResult:
        current_digest = _image_digest(current_image_path)
        next_digest = _image_digest(next_image_path) if next_image_path is not None else ""
        cache_payload = {
            "version": PROMPT_VERSION,
            "model": self.model,
            "trajectory": trajectory,
            "step_index": step_index,
            "current_image_digest": current_digest,
            "next_image_digest": next_digest,
            "action": action,
            "summary": summary,
            "previous_summary": previous_summary,
            "next_summary": next_summary,
        }
        cache_key = hashlib.sha256(
            json.dumps(cache_payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        cached = cache_key in self.cache
        if cached:
            raw = self.cache[cache_key]
        else:
            prompt = (
                "你正在判断移动端 GUI 轨迹中的当前页面是否只是会造成轨迹树偶发分叉的临时中间状态。\n"
                "中间状态包括：开屏/插屏/全屏广告、加载或骨架屏、版本升级提示、权限/隐私/营销弹窗、"
                "临时网络错误、系统遮罩或其他与用户任务无关且很快消失的阻塞状态。\n"
                "不要把正常业务页面、搜索页、结果页、详情页、播放页，或完成任务本身需要操作的业务弹窗判为中间状态。\n"
                "请保守判断；如果证据不足，应判为正常状态。下一张截图仅用于确认当前状态是否短暂消失。\n\n"
                f"轨迹：{trajectory}\n"
                f"步骤：{step_index}\n"
                f"当前 action：{json.dumps(action, ensure_ascii=False)}\n"
                f"当前摘要：{summary or '未提供'}\n"
                f"上一摘要：{previous_summary or '无'}\n"
                f"下一摘要：{next_summary or '无'}\n\n"
                "只输出严格 JSON，不要输出 Markdown："
                '{"is_intermediate":true|false,'
                '"category":"advertisement|loading|update_popup|permission_privacy_popup|transient_error|system_overlay|none",'
                '"confidence":0.0,"reason":"简短中文理由"}'
            )
            content: list[dict[str, Any]] = [
                {"type": "text", "text": prompt},
                {"type": "text", "text": "当前步骤截图："},
                {
                    "type": "image_url",
                    "image_url": {"url": _image_data_url(current_image_path)},
                },
            ]
            if next_image_path is not None:
                content.extend([
                    {"type": "text", "text": "下一步骤截图："},
                    {
                        "type": "image_url",
                        "image_url": {"url": _image_data_url(next_image_path)},
                    },
                ])
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "你是移动端 GUI 轨迹清洗审核器，负责识别不应形成任务树分叉的临时页面状态，"
                            "并严格返回一个 JSON 对象。"
                        ),
                    },
                    {"role": "user", "content": content},
                ],
                temperature=0.0,
                max_tokens=350,
                extra_body={
                    "enable_thinking": False,
                    "chat_template_kwargs": {"enable_thinking": False},
                },
            )
            message = response.choices[0].message
            raw = (
                message.content
                or getattr(message, "reasoning_content", None)
                or getattr(message, "reasoning", None)
                or ""
            ).strip()
            if not raw:
                raise ValueError("Qwen returned an empty intermediate-state classification")
            # Validate before caching so a malformed response never poisons retries.
            parse_classification_response(raw)
            self.cache[cache_key] = raw
            self._save_cache()

        result = parse_classification_response(raw)
        result.cached = cached
        return result
