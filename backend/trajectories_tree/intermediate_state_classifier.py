"""Qwen vision pass shared by tree filtering and AdaRubric observations."""
from __future__ import annotations

import base64, hashlib, io, json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
import httpx
from openai import OpenAI
from PIL import Image

try:
    from ..bounding_box.qwen_reviewer import qwen_settings
except ImportError:
    from bounding_box.qwen_reviewer import qwen_settings

CATEGORIES = {"advertisement", "loading", "update_popup", "permission_privacy_popup", "transient_error", "system_overlay", "none"}
PROMPT_VERSION = "trajectory-intermediate-observation-v4"
LEGACY_PROMPT_VERSION = "trajectory-intermediate-state-v2"

@dataclass
class IntermediateStateResult:
    is_intermediate: bool
    category: str
    confidence: float
    reason: str
    raw_response: str
    cached: bool = False
    observation: str = ""
    classification_changed: bool = False

    def to_dict(self, confidence_threshold: float) -> dict[str, Any]:
        value = asdict(self)
        value["effective_intermediate"] = bool(self.is_intermediate and self.confidence >= confidence_threshold)
        value["uncertain"] = bool(self.is_intermediate and self.confidence < confidence_threshold)
        value["confidence_threshold"] = confidence_threshold
        return value

def parse_classification_response(raw: str, *, require_observation: bool = False) -> IntermediateStateResult:
    start = raw.strip().find("{")
    if start < 0:
        raise ValueError(f"Qwen response does not contain JSON: {raw[:300]!r}")
    try:
        value, _ = json.JSONDecoder().raw_decode(raw.strip()[start:])
    except json.JSONDecodeError as exc:
        raise ValueError(f"Qwen response contains invalid JSON: {raw[:300]!r}") from exc
    if not isinstance(value, dict) or not isinstance(value.get("is_intermediate"), bool):
        raise ValueError("Qwen classification must contain boolean is_intermediate")
    category = str(value.get("category", "")).strip().lower()
    if category not in CATEGORIES:
        raise ValueError(f"invalid intermediate-state category: {category!r}")
    if value["is_intermediate"] and category == "none":
        raise ValueError("intermediate state must use a non-none category")
    if not value["is_intermediate"] and category != "none":
        raise ValueError("stable state must use category='none'")
    confidence = float(value.get("confidence", 0.0))
    if not 0 <= confidence <= 1:
        raise ValueError("confidence must be between 0 and 1")
    reason = str(value.get("reason", "")).strip()
    observation = str(value.get("observation", "")).strip()
    if not reason:
        raise ValueError("classification reason must not be empty")
    if require_observation and not observation:
        raise ValueError("observation must not be empty")
    return IntermediateStateResult(bool(value["is_intermediate"]), category, confidence, reason, raw, False, observation)

def _image_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()

def _image_data_url(path: Path) -> str:
    with Image.open(path) as opened:
        image = opened.convert("RGB")
    image.thumbnail((1080, 1920), Image.Resampling.LANCZOS)
    output = io.BytesIO(); image.save(output, format="JPEG", quality=88, optimize=True)
    return "data:image/jpeg;base64," + base64.b64encode(output.getvalue()).decode("ascii")

class QwenIntermediateStateClassifier:
    def __init__(self, model: str, cache_path: Path) -> None:
        settings = qwen_settings("tree")
        http_client = httpx.Client(proxy=settings["proxy"] or None, verify=settings["verify"], timeout=settings["timeout"], trust_env=settings["trust_env"])
        self.client = OpenAI(api_key=settings["api_key"] or "EMPTY", base_url=settings["base_url"], timeout=settings["timeout"], max_retries=settings["max_retries"], http_client=http_client)
        self.model, self.cache_path = model, cache_path
        self.cache: dict[str, Any] = json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.exists() else {}
        if not isinstance(self.cache, dict):
            raise ValueError(f"classification cache must be a JSON object: {cache_path}")

    def _save_cache(self) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.cache_path.with_name(f".{self.cache_path.name}.tmp")
        temporary.write_text(json.dumps(self.cache, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.cache_path)

    @staticmethod
    def _key(payload: dict[str, Any]) -> str:
        return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()

    def classify(self, *, trajectory: str, step_index: int, current_image_path: Path,
                 next_image_path: Path | None, action: dict[str, Any], summary: str,
                 previous_summary: str, next_summary: str, after_image_path: Path | None = None,
                 task: str = "") -> IntermediateStateResult:
        after = after_image_path or next_image_path or current_image_path
        common = {"model": self.model, "trajectory": trajectory, "step_index": step_index,
                  "current_image_digest": _image_digest(current_image_path),
                  "next_image_digest": _image_digest(next_image_path) if next_image_path else "",
                  "action": action, "summary": summary, "previous_summary": previous_summary,
                  "next_summary": next_summary}
        legacy = self.cache.get(self._key({"version": LEGACY_PROMPT_VERSION, **common}))
        legacy_result = parse_classification_response(legacy) if isinstance(legacy, str) else None
        payload = {"version": PROMPT_VERSION, **common, "task": task, "after_image_digest": _image_digest(after)}
        cache_key = self._key(payload)
        cached = cache_key in self.cache
        if cached:
            item = self.cache[cache_key]
            raw = str(item.get("raw_response", "")) if isinstance(item, dict) else str(item)
        else:
            observation_action = {"action": "terminate"} if action.get("action") == "terminate" else action
            prompt = f"""同时完成两项工作。

第一，判断 Action 执行前的 Before 页面是否为会造成轨迹树偶发分叉的临时中间态。中间态包括广告、加载、升级/权限/隐私弹窗、临时错误和系统遮罩；正常业务页面和完成任务所需的业务弹窗不是中间态。

第二，你是 GUI Agent 轨迹的 Post-action Observation 生成器。根据 Task、Action、Before Screenshot、After Screenshot，用一句中文描述：Action 执行后，两张截图之间与 Task 相关的可见环境状态变化。

Observation 核心规则：
1. Task 只用于决定关注哪些视觉信息，不用于判断任务是否完成。
2. 优先描述 Before → After 的明确变化，包括页面、目标实体、按钮、选中状态、列表、弹窗、加载等。
3. 如果任务相关状态没有明显变化，如实说明“未观察到变化”及仍保持的关键状态。
4. 如果关键区域看不清、被遮挡或结果无法确定，明确写“无法确认”，不要猜测。
5. 只描述视觉事实及前后差异，不解释这些变化意味着什么。描述完变化后立即结束。

Observation 禁止进行成功/失败判断、任务完成判断、Action 正误判断、下一步规划、因果或业务含义解释。禁止使用“表明”“说明”“意味着”“成功”“操作已完成”“任务已完成”“操作已生效”“可以继续”“下一步”等表达。

Task：{task}
轨迹：{trajectory}
步骤：{step_index}
分类 Action：{json.dumps(action, ensure_ascii=False)}
Observation action：{json.dumps(observation_action, ensure_ascii=False)}
当前摘要：{summary or '无'}
上一摘要：{previous_summary or '无'}
下一摘要：{next_summary or '无'}

只输出严格 JSON，不输出分析、Markdown 或额外解释：
{{"is_intermediate":true|false,"category":"advertisement|loading|update_popup|permission_privacy_popup|transient_error|system_overlay|none","confidence":0.0,"reason":"简短中文理由","observation":"一句中文 Observation"}}"""
            content = [{"type": "text", "text": prompt}, {"type": "text", "text": "Before Screenshot："},
                       {"type": "image_url", "image_url": {"url": _image_data_url(current_image_path)}},
                       {"type": "text", "text": "After Screenshot："},
                       {"type": "image_url", "image_url": {"url": _image_data_url(after)}}]
            response = self.client.chat.completions.create(model=self.model,
                messages=[{"role": "system", "content": "你是移动端 GUI 轨迹清洗和视觉 Observation 生成器，严格返回一个 JSON 对象。"}, {"role": "user", "content": content}],
                temperature=0.0, max_tokens=700,
                extra_body={"enable_thinking": False, "chat_template_kwargs": {"enable_thinking": False}})
            message = response.choices[0].message
            raw = (message.content or getattr(message, "reasoning_content", None) or getattr(message, "reasoning", None) or "").strip()
            parse_classification_response(raw, require_observation=True)
            self.cache[cache_key] = {"version": PROMPT_VERSION, "raw_response": raw}
            self._save_cache()
        result = parse_classification_response(raw, require_observation=True)
        if legacy_result:
            result.classification_changed = (result.is_intermediate, result.category, result.confidence) != (legacy_result.is_intermediate, legacy_result.category, legacy_result.confidence)
            result.is_intermediate, result.category, result.confidence, result.reason = legacy_result.is_intermediate, legacy_result.category, legacy_result.confidence, legacy_result.reason
        result.cached = cached
        return result
