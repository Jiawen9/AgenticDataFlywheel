"""Generate initial rubrics for the Jiawen mobile-GUI task.

This script is intentionally separate from ``supply_chain_eval.py`` so rubric
generation can be studied without immediately evaluating the same trajectories.

The default prompt uses task information plus observable environment evidence.
It does not treat the observed rank-1 title as a fixed answer, because the
ranking can change over time. Optional calibration contrast can be enabled with
environment variables, but it is off by default to avoid overfitting.

PowerShell example:
    $env:ADARUBRIC_BASE_URL="http://localhost:8000/v1"
    $env:ADARUBRIC_API_KEY="EMPTY"
    $env:ADARUBRIC_MODEL="your-local-model-name"
    .venv\\Scripts\\python examples\\generate-jiawen-rubrics.py

Experiment settings are read from:
    examples/jiawen_rubric_config.json

You can choose another config file with:
    .venv\\Scripts\\python examples\\generate-jiawen-rubrics.py --config path\\to\\config.json

Environment variables are still supported as temporary overrides:
    ADARUBRIC_JIAWEN_WORKBOOK
    ADARUBRIC_PROCESS_ALL_TASKS
    ADARUBRIC_TASK_IDS
    ADARUBRIC_RUBRIC_PATH
    ADARUBRIC_EVIDENCE_PATH
    ADARUBRIC_RAW_RESPONSE_PATH
    ADARUBRIC_NUM_DIMENSIONS
    ADARUBRIC_TEMPERATURE
    ADARUBRIC_MAX_TOKENS
    ADARUBRIC_INCLUDE_FEW_SHOT
    ADARUBRIC_REPAIR_INVALID_JSON
    ADARUBRIC_VALIDATE_RUBRIC
    ADARUBRIC_VALIDATION_ATTEMPTS
    ADARUBRIC_INCLUDE_CALIBRATION_CONTRAST
    ADARUBRIC_CALIBRATION_TRAJECTORY_IDS
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from adarubric import DynamicRubric, TaskDescription, Trajectory
from adarubric.core.exceptions import RubricGenerationError
from adarubric.generator.prompts import (
    RUBRIC_GENERATION_FEW_SHOT,
    RUBRIC_GENERATION_SYSTEM,
    RUBRIC_GENERATION_USER,
)
from adarubric.generator.validation import OpenAIEmbeddingProvider, RubricValidator
from adarubric.llm.json_extract import extract_json_substring, strip_thinking
from adarubric.llm.openai_client import OpenAIClient, parse_extra_body

PROJECT_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = PROJECT_ROOT.parents[1]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "examples" / "jiawen_rubric_config.json"
DEFAULT_WORKBOOK = (
    REPOSITORY_ROOT / "backend_workspace" / "rubric_trajectories.xlsx"
)
DEFAULT_OUTPUT_ROOT = REPOSITORY_ROOT / "backend_workspace" / "rubric_outputs"
DEFAULT_RUBRIC_PATH = DEFAULT_OUTPUT_ROOT / "rubrics" / "jiawen_gui_initial_rubric.json"
DEFAULT_EVIDENCE_PATH = (
    DEFAULT_OUTPUT_ROOT / "rubrics" / "jiawen_gui_initial_rubric.evidence.md"
)
DEFAULT_RAW_RESPONSE_PATH = (
    DEFAULT_OUTPUT_ROOT / "rubrics" / "jiawen_gui_initial_rubric.raw_response.txt"
)

TITLE_PATTERN = re.compile(r"《([^》]+)》")
Config = dict[str, Any]

JIAWEN_SYSTEM_EXTENSION = """\
### Jiawen GUI Evidence Extension

When supplemental environment evidence is provided, use it only to identify
observable GUI states and completion boundaries. Do not hard-code volatile
observed content such as a currently ranked title; evaluate whether the agent
selects the current ranked target at evaluation time."""

JIAWEN_USER_EXTENSION = """\
### Supplemental Observable Environment Evidence

{environment_evidence}

### Optional Calibration Contrast

{calibration_contrast}

### Rubric Requirements For This Mobile GUI Task

- Derive the target app, target objects, required UI state, and final completion
  condition from the Task Information rather than from a hard-coded prior task.
- If the task refers to dynamic content such as "current", "previously watched",
  "ranked first", "unread", or "all completed" items, evaluate whether the agent
  handles the content visible at evaluation time. Do not hard-code observed item
  names from the evidence.
- Distinguish final task completion from merely reaching a relevant page,
  identifying candidate items, opening an item, or stopping before the requested
  operation is finished.
- Include common mobile-GUI interruptions only when they affect completion:
  permission dialogs, confirmation dialogs, advertising, loading states, identity
  verification, and premature termination.
- Return exactly {num_dimensions} dimensions."""

RUBRIC_JSON_CONTRACT = """\
You MUST return only one valid JSON object. Do not use markdown fences, comments,
or explanatory prose outside the JSON.

The JSON object must have this shape:
{{
  "task_id": "same task id as provided",
  "dimensions": [
    {{
      "name": "ConcisePascalCaseName",
      "description": "at least 10 characters",
      "weight": 0.2,
      "scoring_criteria": {{
        "1": "concrete behavior for score 1",
        "2": "concrete behavior for score 2",
        "3": "concrete behavior for score 3",
        "4": "concrete behavior for score 4",
        "5": "concrete behavior for score 5"
      }}
    }}
  ],
  "generation_rationale": "brief rationale"
}}

Rules:
- dimensions must contain exactly {num_dimensions} objects.
- dimension weights must sum to 1.0.
- scoring_criteria must contain exactly string keys "1", "2", "3", "4", "5".
- do not include any observed volatile title from the evidence."""

JSON_REPAIR_SYSTEM = """\
You repair malformed rubric outputs into valid JSON.
Return only one valid JSON object and preserve the intended rubric content.
Do not add markdown fences, comments, or explanatory prose."""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate an initial AdaRubric rubric for the Jiawen GUI dataset."
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help=f"JSON config path. Defaults to {DEFAULT_CONFIG_PATH}",
    )
    return parser.parse_args()


def load_config(path: Path) -> Config:
    if not path.exists():
        print(f"Config file not found, using built-in defaults: {path}")
        return {}

    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"Config file must contain a JSON object: {path}")
    return data


def _setting(config: Config, key: str, env_name: str, default: Any) -> Any:
    if env_name in os.environ:
        return os.environ[env_name]
    return config.get(key, default)


def _bool_setting(config: Config, key: str, env_name: str, *, default: bool) -> bool:
    configured = _setting(config, key, env_name, default)
    if isinstance(configured, bool):
        return configured
    if configured is None:
        return default
    return str(configured).strip().lower() in {"1", "true", "yes", "y", "on"}


def _int_setting(
    config: Config,
    key: str,
    env_name: str,
    *,
    default: int,
    minimum: int = 1,
) -> int:
    configured = _setting(config, key, env_name, default)
    try:
        value = int(configured)
    except (TypeError, ValueError):
        raise ValueError(f"{key} / {env_name} must be an integer, got {configured!r}") from None
    if value < minimum:
        raise ValueError(f"{key} / {env_name} must be >= {minimum}, got {value}")
    return value


def _float_setting(config: Config, key: str, env_name: str, *, default: float) -> float:
    configured = _setting(config, key, env_name, default)
    try:
        return float(configured)
    except (TypeError, ValueError):
        raise ValueError(f"{key} / {env_name} must be a float, got {configured!r}") from None


def _path_setting(config: Config, key: str, env_name: str, *, default: Path) -> Path:
    configured = _setting(config, key, env_name, None)
    if not configured:
        return default
    path = Path(str(configured))
    return path if path.is_absolute() else PROJECT_ROOT / path


def _list_setting(config: Config, key: str, env_name: str) -> list[str]:
    configured = os.environ[env_name] if env_name in os.environ else config.get(key, [])

    if configured is None or configured == "":
        return []
    if isinstance(configured, str):
        return [item.strip() for item in configured.split(",") if item.strip()]
    if isinstance(configured, list):
        return [str(item).strip() for item in configured if str(item).strip()]
    raise ValueError(f"{key} / {env_name} must be a list or comma-separated string")


def _extra_body_setting(config: Config) -> dict[str, Any] | None:
    if "extra_body" in config:
        if config["extra_body"] in (None, ""):
            return {}
        return parse_extra_body(config["extra_body"], source="extra_body")
    if "ADARUBRIC_EXTRA_BODY_JSON" in os.environ:
        return parse_extra_body(
            os.environ["ADARUBRIC_EXTRA_BODY_JSON"],
            source="ADARUBRIC_EXTRA_BODY_JSON",
        )
    return None


def _safe_filename_part(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", value.strip())
    return safe.strip("._") or "task"


def _path_for_task(path: Path, task_id: str, *, multiple_tasks: bool) -> Path:
    if not multiple_tasks:
        return path
    suffix = _safe_filename_part(task_id)
    return path.with_name(f"{path.stem}__{suffix}{path.suffix}")


def _config_for_task_outputs(
    config: Config,
    task: TaskDescription,
    *,
    multiple_tasks: bool,
) -> Config:
    task_config = dict(config)
    for key, env_name, default in (
        ("rubric_path", "ADARUBRIC_RUBRIC_PATH", DEFAULT_RUBRIC_PATH),
        ("evidence_path", "ADARUBRIC_EVIDENCE_PATH", DEFAULT_EVIDENCE_PATH),
        ("raw_response_path", "ADARUBRIC_RAW_RESPONSE_PATH", DEFAULT_RAW_RESPONSE_PATH),
    ):
        path = _path_setting(config, key, env_name, default=default)
        task_config[key] = str(_path_for_task(path, task.task_id, multiple_tasks=multiple_tasks))
    return task_config


def _task_ids_setting(config: Config) -> list[str]:
    task_ids = _list_setting(config, "task_ids", "ADARUBRIC_TASK_IDS")
    legacy_task_id = _setting(config, "task_id", "ADARUBRIC_TASK_ID", None)
    if legacy_task_id and not task_ids:
        task_ids = [str(legacy_task_id).strip()]
    return task_ids


def _load_jiawen_objects(
    config: Config,
) -> tuple[dict[str, TaskDescription], list[Trajectory], Path]:
    try:
        from trajectory_tools.excel_to_object import load_objects
    except ModuleNotFoundError as exc:
        if exc.name == "openpyxl":
            raise RuntimeError(
                "jiawen-dataset requires openpyxl. Install it with: "
                ".venv\\Scripts\\python -m pip install openpyxl"
            ) from exc
        raise

    workbook_path = _path_setting(
        config,
        "workbook_path",
        "ADARUBRIC_JIAWEN_WORKBOOK",
        default=DEFAULT_WORKBOOK,
    )
    tasks_by_id, trajectories = load_objects(workbook_path)
    if not tasks_by_id:
        raise ValueError("jiawen-dataset returned no TaskDescription objects")

    return tasks_by_id, trajectories, workbook_path


def _select_tasks(config: Config, tasks_by_id: dict[str, TaskDescription]) -> list[TaskDescription]:
    if _bool_setting(
        config,
        "process_all_tasks",
        "ADARUBRIC_PROCESS_ALL_TASKS",
        default=True,
    ):
        return list(tasks_by_id.values())

    task_ids = _task_ids_setting(config)
    if not task_ids:
        return [next(iter(tasks_by_id.values()))]

    missing = [task_id for task_id in task_ids if task_id not in tasks_by_id]
    if missing:
        available = ", ".join(sorted(tasks_by_id))
        raise ValueError(f"Unknown task id(s): {missing}. Available: {available}")
    return [tasks_by_id[task_id] for task_id in task_ids]


def _trajectories_for_task(
    task: TaskDescription,
    trajectories: list[Trajectory],
) -> list[Trajectory]:
    selected = [trajectory for trajectory in trajectories if trajectory.task_id == task.task_id]
    if not selected:
        raise ValueError(f"No trajectories found for task_id={task.task_id}")
    return selected


def _all_text(trajectory: Trajectory) -> str:
    parts: list[str] = []
    if trajectory.final_answer:
        parts.append(trajectory.final_answer)
    for step in trajectory.steps:
        parts.append(step.action)
        parts.append(step.observation)
    return "\n".join(parts)


def _extract_observed_titles(trajectories: Iterable[Trajectory]) -> set[str]:
    titles: set[str] = set()
    for trajectory in trajectories:
        for match in TITLE_PATTERN.finditer(_all_text(trajectory)):
            title = match.group(1).strip()
            if title:
                titles.add(title)
    return titles


def _contains_any(text: str, keywords: Iterable[str]) -> bool:
    return any(keyword in text for keyword in keywords)


def _summarize_environment(trajectories: list[Trajectory]) -> str:
    """Create aggregate UI evidence without exposing volatile show titles."""
    corpus = "\n".join(_all_text(trajectory) for trajectory in trajectories)

    signals = [
        (
            "Mobile home/app launch",
            _contains_any(corpus, ["手机主屏幕", "应用图标", "打开应用", "启动应用"]),
            "The agent can be observed launching an app from the mobile home screen.",
        ),
        (
            "App home and navigation",
            _contains_any(corpus, ["首页", "导航", "底部", "标签", "频道", "我的", "设置"]),
            "The app can expose home, tab, channel, profile, settings, or menu navigation.",
        ),
        (
            "Task-relevant list or content area",
            _contains_any(
                corpus,
                ["列表", "记录", "历史", "播放记录", "收藏", "排行", "搜索", "结果"],
            ),
            (
                "Relevant pages may contain lists, records, history entries, "
                "search results, or ranked items."
            ),
        ),
        (
            "Target item selection",
            _contains_any(corpus, ["选中", "点击", "选择", "排名第一", "已看完", "全部", "目标"]),
            (
                "The agent may need to identify and select target items from the "
                "current visible UI state."
            ),
        ),
        (
            "Requested operation",
            _contains_any(corpus, ["播放", "删除", "清除", "确认", "提交", "完成", "取消", "关闭"]),
            (
                "The requested operation should be completed, not merely prepared "
                "or partially initiated."
            ),
        ),
        (
            "Completion or final state",
            _contains_any(
                corpus,
                ["完成", "成功", "无阻塞", "已删除", "未开始", "未完成", "详情页"],
            ),
            (
                "Completion should be judged from the observable final screen, "
                "final answer, and whether requested changes are visible."
            ),
        ),
        (
            "Completion blockers",
            _contains_any(corpus, ["广告", "弹窗", "身份核实", "加载", "阻塞", "权限", "确认弹窗"]),
            (
                "Common completion blockers include ads, permission dialogs, "
                "confirmation dialogs, loading states, and identity verification."
            ),
        ),
    ]

    lines = [
        "The following evidence is aggregated from stable screenshot observations.",
        "It describes reusable UI states, not fixed answers.",
        "",
        "Observed UI signals:",
    ]
    for name, present, description in signals:
        status = "observed" if present else "not observed"
        lines.append(f"- {name}: {status}. {description}")

    lines.extend(
        [
            "",
            "Important anti-overfitting note:",
            (
                "- Any concrete item name observed in the collected trajectories "
                "may be temporary UI content."
            ),
            (
                "- The rubric must evaluate the task's requested dynamic target or operation, "
                "not a fixed observed item name."
            ),
            (
                "- Do not include exact coordinates, trajectory IDs, step counts, or "
                "observed show titles in dimension names or scoring criteria."
            ),
        ]
    )
    return "\n".join(lines)


def _completion_pattern(trajectory: Trajectory) -> str:
    text = _all_text(trajectory)
    if _contains_any(text, ["身份核实", "验证", "阻塞", "未观察到", "弹窗", "加载"]):
        return (
            "blocked: reaches a relevant task context but does not show clear "
            "completion because of interruption or blocking states"
        )
    if _contains_any(text, ["未完成", "未执行", "未开始", "仅停留", "停留在"]):
        return (
            "partial: reaches a relevant task context but stops before completing "
            "the requested operation"
        )
    if _contains_any(text, ["成功", "完成", "无阻塞", "已删除", "正在播放", "流程顺畅"]):
        return (
            "near-complete or complete: performs the requested task path and "
            "reaches an observable completion state"
        )
    return "unknown: useful trajectory evidence exists, but the completion boundary is ambiguous"


def _calibration_ids_for_task(trajectories: list[Trajectory], config: Config) -> set[str]:
    task_id = trajectories[0].task_id if trajectories else ""
    by_task = config.get("calibration_trajectory_ids_by_task", {})
    use_task_specific_ids = (
        isinstance(by_task, dict)
        and task_id in by_task
        and "ADARUBRIC_CALIBRATION_TRAJECTORY_IDS" not in os.environ
    )
    if use_task_specific_ids:
        configured = by_task[task_id]
        if isinstance(configured, str):
            return {item.strip() for item in configured.split(",") if item.strip()}
        if isinstance(configured, list):
            return {str(item).strip() for item in configured if str(item).strip()}
        raise ValueError(
            f"calibration_trajectory_ids_by_task[{task_id!r}] must be a list or string"
        )

    return set(
        _list_setting(
            config,
            "calibration_trajectory_ids",
            "ADARUBRIC_CALIBRATION_TRAJECTORY_IDS",
        )
    )


def _calibration_trajectories(trajectories: list[Trajectory], config: Config) -> list[Trajectory]:
    requested = _calibration_ids_for_task(trajectories, config)
    if not requested:
        return []
    selected = [
        trajectory
        for trajectory in trajectories
        if trajectory.trajectory_id in requested
        or str(trajectory.metadata.get("source_session_id", "")) in requested
    ]
    missing = requested - {
        trajectory.trajectory_id for trajectory in selected
    } - {str(trajectory.metadata.get("source_session_id", "")) for trajectory in selected}
    if missing:
        raise ValueError(f"Unknown calibration trajectory id(s): {sorted(missing)}")
    return selected


def _summarize_calibration_contrast(trajectories: list[Trajectory], config: Config) -> str:
    if not _bool_setting(
        config,
        "include_calibration_contrast",
        "ADARUBRIC_INCLUDE_CALIBRATION_CONTRAST",
        default=False,
    ):
        return (
            "No calibration trajectories are provided by default. Generate the "
            "initial rubric from the task and reusable environment evidence only."
        )

    selected = _calibration_trajectories(trajectories, config)
    if not selected:
        return (
            "Calibration contrast was requested, but no explicit "
            "ADARUBRIC_CALIBRATION_TRAJECTORY_IDS were provided. Do not infer "
            "labels from all trajectories."
        )

    lines = [
        "Use these examples only to understand completion boundaries.",
        "Do not copy their IDs, coordinates, step counts, or observed titles into the rubric.",
        "",
    ]
    for index, trajectory in enumerate(selected, 1):
        lines.append(f"- Calibration example {index}: {_completion_pattern(trajectory)}.")
    return "\n".join(lines)


def build_messages(
    task: TaskDescription,
    trajectories: list[Trajectory],
    *,
    config: Config,
    num_dimensions: int,
) -> list[dict[str, str]]:
    environment_evidence = _summarize_environment(trajectories)
    calibration_contrast = _summarize_calibration_contrast(trajectories, config)
    system_content = RUBRIC_GENERATION_SYSTEM.format(num_dimensions=num_dimensions)
    if _bool_setting(config, "include_few_shot", "ADARUBRIC_INCLUDE_FEW_SHOT", default=False):
        system_content += "\n\n" + RUBRIC_GENERATION_FEW_SHOT
    system_content += "\n\n" + JIAWEN_SYSTEM_EXTENSION

    base_user_content = RUBRIC_GENERATION_USER.format(
        task_id=task.task_id,
        instruction=task.instruction,
        domain=task.domain or "General",
        complexity=task.complexity.value,
        expected_tools=", ".join(task.expected_tools) if task.expected_tools else "Not specified",
        context=json.dumps(task.context, ensure_ascii=False) if task.context else "None",
    )
    final_instruction = "Generate the evaluation rubric now."
    if base_user_content.endswith(final_instruction):
        base_user_content = base_user_content[: -len(final_instruction)].rstrip()

    user_content = base_user_content + "\n\n" + JIAWEN_USER_EXTENSION.format(
        environment_evidence=environment_evidence,
        calibration_contrast=calibration_contrast,
        num_dimensions=num_dimensions,
    )
    user_content += (
        "\n\n"
        + RUBRIC_JSON_CONTRACT.format(num_dimensions=num_dimensions)
        + "\n\nGenerate the evaluation rubric now."
    )

    return [
        {"role": "system", "content": system_content},
        {"role": "user", "content": user_content},
    ]


def _evidence_from_messages(
    messages: list[dict[str, str]],
    workbook_path: Path,
    config: Config,
    trajectories: list[Trajectory],
) -> str:
    include_few_shot = _bool_setting(
        config,
        "include_few_shot",
        "ADARUBRIC_INCLUDE_FEW_SHOT",
        default=False,
    )
    include_calibration_contrast = _bool_setting(
        config,
        "include_calibration_contrast",
        "ADARUBRIC_INCLUDE_CALIBRATION_CONTRAST",
        default=False,
    )
    calibration_ids = sorted(_calibration_ids_for_task(trajectories, config))
    lines = [
        "# Jiawen GUI Initial Rubric Generation Evidence",
        "",
        f"- Workbook: `{workbook_path}`",
        f"- Model: `{os.environ.get('ADARUBRIC_MODEL', 'gpt-4o')}`",
        f"- AdaRubric few-shot enabled: `{include_few_shot}`",
        f"- Calibration contrast enabled: `{include_calibration_contrast}`",
        f"- Calibration trajectory ids: `{calibration_ids}`",
        "",
    ]
    for message in messages:
        lines.append(f"## {message['role'].title()} Prompt")
        lines.append("")
        lines.append(message["content"])
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _write_raw_response(path: Path, *, stage: str, raw: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"## {stage}\n\n{strip_thinking(raw).rstrip()}\n", encoding="utf-8")


def _rubric_text(rubric: DynamicRubric) -> str:
    return rubric.model_dump_json(indent=2)


def _find_forbidden_title_usage(rubric: DynamicRubric, titles: set[str]) -> list[str]:
    if not titles:
        return []
    rubric_json = _rubric_text(rubric)
    return sorted(title for title in titles if title and title in rubric_json)


def _correct_task_id(rubric: DynamicRubric, task: TaskDescription) -> DynamicRubric:
    if rubric.task_id == task.task_id:
        return rubric
    return rubric.model_copy(update={"task_id": task.task_id})


def _parse_rubric_json(raw: str) -> DynamicRubric:
    extracted = extract_json_substring(raw)
    return DynamicRubric.model_validate_json(extracted)


async def _repair_rubric_json(
    client: OpenAIClient,
    *,
    raw: str,
    task: TaskDescription,
    num_dimensions: int,
    max_tokens: int,
    raw_response_path: Path,
) -> DynamicRubric:
    repair_messages = [
        {"role": "system", "content": JSON_REPAIR_SYSTEM},
        {
            "role": "user",
            "content": (
                RUBRIC_JSON_CONTRACT.format(num_dimensions=num_dimensions)
                + "\n\nThe task_id must be: "
                + task.task_id
                + "\n\nMalformed rubric output to repair:\n"
                + raw
            ),
        },
    ]
    repaired = await client.generate_text(
        repair_messages,
        temperature=0.0,
        max_tokens=max_tokens,
    )
    _write_raw_response(raw_response_path, stage="json_repair_raw_response", raw=repaired)
    return _parse_rubric_json(repaired)


async def _generate_rubric_json(
    client: OpenAIClient,
    *,
    config: Config,
    messages: list[dict[str, str]],
    task: TaskDescription,
    num_dimensions: int,
    temperature: float,
    max_tokens: int,
    raw_response_path: Path,
) -> DynamicRubric:
    raw = await client.generate_text(
        messages,
        temperature=temperature,
        max_tokens=max_tokens,
    )
    _write_raw_response(raw_response_path, stage="rubric_raw_response", raw=raw)

    try:
        return _parse_rubric_json(raw)
    except (ValidationError, json.JSONDecodeError, ValueError):
        if not _bool_setting(
            config,
            "repair_invalid_json",
            "ADARUBRIC_REPAIR_INVALID_JSON",
            default=True,
        ):
            raise
        return await _repair_rubric_json(
            client,
            raw=raw,
            task=task,
            num_dimensions=num_dimensions,
            max_tokens=max_tokens,
            raw_response_path=raw_response_path,
        )


def build_validator(config: Config) -> RubricValidator | None:
    if not _bool_setting(config, "validate_rubric", "ADARUBRIC_VALIDATE_RUBRIC", default=True):
        return None

    embedding_api_key = (
        str(_setting(config, "embedding_api_key", "ADARUBRIC_EMBEDDING_API_KEY", "") or "")
        or os.environ.get("ADARUBRIC_API_KEY")
        or os.environ.get("OPENAI_API_KEY")
        or "EMPTY"
    )
    return RubricValidator(
        OpenAIEmbeddingProvider(
            model=str(
                _setting(
                    config,
                    "embedding_model",
                    "ADARUBRIC_EMBEDDING_MODEL",
                    "text-embedding-3-small",
                )
            ),
            base_url=_setting(config, "embedding_base_url", "ADARUBRIC_EMBEDDING_BASE_URL", None),
            api_key=embedding_api_key,
        )
    )


async def generate_rubric(
    *,
    task: TaskDescription,
    trajectories: list[Trajectory],
    messages: list[dict[str, str]],
    config: Config,
    num_dimensions: int,
) -> DynamicRubric:
    client = OpenAIClient(
        model=str(_setting(config, "model", "ADARUBRIC_MODEL", "gpt-4o")),
        base_url=_setting(config, "base_url", "ADARUBRIC_BASE_URL", None),
        api_key=str(
            _setting(config, "api_key", "ADARUBRIC_API_KEY", None)
            or os.environ.get("OPENAI_API_KEY")
            or "EMPTY"
        ),
        extra_body=_extra_body_setting(config),
    )
    validator = build_validator(config)
    attempts = _int_setting(
        config,
        "validation_attempts",
        "ADARUBRIC_VALIDATION_ATTEMPTS",
        default=10,
    )
    temperature = _float_setting(config, "temperature", "ADARUBRIC_TEMPERATURE", default=0.0)
    max_tokens = _int_setting(config, "max_tokens", "ADARUBRIC_MAX_TOKENS", default=4096)
    raw_response_path = _path_setting(
        config,
        "raw_response_path",
        "ADARUBRIC_RAW_RESPONSE_PATH",
        default=DEFAULT_RAW_RESPONSE_PATH,
    )
    observed_titles = _extract_observed_titles(trajectories)
    last_error = ""

    try:
        for attempt in range(1, attempts + 1):
            print(f"Generating Jiawen rubric attempt {attempt}/{attempts}")
            try:
                rubric = await _generate_rubric_json(
                    client,
                    config=config,
                    messages=messages,
                    task=task,
                    num_dimensions=num_dimensions,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    raw_response_path=raw_response_path,
                )
            except Exception as exc:
                last_error = f"invalid JSON rubric output: {exc}"
                print(f"Rejected rubric: {last_error}")
                continue

            rubric = _correct_task_id(rubric, task)

            if len(rubric.dimensions) != num_dimensions:
                last_error = (
                    f"expected exactly {num_dimensions} dimensions, "
                    f"got {len(rubric.dimensions)}"
                )
                print(f"Rejected rubric: {last_error}")
                continue

            forbidden_titles = _find_forbidden_title_usage(rubric, observed_titles)
            if forbidden_titles:
                last_error = (
                    "rubric hard-coded volatile observed title(s): "
                    + ", ".join(forbidden_titles)
                )
                print(f"Rejected rubric: {last_error}")
                continue

            if validator is not None:
                validation = await validator.validate(rubric, task)
                if not validation.valid:
                    last_error = validation.summary()
                    print(f"Rejected rubric by validation: {last_error}")
                    continue

            return rubric

        raise RubricGenerationError(
            f"Failed to generate a valid Jiawen rubric after {attempts} attempt(s)",
            context={"last_error": last_error, "raw_response_path": str(raw_response_path)},
        )
    finally:
        await client.close()


async def main() -> None:
    args = parse_args()
    config = load_config(args.config)
    tasks_by_id, all_trajectories, workbook_path = _load_jiawen_objects(config)
    tasks = _select_tasks(config, tasks_by_id)
    multiple_tasks = len(tasks) > 1
    num_dimensions = _int_setting(
        config,
        "num_dimensions",
        "ADARUBRIC_NUM_DIMENSIONS",
        default=5,
        minimum=1,
    )
    print(f"Loaded config from {args.config}")
    print(
        f"Loaded workbook with {len(tasks_by_id)} task(s) and "
        f"{len(all_trajectories)} trajectory/trajectories"
    )
    print(f"Generating rubric(s) for {len(tasks)} selected task(s)")

    for task in tasks:
        trajectories = _trajectories_for_task(task, all_trajectories)
        task_config = _config_for_task_outputs(config, task, multiple_tasks=multiple_tasks)
        rubric_path = _path_setting(
            task_config,
            "rubric_path",
            "ADARUBRIC_RUBRIC_PATH",
            default=DEFAULT_RUBRIC_PATH,
        )
        evidence_path = _path_setting(
            task_config,
            "evidence_path",
            "ADARUBRIC_EVIDENCE_PATH",
            default=DEFAULT_EVIDENCE_PATH,
        )

        messages = build_messages(
            task,
            trajectories,
            config=task_config,
            num_dimensions=num_dimensions,
        )
        evidence_path.parent.mkdir(parents=True, exist_ok=True)
        evidence_path.write_text(
            _evidence_from_messages(messages, workbook_path, task_config, trajectories),
            encoding="utf-8",
        )

        rubric = await generate_rubric(
            task=task,
            trajectories=trajectories,
            messages=messages,
            config=task_config,
            num_dimensions=num_dimensions,
        )

        rubric_path.parent.mkdir(parents=True, exist_ok=True)
        rubric_path.write_text(_rubric_text(rubric) + "\n", encoding="utf-8")

        print("")
        print(f"Task {task.task_id}: {len(trajectories)} trajectory/trajectories")
        print(f"Saved generation evidence to {evidence_path}")
        print(f"Saved rubric to {rubric_path}")
        print("Dimensions:")
        for dimension in rubric.dimensions:
            print(f"- [{dimension.weight:.2f}] {dimension.name}: {dimension.description}")


if __name__ == "__main__":
    asyncio.run(main())
