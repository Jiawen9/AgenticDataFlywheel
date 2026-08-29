"""Evaluate Jiawen GUI trajectories with previously generated rubrics.

This script is the scoring/evaluation companion to ``generate-jiawen-rubrics.py``.
It supports multi-task workbooks: each selected task loads its matching generated
rubric and evaluates only that task's trajectories.

PowerShell example:
    $env:ADARUBRIC_BASE_URL="http://localhost:8000/v1"
    $env:ADARUBRIC_API_KEY="EMPTY"
    $env:ADARUBRIC_MODEL="your-local-model-name"
    .venv\\Scripts\\python examples\\evaluate-jiawen-rubrics.py

By default, settings are read from:
    examples/jiawen_rubric_config.json
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from statistics import mean, pstdev
from typing import Any, Callable

from adarubric import (
    AdaRubricPipeline,
    DynamicRubric,
    PipelineResult,
    TaskDescription,
    Trajectory,
    TrajectoryEvaluation,
    TrajectoryStep,
)
from adarubric.core.exceptions import EvaluationError
from adarubric.evaluator.aggregator import (
    ConfidenceNormalizedAggregator,
    GeometricMeanAggregator,
    MinScoreAggregator,
    WeightedMeanAggregator,
)
from adarubric.evaluator.trajectory_evaluator import LLMTrajectoryEvaluator
from adarubric.filter.threshold import (
    AbsoluteThresholdFilter,
    CompositeFilter,
    DimensionAwareFilter,
    PercentileFilter,
)
from adarubric.generator.llm_generator import LLMRubricGenerator
from adarubric.llm.openai_client import OpenAIClient

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "examples" / "jiawen_rubric_config.json"
DEFAULT_REPORT_PATH = PROJECT_ROOT / "docs" / "evaluation_outputs" / "jiawen_gui_eval.md"
DEFAULT_EVALUATIONS_PATH = (
    PROJECT_ROOT / "docs" / "evaluation_outputs" / "jiawen_gui_eval.jsonl"
)
GENERATOR_SCRIPT = PROJECT_ROOT / "examples" / "generate-jiawen-rubrics.py"

Config = dict[str, Any]
EvaluationKey = tuple[str, int, str, str]


@dataclass(frozen=True)
class TaskEvaluationBundle:
    task: TaskDescription
    rubric_path: Path
    results: list[PipelineResult]


@dataclass(frozen=True)
class TrajectoryChunk:
    trajectory: Trajectory
    core_step_ids: set[int]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Evaluate Jiawen GUI trajectories with generated AdaRubric rubrics."
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


def _select_eval_trajectories(
    trajectories: list[Trajectory],
    config: Config,
) -> list[Trajectory]:
    task_id = trajectories[0].task_id if trajectories else ""
    requested = _ids_by_task(config, "evaluation_trajectory_ids_by_task", task_id)
    if not requested:
        requested = set(
            _list_setting(
                config,
                "evaluation_trajectory_ids",
                "ADARUBRIC_EVALUATION_TRAJECTORY_IDS",
            )
        )
    if not requested:
        return trajectories

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
        raise ValueError(f"Unknown evaluation trajectory id(s) for {task_id}: {sorted(missing)}")
    return selected


def _build_aggregator(config: Config) -> Any:
    strategy = str(
        _setting(
            config,
            "aggregation_strategy",
            "ADARUBRIC_EVAL_AGGREGATION_STRATEGY",
            "confidence_normalized",
        )
    )
    recency_decay = _float_setting(config, "recency_decay", "ADARUBRIC_RECENCY_DECAY", default=1.0)

    if strategy == "confidence_normalized":
        min_evidence = _float_setting(
            config,
            "min_evidence",
            "ADARUBRIC_MIN_EVIDENCE",
            default=1e-8,
        )
        return ConfidenceNormalizedAggregator(
            recency_decay=recency_decay,
            min_evidence=min_evidence,
        )
    if strategy == "weighted_mean":
        return WeightedMeanAggregator(recency_decay=recency_decay)
    if strategy == "geometric_mean":
        return GeometricMeanAggregator()
    if strategy == "min_score":
        return MinScoreAggregator()
    raise ValueError(
        "aggregation_strategy must be one of: "
        "confidence_normalized, weighted_mean, geometric_mean, min_score"
    )


def _build_filter(config: Config) -> Any:
    strategy = str(_setting(config, "filter_strategy", "ADARUBRIC_FILTER_STRATEGY", "composite"))
    min_score = _float_setting(config, "min_score", "ADARUBRIC_MIN_SCORE", default=2.5)
    default_dimension_threshold = _float_setting(
        config,
        "default_dimension_threshold",
        "ADARUBRIC_DEFAULT_DIMENSION_THRESHOLD",
        default=2.0,
    )
    dimension_thresholds = config.get("dimension_thresholds", {})
    if not isinstance(dimension_thresholds, dict):
        raise ValueError("dimension_thresholds must be a JSON object")

    if strategy == "absolute":
        return AbsoluteThresholdFilter(min_score=min_score)
    if strategy == "dimension_aware":
        return DimensionAwareFilter(
            dimension_thresholds=dimension_thresholds,
            default_threshold=default_dimension_threshold,
        )
    if strategy == "percentile":
        percentile = _float_setting(config, "percentile", "ADARUBRIC_PERCENTILE", default=75.0)
        return PercentileFilter(percentile=percentile)
    if strategy == "composite":
        return CompositeFilter(
            [
                AbsoluteThresholdFilter(min_score=min_score),
                DimensionAwareFilter(
                    dimension_thresholds=dimension_thresholds,
                    default_threshold=default_dimension_threshold,
                ),
            ]
        )
    raise ValueError(
        "filter_strategy must be one of: absolute, dimension_aware, percentile, composite"
    )


def _build_pipeline(config: Config) -> AdaRubricPipeline:
    client = OpenAIClient(
        model=str(_setting(config, "model", "ADARUBRIC_MODEL", "gpt-4o")),
        base_url=_setting(config, "base_url", "ADARUBRIC_BASE_URL", None),
        api_key=str(
            _setting(config, "api_key", "ADARUBRIC_API_KEY", None)
            or os.environ.get("OPENAI_API_KEY")
            or "EMPTY"
        ),
        extra_body=GEN._extra_body_setting(config),
    )
    max_concurrent = _int_setting(
        config,
        "evaluation_max_concurrent",
        "ADARUBRIC_EVAL_MAX_CONCURRENT",
        default=2,
    )
    eval_max_tokens = _int_setting(
        config,
        "evaluation_max_tokens",
        "ADARUBRIC_EVAL_MAX_TOKENS",
        default=8192,
    )
    return AdaRubricPipeline(
        generator=LLMRubricGenerator(client),
        evaluator=LLMTrajectoryEvaluator(
            client,
            aggregator=_build_aggregator(config),
            max_concurrent=max_concurrent,
            max_tokens=eval_max_tokens,
        ),
        filter_=_build_filter(config),
    )


def _load_rubric(path: Path, task: TaskDescription) -> DynamicRubric:
    if not path.exists():
        raise FileNotFoundError(
            f"Rubric file not found for task {task.task_id}: {path}. "
            "Run examples\\generate-jiawen-rubrics.py first."
        )
    rubric = DynamicRubric.model_validate_json(path.read_text(encoding="utf-8"))
    if rubric.task_id != task.task_id:
        raise ValueError(
            f"Rubric task_id mismatch for {path}: rubric has {rubric.task_id!r}, "
            f"task is {task.task_id!r}"
        )
    return rubric


def _format_dimension_table(rubric: DynamicRubric) -> list[str]:
    lines = [f"Rubric dimensions ({len(rubric.dimensions)}):"]
    for dimension in rubric.dimensions:
        lines.append(f"- [{dimension.weight:.2f}] {dimension.name}: {dimension.description}")
    return lines


def _format_stability_summary(results: list[PipelineResult]) -> list[str]:
    scores_by_trajectory: dict[str, list[float]] = {}
    pass_counts: dict[str, int] = {}
    dimension_scores: dict[str, dict[str, list[float]]] = {}

    for result in results:
        for evaluation in result.all_evaluations:
            scores_by_trajectory.setdefault(evaluation.trajectory_id, []).append(
                evaluation.global_score
            )
            pass_counts[evaluation.trajectory_id] = pass_counts.get(
                evaluation.trajectory_id,
                0,
            ) + int(evaluation.passed_threshold)
            per_dimension = dimension_scores.setdefault(evaluation.trajectory_id, {})
            for name, score in evaluation.dimension_global_scores.items():
                per_dimension.setdefault(name, []).append(score)

    lines = ["### Stability Summary"]
    for trajectory_id, scores in scores_by_trajectory.items():
        score_std = pstdev(scores) if len(scores) > 1 else 0.0
        lines.append("")
        lines.append(f"#### {trajectory_id}")
        lines.append(
            f"- global_score: mean={mean(scores):.3f}, std={score_std:.3f}, "
            f"min={min(scores):.3f}, max={max(scores):.3f}, "
            f"pass={pass_counts.get(trajectory_id, 0)}/{len(scores)}"
        )
        for dimension_name, dim_scores in dimension_scores.get(trajectory_id, {}).items():
            dim_std = pstdev(dim_scores) if len(dim_scores) > 1 else 0.0
            lines.append(
                f"- {dimension_name}: mean={mean(dim_scores):.3f}, std={dim_std:.3f}, "
                f"min={min(dim_scores):.3f}, max={max(dim_scores):.3f}"
            )
    return lines


def _format_run_result(result: PipelineResult, run_number: int) -> list[str]:
    lines = [f"### Run {run_number}"]
    for evaluation in result.all_evaluations:
        status = "PASS" if evaluation.passed_threshold else "FAIL"
        lines.append("")
        lines.append(
            f"#### {evaluation.trajectory_id} [{status}] "
            f"global_score={evaluation.global_score:.3f}"
        )
        for name, score in evaluation.dimension_global_scores.items():
            lines.append(f"- {name}: {score:.3f}")

    lines.append("")
    lines.append(f"Survival rate: {result.survival_rate:.0%}")
    survivor_ids = [evaluation.trajectory_id for evaluation in result.surviving_evaluations]
    lines.append(f"Survivors: {survivor_ids}")
    return lines


def build_report(bundles: list[TaskEvaluationBundle], config: Config) -> str:
    evaluation_runs = _int_setting(
        config,
        "evaluation_runs",
        "ADARUBRIC_EVAL_RUNS",
        default=1,
    )
    aggregation_strategy = _setting(
        config,
        "aggregation_strategy",
        "ADARUBRIC_EVAL_AGGREGATION_STRATEGY",
        "confidence_normalized",
    )
    filter_strategy = _setting(
        config,
        "filter_strategy",
        "ADARUBRIC_FILTER_STRATEGY",
        "composite",
    )
    lines = [
        "# Jiawen GUI Rubric Evaluation Report",
        "",
        f"Generated at: {datetime.now().isoformat(timespec='seconds')}",
        f"Evaluation runs: {evaluation_runs}",
        f"Aggregation strategy: `{aggregation_strategy}`",
        f"Filter strategy: `{filter_strategy}`",
        "",
    ]

    for bundle in bundles:
        first_result = bundle.results[0]
        lines.extend(
            [
                "## Task",
                "",
                f"- task_id: `{bundle.task.task_id}`",
                f"- rubric: `{bundle.rubric_path}`",
                f"- trajectories: {len(first_result.all_evaluations)}",
                "",
                bundle.task.instruction,
                "",
            ]
        )
        lines.extend(_format_dimension_table(first_result.rubric))
        lines.append("")
        if first_result.rubric.generation_rationale:
            lines.append(f"Rationale: {first_result.rubric.generation_rationale}")
            lines.append("")
        lines.extend(_format_stability_summary(bundle.results))
        lines.append("")
        for run_number, result in enumerate(bundle.results, 1):
            lines.extend(_format_run_result(result, run_number))
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def save_report(report: str, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(report, encoding="utf-8")


def save_evaluations_jsonl(bundles: list[TaskEvaluationBundle], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as file:
        for bundle in bundles:
            for run_number, result in enumerate(bundle.results, 1):
                for evaluation in result.all_evaluations:
                    record = {
                        "task_id": bundle.task.task_id,
                        "rubric_path": str(bundle.rubric_path),
                        "run_number": run_number,
                        "evaluation": json.loads(evaluation.model_dump_json()),
                    }
                    file.write(json.dumps(record, ensure_ascii=False) + "\n")


def initialize_evaluations_jsonl(path: Path, *, resume: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if resume and path.exists():
        return
    path.write_text("", encoding="utf-8")


def _evaluation_settings(config: Config) -> dict[str, Any]:
    return {
        "aggregation_strategy": _setting(
            config,
            "aggregation_strategy",
            "ADARUBRIC_EVAL_AGGREGATION_STRATEGY",
            "confidence_normalized",
        ),
        "default_dimension_threshold": _float_setting(
            config,
            "default_dimension_threshold",
            "ADARUBRIC_DEFAULT_DIMENSION_THRESHOLD",
            default=2.0,
        ),
        "dimension_thresholds": config.get("dimension_thresholds", {}),
        "evaluation_chunk_enabled": _bool_setting(
            config,
            "evaluation_chunk_enabled",
            "ADARUBRIC_EVAL_CHUNK_ENABLED",
            default=False,
        ),
        "evaluation_chunk_threshold": _int_setting(
            config,
            "evaluation_chunk_threshold",
            "ADARUBRIC_EVAL_CHUNK_THRESHOLD",
            default=10,
        ),
        "evaluation_max_tokens": _int_setting(
            config,
            "evaluation_max_tokens",
            "ADARUBRIC_EVAL_MAX_TOKENS",
            default=8192,
        ),
        "evaluation_include_action": _bool_setting(
            config,
            "evaluation_include_action",
            "ADARUBRIC_EVAL_INCLUDE_ACTION",
            default=True,
        ),
        "evaluation_include_action_input": _bool_setting(
            config,
            "evaluation_include_action_input",
            "ADARUBRIC_EVAL_INCLUDE_ACTION_INPUT",
            default=True,
        ),
        "evaluation_step_lookahead": _int_setting(
            config,
            "evaluation_step_lookahead",
            "ADARUBRIC_EVAL_STEP_LOOKAHEAD",
            default=1,
            minimum=0,
        ),
        "evaluation_chunk_input_mode": "summary_history_plus_lookahead_v2",
        "filter_strategy": _setting(
            config,
            "filter_strategy",
            "ADARUBRIC_FILTER_STRATEGY",
            "composite",
        ),
        "min_evidence": _float_setting(
            config,
            "min_evidence",
            "ADARUBRIC_MIN_EVIDENCE",
            default=1e-8,
        ),
        "min_score": _float_setting(config, "min_score", "ADARUBRIC_MIN_SCORE", default=2.5),
        "percentile": _float_setting(config, "percentile", "ADARUBRIC_PERCENTILE", default=75.0),
        "recency_decay": _float_setting(
            config,
            "recency_decay",
            "ADARUBRIC_RECENCY_DECAY",
            default=1.0,
        ),
        "extra_body": GEN._extra_body_setting(config),
    }


def _settings_signature(settings: dict[str, Any]) -> str:
    return json.dumps(settings, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _evaluation_key(
    *,
    task_id: str,
    run_number: int,
    trajectory_id: str,
    settings_signature: str,
) -> EvaluationKey:
    return (task_id, run_number, trajectory_id, settings_signature)


def load_existing_evaluations_jsonl(path: Path) -> dict[EvaluationKey, TrajectoryEvaluation]:
    if not path.exists():
        return {}

    existing: dict[EvaluationKey, TrajectoryEvaluation] = {}
    with path.open("r", encoding="utf-8") as file:
        for line_number, line in enumerate(file, 1):
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
                evaluation_data = record["evaluation"]
                evaluation = TrajectoryEvaluation.model_validate(evaluation_data)
                task_id = str(record.get("task_id") or evaluation.task_id)
                run_number = int(record["run_number"])
                settings_signature = str(
                    record.get("evaluation_settings_signature")
                    or _settings_signature(record.get("evaluation_settings", {}))
                )
            except (KeyError, TypeError, ValueError) as exc:
                print(f"Skipping invalid JSONL checkpoint line {line_number}: {exc}")
                continue

            key = _evaluation_key(
                task_id=task_id,
                run_number=run_number,
                trajectory_id=evaluation.trajectory_id,
                settings_signature=settings_signature,
            )
            existing[key] = evaluation
    return existing


def append_evaluation_jsonl(
    *,
    path: Path,
    task: TaskDescription,
    rubric_path: Path,
    run_number: int,
    evaluation: TrajectoryEvaluation,
    evaluation_settings: dict[str, Any],
) -> None:
    record = {
        "task_id": task.task_id,
        "rubric_path": str(rubric_path),
        "run_number": run_number,
        "evaluation_settings": evaluation_settings,
        "evaluation_settings_signature": _settings_signature(evaluation_settings),
        "evaluation": json.loads(evaluation.model_dump_json()),
    }
    with path.open("a", encoding="utf-8") as file:
        file.write(json.dumps(record, ensure_ascii=False) + "\n")


def _trajectory_chunks(
    trajectory: Trajectory,
    *,
    lookahead_steps: int,
) -> list[TrajectoryChunk]:
    chunks: list[TrajectoryChunk] = []
    total_steps = len(trajectory.steps)
    for chunk_index, target_index in enumerate(range(total_steps), 1):
        target_step = trajectory.steps[target_index]
        context_end = min(total_steps, target_index + 1 + lookahead_steps)
        steps = trajectory.steps[:context_end]
        core_step_ids = {target_step.step_id}
        metadata = dict(trajectory.metadata)
        metadata.update(
            {
                "chunk_index": chunk_index,
                "chunk_input_mode": "summary_history_plus_lookahead_v2",
                "chunk_context_start_step_id": trajectory.steps[0].step_id,
                "chunk_context_end_step_id": steps[-1].step_id,
                "chunk_core_start_step_id": target_step.step_id,
                "chunk_core_end_step_id": target_step.step_id,
                "chunk_core_step_ids": sorted(core_step_ids),
                "chunk_lookahead_steps": lookahead_steps,
                "original_num_steps": total_steps,
            }
        )
        chunks.append(
            TrajectoryChunk(
                trajectory=Trajectory(
                    trajectory_id=trajectory.trajectory_id,
                    task_id=trajectory.task_id,
                    steps=steps,
                    final_answer=trajectory.final_answer,
                    metadata=metadata,
                ),
                core_step_ids=core_step_ids,
            )
        )
    return chunks


def _trajectory_for_evaluation(
    trajectory: Trajectory,
    *,
    include_action: bool,
    include_action_input: bool,
) -> Trajectory:
    if include_action and include_action_input:
        return trajectory

    steps = [
        TrajectoryStep(
            step_id=step.step_id,
            action=step.action if include_action else "Action hidden for this ablation.",
            action_input=step.action_input if include_action_input else {},
            observation=step.observation,
        )
        for step in trajectory.steps
    ]
    metadata = dict(trajectory.metadata)
    metadata.update(
        {
            "evaluation_include_action": include_action,
            "evaluation_include_action_input": include_action_input,
        }
    )
    return Trajectory(
        trajectory_id=trajectory.trajectory_id,
        task_id=trajectory.task_id,
        steps=steps,
        final_answer=trajectory.final_answer,
        metadata=metadata,
    )


async def evaluate_trajectory(
    *,
    pipeline: AdaRubricPipeline,
    task: TaskDescription,
    trajectory: Trajectory,
    rubric: DynamicRubric,
    config: Config,
    temperature: float,
    eval_max_tokens: int,
) -> TrajectoryEvaluation:
    include_action = _bool_setting(
        config,
        "evaluation_include_action",
        "ADARUBRIC_EVAL_INCLUDE_ACTION",
        default=True,
    )
    include_action_input = _bool_setting(
        config,
        "evaluation_include_action_input",
        "ADARUBRIC_EVAL_INCLUDE_ACTION_INPUT",
        default=True,
    )
    trajectory = _trajectory_for_evaluation(
        trajectory,
        include_action=include_action,
        include_action_input=include_action_input,
    )
    chunk_enabled = _bool_setting(
        config,
        "evaluation_chunk_enabled",
        "ADARUBRIC_EVAL_CHUNK_ENABLED",
        default=False,
    )
    chunk_threshold = _int_setting(
        config,
        "evaluation_chunk_threshold",
        "ADARUBRIC_EVAL_CHUNK_THRESHOLD",
        default=10,
    )
    lookahead_steps = _int_setting(
        config,
        "evaluation_step_lookahead",
        "ADARUBRIC_EVAL_STEP_LOOKAHEAD",
        default=1,
        minimum=0,
    )

    if not chunk_enabled or len(trajectory.steps) <= chunk_threshold:
        evaluation = await pipeline.evaluate(
            trajectory,
            rubric,
            temperature=temperature,
            task_instruction=task.instruction,
            max_tokens=eval_max_tokens,
        )
        evaluation.metadata.update(
            {
                "evaluation_chunk_enabled": chunk_enabled,
                "evaluation_chunk_threshold": chunk_threshold,
                "evaluation_step_lookahead": lookahead_steps,
                "evaluation_chunk_input_mode": "summary_history_plus_lookahead_v2",
                "evaluation_num_chunks": 1,
                "evaluation_include_action": include_action,
                "evaluation_include_action_input": include_action_input,
            }
        )
        return evaluation

    chunks = _trajectory_chunks(
        trajectory,
        lookahead_steps=lookahead_steps,
    )
    print(
        f"Trajectory {trajectory.trajectory_id}: evaluating "
        f"{len(trajectory.steps)} steps step-by-step in {len(chunks)} request(s), "
        f"threshold={chunk_threshold}, lookahead={lookahead_steps}, "
        "input_mode=summary_history_plus_lookahead_v2"
    )

    step_evaluations = []
    for chunk_number, chunk in enumerate(chunks, 1):
        try:
            chunk_eval = await pipeline.evaluate(
                chunk.trajectory,
                rubric,
                temperature=temperature,
                task_instruction=task.instruction,
                max_tokens=eval_max_tokens,
                target_step_ids=chunk.core_step_ids,
            )
        except Exception as exc:
            steps = chunk.trajectory.steps
            core_range = (
                f"{min(chunk.core_step_ids)}-{max(chunk.core_step_ids)}"
                if chunk.core_step_ids
                else "unknown"
            )
            raise EvaluationError(
                f"Failed to evaluate trajectory {trajectory.trajectory_id} "
                f"chunk {chunk_number}/{len(chunks)} "
                f"(target_step_ids={core_range}): {exc}",
                context={
                    "trajectory_id": trajectory.trajectory_id,
                    "chunk_number": chunk_number,
                    "num_chunks": len(chunks),
                    "step_ids": [step.step_id for step in steps],
                    "core_step_ids": sorted(chunk.core_step_ids),
                },
            ) from exc

        step_evaluations.extend(
            step_eval
            for step_eval in chunk_eval.step_evaluations
            if step_eval.step_id in chunk.core_step_ids
        )

    step_evaluations.sort(key=lambda step_eval: step_eval.step_id)
    dimension_globals, global_score = _build_aggregator(config).aggregate_steps(
        step_evaluations,
        rubric,
    )
    return TrajectoryEvaluation(
        trajectory_id=trajectory.trajectory_id,
        task_id=trajectory.task_id,
        rubric_used=rubric,
        step_evaluations=step_evaluations,
        dimension_global_scores=dimension_globals,
        global_score=global_score,
        metadata={
            "evaluation_chunk_enabled": chunk_enabled,
            "evaluation_chunk_threshold": chunk_threshold,
            "evaluation_step_lookahead": lookahead_steps,
            "evaluation_chunk_input_mode": "summary_history_plus_lookahead_v2",
            "evaluation_num_chunks": len(chunks),
            "evaluation_include_action": include_action,
            "evaluation_include_action_input": include_action_input,
        },
    )


async def evaluate_run_incrementally(
    *,
    pipeline: AdaRubricPipeline,
    task: TaskDescription,
    trajectories: list[Trajectory],
    rubric: DynamicRubric,
    rubric_path: Path,
    run_number: int,
    temperature: float,
    eval_max_tokens: int,
    max_concurrent: int,
    evaluations_path: Path,
    config: Config,
    existing_evaluations: dict[EvaluationKey, TrajectoryEvaluation],
    on_trajectory_complete: Callable[[TrajectoryEvaluation], None] | None = None,
) -> PipelineResult:
    sem = asyncio.Semaphore(max(1, max_concurrent))
    evaluation_settings = _evaluation_settings(config)
    settings_signature = _settings_signature(evaluation_settings)
    ordered_evaluations: list[TrajectoryEvaluation | None] = [None] * len(trajectories)
    pending: list[tuple[int, Trajectory]] = []

    for index, trajectory in enumerate(trajectories):
        key = _evaluation_key(
            task_id=task.task_id,
            run_number=run_number,
            trajectory_id=trajectory.trajectory_id,
            settings_signature=settings_signature,
        )
        existing = existing_evaluations.get(key)
        if existing is None:
            pending.append((index, trajectory))
            continue

        ordered_evaluations[index] = existing
        print(
            f"Skipping existing JSONL result: task={task.task_id}, "
            f"run={run_number}, trajectory={trajectory.trajectory_id}, "
            f"global_score={existing.global_score:.3f}"
        )

    async def _evaluate_one(
        index: int,
        trajectory: Trajectory,
    ) -> tuple[int, TrajectoryEvaluation]:
        async with sem:
            evaluation = await evaluate_trajectory(
                pipeline=pipeline,
                task=task,
                trajectory=trajectory,
                rubric=rubric,
                config=config,
                temperature=temperature,
                eval_max_tokens=eval_max_tokens,
            )
            return index, evaluation

    if not pending:
        print(f"Task {task.task_id}: run {run_number} already complete in JSONL")

    tasks = [
        asyncio.create_task(_evaluate_one(index, trajectory))
        for index, trajectory in pending
    ]

    try:
        for completed in asyncio.as_completed(tasks):
            index, evaluation = await completed
            ordered_evaluations[index] = evaluation
            append_evaluation_jsonl(
                path=evaluations_path,
                task=task,
                rubric_path=rubric_path,
                run_number=run_number,
                evaluation=evaluation,
                evaluation_settings=evaluation_settings,
            )
            print(
                f"Saved JSONL result: task={task.task_id}, "
                f"run={run_number}, trajectory={evaluation.trajectory_id}, "
                f"global_score={evaluation.global_score:.3f}"
            )
            key = _evaluation_key(
                task_id=task.task_id,
                run_number=run_number,
                trajectory_id=evaluation.trajectory_id,
                settings_signature=settings_signature,
            )
            existing_evaluations[key] = evaluation
            if on_trajectory_complete is not None:
                on_trajectory_complete(evaluation)
    except Exception:
        for task_handle in tasks:
            task_handle.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise

    all_evaluations = [evaluation for evaluation in ordered_evaluations if evaluation is not None]
    survivors = pipeline.filter_evaluations(all_evaluations)
    return PipelineResult(
        task=task,
        rubric=rubric,
        all_evaluations=all_evaluations,
        surviving_evaluations=survivors,
    )


async def evaluate_task(
    *,
    task: TaskDescription,
    trajectories: list[Trajectory],
    rubric_path: Path,
    config: Config,
    evaluations_path: Path,
    existing_evaluations: dict[EvaluationKey, TrajectoryEvaluation],
) -> TaskEvaluationBundle:
    selected_trajectories = _select_eval_trajectories(trajectories, config)
    rubric = _load_rubric(rubric_path, task)
    pipeline = _build_pipeline(config)
    runs = _int_setting(config, "evaluation_runs", "ADARUBRIC_EVAL_RUNS", default=1)
    temperature = _float_setting(
        config,
        "evaluation_temperature",
        "ADARUBRIC_EVAL_TEMPERATURE",
        default=0.0,
    )
    eval_max_tokens = _int_setting(
        config,
        "evaluation_max_tokens",
        "ADARUBRIC_EVAL_MAX_TOKENS",
        default=8192,
    )
    max_concurrent = _int_setting(
        config,
        "evaluation_max_concurrent",
        "ADARUBRIC_EVAL_MAX_CONCURRENT",
        default=2,
    )

    results: list[PipelineResult] = []
    for run_number in range(1, runs + 1):
        print(
            f"Task {task.task_id}: evaluation run {run_number}/{runs} "
            f"({len(selected_trajectories)} trajectories)"
        )
        result = await evaluate_run_incrementally(
            pipeline=pipeline,
            task=task,
            trajectories=selected_trajectories,
            rubric=rubric,
            rubric_path=rubric_path,
            run_number=run_number,
            temperature=temperature,
            eval_max_tokens=eval_max_tokens,
            max_concurrent=max_concurrent,
            evaluations_path=evaluations_path,
            config=config,
            existing_evaluations=existing_evaluations,
        )
        results.append(result)
    return TaskEvaluationBundle(task=task, rubric_path=rubric_path, results=results)


async def main() -> None:
    args = parse_args()
    config = GEN.load_config(args.config)
    tasks_by_id, all_trajectories, workbook_path = GEN._load_jiawen_objects(config)
    tasks = GEN._select_tasks(config, tasks_by_id)
    multiple_tasks = len(tasks) > 1

    print(f"Loaded config from {args.config}")
    print(
        f"Loaded workbook {workbook_path} with {len(tasks_by_id)} task(s) "
        f"and {len(all_trajectories)} trajectory/trajectories"
    )
    print(f"Evaluating {len(tasks)} selected task(s)")

    evaluations_path = _path_setting(
        config,
        "evaluation_jsonl_path",
        "ADARUBRIC_EVALUATION_JSONL_PATH",
        default=DEFAULT_EVALUATIONS_PATH,
    )
    resume_from_jsonl = _bool_setting(
        config,
        "evaluation_resume_from_jsonl",
        "ADARUBRIC_EVALUATION_RESUME_FROM_JSONL",
        default=True,
    )
    initialize_evaluations_jsonl(evaluations_path, resume=resume_from_jsonl)
    existing_evaluations = (
        load_existing_evaluations_jsonl(evaluations_path) if resume_from_jsonl else {}
    )
    print(f"Streaming evaluation JSONL to {evaluations_path}")
    if resume_from_jsonl:
        print(f"Loaded {len(existing_evaluations)} existing JSONL checkpoint(s)")

    bundles: list[TaskEvaluationBundle] = []
    for task in tasks:
        trajectories = GEN._trajectories_for_task(task, all_trajectories)
        task_config = GEN._config_for_task_outputs(config, task, multiple_tasks=multiple_tasks)
        rubric_path = GEN._path_setting(
            task_config,
            "rubric_path",
            "ADARUBRIC_RUBRIC_PATH",
            default=GEN.DEFAULT_RUBRIC_PATH,
        )
        bundle = await evaluate_task(
            task=task,
            trajectories=trajectories,
            rubric_path=rubric_path,
            config=task_config,
            evaluations_path=evaluations_path,
            existing_evaluations=existing_evaluations,
        )
        bundles.append(bundle)

    report_path = _path_setting(
        config,
        "evaluation_report_path",
        "ADARUBRIC_EVALUATION_REPORT_PATH",
        default=DEFAULT_REPORT_PATH,
    )
    report = build_report(bundles, config)
    save_report(report, report_path)

    print("")
    print(report)
    print(f"Saved report to {report_path}")
    print(f"Saved evaluation JSONL to {evaluations_path}")


if __name__ == "__main__":
    asyncio.run(main())
