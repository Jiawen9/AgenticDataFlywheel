"""Qwen review for a structurally matched short-gap trajectory resync."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import httpx
from openai import OpenAI

try:
    from ..bounding_box.qwen_reviewer import qwen_settings
    from .intermediate_state_classifier import _image_data_url, _image_digest
except ImportError:  # Support direct imports during local debugging.
    from bounding_box.qwen_reviewer import qwen_settings
    from trajectories_tree.intermediate_state_classifier import (
        _image_data_url,
        _image_digest,
    )


PROMPT_VERSION = "trajectory-state-alignment-v1"


@dataclass
class StateAlignmentResult:
    same_task_state: bool
    confidence: float
    reason: str
    raw_response: str
    cached: bool = False

    def to_dict(self, confidence_threshold: float) -> dict[str, Any]:
        value = asdict(self)
        value["accepted"] = bool(
            self.same_task_state and self.confidence >= confidence_threshold
        )
        value["confidence_threshold"] = confidence_threshold
        return value


def parse_alignment_response(raw: str) -> StateAlignmentResult:
    text = raw.strip()
    object_start = text.find("{")
    if object_start < 0:
        raise ValueError(f"Qwen response does not contain JSON: {raw[:300]!r}")
    try:
        value, _ = json.JSONDecoder().raw_decode(text[object_start:])
    except json.JSONDecodeError as exc:
        raise ValueError(f"Qwen response contains invalid JSON: {raw[:300]!r}") from exc
    if not isinstance(value, dict):
        raise ValueError("Qwen alignment review must be a JSON object")

    same_task_state = value.get("same_task_state")
    if not isinstance(same_task_state, bool):
        raise ValueError(
            f"same_task_state must be boolean, got {same_task_state!r}"
        )
    try:
        confidence = float(value.get("confidence"))
    except (TypeError, ValueError) as exc:
        raise ValueError("confidence must be numeric") from exc
    if not 0.0 <= confidence <= 1.0:
        raise ValueError(f"confidence must be between 0 and 1, got {confidence!r}")
    reason = str(value.get("reason", "")).strip()
    if not reason:
        raise ValueError("alignment reason must not be empty")
    return StateAlignmentResult(
        same_task_state=same_task_state,
        confidence=confidence,
        reason=reason,
        raw_response=raw,
    )


class QwenStateAlignmentReviewer:
    """Confirm that two post-gap steps match two existing tree states."""

    def __init__(self, model: str, cache_path: Path) -> None:
        settings = qwen_settings("tree")
        http_client = httpx.Client(
            proxy=settings["proxy"] or None,
            verify=settings["verify"],
            timeout=settings["timeout"],
            trust_env=settings["trust_env"],
        )
        self.client = OpenAI(
            api_key=settings["api_key"] or "EMPTY",
            base_url=settings["base_url"],
            timeout=settings["timeout"],
            max_retries=settings["max_retries"],
            http_client=http_client,
        )
        self.model = model
        self.cache_path = cache_path
        self.cache: dict[str, str] = {}
        if cache_path.exists():
            payload = json.loads(cache_path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError(f"alignment cache must be a JSON object: {cache_path}")
            self.cache = {str(key): str(value) for key, value in payload.items()}

    def _save_cache(self) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = self.cache_path.with_name(f".{self.cache_path.name}.tmp")
        temporary_path.write_text(
            json.dumps(self.cache, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temporary_path.replace(self.cache_path)

    def review(
        self,
        *,
        trajectory: str,
        skipped_steps: list[dict[str, Any]],
        candidate_image_paths: list[Path],
        reference_image_paths: list[Path],
        candidate_steps: list[dict[str, Any]],
        reference_steps: list[dict[str, Any]],
    ) -> StateAlignmentResult:
        if len(candidate_image_paths) != len(reference_image_paths):
            raise ValueError("candidate/reference image pair count differs")
        if not candidate_image_paths or len(candidate_image_paths) > 2:
            raise ValueError("alignment review requires one or two image pairs")

        cache_payload = {
            "version": PROMPT_VERSION,
            "model": self.model,
            "trajectory": trajectory,
            "skipped_steps": skipped_steps,
            "candidate_steps": candidate_steps,
            "reference_steps": reference_steps,
            "candidate_image_digests": [
                _image_digest(path) for path in candidate_image_paths
            ],
            "reference_image_digests": [
                _image_digest(path) for path in reference_image_paths
            ],
        }
        cache_key = hashlib.sha256(
            json.dumps(cache_payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        cached = cache_key in self.cache
        if cached:
            raw = self.cache[cache_key]
        else:
            prompt = (
                "你正在复核移动端 GUI 轨迹的短暂跳步对齐。action 和 bbox 已经连续匹配，"
                "现在只判断每组候选截图与已有轨迹截图是否处于相同的任务状态。\n"
                "允许忽略时间、电量、小幅动画、视频帧和动态推荐内容的差异；广告、加载遮罩、"
                "弹窗、页面层级、选中项或任务进度不同都应视为不同状态。\n"
                "只有所有截图对均表示相同任务状态时才返回 true；证据不足时返回 false。\n\n"
                f"轨迹：{trajectory}\n"
                f"拟跳过步骤：{json.dumps(skipped_steps, ensure_ascii=False)}\n"
                f"候选确认步骤：{json.dumps(candidate_steps, ensure_ascii=False)}\n"
                f"已有轨迹确认步骤：{json.dumps(reference_steps, ensure_ascii=False)}\n\n"
                "只输出严格 JSON，不要输出 Markdown："
                '{"same_task_state":true|false,"confidence":0.0,"reason":"简短中文理由"}'
            )
            content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
            for index, (candidate_path, reference_path) in enumerate(
                zip(candidate_image_paths, reference_image_paths), 1
            ):
                content.extend(
                    [
                        {"type": "text", "text": f"第 {index} 组：候选轨迹截图"},
                        {
                            "type": "image_url",
                            "image_url": {"url": _image_data_url(candidate_path)},
                        },
                        {"type": "text", "text": f"第 {index} 组：已有轨迹截图"},
                        {
                            "type": "image_url",
                            "image_url": {"url": _image_data_url(reference_path)},
                        },
                    ]
                )
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "你是移动端 GUI 轨迹对齐审核器，只判断截图是否表示相同任务状态，"
                            "并严格返回一个 JSON 对象。"
                        ),
                    },
                    {"role": "user", "content": content},
                ],
                temperature=0.0,
                max_tokens=250,
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
                raise ValueError("Qwen returned an empty state-alignment review")
            parse_alignment_response(raw)
            self.cache[cache_key] = raw
            self._save_cache()

        result = parse_alignment_response(raw)
        result.cached = cached
        return result
