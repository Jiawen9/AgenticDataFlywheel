"""Conservative JSON extraction: never mine examples out of an analysis paragraph."""
from __future__ import annotations

import json
import re
from typing import Any


def final_text(raw: str) -> str:
    text = raw.strip()
    if "</think>" in text:
        text = text.rsplit("</think>", 1)[1].strip()
    if re.search(r"<think(?:\s|>)", text, re.I):
        raise ValueError("模型仅返回未结束的思考过程，没有最终答案")
    # A final-answer heading is an explicit boundary, unlike arbitrary JSON in prose.
    markers = list(re.finditer(r"(?im)^[ \t]*(?:#{1,6}[ \t]*)?(?:\*\*)?(?:final answer|final output|最终答案|最终输出)(?:\*\*)?[ \t]*(?:[:：][ \t]*(?:\*\*)?[ \t]*|$)", text))
    if markers:
        text = text[markers[-1].end():].strip()
    return text


def _decode_document(text: str) -> list[Any]:
    decoder = json.JSONDecoder()
    values = []
    while text.strip():
        text = text.lstrip()
        value, end = decoder.raw_decode(text)
        values.append(value)
        text = text[end:]
    return values


def json_documents(raw: str) -> list[Any]:
    text = final_text(raw)
    if not text:
        raise ValueError("模型没有最终答案")
    # Accept one fence enclosing the entire answer; multiple blocks/examples are ambiguous.
    fence = re.fullmatch(r"```(?:jsonl?|JSONL?)?\s*\n?([\s\S]*?)\s*```", text)
    if fence:
        text = fence.group(1).strip()
        if "```" in text:
            raise ValueError("模型返回多个代码块，无法确定最终答案")
    try:
        return _decode_document(text)
    except (ValueError, json.JSONDecodeError) as exc:
        raise ValueError("模型最终答案不是完整 JSON/JSONL（可能混有思考、示例或被截断）") from exc


def parse_json_value(raw: str, expected: type | None = None) -> Any:
    documents = json_documents(raw)
    if len(documents) != 1 or (expected is not None and not isinstance(documents[0], expected)):
        raise ValueError("模型必须返回单个指定类型的 JSON 值")
    return documents[0]


def parse_jsonl_tasks(raw: str) -> list[Any]:
    documents = json_documents(raw)
    if len(documents) == 1:
        value = documents[0]
        if isinstance(value, list):
            return value
        if isinstance(value, dict) and "tasks" in value:
            if not isinstance(value["tasks"], list):
                raise ValueError("tasks 必须是数组")
            return value["tasks"]
    return documents


def valid_task_text(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("task 必须为字符串")
    text = value.strip()
    compact = re.sub(r"\s+", "", text).strip('"\'`<>[]{}。.!！?？:：')
    if not compact or not any(char.isalnum() for char in compact):
        raise ValueError("任务只有空白或标点占位符")
    if compact.lower() in {"生成的任务描述", "任务描述", "变体任务描述", "前置任务", "weak时填写前置任务，否则为null", "task", "taskdescription", "generatedtaskdescription", "todo", "tbd", "null", "none", "待生成", "示例", "示例任务"}:
        raise ValueError("任务是模板占位文本")
    if re.search(r"\.{3,}|…|<[^>]+>|\{\{.*?\}\}", text):
        raise ValueError("任务包含省略号或未填写的模板变量")
    if re.match(r"(?i)(here['’]s (?:a |the )?thinking|let me think|thinking process|analysis\s*:|思考过程\s*[:：])", text):
        raise ValueError("任务是思考说明，不是操作指令")
    if len(text) > 4000:
        raise ValueError("任务文本超过 4000 字符")
    return text
