"""Pure direct-judge baseline for Jiawen GUI trajectory ranking.

Baseline A intentionally does not use AdaRubric rubrics or human labels. For
each selected task, it sends only the task instruction and each candidate
trajectory's observable Action / Action Input / Observation sequence to an LLM,
then asks the LLM to rank the trajectories from best to worst with reasons.
"""

from __future__ import annotations

import argparse
import ast
import asyncio
import csv
import importlib.util
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from statistics import mean, pstdev
from typing import Any

from pydantic import BaseModel, Field, ValidationError, model_validator

from adarubric import TaskDescription, Trajectory
from adarubric.core.exceptions import LLMClientError
from adarubric.llm.json_extract import extract_json_substring, strip_thinking
from adarubric.llm.openai_client import OpenAIClient

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "examples" / "jiawen_rubric_config.json"
DEFAULT_JSONL_PATH = (
    PROJECT_ROOT / "docs" / "evaluation_outputs" / "jiawen_direct_judge_baseline.jsonl"
)
DEFAULT_REPORT_PATH = (
    PROJECT_ROOT / "docs" / "evaluation_outputs" / "jiawen_direct_judge_baseline.md"
)
DEFAULT_CSV_PATH = (
    PROJECT_ROOT / "docs" / "evaluation_outputs" / "jiawen_direct_judge_baseline.csv"
)
DEFAULT_RAW_RESPONSE_PATH = (
    PROJECT_ROOT / "docs" / "evaluation_outputs" / "jiawen_direct_judge_baseline.raw_response.jsonl"
)
GENERATOR_SCRIPT = PROJECT_ROOT / "examples" / "generate-jiawen-rubrics.py"

Config = dict[str, Any]
BaselineKey = tuple[str, int, str]


DIRECT_JUDGE_SYSTEM = """\
You are an impartial evaluator of mobile-GUI agent trajectories.

You will compare multiple candidate trajectories for the same task. Use only
the observable Action, Action Input, and Observation text provided. Do not assume
hidden intentions, do not use chain-of-thought, and do not rely on hidden
annotations.

Rank trajectories from best to worst according to whether they actually achieve
the user's task goal and how well the observable execution supports that result."""

DIRECT_JUDGE_COMPLETION_FIRST = """\
Completion of the requested task is the most important criterion. If completion
evidence is ambiguous, treat it conservatively and explain the uncertainty."""

DIRECT_JUDGE_USER = """\
### Task

Task ID: {task_id}
Instruction: {instruction}

### Candidate Trajectories

{trajectory_text}

### Required Output

Rank all candidate trajectories from best to worst. Provide concise observable
reasons for each ranking decision.\
"""

DIRECT_JUDGE_JSON_CONTRACT = """\

Return only one valid JSON object. Do not use markdown fences or prose outside
the JSON. The JSON object must have this exact shape:

{{
  "task_id": "{task_id}",
  "ranking": [
    {{
      "rank": 1,
      "trajectory_id": "C1",
      "reason": "concise observable reason"
    }}
  ],
  "overall_reason": "brief overall comparison"
}}

Rules:
- ranking must include every candidate trajectory exactly once.
- rank must be consecutive integers starting at 1.
- Prefer exact trajectory_id values when possible.
- If an ID is too long to copy safely, use its candidate label instead: {candidate_labels}.
- Never output descriptive placeholder text; use only listed IDs or labels.
- Do not include scores, pass/fail labels, hidden thoughts, or final_answer.\
"""

JSON_REPAIR_SYSTEM = """\
You repair malformed direct-judge ranking outputs into valid JSON.
Return only one valid JSON object and preserve the intended ranking.
Do not add markdown fences, comments, or explanatory prose."""


class DirectJudgeRank(BaseModel):
    rank: int = Field(ge=1)
    trajectory_id: str
    reason: str


class DirectJudgeResponse(BaseModel):
    task_id: str = ""
    ranking: list[DirectJudgeRank]
    overall_reason: str = ""

    @model_validator(mode="after")
    def _validate_ranks_unique(self) -> DirectJudgeResponse:
        ranks = [item.rank for item in self.ranking]
        if len(set(ranks)) != len(ranks):
            raise ValueError("ranking ranks must be unique")
        return self


@dataclass(frozen=True)
class DirectJudgeResult:
    task: TaskDescription
    run_number: int
    candidate_ids: list[str]
    response: DirectJudgeResponse

    @property
    def candidate_key(self) -> str:
        return ",".join(self.candidate_ids)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run a pure LLM-as-a-Judge ranking baseline for Jiawen GUI tasks."
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help=f"JSON config path. Defaults to {DEFAULT_CONFIG_PATH}",
    )
    return parser.parse_args()


def _load_generation_module() -> Any:
    spec = importlib.util.spec_from_file_location("generate_jiawen_rubrics", GENERATOR_SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load generator helpers from {GENERATOR_SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["generate_jiawen_rubrics"] = module
    spec.loader.exec_module(module)
    return module


GEN = _load_generation_module()


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


def _ids_by_task(config: Config, key: str, task_id: str) -> set[str]:
    configured = config.get(key, {})
    if not isinstance(configured, dict) or task_id not in configured:
        return set()

    value = configured[task_id]
    if value is None or value == "":
        return set()
    if isinstance(value, str):
        return {item.strip() for item in value.split(",") if item.strip()}
    if isinstance(value, list):
        return {str(item).strip() for item in value if str(item).strip()}
    raise ValueError(f"{key}[{task_id!r}] must be a list or comma-separated string")


def _trajectory_matches_id(trajectory: Trajectory, requested_id: str) -> bool:
    source_session_id = str(trajectory.metadata.get("source_session_id", ""))
    return trajectory.trajectory_id == requested_id or source_session_id == requested_id


def _clean_trajectory_id(value: Any) -> str:
    return str(value).strip().strip("`").strip()


def _trajectory_aliases(trajectory: Trajectory, *, index: int | None = None) -> set[str]:
    aliases = {trajectory.trajectory_id}
    source_session_id = str(trajectory.metadata.get("source_session_id", "")).strip()
    if source_session_id:
        aliases.add(source_session_id)
    if "__" in trajectory.trajectory_id:
        aliases.add(trajectory.trajectory_id.split("__", 1)[1])
    if index is not None:
        aliases.update(
            {
                f"C{index}",
                f"c{index}",
                f"Candidate {index}",
                f"candidate {index}",
                f"Trajectory {index}",
                f"trajectory {index}",
            }
        )
    return {alias for alias in aliases if alias}


def _trajectory_alias_map(trajectories: list[Trajectory]) -> dict[str, str]:
    alias_to_id: dict[str, str] = {}
    ambiguous: set[str] = set()
    for index, trajectory in enumerate(trajectories, 1):
        for alias in _trajectory_aliases(trajectory, index=index):
            cleaned = _clean_trajectory_id(alias)
            existing = alias_to_id.get(cleaned)
            if existing is not None and existing != trajectory.trajectory_id:
                ambiguous.add(cleaned)
                continue
            alias_to_id[cleaned] = trajectory.trajectory_id
    for alias in ambiguous:
        alias_to_id.pop(alias, None)
    return alias_to_id


def _select_direct_judge_trajectories(
    trajectories: list[Trajectory],
    config: Config,
) -> list[Trajectory]:
    task_id = trajectories[0].task_id if trajectories else ""
    requested = _ids_by_task(config, "direct_judge_trajectory_ids_by_task", task_id)
    if not requested:
        requested = set(
            _list_setting(
                config,
                "direct_judge_trajectory_ids",
                "ADARUBRIC_DIRECT_JUDGE_TRAJECTORY_IDS",
            )
        )

    if requested:
        selected = [
            trajectory
            for trajectory in trajectories
            if any(_trajectory_matches_id(trajectory, item) for item in requested)
        ]
        found = {
            requested_id
            for requested_id in requested
            if any(_trajectory_matches_id(trajectory, requested_id) for trajectory in selected)
        }
        missing = requested - found
        if missing:
            raise ValueError(
                f"Unknown direct judge trajectory id(s) for {task_id}: {sorted(missing)}"
            )
    else:
        selected = trajectories

    max_trajectories = _int_setting(
        config,
        "direct_judge_max_trajectories",
        "ADARUBRIC_DIRECT_JUDGE_MAX_TRAJECTORIES",
        default=3,
    )
    if len(selected) > max_trajectories:
        selected = selected[:max_trajectories]

    if len(selected) < 2:
        raise ValueError(
            f"Direct judge baseline needs at least 2 trajectories for task_id={task_id}"
        )
    return selected


def _format_action_input(action_input: Any) -> str:
    if isinstance(action_input, str):
        return action_input
    return json.dumps(action_input, ensure_ascii=False)


def format_trajectory_for_direct_judge(trajectory: Trajectory, *, index: int) -> str:
    lines = [
        f"## Candidate C{index}",
        f"Trajectory ID: `{trajectory.trajectory_id}`",
    ]
    source_session_id = str(trajectory.metadata.get("source_session_id", "")).strip()
    if source_session_id and source_session_id != trajectory.trajectory_id:
        lines.append(f"Source Session ID: `{source_session_id}`")
    for step in trajectory.steps:
        lines.extend(
            [
                f"--- Step {step.step_id} ---",
                f"Action: {step.action}",
            ]
        )
        if step.action_input:
            lines.append(f"Action Input: {_format_action_input(step.action_input)}")
        lines.append(f"Observation: {step.observation}")
        lines.append("")
    return "\n".join(lines).rstrip()


def build_messages(
    task: TaskDescription,
    trajectories: list[Trajectory],
    *,
    config: Config,
) -> list[dict[str, str]]:
    trajectory_text = "\n\n".join(
        format_trajectory_for_direct_judge(trajectory, index=index)
        for index, trajectory in enumerate(trajectories, 1)
    )
    system_content = DIRECT_JUDGE_SYSTEM
    if _bool_setting(
        config,
        "direct_judge_completion_first",
        "ADARUBRIC_DIRECT_JUDGE_COMPLETION_FIRST",
        default=False,
    ):
        system_content += "\n\n" + DIRECT_JUDGE_COMPLETION_FIRST

    user_content = DIRECT_JUDGE_USER.format(
        task_id=task.task_id,
        instruction=task.instruction,
        trajectory_text=trajectory_text,
    )
    candidate_ids = ", ".join(trajectory.trajectory_id for trajectory in trajectories)
    candidate_labels = ", ".join(
        f"C{index}={trajectory.trajectory_id}"
        for index, trajectory in enumerate(trajectories, 1)
    )
    user_content += "\n\n" + DIRECT_JUDGE_JSON_CONTRACT.format(
        task_id=task.task_id,
        candidate_ids=candidate_ids,
        candidate_labels=candidate_labels,
    )
    return [
        {"role": "system", "content": system_content},
        {"role": "user", "content": user_content},
    ]


def _build_client(config: Config) -> OpenAIClient:
    return OpenAIClient(
        model=str(_setting(config, "model", "ADARUBRIC_MODEL", "gpt-4o")),
        base_url=_setting(config, "base_url", "ADARUBRIC_BASE_URL", None),
        api_key=str(
            _setting(config, "api_key", "ADARUBRIC_API_KEY", None)
            or os.environ.get("OPENAI_API_KEY")
            or "EMPTY"
        ),
        extra_body=GEN._extra_body_setting(config),
    )


def _candidate_key(trajectories: list[Trajectory]) -> str:
    return ",".join(trajectory.trajectory_id for trajectory in trajectories)


def _baseline_key(task_id: str, run_number: int, candidate_key: str) -> BaselineKey:
    return (task_id, run_number, candidate_key)


def load_existing_results(path: Path) -> dict[BaselineKey, DirectJudgeResult]:
    if not path.exists():
        return {}

    existing: dict[BaselineKey, DirectJudgeResult] = {}
    with path.open("r", encoding="utf-8-sig") as file:
        for line_number, line in enumerate(file, 1):
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
                task = TaskDescription.model_validate(record["task"])
                response = DirectJudgeResponse.model_validate(record["response"])
                run_number = int(record["run_number"])
                candidate_ids = [str(item) for item in record["candidate_ids"]]
            except (KeyError, TypeError, ValueError) as exc:
                print(f"Skipping invalid baseline JSONL line {line_number}: {exc}")
                continue
            result = DirectJudgeResult(
                task=task,
                run_number=run_number,
                candidate_ids=candidate_ids,
                response=response,
            )
            existing[_baseline_key(task.task_id, run_number, result.candidate_key)] = result
    return existing


def initialize_jsonl(path: Path, *, resume: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if resume and path.exists():
        return
    path.write_text("", encoding="utf-8")


def append_jsonl(path: Path, result: DirectJudgeResult) -> None:
    record = {
        "task": json.loads(result.task.model_dump_json()),
        "run_number": result.run_number,
        "candidate_ids": result.candidate_ids,
        "response": json.loads(result.response.model_dump_json()),
    }
    with path.open("a", encoding="utf-8") as file:
        file.write(json.dumps(record, ensure_ascii=False) + "\n")


def append_raw_response_jsonl(
    path: Path,
    *,
    task: TaskDescription,
    run_number: int,
    candidate_ids: list[str],
    stage: str,
    raw: str,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "task_id": task.task_id,
        "run_number": run_number,
        "candidate_ids": candidate_ids,
        "stage": stage,
        "raw_response": strip_thinking(raw),
    }
    with path.open("a", encoding="utf-8") as file:
        file.write(json.dumps(record, ensure_ascii=False) + "\n")


def _normalize_response(
    response: DirectJudgeResponse,
    task: TaskDescription,
    trajectories: list[Trajectory],
) -> DirectJudgeResponse:
    expected_ids = [trajectory.trajectory_id for trajectory in trajectories]
    alias_to_id = _trajectory_alias_map(trajectories)
    ranked = [
        DirectJudgeRank(
            rank=item.rank,
            trajectory_id=alias_to_id.get(
                _clean_trajectory_id(item.trajectory_id),
                item.trajectory_id,
            ),
            reason=item.reason,
        )
        for item in response.ranking
    ]
    expected_set = set(expected_ids)
    ranked = [item for item in ranked if item.trajectory_id in expected_set]
    ranked_ids = {item.trajectory_id for item in ranked}
    missing = [trajectory_id for trajectory_id in expected_ids if trajectory_id not in ranked_ids]
    if missing and not ranked:
        received_ids = [item.trajectory_id for item in response.ranking]
        raise ValueError(
            f"Direct judge response did not match any trajectory id(s) for task "
            f"{task.task_id}. Received: {received_ids}; expected: {expected_ids}"
        )
    for trajectory_id in missing:
        ranked.append(
            DirectJudgeRank(
                rank=len(ranked) + 1,
                trajectory_id=trajectory_id,
                reason="Missing from model ranking; appended after ranked candidates.",
            )
        )

    ranked.sort(key=lambda item: item.rank)
    normalized = [
        DirectJudgeRank(rank=rank, trajectory_id=item.trajectory_id, reason=item.reason)
        for rank, item in enumerate(ranked, 1)
    ]
    return DirectJudgeResponse(
        task_id=task.task_id,
        ranking=normalized,
        overall_reason=response.overall_reason,
    )


def _coerce_direct_judge_data(
    data: Any,
    *,
    task: TaskDescription,
) -> dict[str, Any]:
    if isinstance(data, list):
        data = {"ranking": data}
    if not isinstance(data, dict):
        raise ValueError("direct judge response must be a JSON object or ranking array")

    coerced = dict(data)
    if "ranking" not in coerced and "rankings" in coerced:
        coerced["ranking"] = coerced["rankings"]

    ranking = coerced.get("ranking")
    if not isinstance(ranking, list):
        return coerced

    normalized_ranking: list[dict[str, Any]] = []
    for index, item in enumerate(ranking, 1):
        if isinstance(item, str):
            normalized_ranking.append(
                {
                    "rank": index,
                    "trajectory_id": item,
                    "reason": "",
                }
            )
            continue
        if not isinstance(item, dict):
            normalized_ranking.append(item)
            continue

        normalized = dict(item)
        normalized.setdefault("rank", index)
        if "trajectory_id" not in normalized:
            for alias in ("id", "trajectory", "trajectoryId"):
                if alias in normalized:
                    normalized["trajectory_id"] = normalized[alias]
                    break
        if "reason" not in normalized:
            normalized["reason"] = str(
                normalized.get("rationale") or normalized.get("explanation") or ""
            )
        normalized_ranking.append(normalized)

    coerced["task_id"] = str(coerced.get("task_id") or task.task_id)
    coerced["ranking"] = normalized_ranking
    coerced["overall_reason"] = str(
        coerced.get("overall_reason") or coerced.get("reason") or coerced.get("rationale") or ""
    )
    return coerced


def _parse_direct_judge_response(raw: str, *, task: TaskDescription) -> DirectJudgeResponse:
    extracted = extract_json_substring(raw)
    try:
        data = json.loads(extracted)
    except json.JSONDecodeError:
        data = ast.literal_eval(extracted)
    return DirectJudgeResponse.model_validate(_coerce_direct_judge_data(data, task=task))


def _line_containing(text: str, needle: str) -> str:
    for line in text.splitlines():
        if needle in line:
            return line.strip()
    return ""


def _response_from_candidate_mentions(
    raw: str,
    *,
    task: TaskDescription,
    trajectories: list[Trajectory],
) -> DirectJudgeResponse:
    text = strip_thinking(raw)
    mentions: list[tuple[int, str, str]] = []
    for index, trajectory in enumerate(trajectories, 1):
        best: tuple[int, str] | None = None
        for alias in _trajectory_aliases(trajectory, index=index):
            index = text.find(alias)
            if index == -1:
                continue
            if best is None or index < best[0]:
                best = (index, alias)
        if best is not None:
            mentions.append((best[0], trajectory.trajectory_id, best[1]))

    if len(mentions) != len(trajectories):
        found_ids = [trajectory_id for _, trajectory_id, _ in mentions]
        expected_ids = [trajectory.trajectory_id for trajectory in trajectories]
        raise ValueError(
            "Could not recover complete ranking from non-JSON response. "
            f"Found {found_ids}, expected {expected_ids}"
        )

    mentions.sort(key=lambda item: item[0])
    return DirectJudgeResponse(
        task_id=task.task_id,
        ranking=[
            DirectJudgeRank(
                rank=rank,
                trajectory_id=trajectory_id,
                reason=_line_containing(text, matched_alias)
                or "Recovered from non-JSON direct judge output.",
            )
            for rank, (_, trajectory_id, matched_alias) in enumerate(mentions, 1)
        ],
        overall_reason="Recovered from non-JSON direct judge output.",
    )


async def _repair_direct_judge_response(
    *,
    client: OpenAIClient,
    raw: str,
    task: TaskDescription,
    trajectories: list[Trajectory],
    max_tokens: int,
    raw_response_path: Path,
    run_number: int,
) -> DirectJudgeResponse:
    candidate_ids = [trajectory.trajectory_id for trajectory in trajectories]
    candidate_labels = ", ".join(
        f"C{index}={trajectory.trajectory_id}"
        for index, trajectory in enumerate(trajectories, 1)
    )
    repair_messages = [
        {"role": "system", "content": JSON_REPAIR_SYSTEM},
        {
            "role": "user",
            "content": (
                DIRECT_JUDGE_JSON_CONTRACT.format(
                    task_id=task.task_id,
                    candidate_ids=", ".join(candidate_ids),
                    candidate_labels=candidate_labels,
                )
                + "\n\nMalformed direct-judge output to repair:\n"
                + raw
            ),
        },
    ]
    repaired = await client.generate_text(
        repair_messages,
        temperature=0.0,
        max_tokens=max_tokens,
    )
    append_raw_response_jsonl(
        raw_response_path,
        task=task,
        run_number=run_number,
        candidate_ids=candidate_ids,
        stage="json_repair_raw_response",
        raw=repaired,
    )
    return _parse_direct_judge_response(repaired, task=task)


def _parse_and_normalize_response(
    raw: str,
    *,
    task: TaskDescription,
    trajectories: list[Trajectory],
) -> DirectJudgeResponse:
    response = _parse_direct_judge_response(raw, task=task)
    return _normalize_response(response, task, trajectories)


async def judge_task_once(
    *,
    client: OpenAIClient,
    task: TaskDescription,
    trajectories: list[Trajectory],
    config: Config,
    run_number: int,
) -> DirectJudgeResult:
    messages = build_messages(task, trajectories, config=config)
    temperature = _float_setting(
        config,
        "direct_judge_temperature",
        "ADARUBRIC_DIRECT_JUDGE_TEMPERATURE",
        default=0.0,
    )
    max_tokens = _int_setting(
        config,
        "direct_judge_max_tokens",
        "ADARUBRIC_DIRECT_JUDGE_MAX_TOKENS",
        default=2048,
    )
    raw_response_path = _path_setting(
        config,
        "direct_judge_raw_response_path",
        "ADARUBRIC_DIRECT_JUDGE_RAW_RESPONSE_PATH",
        default=DEFAULT_RAW_RESPONSE_PATH,
    )
    candidate_ids = [trajectory.trajectory_id for trajectory in trajectories]

    raw = await client.generate_text(
        messages,
        temperature=temperature,
        max_tokens=max_tokens,
    )
    append_raw_response_jsonl(
        raw_response_path,
        task=task,
        run_number=run_number,
        candidate_ids=candidate_ids,
        stage="direct_judge_raw_response",
        raw=raw,
    )
    try:
        response = _parse_and_normalize_response(
            raw,
            task=task,
            trajectories=trajectories,
        )
    except (SyntaxError, ValidationError, json.JSONDecodeError, ValueError) as parse_exc:
        try:
            repaired_response = await _repair_direct_judge_response(
                client=client,
                raw=raw,
                task=task,
                trajectories=trajectories,
                max_tokens=max_tokens,
                raw_response_path=raw_response_path,
                run_number=run_number,
            )
            response = _normalize_response(repaired_response, task, trajectories)
        except (SyntaxError, ValidationError, json.JSONDecodeError, ValueError) as repair_exc:
            try:
                response = _response_from_candidate_mentions(
                    raw,
                    task=task,
                    trajectories=trajectories,
                )
            except ValueError as fallback_exc:
                raise LLMClientError(
                    "Failed to parse direct judge response after JSON repair and text fallback",
                    context={
                        "task_id": task.task_id,
                        "run_number": run_number,
                        "candidate_ids": candidate_ids,
                        "raw_response_path": str(raw_response_path),
                        "parse_error": str(parse_exc),
                        "repair_error": str(repair_exc),
                        "fallback_error": str(fallback_exc),
                        "raw_response_preview": strip_thinking(raw)[:1000],
                    },
                ) from repair_exc

    return DirectJudgeResult(
        task=task,
        run_number=run_number,
        candidate_ids=candidate_ids,
        response=response,
    )


async def judge_task(
    *,
    client: OpenAIClient,
    task: TaskDescription,
    trajectories: list[Trajectory],
    config: Config,
    output_path: Path,
    existing: dict[BaselineKey, DirectJudgeResult],
) -> list[DirectJudgeResult]:
    selected = _select_direct_judge_trajectories(trajectories, config)
    candidate_key = _candidate_key(selected)
    runs = _int_setting(config, "direct_judge_runs", "ADARUBRIC_DIRECT_JUDGE_RUNS", default=1)

    results: list[DirectJudgeResult] = []
    for run_number in range(1, runs + 1):
        key = _baseline_key(task.task_id, run_number, candidate_key)
        if key in existing:
            result = existing[key]
            print(
                f"Skipping existing direct judge result: task={task.task_id}, "
                f"run={run_number}, candidates={candidate_key}"
            )
        else:
            print(
                f"Direct judging task={task.task_id}, run={run_number}/{runs}, "
                f"candidates={candidate_key}"
            )
            result = await judge_task_once(
                client=client,
                task=task,
                trajectories=selected,
                config=config,
                run_number=run_number,
            )
            append_jsonl(output_path, result)
            existing[key] = result
        print_direct_judge_result(result)
        results.append(result)
    return results


def _ranking_string(result: DirectJudgeResult) -> str:
    return ">".join(item.trajectory_id for item in result.response.ranking)


def print_direct_judge_result(result: DirectJudgeResult) -> None:
    print(
        f"Direct judge result: task={result.task.task_id}, "
        f"run={result.run_number}, ranking={_ranking_string(result)}"
    )
    for item in result.response.ranking:
        print(f"  {item.rank}. {item.trajectory_id}: {item.reason}")
    if result.response.overall_reason:
        print(f"  Overall: {result.response.overall_reason}")


def build_report(results: list[DirectJudgeResult], source_workbook: Path) -> str:
    lines = [
        "# Jiawen Direct Judge Baseline",
        "",
        f"Generated at: {datetime.now().isoformat(timespec='seconds')}",
        f"Workbook: `{source_workbook}`",
        "",
    ]

    for result in results:
        lines.extend(
            [
                f"## Task `{result.task.task_id}` Run {result.run_number}",
                "",
                result.task.instruction,
                "",
                f"Ranking: `{_ranking_string(result)}`",
                "",
                "| Rank | Trajectory ID | Reason |",
                "|---:|---|---|",
            ]
        )
        for item in result.response.ranking:
            lines.append(f"| {item.rank} | `{item.trajectory_id}` | {item.reason} |")
        if result.response.overall_reason:
            lines.extend(["", f"Overall reason: {result.response.overall_reason}"])
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def save_report(report: str, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(report, encoding="utf-8")


def save_csv(results: list[DirectJudgeResult], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(
            [
                "task_id",
                "run_number",
                "candidate_ids",
                "ranking",
                "rank",
                "trajectory_id",
                "reason",
            ]
        )
        for result in results:
            ranking = _ranking_string(result)
            for item in result.response.ranking:
                writer.writerow(
                    [
                        result.task.task_id,
                        result.run_number,
                        result.candidate_key,
                        ranking,
                        item.rank,
                        item.trajectory_id,
                        item.reason,
                    ]
                )


def build_stability_report(results: list[DirectJudgeResult]) -> str:
    rank_values: dict[tuple[str, str], list[int]] = {}
    for result in results:
        for item in result.response.ranking:
            rank_values.setdefault((result.task.task_id, item.trajectory_id), []).append(item.rank)

    lines = ["# Jiawen Direct Judge Baseline Stability", ""]
    for (task_id, trajectory_id), ranks in sorted(rank_values.items()):
        lines.append(
            f"- `{task_id}` / `{trajectory_id}`: "
            f"mean_rank={mean(ranks):.3f}, "
            f"std={pstdev(ranks) if len(ranks) > 1 else 0.0:.3f}, "
            f"runs={len(ranks)}"
        )
    return "\n".join(lines).rstrip() + "\n"


async def main() -> None:
    args = parse_args()
    config = GEN.load_config(args.config)
    tasks_by_id, all_trajectories, workbook_path = GEN._load_jiawen_objects(config)
    tasks = GEN._select_tasks(config, tasks_by_id)
    client = _build_client(config)

    output_path = _path_setting(
        config,
        "direct_judge_jsonl_path",
        "ADARUBRIC_DIRECT_JUDGE_JSONL_PATH",
        default=DEFAULT_JSONL_PATH,
    )
    report_path = _path_setting(
        config,
        "direct_judge_report_path",
        "ADARUBRIC_DIRECT_JUDGE_REPORT_PATH",
        default=DEFAULT_REPORT_PATH,
    )
    csv_path = _path_setting(
        config,
        "direct_judge_csv_path",
        "ADARUBRIC_DIRECT_JUDGE_CSV_PATH",
        default=DEFAULT_CSV_PATH,
    )
    resume = _bool_setting(
        config,
        "direct_judge_resume_from_jsonl",
        "ADARUBRIC_DIRECT_JUDGE_RESUME_FROM_JSONL",
        default=True,
    )
    initialize_jsonl(output_path, resume=resume)
    existing = load_existing_results(output_path) if resume else {}

    print(f"Loaded config from {args.config}")
    print(
        f"Loaded workbook {workbook_path} with {len(tasks_by_id)} task(s) "
        f"and {len(all_trajectories)} trajectory/trajectories"
    )
    print(f"Direct judging {len(tasks)} selected task(s)")
    print(f"Streaming direct judge JSONL to {output_path}")
    if resume:
        print(f"Loaded {len(existing)} existing direct judge checkpoint(s)")

    all_results: list[DirectJudgeResult] = []
    for task in tasks:
        trajectories = GEN._trajectories_for_task(task, all_trajectories)
        task_results = await judge_task(
            client=client,
            task=task,
            trajectories=trajectories,
            config=config,
            output_path=output_path,
            existing=existing,
        )
        all_results.extend(task_results)

    report = build_report(all_results, workbook_path)
    save_report(report, report_path)
    save_csv(all_results, csv_path)

    stability_report = build_stability_report(all_results)
    print("")
    print(report)
    print(stability_report)
    print(f"Saved direct judge JSONL to {output_path}")
    print(f"Saved direct judge report to {report_path}")
    print(f"Saved direct judge CSV to {csv_path}")


if __name__ == "__main__":
    asyncio.run(main())
