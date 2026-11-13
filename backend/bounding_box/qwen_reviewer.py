from __future__ import annotations

import base64
import hashlib
import io
import json
import math
import mimetypes
import os
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from backend.file_io import io_path
from backend import pipeline_diagnostics as diagnostics
from backend.pipeline_retry_errors import ModelOutputError
from backend.short_paths import short_temporary_file

from typing import Any

import httpx
from openai import OpenAI
from PIL import Image, ImageDraw


JSON_RE = re.compile(r"\{.*\}", re.DOTALL)

_OUTPUT_FEEDBACK = {
    "empty_choices": "没有返回候选结果，请输出完整 JSON。",
    "empty_content": "返回内容为空，请把最终 JSON 放在正文中。",
    "truncated": "返回被截断，请仅输出简短、完整的 JSON。",
    "invalid_json": "返回不是合法 JSON，请只输出一个 JSON 对象。",
    "invalid_decision": "decision 必须为 accept 或 replace。",
    "invalid_bbox": "bbox 必须是四个有限数值，不能包含字符串、布尔值或空值。",
    "bbox_out_of_range": "replace 的 bbox 每个坐标必须在 0～1000 内，不能使用像素坐标。",
    "bbox_order_invalid": "bbox 必须满足 x1 < x2 且 y1 < y2。",
    "bbox_too_small": "框转换后不足 8 像素；请重新定位完整目标控件。若候选框无效，必须 replace，不能 accept。",
    "bbox_excludes_action": "框没有包含实际执行点。请以黄色实际动作点为准重新定位；不要用任务文字描述代替实际执行位置。",
    "invalid_confidence": "confidence 必须为有限数值。",
}


class BoxReviewOutputError(ModelOutputError):
    """A local validation failure; raw provider content lives only in diagnostics."""

    def __init__(self, failure_code: str, details: dict[str, Any] | None = None):
        super().__init__("bbox_invalid")
        self.failure_code = failure_code
        self.details = details or {}
        self.args = (f"Qwen box review invalid ({failure_code}): {_OUTPUT_FEEDBACK[failure_code]}",)


def _small_diagnostic(value: Any) -> Any:
    """Bound sanitized text/containers without copying image or prompt bodies."""
    value = diagnostics.sanitize(value)
    def bound(item, depth=0):
        if depth > 4:
            return "[NESTED_VALUE_OMITTED]"
        if isinstance(item, str):
            return item[:2048]
        if isinstance(item, dict):
            return {str(key)[:128]: bound(val, depth + 1) for key, val in list(item.items())[:16]}
        if isinstance(item, list):
            return [bound(val, depth + 1) for val in item[:16]]
        return item
    result = bound(value)
    encoded = json.dumps(result, ensure_ascii=False, allow_nan=False).encode("utf-8", errors="replace")
    return result if len(encoded) <= 4096 else {"truncated": True, "preview": encoded[:512].decode("utf-8", errors="ignore")}


def _diagnostic_fields(value: Any) -> dict:
    # Keep geometry/identity fields even if a free-text field is unusually long.
    if not isinstance(value, dict):
        return {}
    return {key: _small_diagnostic(item) for key, item in list(value.items())[:16]}


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


def qwen_settings(*, api_key: str | None = None, base_url: str | None = None) -> dict[str, Any]:
    api_key = api_key if api_key is not None else os.environ.get("TRAJECTORY_VLA_API_KEY") or os.environ.get("TRAJECTORY_API_KEY", "")
    if not api_key:
        raise RuntimeError(
            "Qwen review requires TRAJECTORY_API_KEY or TRAJECTORY_VLA_API_KEY. "
            "No key is currently configured."
        )
    return {
        "api_key": api_key,
        "base_url": base_url or os.environ.get("TRAJECTORY_VLA_API_BASE_URL")
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
    with Image.open(io_path(image_path)) as opened:
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
    action: dict[str, Any] | None = None,
) -> ReviewResult:
    match = JSON_RE.search(raw.strip())
    if not match:
        raise BoxReviewOutputError("invalid_json")
    try:
        value = json.loads(match.group(0))
    except ValueError as exc:
        raise BoxReviewOutputError("invalid_json") from exc
    if not isinstance(value, dict):
        raise BoxReviewOutputError("invalid_json")
    decision = str(value.get("decision", "")).strip().lower()
    detail = {"decision": decision, "model_bbox": value.get("bbox"),
              "candidate_bbox_pixels": list(candidate_bbox), "image_size": list(image_size)}
    if decision not in {"accept", "replace"}:
        raise BoxReviewOutputError("invalid_decision", detail)
    bbox_value = candidate_bbox if decision == "accept" else value.get("bbox")
    if (not isinstance(bbox_value, (list, tuple)) or len(bbox_value) != 4
            or any(isinstance(item, bool) or not isinstance(item, (int, float)) for item in bbox_value)):
        raise BoxReviewOutputError("invalid_bbox", detail)
    width, height = image_size
    try:
        values = [float(item) for item in bbox_value]
    except (ValueError, OverflowError) as exc:
        raise BoxReviewOutputError("invalid_bbox", detail) from exc
    if not all(math.isfinite(item) for item in values):
        raise BoxReviewOutputError("invalid_bbox", detail)
    out_of_range = decision == "replace" and normalized_output and any(item < 0 or item > 1000 for item in values)
    ordered = values[0] < values[2] and values[1] < values[3]
    if decision == "replace" and normalized_output:
        # This reviewer contract is independent of the collection model's adapter.
        values = [
            values[0] / 1000.0 * width,
            values[1] / 1000.0 * height,
            values[2] / 1000.0 * width,
            values[3] / 1000.0 * height,
        ]
    detail["selected_coordinate_space"] = "normalized_1000" if decision == "replace" and normalized_output else "pixel"
    detail["pixel_bbox_before_clamp"] = values
    if not all(math.isfinite(item) for item in values):
        raise BoxReviewOutputError("bbox_out_of_range", detail)
    x1, y1, x2, y2 = (round(item) for item in values)
    x1, x2 = sorted((max(0, min(width - 2, x1)), max(1, min(width - 1, x2))))
    y1, y2 = sorted((max(0, min(height - 2, y1)), max(1, min(height - 1, y2))))
    detail["pixel_bbox_after_clamp"] = [x1, y1, x2, y2]
    if out_of_range:
        raise BoxReviewOutputError("bbox_out_of_range", detail)
    if not ordered:
        raise BoxReviewOutputError("bbox_order_invalid", detail)
    if x2 - x1 < 8 or y2 - y1 < 8:
        raise BoxReviewOutputError("bbox_too_small", detail)
    if action:
        kind = action.get("action")
        keys = ("coordinate",) if kind in {"click", "long_press"} else (
            ("start_coordinate", "end_coordinate") if kind == "swipe" else ())
        points = [action.get(key) for key in keys]
        for point in points:
            if (not isinstance(point, (list, tuple)) or len(point) != 2
                    or any(isinstance(v, bool) or not isinstance(v, (int, float))
                           or not math.isfinite(v) for v in point)):
                raise ValueError("Executed action requires finite pixel coordinates")
            if not (0 <= point[0] < width and 0 <= point[1] < height):
                raise ValueError("Executed action pixel coordinates are outside the screenshot")
        detail["executed_points_pixels"] = points
        if any(not (x1 <= point[0] <= x2 and y1 <= point[1] <= y2) for point in points):
            raise BoxReviewOutputError("bbox_excludes_action", detail)
    try:
        confidence = float(value.get("confidence", 0.5))
    except (ValueError, TypeError, OverflowError) as exc:
        raise BoxReviewOutputError("invalid_confidence", detail) from exc
    if not math.isfinite(confidence):
        raise BoxReviewOutputError("invalid_confidence", detail)
    confidence = max(0.0, min(1.0, confidence))
    return ReviewResult(
        decision=decision,
        bbox=(x1, y1, x2, y2),
        confidence=confidence,
        reason=str(value.get("reason", "")).strip(),
        raw_response=raw,
    )


class QwenBoxReviewer:
    def __init__(self, model: str, cache_path: Path, *, client_settings: dict[str, Any] | None = None) -> None:
        settings = client_settings if client_settings is not None else qwen_settings()
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
        diagnostics.register_secrets(settings["api_key"])
        self.max_output_attempts = 3
        self.cache_path = cache_path
        self.cache: dict[str, str] = {}
        if io_path(cache_path).exists():
            self.cache = json.loads(io_path(cache_path).read_text(encoding="utf-8"))

    def _save_cache(self) -> None:
        io_path(self.cache_path.parent).mkdir(parents=True, exist_ok=True)
        io_path(self.cache_path).write_text(
            json.dumps(self.cache, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def _failure_path(self, key: str) -> Path:
        return self.cache_path.parent / "box_failures" / (key[:20] + ".json")

    def _previous_failure(self, key: str) -> tuple[str, list]:
        try:
            with io_path(self._failure_path(key)).open("rb") as stream:
                raw = stream.read(128 * 1024 + 1)
        except FileNotFoundError:
            return "", []
        if len(raw) > 128 * 1024:
            return "", []
        try:
            record = json.loads(raw)
        except (ValueError, UnicodeError):
            return "", []
        if not isinstance(record, dict) or record.get("cache_key") != key or record.get("resolved"):
            return "", []
        attempts = record.get("attempts")
        if not isinstance(attempts, list) or not attempts or not isinstance(attempts[-1], dict):
            return "", []
        code = attempts[-1].get("failure_code")
        if not isinstance(code, str) or code not in _OUTPUT_FEEDBACK:
            return "", []
        # Never replay provider text as instructions. Keep only bounded evidence.
        previous = []
        for item in attempts[-8:]:
            if not isinstance(item, dict) or not isinstance(item.get("failure_code"), str):
                continue
            if item["failure_code"] not in _OUTPUT_FEEDBACK:
                continue
            raw_text = item.get("raw_response")
            cleaned = diagnostics.sanitize_partial(raw_text if isinstance(raw_text, str) else "")
            previous.append({"source": "previous_review", "failure_code": item["failure_code"],
                "details": _diagnostic_fields(item.get("details")),
                "raw_response": cleaned.encode("utf-8", errors="replace")[:8192].decode("utf-8", errors="ignore"),
                "observed_at": _small_diagnostic(item.get("observed_at")),
                "raw_response_truncated": bool(item.get("raw_response_truncated")),
                "raw_response_sha256": _small_diagnostic(item.get("raw_response_sha256"))})
        return _OUTPUT_FEEDBACK[code], previous

    def _write_failures(self, key: str, payload: dict, image_path: Path, attempts: list,
                        *, resolved: bool = False) -> Path:
        path = self._failure_path(key)
        record = {"schema_version": 1, "cache_key": key, "resolved": resolved,
                  "updated_at": datetime.now(timezone.utc).isoformat(),
                  "image_path": _small_diagnostic(str(image_path)),
                  "request": _diagnostic_fields(payload), "attempts": attempts[-8:]}
        raw = json.dumps(record, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8")
        # JSON escaping can expand the bounded strings. Keep the newest evidence
        # and ensure the next job can always read this file within its fixed limit.
        while len(raw) > 128 * 1024 and len(record["attempts"]) > 1:
            record["attempts"].pop(0)
            raw = json.dumps(record, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8")
        with short_temporary_file(path) as temporary:
            io_path(temporary).write_bytes(raw)
            io_path(temporary).replace(io_path(path))
        return path

    @staticmethod
    def _failure_record(error: BoxReviewOutputError, raw: Any, *, source: str, attempt: int) -> dict:
        # Scrub complete credentials before truncation, including malformed JSON.
        text = raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False, default=str)
        clean = diagnostics.sanitize_partial(text)
        clean_bytes = clean.encode("utf-8", errors="replace")
        return {"source": source, "attempt": attempt, "failure_code": error.failure_code,
                "observed_at": datetime.now(timezone.utc).isoformat(),
                "details": _diagnostic_fields(error.details),
                "raw_response": clean_bytes[:8192].decode("utf-8", errors="ignore"),
                "raw_response_truncated": len(clean_bytes) > 8192,
                "raw_response_sha256": hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()}

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
        image_digest = hashlib.sha256(io_path(image_path).read_bytes()).hexdigest()
        payload = {
            "version": "single-action-box-review-v4-output-recovery",
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
        feedback, failures = self._previous_failure(cache_key)
        for version in ("single-action-box-review-v3-action-marker", "single-action-box-review-v2-normalized", "single-action-box-review-v1"):
            legacy_payload = dict(payload)
            legacy_payload["version"] = version
            legacy_keys.append(
                hashlib.sha256(
                    json.dumps(legacy_payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
                ).hexdigest()
            )
        for key in (cache_key, *legacy_keys):
            raw = self.cache.get(key)
            if not isinstance(raw, str):
                continue
            try:
                result = _parse_response(raw, candidate_bbox, image_size, normalized_output=True, action=action)
            except BoxReviewOutputError as exc:
                # Earlier versions saved before validation. Leave failed cache
                # entries untouched until a valid fresh response replaces them.
                failures.append(self._failure_record(exc, raw, source="cache", attempt=0))
                feedback = _OUTPUT_FEEDBACK[exc.failure_code]
                result = None
            if result is None:
                self._write_failures(cache_key, payload, image_path, failures)
                continue
            if failures:
                self._write_failures(cache_key, payload, image_path, failures, resolved=True)
            result.cached = True
            return result

        candidate_normalized = [
            round(candidate_bbox[0] / image_size[0] * 1000),
            round(candidate_bbox[1] / image_size[1] * 1000),
            round(candidate_bbox[2] / image_size[0] * 1000),
            round(candidate_bbox[3] / image_size[1] * 1000),
        ]
        action_normalized = dict(action)
        for name in ("coordinate", "start_coordinate", "end_coordinate"):
            point = action.get(name)
            if isinstance(point, (list, tuple)) and len(point) == 2:
                action_normalized[name] = [round(float(point[0]) / image_size[0] * 1000),
                                           round(float(point[1]) / image_size[1] * 1000)]
        prompt = (
            "你正在复核移动端 GUI action 的唯一可执行区域框。截图上的红框是当前候选框。\n"
            "黄色十字圆点是轨迹记录的实际执行 click 点；swipe 时两个黄色点分别是起点和终点。\n"
            f"截图尺寸：{image_size[0]}x{image_size[1]}。\n"
            f"实际执行 action（坐标单位为截图像素，不是0～1000）：{json.dumps(action, ensure_ascii=False)}\n"
            f"同一实际动作在输出坐标系中的位置（0～1000）：{json.dumps(action_normalized, ensure_ascii=False)}\n"
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
        output_settings: dict[str, Any] = {"max_tokens": 500}
        if self.model == "qwen3.8-max":
            output_settings = {"max_tokens": 2048, "response_format": {"type": "json_object"}}
        messages = [
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
            ]
        attempt_limit = max(1, min(3, int(self.max_output_attempts)))
        for attempt in range(1, attempt_limit + 1):
            request_messages = messages
            if feedback:
                request_messages = [*messages, {"role": "user", "content": (
                    "上一次复核未通过本地校验：" + feedback
                    + " 请根据同一截图重新给出合法结果；不要重复无效坐标。横坐标除以截图宽、"
                    "纵坐标除以截图高后乘1000才是要求的归一化坐标。") }]
            # Transport failures are owned by the existing SDK/Pipeline policy,
            # never by this model-output correction loop.
            response = self.client.chat.completions.create(
                model=self.model, messages=request_messages, temperature=0.1,
                **output_settings, extra_body={"enable_thinking": False,
                    "chat_template_kwargs": {"enable_thinking": False}},
            )
            raw = None
            error = None
            try:
                if not response.choices:
                    raise BoxReviewOutputError("empty_choices")
                choice = response.choices[0]
                raw = choice.message.content
                if getattr(choice, "finish_reason", None) == "length":
                    raise BoxReviewOutputError("truncated")
                if not isinstance(raw, str) or not raw.strip():
                    raise BoxReviewOutputError("empty_content")
                raw = raw.strip()
                result = _parse_response(raw, candidate_bbox, image_size, normalized_output=True, action=action)
            except BoxReviewOutputError as exc:
                error = exc
            # Outside except: I/O failures must not inherit a model-error context
            # and be incorrectly classified for automatic model retries.
            if error is not None:
                failures.append(self._failure_record(error, raw, source="model", attempt=attempt))
                path = self._write_failures(cache_key, payload, image_path, failures)
                feedback = _OUTPUT_FEEDBACK[error.failure_code]
                if attempt == attempt_limit:
                    error.args = (f"{error}; diagnostic: {diagnostics.sanitize(str(path))}",)
                    raise error
                continue
            self.cache[cache_key] = raw
            self._save_cache()
            if failures:
                self._write_failures(cache_key, payload, image_path, failures, resolved=True)
            result.cached = False
            return result
        raise AssertionError("unreachable box review attempt state")
