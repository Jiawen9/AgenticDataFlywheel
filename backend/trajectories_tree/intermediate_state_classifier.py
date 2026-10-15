"""Qwen vision pass shared by tree filtering and AdaRubric observations."""
from __future__ import annotations

import base64, hashlib, io, json
import re
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from backend.file_io import io_path
from backend.pipeline_retry_errors import ModelOutputError
from backend.trajectories_tree.model_response import response_text

from typing import Any
import httpx
from backend.pipeline_diagnostic_http import instrument_http_client, model_operation
from openai import OpenAI
from PIL import Image

try:
    from ..bounding_box.qwen_reviewer import qwen_settings
except ImportError:
    from bounding_box.qwen_reviewer import qwen_settings

CATEGORIES = {"advertisement", "loading", "update_popup", "permission_privacy_popup", "transient_error", "system_overlay", "none"}
PROMPT_VERSION = "trajectory-intermediate-observation-v5"
PREVIOUS_PROMPT_VERSION = "trajectory-intermediate-observation-v4"
LEGACY_PROMPT_VERSION = "trajectory-intermediate-state-v2"
SUPPORTED_PROMPT_VERSIONS = {PROMPT_VERSION, PREVIOUS_PROMPT_VERSION, LEGACY_PROMPT_VERSION}

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
    classification_source: dict[str, str] | None = None
    observation_source: dict[str, str] | None = None

    def to_dict(self, confidence_threshold: float) -> dict[str, Any]:
        value = asdict(self)
        value["effective_intermediate"] = bool(self.is_intermediate and self.confidence >= confidence_threshold)
        value["uncertain"] = bool(self.is_intermediate and self.confidence < confidence_threshold)
        value["confidence_threshold"] = confidence_threshold
        return value

def parse_classification_response(raw: str, *, require_observation: bool = False,
                                  protocol_version: str = PROMPT_VERSION) -> IntermediateStateResult:
    if not isinstance(protocol_version, str) or protocol_version not in SUPPORTED_PROMPT_VERSIONS:
        raise ValueError("unsupported classification protocol")
    if not isinstance(raw, str):
        raise ModelOutputError("invalid_content")
    start = raw.strip().find("{")
    if start < 0:
        raise ModelOutputError("invalid_json")
    try:
        value, _ = json.JSONDecoder().raw_decode(raw.strip()[start:])
    except json.JSONDecodeError as exc:
        raise ModelOutputError("invalid_json") from exc
    if not isinstance(value, dict) or not isinstance(value.get("category"), str):
        raise ModelOutputError("invalid_schema")
    category = value["category"].strip().lower()
    if category not in CATEGORIES:
        raise ModelOutputError("invalid_schema")
    is_intermediate = category != "none"
    if protocol_version != PROMPT_VERSION:
        # Old responses must satisfy the old contract before they can be reused.
        # Never reinterpret contradictory legacy fields using the v5 contract.
        if not isinstance(value.get("is_intermediate"), bool):
            raise ModelOutputError("invalid_schema")
        if value["is_intermediate"] != is_intermediate:
            raise ModelOutputError("inconsistent_fields")
    confidence_value = value.get("confidence")
    if isinstance(confidence_value, bool):
        raise ModelOutputError("invalid_schema")
    try:
        confidence = float(confidence_value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ModelOutputError("invalid_schema") from exc
    if not 0 <= confidence <= 1:
        raise ModelOutputError("invalid_schema")
    reason = value.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise ModelOutputError("invalid_schema")
    reason = reason.strip()
    observation = value.get("observation", "")
    if not require_observation and observation is None:
        observation = ""
    if not isinstance(observation, str):
        raise ModelOutputError("invalid_schema")
    observation = observation.strip()
    if require_observation and not observation:
        raise ModelOutputError("missing_observation")
    return IntermediateStateResult(is_intermediate, category, confidence, reason, raw, False, observation)


def _normalized_result(result: IntermediateStateResult) -> dict[str, Any]:
    return {"category": result.category, "confidence": result.confidence,
            "reason": result.reason, "observation": result.observation}


def parse_cached_classification(item: Any, *, version: str = PROMPT_VERSION) -> IntermediateStateResult:
    """Read each cache using its own contract, including the effective v5 result."""
    if version != PROMPT_VERSION:
        if isinstance(item, dict) and item.get("version", version) != version:
            raise ModelOutputError("invalid_schema")
        raw = item.get("raw_response") if isinstance(item, dict) else item
        result = parse_classification_response(raw, require_observation=version != LEGACY_PROMPT_VERSION,
                                               protocol_version=version)
    else:
        if (not isinstance(item, dict) or item.get("version") != PROMPT_VERSION
                or not isinstance(item.get("normalized_result"), dict)
                or not isinstance(item.get("raw_response"), str) or not item["raw_response"].strip()):
            raise ModelOutputError("invalid_schema")
        result = parse_classification_response(json.dumps(item["normalized_result"], ensure_ascii=False),
                                               require_observation=True)
        for name in ("classification_source", "observation_source"):
            source = item.get(name)
            if (not isinstance(source, dict) or not isinstance(source.get("version"), str)
                    or source["version"] not in SUPPORTED_PROMPT_VERSIONS
                    or not isinstance(source.get("cache_key"), str)
                    or re.fullmatch(r"[a-f0-9]{64}", source["cache_key"]) is None
                    or (name == "observation_source" and source["version"] == LEGACY_PROMPT_VERSION)):
                raise ModelOutputError("invalid_schema")
            setattr(result, name, {"version": source["version"], "cache_key": source["cache_key"]})
        result.raw_response = item["raw_response"]
        result.classification_changed = item.get("classification_changed", False)
        if not isinstance(result.classification_changed, bool):
            raise ModelOutputError("invalid_schema")
    result.cached = True
    return result

def _image_digest(path: Path) -> str:
    return hashlib.sha256(io_path(path).read_bytes()).hexdigest()

def _image_data_url(path: Path) -> str:
    with Image.open(io_path(path)) as opened:
        image = opened.convert("RGB")
    image.thumbnail((1080, 1920), Image.Resampling.LANCZOS)
    output = io.BytesIO(); image.save(output, format="JPEG", quality=88, optimize=True)
    return "data:image/jpeg;base64," + base64.b64encode(output.getvalue()).decode("ascii")

class QwenIntermediateStateClassifier:
    def __init__(self, model: str, cache_path: Path) -> None:
        settings = qwen_settings()
        http_client = httpx.Client(proxy=settings["proxy"] or None, verify=settings["verify"], timeout=settings["timeout"], trust_env=settings["trust_env"])
        instrument_http_client(http_client, api_key=settings["api_key"], settings={
            "proxy": settings["proxy"], "trust_env": settings["trust_env"], "sdk_retries": 0})
        self.client = OpenAI(api_key=settings["api_key"], base_url=settings["base_url"], timeout=settings["timeout"], max_retries=0, http_client=http_client)
        self.model, self.cache_path = model, cache_path
        self.cache: dict[str, Any] = json.loads(io_path(cache_path).read_text(encoding="utf-8")) if io_path(cache_path).exists() else {}
        if not isinstance(self.cache, dict):
            raise ValueError(f"classification cache must be a JSON object: {cache_path}")
        self._cache_lock = threading.RLock()

    def _save_cache_unlocked(self) -> None:
        io_path(self.cache_path.parent).mkdir(parents=True, exist_ok=True)
        temporary = self.cache_path.with_name(f".{self.cache_path.name}.tmp")
        io_path(temporary).write_text(json.dumps(self.cache, ensure_ascii=False, indent=2), encoding="utf-8")
        io_path(temporary).replace(io_path(self.cache_path))

    def _save_cache(self) -> None:
        with self._cache_lock:
            self._save_cache_unlocked()

    @staticmethod
    def _key(payload: dict[str, Any]) -> str:
        return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()

    def _cached_result(self, cache_key: str, version: str) -> IntermediateStateResult | None:
        with self._cache_lock:
            item = self.cache.get(cache_key)
        if item is None:
            return None
        try:
            return parse_cached_classification(item, version=version)
        except ModelOutputError:
            return None

    def _store_result(self, cache_key: str, result: IntermediateStateResult) -> IntermediateStateResult:
        entry = {"version": PROMPT_VERSION, "normalized_result": _normalized_result(result),
                 # This is the unmodified response that supplied Observation. In
                 # a legacy combination its classification may differ; the two
                 # source references and normalized result make that explicit.
                 "raw_response": result.raw_response,
                 "classification_source": result.classification_source,
                 "observation_source": result.observation_source,
                 "classification_changed": result.classification_changed}
        parse_cached_classification(entry)
        with self._cache_lock:
            existing = self._cached_result(cache_key, PROMPT_VERSION)
            if existing is not None:
                return existing
            missing = object()
            previous = self.cache.get(cache_key, missing)
            self.cache[cache_key] = entry
            try:
                self._save_cache_unlocked()
            except Exception:
                # A failed disk write must not masquerade as a cache hit on the
                # next attempt in this process. Other entries remain untouched.
                if previous is missing:
                    self.cache.pop(cache_key, None)
                else:
                    self.cache[cache_key] = previous
                raise
        return result

    @model_operation("intermediate_classify")
    def classify(self, *, trajectory: str, step_index: int, current_image_path: Path,
                 next_image_path: Path | None, action: dict[str, Any], summary: str,
                 previous_summary: str, next_summary: str, after_image_path: Path | None = None,
                 task: str = "") -> IntermediateStateResult:
        # A few tests construct this class with ``object.__new__``; keep that
        # lightweight fixture compatible with the production lock.
        if not hasattr(self, "_cache_lock"):
            self._cache_lock = threading.RLock()
        after = after_image_path or next_image_path or current_image_path
        common = {"model": self.model, "trajectory": trajectory, "step_index": step_index,
                  "current_image_digest": _image_digest(current_image_path),
                  "next_image_digest": _image_digest(next_image_path) if next_image_path else "",
                  "action": action, "summary": summary, "previous_summary": previous_summary,
                  "next_summary": next_summary}
        combined = {**common, "task": task, "after_image_digest": _image_digest(after)}
        cache_key = self._key({"version": PROMPT_VERSION, **combined})
        current = self._cached_result(cache_key, PROMPT_VERSION)
        if current is not None:
            return current

        legacy_key = self._key({"version": LEGACY_PROMPT_VERSION, **common})
        previous_key = self._key({"version": PREVIOUS_PROMPT_VERSION, **combined})
        legacy_result = self._cached_result(legacy_key, LEGACY_PROMPT_VERSION)
        result = self._cached_result(previous_key, PREVIOUS_PROMPT_VERSION)
        if result is not None:
            result.classification_source = {"version": PREVIOUS_PROMPT_VERSION, "cache_key": previous_key}
            result.observation_source = dict(result.classification_source)
        else:
            observation_action = {"action": "terminate"} if action.get("action") == "terminate" else action
            prompt = f"""同时完成两项工作。

第一，判断 Action 执行前的 Before 页面是否为会造成轨迹树偶发分叉的临时中间态。中间态包括广告、加载、升级/权限/隐私弹窗、临时错误和系统遮罩；正常业务页面和完成任务所需的业务弹窗不是中间态。

分类只返回一个 category，不返回 is_intermediate；是否中间态由程序根据 category 推导。
category 必须是以下七个字符串之一：none、advertisement、loading、update_popup、permission_privacy_popup、transient_error、system_overlay。
正常业务页面和完成任务必需的业务弹窗必须使用 none；临时中间态选择一个最符合的具体类别，不返回多个类别、null 或空字符串。
confidence 表示对此类别判断的置信度，取 0 到 1；看不清或证据不足时降低 confidence，并在 reason 中如实说明。

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

只输出包含 category、confidence、reason、observation 的严格 JSON，不输出分析、Markdown 或额外解释。
正常页面示例（仅说明格式，内容必须根据当前截图填写）：
{{"category":"none","confidence":0.96,"reason":"动作前是正常商品列表页","observation":"点击后页面从商品列表切换为商品详情。"}}
临时页面示例（仅说明格式，内容必须根据当前截图填写）：
{{"category":"advertisement","confidence":0.94,"reason":"动作前有遮挡业务页面的偶发广告","observation":"点击后广告弹窗消失，露出商品列表。"}}"""
            content = [{"type": "text", "text": prompt}, {"type": "text", "text": "Before Screenshot："},
                       {"type": "image_url", "image_url": {"url": _image_data_url(current_image_path)}},
                       {"type": "text", "text": "After Screenshot："},
                       {"type": "image_url", "image_url": {"url": _image_data_url(after)}}]
            response = self.client.chat.completions.create(model=self.model,
                messages=[{"role": "system", "content": "你是移动端 GUI 轨迹清洗和视觉 Observation 生成器，严格返回一个 JSON 对象。"}, {"role": "user", "content": content}],
                temperature=0.0, max_tokens=2048 if self.model.strip().lower() == "qwen3.8-max" else 700,
                extra_body={"enable_thinking": False, "chat_template_kwargs": {"enable_thinking": False}},
                **({"response_format": {"type": "json_object"}} if self.model.strip().lower() == "qwen3.8-max" else {}))
            raw = response_text(response)
            result = parse_classification_response(raw, require_observation=True)
            result.classification_source = {"version": PROMPT_VERSION, "cache_key": cache_key}
            result.observation_source = dict(result.classification_source)
        if legacy_result:
            result.classification_changed = (result.is_intermediate, result.category, result.confidence) != (legacy_result.is_intermediate, legacy_result.category, legacy_result.confidence)
            result.is_intermediate, result.category, result.confidence, result.reason = legacy_result.is_intermediate, legacy_result.category, legacy_result.confidence, legacy_result.reason
            result.classification_source = {"version": LEGACY_PROMPT_VERSION, "cache_key": legacy_key}
        return self._store_result(cache_key, result)
