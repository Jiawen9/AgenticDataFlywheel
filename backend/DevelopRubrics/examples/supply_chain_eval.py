"""AdaRubric — Multi-Step API Orchestration Evaluation.

Demonstrates evaluating multiple agent trajectories for a complex
procurement API-chaining task, with composite filtering (AbsoluteThreshold
+ DimensionAwareFilter). Shows how the DimensionAwareFilter prevents a
high average score from masking a catastrophic failure on a single dimension.

Usage:
    export OPENAI_API_KEY="sk-..."
    python examples/supply_chain_eval.py

For an OpenAI-compatible local model server on PowerShell:
    $env:ADARUBRIC_BASE_URL="http://localhost:8000/v1"
    $env:ADARUBRIC_API_KEY="EMPTY"
    $env:ADARUBRIC_MODEL="your-local-model-name"
    .venv\\Scripts\\python examples\\supply_chain_eval.py

Rubric persistence:
    By default, the script loads an existing rubric from
    docs/rubrics/supply_chain_rubric.json. If the file does not exist, it
    generates a rubric once and saves it there. Set ADARUBRIC_REGENERATE_RUBRIC=1
    to force regeneration, or ADARUBRIC_RUBRIC_PATH to use a different JSON file.

Jiawen dataset:
    If jiawen-dataset exists, the script loads TaskDescription and Trajectory
    objects from jiawen-dataset/trajectory_tools/excel_to_object.py by default.
    Set ADARUBRIC_USE_JIAWEN_DATA=0 to use the original built-in supply-chain
    demo. Set ADARUBRIC_TASK_ID to choose a specific task, and
    ADARUBRIC_MAX_TRAJECTORIES to evaluate only the first N trajectories.

Stability evaluation:
    By default, the script runs evaluation 10 times against the same persisted
    rubric and writes a stability report. Set ADARUBRIC_EVAL_RUNS to change the
    repeat count.

Rubric validation:
    Rubric generation uses AdaRubric paper validation by default. Set
    ADARUBRIC_VALIDATE_RUBRIC=0 to disable it. Configure embeddings with
    ADARUBRIC_EMBEDDING_MODEL, ADARUBRIC_EMBEDDING_BASE_URL, and
    ADARUBRIC_EMBEDDING_API_KEY. Set ADARUBRIC_VALIDATION_ATTEMPTS to change the
    default retry limit of 10.
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime
from pathlib import Path
from statistics import mean, pstdev

from adarubric import (
    AdaRubricPipeline,
    DynamicRubric,
    PipelineResult,
    TaskDescription,
    Trajectory,
    TrajectoryStep,
)
from adarubric.evaluator.aggregator import ConfidenceNormalizedAggregator
from adarubric.evaluator.trajectory_evaluator import LLMTrajectoryEvaluator
from adarubric.filter.threshold import (
    AbsoluteThresholdFilter,
    CompositeFilter,
    DimensionAwareFilter,
)
from adarubric.generator.llm_generator import LLMRubricGenerator
from adarubric.generator.validation import OpenAIEmbeddingProvider, RubricValidator
from adarubric.llm.openai_client import OpenAIClient

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RUBRIC_PATH = PROJECT_ROOT / "docs" / "rubrics" / "supply_chain_rubric.json"
DEFAULT_JIAWEN_RUBRIC_PATH = PROJECT_ROOT / "docs" / "rubrics" / "jiawen_gui_rubric.json"
DEFAULT_REPORT_DIR = PROJECT_ROOT / "docs" / "evaluation_outputs"
JIAWEN_DATASET_ROOT = PROJECT_ROOT / "jiawen-dataset"


def _rubric_path_from_env(default_path: Path) -> Path:
    configured = os.environ.get("ADARUBRIC_RUBRIC_PATH")
    if not configured:
        return default_path
    path = Path(configured)
    return path if path.is_absolute() else PROJECT_ROOT / path


def _bool_env(name: str, *, default: bool) -> bool:
    configured = os.environ.get(name)
    if configured is None:
        return default
    return configured.strip().lower() in {"1", "true", "yes", "y"}


def _should_regenerate_rubric() -> bool:
    return _bool_env("ADARUBRIC_REGENERATE_RUBRIC", default=False)


def _use_jiawen_dataset() -> bool:
    return _bool_env("ADARUBRIC_USE_JIAWEN_DATA", default=JIAWEN_DATASET_ROOT.exists())


def _use_rubric_validation() -> bool:
    return _bool_env("ADARUBRIC_VALIDATE_RUBRIC", default=True)


def _report_path_from_env() -> Path:
    configured = os.environ.get("ADARUBRIC_REPORT_PATH")
    if configured:
        path = Path(configured)
        return path if path.is_absolute() else PROJECT_ROOT / path

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return DEFAULT_REPORT_DIR / f"supply_chain_eval_{timestamp}.txt"


def _eval_runs_from_env() -> int:
    configured = os.environ.get("ADARUBRIC_EVAL_RUNS", "10")
    try:
        runs = int(configured)
    except ValueError:
        raise ValueError(f"ADARUBRIC_EVAL_RUNS must be an integer, got {configured!r}") from None
    if runs < 1:
        raise ValueError(f"ADARUBRIC_EVAL_RUNS must be >= 1, got {runs}")
    return runs


def _validation_attempts_from_env() -> int:
    configured = os.environ.get("ADARUBRIC_VALIDATION_ATTEMPTS", "10")
    try:
        attempts = int(configured)
    except ValueError:
        raise ValueError(
            f"ADARUBRIC_VALIDATION_ATTEMPTS must be an integer, got {configured!r}"
        ) from None
    if attempts < 1:
        raise ValueError(f"ADARUBRIC_VALIDATION_ATTEMPTS must be >= 1, got {attempts}")
    return attempts


def _max_trajectories_from_env() -> int | None:
    configured = os.environ.get("ADARUBRIC_MAX_TRAJECTORIES")
    if not configured:
        return None
    try:
        max_trajectories = int(configured)
    except ValueError:
        raise ValueError(
            f"ADARUBRIC_MAX_TRAJECTORIES must be an integer, got {configured!r}"
        ) from None
    if max_trajectories < 1:
        raise ValueError(f"ADARUBRIC_MAX_TRAJECTORIES must be >= 1, got {max_trajectories}")
    return max_trajectories


def load_jiawen_task_and_trajectories() -> tuple[TaskDescription, list[Trajectory]]:
    """Load GUI task and trajectories produced by jiawen-dataset."""
    if not JIAWEN_DATASET_ROOT.exists():
        raise FileNotFoundError(f"Jiawen dataset directory not found: {JIAWEN_DATASET_ROOT}")

    dataset_path = str(JIAWEN_DATASET_ROOT)
    if dataset_path not in sys.path:
        sys.path.insert(0, dataset_path)

    try:
        from trajectory_tools.excel_to_object import load_objects
    except ModuleNotFoundError as exc:
        if exc.name == "openpyxl":
            raise RuntimeError(
                "jiawen-dataset requires openpyxl. Install it with: "
                ".venv\\Scripts\\python -m pip install openpyxl"
            ) from exc
        raise

    tasks_by_id, trajectories = load_objects()
    if not tasks_by_id:
        raise ValueError("jiawen-dataset returned no TaskDescription objects")

    task_id = os.environ.get("ADARUBRIC_TASK_ID")
    if task_id:
        if task_id not in tasks_by_id:
            available = ", ".join(sorted(tasks_by_id))
            raise ValueError(f"Unknown ADARUBRIC_TASK_ID={task_id!r}. Available: {available}")
        task = tasks_by_id[task_id]
    else:
        task = next(iter(tasks_by_id.values()))

    selected = [trajectory for trajectory in trajectories if trajectory.task_id == task.task_id]
    max_trajectories = _max_trajectories_from_env()
    if max_trajectories is not None:
        selected = selected[:max_trajectories]

    if not selected:
        raise ValueError(f"No trajectories found for task_id={task.task_id}")

    print(
        f"Loaded Jiawen dataset task={task.task_id} with "
        f"{len(selected)} trajectory/trajectories"
    )
    return task, selected


def build_rubric_validator() -> RubricValidator | None:
    if not _use_rubric_validation():
        print("Rubric validation disabled")
        return None

    embedding_model = os.environ.get("ADARUBRIC_EMBEDDING_MODEL", "text-embedding-3-small")
    embedding_base_url = os.environ.get("ADARUBRIC_EMBEDDING_BASE_URL")
    embedding_api_key = os.environ.get("ADARUBRIC_EMBEDDING_API_KEY") or os.environ.get(
        "OPENAI_API_KEY"
    )
    print(f"Rubric validation enabled with embedding model: {embedding_model}")
    return RubricValidator(
        OpenAIEmbeddingProvider(
            model=embedding_model,
            base_url=embedding_base_url,
            api_key=embedding_api_key,
        )
    )


async def load_or_generate_rubric(
    pipeline: AdaRubricPipeline,
    task: TaskDescription,
    *,
    rubric_path: Path,
    num_dimensions: int = 5,
) -> DynamicRubric:
    if rubric_path.exists() and not _should_regenerate_rubric():
        rubric = DynamicRubric.model_validate_json(rubric_path.read_text(encoding="utf-8"))
        print(f"Loaded rubric from {rubric_path}")
        return rubric

    rubric = await pipeline.generate_rubric(task, num_dimensions=num_dimensions)
    rubric_path.parent.mkdir(parents=True, exist_ok=True)
    rubric_path.write_text(rubric.model_dump_json(indent=2) + "\n", encoding="utf-8")
    print(f"Generated and saved rubric to {rubric_path}")
    return rubric


def _format_run_result(result: PipelineResult, run_number: int) -> list[str]:
    lines: list[str] = []
    lines.append(f"## Run {run_number}")
    for ev in result.all_evaluations:
        status = "PASS" if ev.passed_threshold else "FAIL"
        lines.append("")
        lines.append(f"--- {ev.trajectory_id} [{status}] score={ev.global_score:.2f} ---")
        for name, score in ev.dimension_global_scores.items():
            lines.append(f"  {name}: {score:.2f}")

    lines.append("")
    lines.append(f"Survival rate: {result.survival_rate:.0%}")
    lines.append(f"Survivors: {[e.trajectory_id for e in result.surviving_evaluations]}")
    return lines


def _format_stability_summary(results: list[PipelineResult]) -> list[str]:
    lines: list[str] = []
    lines.append("## Stability Summary")

    scores_by_traj: dict[str, list[float]] = {}
    pass_counts: dict[str, int] = {}
    dimension_scores_by_traj: dict[str, dict[str, list[float]]] = {}

    for result in results:
        for ev in result.all_evaluations:
            scores_by_traj.setdefault(ev.trajectory_id, []).append(ev.global_score)
            pass_counts[ev.trajectory_id] = pass_counts.get(ev.trajectory_id, 0) + int(
                ev.passed_threshold
            )
            dim_scores = dimension_scores_by_traj.setdefault(ev.trajectory_id, {})
            for dim_name, dim_score in ev.dimension_global_scores.items():
                dim_scores.setdefault(dim_name, []).append(dim_score)

    for trajectory_id, scores in scores_by_traj.items():
        score_std = pstdev(scores) if len(scores) > 1 else 0.0
        lines.append("")
        lines.append(f"--- {trajectory_id} ---")
        lines.append(
            "global_score: "
            f"mean={mean(scores):.3f}, std={score_std:.3f}, "
            f"min={min(scores):.3f}, max={max(scores):.3f}, "
            f"pass={pass_counts.get(trajectory_id, 0)}/{len(scores)}"
        )

        for dim_name, dim_scores in dimension_scores_by_traj.get(trajectory_id, {}).items():
            dim_std = pstdev(dim_scores) if len(dim_scores) > 1 else 0.0
            lines.append(
                f"  {dim_name}: mean={mean(dim_scores):.3f}, "
                f"std={dim_std:.3f}, min={min(dim_scores):.3f}, max={max(dim_scores):.3f}"
            )

    return lines


def build_report(results: list[PipelineResult], *, rubric_path: Path) -> str:
    if not results:
        raise ValueError("At least one PipelineResult is required")

    first_result = results[0]
    lines: list[str] = []
    lines.append("=" * 60)
    lines.append("AdaRubric - Multi-Step API Orchestration Stability Evaluation")
    lines.append("=" * 60)
    lines.append(f"Rubric file: {rubric_path}")
    lines.append(f"Evaluation runs: {len(results)}")
    lines.append("")
    lines.append(f"Rubric Dimensions ({len(first_result.rubric.dimensions)}):")
    for dim in first_result.rubric.dimensions:
        lines.append(f"  [{dim.weight:.1f}x] {dim.name}: {dim.description[:70]}...")

    lines.append("")
    lines.append(f"Rationale: {first_result.rubric.generation_rationale[:200]}")
    lines.append("")
    lines.extend(_format_stability_summary(results))

    for run_number, result in enumerate(results, 1):
        lines.append("")
        lines.extend(_format_run_result(result, run_number))

    return "\n".join(lines)


def save_report(report: str, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(report + "\n", encoding="utf-8")


async def main() -> None:
    task = TaskDescription(
        task_id="sc-001",
        instruction=(
            "You are a procurement agent. Find 3 suppliers of industrial-grade "
            "ball bearings (6205-2RS type) in the EU, request quotes for 10,000 "
            "units, compare total cost including shipping, and recommend the "
            "best value option considering both price and lead time."
        ),
        domain="Procurement API Orchestration",
        complexity="complex",
        expected_tools=[
            "supplier_search",
            "request_quote",
            "shipping_calculator",
            "compare_options",
            "submit_recommendation",
        ],
        context={
            "budget_eur": 50000,
            "delivery_deadline_days": 30,
            "quality_standard": "ISO 9001",
        },
    )

    good_trajectory = Trajectory(
        trajectory_id="sc-traj-good",
        task_id="sc-001",
        steps=[
            TrajectoryStep(
                step_id=0,
                thought="Search for EU-based ball bearing suppliers with ISO 9001 cert",
                action="supplier_search",
                action_input={
                    "product": "6205-2RS ball bearing",
                    "region": "EU",
                    "certification": "ISO 9001",
                },
                observation=(
                    "Found 5 suppliers: SKF (SE), FAG/Schaeffler (DE), "
                    "NTN-SNR (FR), NSK (UK), ZKL (CZ)"
                ),
            ),
            TrajectoryStep(
                step_id=1,
                thought="Request quotes from top 3 by reputation",
                action="request_quote",
                action_input={
                    "suppliers": ["SKF", "Schaeffler", "NTN-SNR"],
                    "quantity": 10000,
                    "part": "6205-2RS",
                },
                observation=(
                    "SKF: 3.20 EUR/unit, MOQ 5000, lead 14d | "
                    "Schaeffler: 2.85 EUR/unit, MOQ 10000, lead 21d | "
                    "NTN-SNR: 3.05 EUR/unit, MOQ 1000, lead 10d"
                ),
            ),
            TrajectoryStep(
                step_id=2,
                thought="Calculate shipping costs for each",
                action="shipping_calculator",
                action_input={
                    "origins": ["Stockholm", "Herzogenaurach", "Annecy"],
                    "destination": "customer_warehouse",
                    "weight_kg": 850,
                },
                observation="SKF: 1200 EUR | Schaeffler: 800 EUR | NTN-SNR: 950 EUR",
            ),
            TrajectoryStep(
                step_id=3,
                thought="Compare total costs and factor in lead times",
                action="compare_options",
                action_input="structured comparison",
                observation=(
                    "Total cost — SKF: 33,200 EUR (14d) | "
                    "Schaeffler: 29,300 EUR (21d) | NTN-SNR: 31,450 EUR (10d)"
                ),
            ),
            TrajectoryStep(
                step_id=4,
                thought=(
                    "Schaeffler is cheapest but 21d lead time is close to the 30d deadline. "
                    "NTN-SNR offers fastest delivery with moderate cost. "
                    "Recommending Schaeffler with NTN-SNR as backup."
                ),
                action="submit_recommendation",
                action_input={
                    "primary": "Schaeffler",
                    "backup": "NTN-SNR",
                    "rationale": "Best price within deadline, with faster backup option",
                },
                observation="Recommendation submitted and acknowledged by procurement manager.",
            ),
        ],
        final_answer="Recommend Schaeffler at 29,300 EUR total (21d lead). Backup: NTN-SNR.",
    )

    weak_trajectory = Trajectory(
        trajectory_id="sc-traj-weak",
        task_id="sc-001",
        steps=[
            TrajectoryStep(
                step_id=0,
                thought="Search for bearings",
                action="supplier_search",
                action_input={"product": "bearings"},
                observation="Found 200+ results for generic bearings worldwide",
            ),
            TrajectoryStep(
                step_id=1,
                thought="Just pick the first one",
                action="submit_recommendation",
                action_input={"primary": "BearingCo", "rationale": "first result"},
                observation="Recommendation submitted.",
            ),
        ],
        final_answer="Use BearingCo.",
    )

    trajectories_for_eval = [good_trajectory, weak_trajectory]
    default_rubric_path = DEFAULT_RUBRIC_PATH
    if _use_jiawen_dataset():
        task, trajectories_for_eval = load_jiawen_task_and_trajectories()
        default_rubric_path = DEFAULT_JIAWEN_RUBRIC_PATH
    else:
        print("Using built-in supply-chain demo task and trajectories")

    rubric_path = _rubric_path_from_env(default_rubric_path)
    should_generate_rubric = _should_regenerate_rubric() or not rubric_path.exists()

    client = OpenAIClient(
        model=os.environ.get("ADARUBRIC_MODEL", "gpt-4o"),
        base_url=os.environ.get("ADARUBRIC_BASE_URL"),
        api_key=os.environ.get("ADARUBRIC_API_KEY") or os.environ.get("OPENAI_API_KEY"),
    )

    rubric_validator = build_rubric_validator() if should_generate_rubric else None
    if rubric_validator is None and not should_generate_rubric:
        print("Existing rubric will be loaded; generation validation skipped")
    pipeline = AdaRubricPipeline(
        generator=LLMRubricGenerator(
            client,
            rubric_validator=rubric_validator,
            max_validation_attempts=_validation_attempts_from_env(),
        ),
        evaluator=LLMTrajectoryEvaluator(
            client,
            aggregator=ConfidenceNormalizedAggregator(recency_decay=1.0),
        ),
        filter_=CompositeFilter([
            AbsoluteThresholdFilter(min_score=2.5),
            DimensionAwareFilter(default_threshold=2.0),
        ]),
    )

    rubric = await load_or_generate_rubric(
        pipeline,
        task,
        rubric_path=rubric_path,
        num_dimensions=5,
    )

    eval_runs = _eval_runs_from_env()
    results: list[PipelineResult] = []
    for run_number in range(1, eval_runs + 1):
        print(f"Evaluation run {run_number}/{eval_runs}")
        result = await pipeline.run(
            task,
            trajectories_for_eval,
            rubric=rubric,
        )
        results.append(result)

    report = build_report(results, rubric_path=rubric_path)
    report_path = _report_path_from_env()
    save_report(report, report_path)
    print(report)
    print(f"\nSaved report to {report_path}")


if __name__ == "__main__":
    asyncio.run(main())
