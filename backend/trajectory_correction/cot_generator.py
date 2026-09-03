"""Generate replacement GUI-agent thought/summary for corrected actions."""

from __future__ import annotations

import base64
import hashlib
import json
import mimetypes
import os
import re
from pathlib import Path
from typing import Any

from .constants import CORRECTION_COT_CACHE_DIR, PROJECT_ROOT


SYSTEM_PROMPT = r'''You are a GUI agent. You are given a task and your action history, with screenshots. You need to perform the next action to complete the task.

# Action Space
{"action": "click", "coordinate": [x, y]}
{"action": "long_press", "coordinate": [x, y]}
{"action": "type", "text": ""}
{"action": "open", "text": "app_name"}
{"action": "swipe", "start_coordinate": [x1, y1], "end_coordinate": [x2, y2]}
{"action": "system_button", "button": "button_name"}
{"action": "wait"}
{"action": "terminate", "status": "success or failure"}
{"action": "answer", "text": "xxx"}

# Output Requirements
1.Your response must contain exactly three parts in the following order: a thought, a tool call, and an action summary. The thought and summary must be written in Chinese. The tool call must be valid JSON and strictly follow the Action Space.
2.The Reference Answer specifies the action to be performed and must be treated as the ground-truth action.
3.The thought must be one short Chinese sentence describing the visible target or interface state relevant to the Reference Answer, together with the specified action. Do not introduce a different action, extended reasoning, or future planning.
4.The summary must be one short Chinese sentence describing the action specified by the Reference Answer.

# Consistency Requirements
1.Reference Action Alignment: The tool call must exactly represent the action specified by the Reference Answer.
2.Thought Alignment: The thought must describe and support the action given in the Reference Answer, rather than independently deciding a new action.

# Output Template
<thought>简要描述与 Reference Answer 动作对应的界面目标或状态，以及该动作</thought><tool_call>{"action": <action-name>, ...}</tool_call><summary>简要描述 Reference Answer 指定的动作</summary>'''


def read_env(path: Path | None = None) -> dict[str, str]:
    path = path or PROJECT_ROOT / "backend" / ".env"
    values: dict[str, str] = {}
    if path.is_file():
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip().strip('"').strip("'")
    missing = [name for name in ("YUNAI_API_KEY", "MODEL_URL", "MODEL_NAME") if not values.get(name)]
    if missing:
        raise RuntimeError(f"缺少模型配置：{', '.join(missing)}")
    return values


def parse_cot_response(raw: str, expected_action: dict[str, Any] | None = None) -> dict[str, str]:
    """Validate the COT response while leaving ``tool_call`` model-owned."""
    text = str(raw or "")
    def _matches(tag: str) -> list[str]:
        return re.findall(
            rf"<{tag}\b[^>]*>\s*(.*?)\s*</{tag}\s*>",
            text,
            re.DOTALL | re.IGNORECASE,
        )

    thought_values = _matches("thought")
    summary_values = _matches("summary")
    # Do not make a successful response depend on the gateway preserving tag
    # order, and intentionally do not parse tool_call.  The corrected action
    # remains authoritative in the correction session.
    missing = [
        name
        for name, match in (
            ("thought", thought_values),
            ("summary", summary_values),
        )
        if not match
    ]
    if missing:
        raise ValueError(f"Qwen COT 响应缺少标签：{', '.join(missing)}；必须包含 thought、summary")
    # Gateways may echo the requested format before the actual answer.  Use
    # the last thought and summary without interpreting tool_call/JSON.
    thought = thought_values[-1].strip()
    summary = summary_values[-1].strip()
    if not thought or not summary:
        raise ValueError("Qwen COT 的 thought 和 summary 不能为空")
    return {"thought": thought, "summary": summary}


def _image_data_url(path: Path) -> str:
    data = path.read_bytes()
    mime = mimetypes.guess_type(path.name)[0] or "image/jpeg"
    return f"data:{mime};base64," + base64.b64encode(data).decode("ascii")


class QwenCotGenerator:
    def __init__(self, *, env_file: Path | None = None, cache_dir: Path = CORRECTION_COT_CACHE_DIR) -> None:
        from openai import OpenAI

        values = read_env(env_file)
        self.model = values.get("COT_MODEL_NAME") or "qwen3-vl-32b-instruct"
        self.client = OpenAI(api_key=values["YUNAI_API_KEY"], base_url=values["MODEL_URL"], max_retries=2)
        self.cache_dir = cache_dir

    @staticmethod
    def _key(*, task: str, trajectory_id: str, step: int, history: str, action: dict[str, Any], reference_answer: str, image: Path, model: str) -> str:
        digest = hashlib.sha256(image.read_bytes()).hexdigest()
        payload = {"version": "cot-v8-expert-action-only", "model": model, "task": task, "trajectory_id": trajectory_id, "step": step, "history": history, "action": action, "image": digest}
        return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()

    def generate(self, *, task: str, trajectory_id: str, step: int, history: str, action: dict[str, Any], image: Path, reference_answer: str = "") -> dict[str, str | bool]:
        key = self._key(task=task, trajectory_id=trajectory_id, step=step, history=history, action=action, reference_answer=reference_answer, image=image, model=self.model)
        path = self.cache_dir / f"{key}.json"
        if path.is_file():
            cached = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(cached, dict) and cached.get("thought") and cached.get("summary"):
                return {"thought": str(cached["thought"]), "summary": str(cached["summary"]), "bbox_hash": hashlib.sha256(reference_answer.encode()).hexdigest(), "cached": True}

        expert_action = json.dumps(action, ensure_ascii=False)
        prompt = (
            f"## Task\n{task}\n\n## Execution History\n{history or 'Empty'}\n\n"
            f"## Reference Answer\n{expert_action}\n\n"
            "## Current Screenshot\n"
        )
        response = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": [{"type": "text", "text": prompt}, {"type": "image_url", "image_url": {"url": _image_data_url(image)}}]},
            ],
            temperature=0.0,
            top_p=1.0,
            max_tokens=512,
            extra_body={"top_k": 0, "repetition_penalty": 1.0, "enable_thinking": False, "chat_template_kwargs": {"enable_thinking": False}},
        )
        raw = (response.choices[0].message.content or "").strip()
        result = parse_cot_response(raw, action)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        bbox_hash = hashlib.sha256(reference_answer.encode()).hexdigest()
        temporary.write_text(json.dumps({**result, "model": self.model, "content_tag": "thought_summary", "bbox_hash": bbox_hash}, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, path)
        return {**result, "bbox_hash": bbox_hash, "cached": False}
