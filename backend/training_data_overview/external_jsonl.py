"""Summarize historical training JSONL. Image references are used as text only."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import date
import hashlib
import json
from pathlib import Path
import re

from ..file_io import io_path
from .converter import JSONL_MANUAL_RECORD_SOURCE, _classification_known


PARSER_VERSION = 1
SOURCE_FORMATS = ("data_flywheel", "factory", "health", "middle")
_LABELS = (
    ("APP", ("APP", "app", "涉及APP"), "未记录 App"),
    ("一级场景", ("一级场景", "level1", "scene"), "未分类"),
    ("二级场景", ("二级场景", "level2", "capability"), "未分类"),
)
_TOOL_CALL = re.compile(r"<tool_call>\s*(.*?)\s*</tool_call>", re.DOTALL | re.IGNORECASE)
_ACTION_NAME = re.compile(r"[a-zA-Z][a-zA-Z0-9_]{0,63}")
_FUNCTION_CALL = re.compile(r"\s*([a-zA-Z][a-zA-Z0-9_]{0,63})\s*\(")
_IMAGE_STEP = re.compile(r"(task[_-]?\d+)[_-]step[_-]?\d+\.(?:jpg|jpeg|png|webp)", re.IGNORECASE)
_SECTION = re.compile(r"^##[ \t]+(Task|Execution History|Current Screenshot)[ \t]*\r?$", re.MULTILINE | re.IGNORECASE)
_HISTORY_STEP = re.compile(r"Step\s+(\d{1,6}):\s*(.*)", re.IGNORECASE)


@dataclass
class _Trajectory:
    grouping: str
    steps: int = 0
    actions: Counter = field(default_factory=Counter)
    labels: list[set[str]] = field(default_factory=lambda: [set() for _ in _LABELS])
    known_steps: int = 0
    refined_rows: list[int] = field(default_factory=list)


@dataclass
class _HistoryTail:
    parent: str
    task: str
    history: tuple[str, ...]
    identity: tuple[str, ...]


def _metadata(row: dict, names: tuple[str, ...]) -> str:
    for values in (row, row.get("metadata"), row.get("meta")):
        if isinstance(values, dict):
            for name in names:
                value = values.get(name)
                if isinstance(value, str) and value.strip():
                    return value.strip()
                if type(value) is int:
                    return str(value)
    return ""


def _message(row: dict, role: str) -> str:
    messages = row.get("messages")
    if not isinstance(messages, list):
        return ""
    for message in reversed(messages):
        if not isinstance(message, dict) or message.get("role") != role:
            continue
        content = message.get("content")
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            return "\n".join(item["text"] for item in content
                             if isinstance(item, dict) and isinstance(item.get("text"), str)).strip()
    return ""


def _json_object(value) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("```") and text.endswith("```"):
            text = text.partition("\n")[2][:-3].strip()
        try:
            decoded = json.loads(text)
        except (ValueError, RecursionError):
            return {}
        if isinstance(decoded, dict):
            return decoded
    return {}


def _action_name(value, *, function_call: bool = False) -> str:
    action = _json_object(value).get("action")
    if isinstance(action, str) and _ACTION_NAME.fullmatch(action.strip()):
        return action.strip().lower()
    if function_call and isinstance(value, str):
        match = _FUNCTION_CALL.match(value)
        if match and value.rstrip().endswith(")"):
            return match[1].lower()
    return "unknown"


def _sample_action(row: dict, source_format: str) -> tuple[str, bool]:
    solution = row.get("solution")
    if source_format == "middle":
        payload = _json_object(solution)
        call = payload.get("sftcall")
        return _action_name(payload.get("gt") if call in (None, "") else call), False
    text = solution if source_format == "data_flywheel" else _message(row, "assistant")
    if not isinstance(text, str):
        return "unknown", False
    calls = _TOOL_CALL.findall(text)
    if len(calls) != 1:
        return "unknown", False
    call = calls[0]
    action = _action_name(call, function_call=source_format == "data_flywheel")
    if action == "unknown" and source_format in {"factory", "health"} and call.endswith("}}"):
        # Some historical terminal tool calls serialized a second closing brace.
        # Recover only when removing that single brace gives an action object.
        recovered = _action_name(call[:-1])
        if recovered != "unknown":
            return recovered, True
    return action, False


def _image_reference(row: dict) -> tuple[str, str]:
    images = row.get("images")
    if not isinstance(images, list) or len(images) != 1 or not isinstance(images[0], str):
        return "", ""
    parent, _, name = images[0].strip().replace("\\", "/").split("?", 1)[0].rpartition("/")
    return parent, name


def _factory_context(prompt: str) -> tuple[str, tuple[str, ...]] | None:
    sections = list(_SECTION.finditer(prompt))
    values = {match[1].lower(): prompt[match.end(): sections[i + 1].start() if i + 1 < len(sections) else len(prompt)].strip()
              for i, match in enumerate(sections)}
    task = " ".join(values.get("task", "").split())
    history = values.get("execution history", "")
    if not task or not history:
        return None
    if history.casefold() in {"empty", "step 1: empty"}:
        return task, ()
    steps = []
    for index, line in enumerate(history.splitlines(), 1):
        match = _HISTORY_STEP.fullmatch(line.strip())
        if not match or index != int(match[1]) or not match[2].strip():
            return None
        steps.append(" ".join(match[2].split()))
    return task, tuple(steps)


def _trajectory_for(row: dict, number: int, source_format: str, prompt: str,
                    previous: _HistoryTail | None) -> tuple[tuple[str, ...], str, _HistoryTail | None]:
    trajectory_id = _metadata(row, ("trajectory_id", "trajectory", "trajectory_identity", "轨迹编号"))
    if trajectory_id:
        return ("explicit", trajectory_id), "explicit", None
    parent, filename = _image_reference(row)
    single = ("sample", str(number))
    if source_format == "data_flywheel" and parent:
        match = _IMAGE_STEP.fullmatch(filename)
        if match:
            return ("image_task", parent, match[1].lower()), "image_task", None
    if source_format in {"factory", "health"} and parent:
        context = _factory_context(prompt)
        if context:
            task, history = context
            continuous = (previous and parent == previous.parent and task == previous.task
                          and len(history) == len(previous.history) + 1 and history[:-1] == previous.history)
            identity = previous.identity if continuous else single
            return identity, "history" if continuous else "single_step", _HistoryTail(parent, task, history, identity)
    return single, "single_step", None


def _issue(row: int | None, field: str, message: str) -> dict:
    return {"row": row, "field": field, "message": message}


def _project(identity: tuple[str, ...], group: _Trajectory, *, data_source: str, data_date: str,
             defaults: tuple[str, ...]) -> dict:
    labels = [next(iter(values)) if len(values) == 1 else default or fallback
              for values, default, (_, _, fallback) in zip(group.labels, defaults, _LABELS)]
    return {
        "trajectory_identity": hashlib.sha256(json.dumps(identity, ensure_ascii=True).encode()).hexdigest(),
        "trajectory_grouping": group.grouping,
        "step数量": group.steps,
        "APP": labels[0], "一级场景": labels[1], "二级场景": labels[2],
        "app_known": _classification_known(labels[0]), "level1_known": _classification_known(labels[1]),
        "level2_known": _classification_known(labels[2]),
        "时间": data_date, "生产方式": data_source,
        "人工精修步骤数量": len(group.refined_rows) if group.known_steps else None,
        "manual_record_source": JSONL_MANUAL_RECORD_SOURCE,
        "manual_known": group.known_steps == group.steps,
        "manual_known_steps": group.known_steps, "manual_unknown_steps": group.steps - group.known_steps,
        "manual_refined_jsonl_rows": group.refined_rows,
        "action_box": dict(sorted(group.actions.items())),
    }


def _summary(rows: list[dict], line_count: int, excluded_rows: int) -> dict:
    apps: dict[str, dict] = {}
    scenes: dict[tuple[str, str], dict] = {}
    actions: Counter = Counter()
    for row in rows:
        app = apps.setdefault(row["APP"], {"name": row["APP"], "trajectory_count": 0, "step_count": 0})
        scene = scenes.setdefault((row["一级场景"], row["二级场景"]), {
            "level1": row["一级场景"], "level2": row["二级场景"], "trajectory_count": 0, "step_count": 0})
        for counts in (app, scene):
            counts["trajectory_count"] += 1
            counts["step_count"] += row["step数量"]
        actions.update(row["action_box"])
    known = sum(row["manual_known_steps"] for row in rows)
    steps = sum(row["step数量"] for row in rows)
    return {"line_count": line_count, "excluded_rows": excluded_rows, "trajectory_count": len(rows),
            "step_count": steps, "sample_count": steps, "apps": list(apps.values()), "scenes": list(scenes.values()),
            "action_counts": dict(sorted(actions.items())), "manual_known_steps": known,
            "manual_unknown_steps": steps - known,
            "manual_refine_steps": sum(row["人工精修步骤数量"] or 0 for row in rows) if known else None,
            "single_step_fallback_count": sum(row["step数量"] for row in rows if row["trajectory_grouping"] == "single_step")}


def parse_external_jsonl(path: Path | str, *, source_format: str, data_source: str, data_date: str,
                         app: str = "", level1: str = "", level2: str = "") -> dict:
    """Read one line at a time; retain counters by trajectory and reported issues."""
    errors: list[dict] = []
    warnings: list[dict] = []
    result = {"parser_version": PARSER_VERSION, "valid": False, "errors": errors, "warnings": warnings,
              "rows": [], "summary": _summary([], 0, 0), "source_sha256": "", "source_size": 0}
    try:
        if date.fromisoformat(data_date).isoformat() != data_date:
            raise ValueError()
    except (ValueError, TypeError):
        errors.append(_issue(None, "data_date", "数据日期必须是有效的 YYYY-MM-DD 日期"))
    if source_format not in SOURCE_FORMATS:
        errors.append(_issue(None, "source_format", "不支持的历史训练数据格式"))
    if not isinstance(data_source, str) or not data_source.strip():
        errors.append(_issue(None, "data_source", "必须填写数据来源"))
    if not all(isinstance(value, str) for value in (app, level1, level2)):
        errors.append(_issue(None, "classification", "默认 App 和场景必须为文本"))
    path = Path(path)
    if path.suffix.lower() != ".jsonl":
        errors.append(_issue(None, "file", "历史训练数据须为 .jsonl 文件"))
    if errors:
        return result

    digest = hashlib.sha256()
    size = line_count = excluded_rows = 0
    groups: dict[tuple[str, ...], _Trajectory] = {}
    previous = None
    try:
        with io_path(path).open("rb") as stream:
            for line_count, raw in enumerate(stream, 1):
                digest.update(raw)
                size += len(raw)
                try:
                    line = raw.decode("utf-8-sig" if line_count == 1 else "utf-8").strip()
                    if not line:
                        continue
                    row = json.loads(line)
                except (ValueError, RecursionError):
                    warnings.append(_issue(line_count, "sample", "JSONL 行格式或编码无效，统计已排除"))
                    excluded_rows += 1
                    previous = None
                    continue
                prompt = _message(row, "user") if isinstance(row, dict) else ""
                solution = row.get("solution") if isinstance(row, dict) else None
                if not isinstance(row, dict) or not (prompt or (isinstance(solution, (str, dict)) and solution)):
                    warnings.append(_issue(line_count, "sample", "缺少训练样本正文，统计已排除"))
                    excluded_rows += 1
                    previous = None
                    continue
                identity, grouping, previous = _trajectory_for(row, line_count, source_format, prompt, previous)
                group = groups.get(identity)
                if group is None:
                    group = groups[identity] = _Trajectory(grouping)
                if grouping == "history":
                    group.grouping = grouping
                group.steps += 1
                action, recovered = _sample_action(row, source_format)
                group.actions[action] += 1
                if action == "unknown":
                    warnings.append(_issue(line_count, "action", "动作无法识别，已计入 unknown"))
                elif recovered:
                    warnings.append(_issue(line_count, "action", "已解析工具调用中多出的单个右括号，原文件保持原样"))
                for labels, (label, names, _) in zip(group.labels, _LABELS):
                    value = _metadata(row, names)
                    if value and _classification_known(value):
                        if len(labels) == 1 and value not in labels:
                            warnings.append(_issue(line_count, label, "轨迹分类有冲突，使用清单分类或未分类"))
                        labels.add(value)
                refined = row.get("is_refined")
                if isinstance(refined, bool) or (type(refined) is int and refined in (0, 1)):
                    group.known_steps += 1
                    if refined:
                        group.refined_rows.append(line_count)
    except OSError as exc:
        errors.append(_issue(None, "file", f"无法读取 JSONL：{exc.strerror or type(exc).__name__}"))
        return result

    defaults = tuple(value.strip() for value in (app, level1, level2))
    rows = [_project(identity, group, data_source=data_source.strip(), data_date=data_date, defaults=defaults)
            for identity, group in groups.items()]
    if not rows:
        errors.append(_issue(None, "file", "JSONL 没有可统计的训练样本"))
    result.update(valid=not errors, rows=rows, source_sha256=digest.hexdigest(), source_size=size,
                  summary=_summary(rows, line_count, excluded_rows))
    return result
