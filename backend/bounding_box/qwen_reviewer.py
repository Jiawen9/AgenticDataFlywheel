from __future__ import annotations

import base64
import hashlib
import io
import json
import mimetypes
import os
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import httpx
from openai import OpenAI
from PIL import Image, ImageDraw


JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


@dataclass
class ReviewResult:
    decision: str
    bbox: tuple[int, int, int, int]
    confidence: float
    reason: str
    raw_response: str
    cached: bool = False

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["bbox"] = list(self.bbox)
        return value


def _bool_env(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _verify_env(name: str, default: bool = True) -> bool | str:
    value = os.environ.get(name)
    if value is None:
        return default
    if value.strip().lower() in {"1", "true", "yes", "on"}:
        return True
    if value.strip().lower() in {"0", "false", "no", "off"}:
        return False
    return value


def qwen_settings() -> dict[str, Any]:
    api_key = os.environ.get("TRAJECTORY_VLA_API_KEY") or os.environ.get("TRAJECTORY_API_KEY", "")
    if not api_key:
        raise RuntimeError(
            "Qwen review requires TRAJECTORY_API_KEY or TRAJECTORY_VLA_API_KEY. "
            "No key is currently configured."
        )
    return {
        "api_key": api_key,
        "base_url": os.environ.get("TRAJECTORY_VLA_API_BASE_URL")
        or os.environ.get("TRAJECTORY_API_BASE_URL", "https://yunai.chat/v1"),
        "proxy": os.environ.get("TRAJECTORY_VLA_HTTP_PROXY_URL")
        or os.environ.get("TRAJECTORY_HTTP_PROXY_URL", ""),
        "verify": _verify_env(
            "TRAJECTORY_VLA_HTTP_VERIFY", _verify_env("TRAJECTORY_HTTP_VERIFY", True)
        ),
        "timeout": float(
            os.environ.get("TRAJECTORY_VLA_HTTP_TIMEOUT")
            or os.environ.get("TRAJECTORY_HTTP_TIMEOUT")
            or 120
        ),
        "trust_env": _bool_env(
            "TRAJECTORY_VLA_HTTP_TRUST_ENV",
            _bool_env("TRAJECTORY_HTTP_TRUST_ENV", False),
        ),
    }


def _boxed_image_data_url(
    image_path: Path,
    bbox: tuple[int, int, int, int],
    action: dict[str, Any],
) -> str:
    with Image.open(image_path) as opened:
        image = opened.convert("RGB")
    draw = ImageDraw.Draw(image)
    line_width = max(6, round(min(image.size) / 150))
    draw.rectangle(bbox, outline=(255, 40, 35), width=line_width)
    points: list[tuple[float, float]] = []
    if action.get("action") in {"click", "long_press"}:
        value = action.get("coordinate", [])
        if isinstance(value, list) and len(value) >= 2:
            points.append((float(value[0]), float(value[1])))
    elif action.get("action") == "swipe":
        for key in ("start_coordinate", "end_coordinate"):
            value = action.get(key, [])
            if isinstance(value, list) and len(value) >= 2:
                points.append((float(value[0]), float(value[1])))
    marker = max(12, round(min(image.size) / 55))
    for x, y in points:
        draw.ellipse(
            (x - marker, y - marker, x + marker, y + marker),
            outline=(255, 230, 0),
            width=max(4, line_width // 2),
        )
        draw.line((x - marker, y, x + marker, y), fill=(255, 230, 0), width=3)
        draw.line((x, y - marker, x, y + marker), fill=(255, 230, 0), width=3)
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=92, subsampling=0)
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f"data:image/jpeg;base64,{encoded}"


def _parse_response(
    raw: str,
    candidate_bbox: tuple[int, int, int, int],
    image_size: tuple[int, int],
    normalized_output: bool = True,
) -> ReviewResult:
    match = JSON_RE.search(raw.strip())
    if not match:
        raise ValueError(f"Qwen response does not contain JSON: {raw[:300]!r}")
    value = json.loads(match.group(0))
    decision = str(value.get("decision", "")).strip().lower()
    if decision not in {"accept", "replace"}:
        raise ValueError(f"invalid Qwen decision: {decision!r}")
    bbox_value = candidate_bbox if decision == "accept" else value.get("bbox")
    if not isinstance(bbox_value, (list, tuple)) or len(bbox_value) != 4:
        raise ValueError(f"invalid Qwen bbox: {bbox_value!r}")
    width, height = image_size
    values = [float(item) for item in bbox_value]
    if decision == "replace" and normalized_output:
        # Qwen GUI/VLA coordinates use an independent 0..1000 scale for each axis.
        values = [
            values[0] / 1000.0 * width,
            values[1] / 1000.0 * height,
            values[2] / 1000.0 * width,
            values[3] / 1000.0 * height,
        ]
    x1, y1, x2, y2 = (round(item) for item in values)
    x1, x2 = sorted((max(0, min(width - 2, x1)), max(1, min(width - 1, x2))))
    y1, y2 = sorted((max(0, min(height - 2, y1)), max(1, min(height - 1, y2))))
    if x2 - x1 < 8 or y2 - y1 < 8:
        raise ValueError(f"Qwen bbox is too small: {[x1, y1, x2, y2]}")
    confidence = max(0.0, min(1.0, float(value.get("confidence", 0.5))))
    return ReviewResult(
        decision=decision,
        bbox=(x1, y1, x2, y2),
        confidence=confidence,
        reason=str(value.get("reason", "")).strip(),
        raw_response=raw,
    )


class QwenBoxReviewer:
    def __init__(self, model: str, cache_path: Path) -> None:
        settings = qwen_settings()
        proxy = settings["proxy"] or None
        http_client = httpx.Client(
            proxy=proxy,
            verify=settings["verify"],
            timeout=settings["timeout"],
            trust_env=settings["trust_env"],
        )
        self.client = OpenAI(
            api_key=settings["api_key"],
            base_url=settings["base_url"],
            timeout=settings["timeout"],
            http_client=http_client,
        )
        self.model = model
        self.cache_path = cache_path
        self.cache: dict[str, str] = {}
        if cache_path.exists():
            self.cache = json.loads(cache_path.read_text(encoding="utf-8"))

    def _save_cache(self) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.cache_path.write_text(
            json.dumps(self.cache, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def review(
        self,
        image_path: Path,
        action: dict[str, Any],
        action_summary: str,
        candidate_bbox: tuple[int, int, int, int],
        image_size: tuple[int, int],
        rule_context: dict[str, Any],
        round_index: int,
    ) -> ReviewResult:
        image_digest = hashlib.sha256(image_path.read_bytes()).hexdigest()
        payload = {
            "version": "single-action-box-review-v3-action-marker",
            "model": self.model,
            "image_digest": image_digest,
            "image_size": image_size,
            "action": action,
            "action_summary": action_summary,
            "candidate_bbox": candidate_bbox,
            "rule_context": rule_context,
            "round": round_index,
        }
        cache_key = hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        # Reuse v1 first-round responses. Accepted boxes need no coordinate parsing;
        # replaced boxes were in fact normalized even though the old prompt requested pixels.
        legacy_keys = []
        for version in ("single-action-box-review-v2-normalized", "single-action-box-review-v1"):
            legacy_payload = dict(payload)
            legacy_payload["version"] = version
            legacy_keys.append(
                hashlib.sha256(
                    json.dumps(legacy_payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
                ).hexdigest()
            )
        cached_key = next((key for key in (cache_key, *legacy_keys) if key in self.cache), None)
        cached = cached_key is not None
        if cache_key in self.cache:
            raw = self.cache[cache_key]
        elif cached_key is not None:
            raw = self.cache[cached_key]
        else:
            candidate_normalized = [
                round(candidate_bbox[0] / image_size[0] * 1000),
                round(candidate_bbox[1] / image_size[1] * 1000),
                round(candidate_bbox[2] / image_size[0] * 1000),
                round(candidate_bbox[3] / image_size[1] * 1000),
            ]
            prompt = (
                "你正在复核移动端 GUI action 的唯一可执行区域框。截图上的红框是当前候选框。\n"
                "黄色十字圆点是实际执行成功的 click 点；swipe 时两个黄色点分别是起点和终点。\n"
                f"截图尺寸：{image_size[0]}x{image_size[1]}。\n"
                f"action：{json.dumps(action, ensure_ascii=False)}\n"
                f"action_summary：{action_summary or '未提供'}\n"
                f"候选 bbox（横纵轴分别归一化到0～1000）：{candidate_normalized}\n"
                f"规则依据：{json.dumps(rule_context, ensure_ascii=False)}\n\n"
                "判断标准：click 框应覆盖完整且安全的目标控件区域；swipe 框应覆盖该手势可安全执行的滚动内容区域；"
                "type 框应覆盖接收输入的输入控件。最终只能有一个矩形框。不要框动作点、箭头、文字标签或整个屏幕，"
                "除非整个屏幕确实是唯一可执行区域。必须结合 action_summary 判断动作意图，但 action_summary 与实际位置冲突时，"
                "以黄色实际执行点和截图可见控件为准。click 框必须包含黄色点击点；swipe 框必须包含两个黄色端点。\n"
                "若红框合适，decision=accept，bbox 原样返回；若不合适，decision=replace，并给出你重新定位后的归一化 bbox。"
                "bbox 坐标必须将截图横轴和纵轴分别归一化到0～1000，不能输出原图像素坐标。只输出严格 JSON："
                '{"decision":"accept|replace","bbox":[x1,y1,x2,y2],"confidence":0.0,"reason":"简短原因"}'
            )
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "你是精确的移动端 GUI 视觉标框审核器。你必须检查候选框，必要时重新生成一个框，"
                            "并严格输出一个 JSON 对象。"
                        ),
                    },
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt},
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": _boxed_image_data_url(image_path, candidate_bbox, action)
                                },
                            },
                        ],
                    },
                ],
                temperature=0.1,
                max_tokens=500,
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
                raise ValueError("Qwen returned an empty box review")
            self.cache[cache_key] = raw
            self._save_cache()
        result = _parse_response(raw, candidate_bbox, image_size, normalized_output=True)
        result.cached = cached
        return result
