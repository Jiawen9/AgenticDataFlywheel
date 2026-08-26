"""Diagnostic check for local evaluator model quality.

This script bypasses rubric generation and evaluates fixed calibration
trajectories against a fixed rubric. It is meant to answer one question:
is the evaluator model following the scoring and confidence semantics?

PowerShell:
    $env:ADARUBRIC_BASE_URL="http://localhost:8000/v1"
    $env:ADARUBRIC_API_KEY="your_api_key"
    $env:ADARUBRIC_MODEL="your-local-model-name"
    .venv\\Scripts\\python examples\\local_model_diagnostic.py
"""

from __future__ import annotations

import asyncio
import os
from statistics import mean

from adarubric.core.models import (
    DynamicRubric,
    EvalDimension,
    TaskDescription,
    Trajectory,
    TrajectoryEvaluation,
    TrajectoryStep,
)
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
)
from adarubric.llm.openai_client import OpenAIClient


def build_task() -> TaskDescription:
    return TaskDescription(
        task_id="diagnostic-procurement",
        instruction=(
            "Find 3 EU suppliers for industrial-grade 6205-2RS ball bearings, "
            "request quotes for 10,000 units, calculate shipping, compare total "
            "cost and lead time, and recommend the best value option."
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
        context={"budget_eur": 50000, "delivery_deadline_days": 30},
    )


def build_fixed_rubric(task_id: str) -> DynamicRubric:
    return DynamicRubric(
        task_id=task_id,
        dimensions=[
            EvalDimension(
                name="RequirementCoverage",
                description=(
                    "Covers the exact product, quantity, EU region, and delivery constraints."
                ),
                weight=1.0,
                scoring_criteria={
                    1: "Ignores most task requirements or targets the wrong product/region.",
                    2: "Mentions the product but misses multiple critical constraints.",
                    3: "Covers the main product and region with minor omissions.",
                    4: "Covers product, region, quantity, and deadline clearly.",
                    5: "Fully covers all requirements and uses them throughout the workflow.",
                },
            ),
            EvalDimension(
                name="ToolSequenceCompleteness",
                description=(
                    "Uses the expected tools in a sensible order without skipping required stages."
                ),
                weight=1.0,
                scoring_criteria={
                    1: "Skips most required tools or jumps directly to an unsupported answer.",
                    2: "Uses one or two tools but misses major required stages.",
                    3: "Uses several relevant tools but with gaps in ordering or coverage.",
                    4: "Uses all essential tools in a reasonable sequence.",
                    5: "Uses all tools in a complete, efficient, and well-justified sequence.",
                },
            ),
            EvalDimension(
                name="QuoteDataQuality",
                description=(
                    "Obtains concrete supplier quote data with price, quantity, "
                    "and lead-time evidence."
                ),
                weight=1.0,
                scoring_criteria={
                    1: "Provides no concrete quote data.",
                    2: "Provides vague or incomplete supplier data.",
                    3: "Provides basic quote data with some missing fields.",
                    4: "Provides concrete quote data for all required suppliers.",
                    5: (
                        "Provides complete, validated quote data with relevant "
                        "supplier constraints."
                    ),
                },
            ),
            EvalDimension(
                name="CostLeadTimeReasoning",
                description=(
                    "Correctly combines unit price, quantity, shipping, total cost, and lead time."
                ),
                weight=1.0,
                scoring_criteria={
                    1: "Does not compare cost or lead time.",
                    2: "Mentions cost or lead time but does not compute total value.",
                    3: "Performs a basic comparison with partial calculations.",
                    4: "Correctly compares total cost and lead time across suppliers.",
                    5: "Performs rigorous value analysis with tradeoffs and constraints.",
                },
            ),
            EvalDimension(
                name="RecommendationGrounding",
                description=(
                    "Final recommendation is justified by the collected data and task constraints."
                ),
                weight=1.0,
                scoring_criteria={
                    1: "Recommendation is absent, arbitrary, or contradicted by evidence.",
                    2: "Recommendation has weak or circular justification.",
                    3: "Recommendation is plausible but only lightly grounded.",
                    4: "Recommendation is clearly grounded in comparison data.",
                    5: "Recommendation is strongly justified with alternatives and caveats.",
                },
            ),
        ],
        generation_rationale="Fixed diagnostic rubric for local model calibration.",
    )


def build_trajectories(task_id: str) -> list[Trajectory]:
    ideal = Trajectory(
        trajectory_id="ideal-clear-pass",
        task_id=task_id,
        steps=[
            TrajectoryStep(
                step_id=0,
                thought="Search EU suppliers for the exact 6205-2RS part and ISO quality.",
                action="supplier_search",
                action_input={
                    "product": "6205-2RS ball bearing",
                    "region": "EU",
                    "quantity": 10000,
                    "certification": "ISO 9001",
                },
                observation="Found SKF, Schaeffler, NTN-SNR, ZKL, and NSK Europe.",
            ),
            TrajectoryStep(
                step_id=1,
                thought="Request quotes from three EU suppliers.",
                action="request_quote",
                action_input={
                    "suppliers": ["SKF", "Schaeffler", "NTN-SNR"],
                    "quantity": 10000,
                    "part": "6205-2RS",
                },
                observation=(
                    "SKF: 3.20 EUR/unit, lead 14d. Schaeffler: 2.85 EUR/unit, lead 21d. "
                    "NTN-SNR: 3.05 EUR/unit, lead 10d."
                ),
            ),
            TrajectoryStep(
                step_id=2,
                thought="Calculate shipping for each supplier.",
                action="shipping_calculator",
                action_input={"weight_kg": 850, "destination": "customer_warehouse"},
                observation="SKF shipping 1200 EUR; Schaeffler 800 EUR; NTN-SNR 950 EUR.",
            ),
            TrajectoryStep(
                step_id=3,
                thought="Compare total costs and lead times.",
                action="compare_options",
                action_input="compare total cost and lead time",
                observation=(
                    "SKF total 33,200 EUR/14d; Schaeffler 29,300 EUR/21d; "
                    "NTN-SNR 31,450 EUR/10d. All meet the 30-day deadline."
                ),
            ),
            TrajectoryStep(
                step_id=4,
                thought="Recommend the best value supplier with backup.",
                action="submit_recommendation",
                action_input={
                    "primary": "Schaeffler",
                    "backup": "NTN-SNR",
                    "rationale": "lowest total cost within deadline; fastest backup",
                },
                observation="Recommendation submitted.",
            ),
        ],
        final_answer="Recommend Schaeffler at 29,300 EUR total and 21-day lead time.",
    )

    obvious_fail = Trajectory(
        trajectory_id="obvious-fail",
        task_id=task_id,
        steps=[
            TrajectoryStep(
                step_id=0,
                thought="I will pick a supplier without searching or requesting quotes.",
                action="submit_recommendation",
                action_input={"primary": "RandomBearingCo", "rationale": "sounds fine"},
                observation=(
                    "Recommendation submitted without supplier search, quotes, or shipping."
                ),
            )
        ],
        final_answer="Use RandomBearingCo.",
    )

    irrelevant = Trajectory(
        trajectory_id="irrelevant-actions",
        task_id=task_id,
        steps=[
            TrajectoryStep(
                step_id=0,
                thought="Check today's weather because shipping might be affected.",
                action="weather_lookup",
                action_input={"city": "Berlin"},
                observation="Berlin weather is sunny.",
            ),
            TrajectoryStep(
                step_id=1,
                thought="Write a friendly message instead of procurement analysis.",
                action="draft_email",
                action_input={"tone": "friendly"},
                observation="Drafted a generic greeting.",
            ),
        ],
        final_answer="No supplier recommendation was produced.",
    )

    partial = Trajectory(
        trajectory_id="partial-middle",
        task_id=task_id,
        steps=[
            TrajectoryStep(
                step_id=0,
                thought="Search for EU 6205-2RS bearing suppliers.",
                action="supplier_search",
                action_input={"product": "6205-2RS", "region": "EU"},
                observation="Found SKF, Schaeffler, and NTN-SNR.",
            ),
            TrajectoryStep(
                step_id=1,
                thought="Recommend one well-known supplier without requesting quotes.",
                action="submit_recommendation",
                action_input={"primary": "SKF", "rationale": "well-known brand"},
                observation="Recommendation submitted.",
            ),
        ],
        final_answer="Recommend SKF because it is well known.",
    )

    return [ideal, obvious_fail, irrelevant, partial]


def confidence_summary(ev: TrajectoryEvaluation) -> str:
    confidences = [
        ds.confidence for step in ev.step_evaluations for ds in step.dimension_scores
    ]
    if not confidences:
        return "no confidence values"
    high_conf_low_score = sum(
        1
        for step in ev.step_evaluations
        for ds in step.dimension_scores
        if ds.confidence >= 0.8 and ds.score <= 2
    )
    low_conf_count = sum(1 for c in confidences if c <= 0.3)
    return (
        f"mean_conf={mean(confidences):.2f}, "
        f"low_conf(<=0.3)={low_conf_count}/{len(confidences)}, "
        f"high_conf_low_score={high_conf_low_score}"
    )


def print_evaluation(ev: TrajectoryEvaluation, rubric: DynamicRubric) -> None:
    weighted_dims, weighted_global = WeightedMeanAggregator(recency_decay=0.0).aggregate_steps(
        ev.step_evaluations, rubric
    )
    conf_dims, conf_global = ConfidenceNormalizedAggregator(
        recency_decay=0.0
    ).aggregate_steps(ev.step_evaluations, rubric)
    geo_dims, geo_global = GeometricMeanAggregator().aggregate_steps(ev.step_evaluations, rubric)
    min_dims, min_global = MinScoreAggregator().aggregate_steps(ev.step_evaluations, rubric)

    print(f"\n=== {ev.trajectory_id} ===")
    print(
        f"weighted_mean={weighted_global:.2f}  "
        f"confidence_normalized={conf_global:.2f}  "
        f"geometric_mean={geo_global:.2f}  min_score={min_global:.2f}"
    )
    print(f"confidence: {confidence_summary(ev)}")
    print("dimension scores:")
    for dim in rubric.dimension_names:
        print(
            f"  {dim}: weighted={weighted_dims.get(dim, 0.0):.2f}, "
            f"conf_norm={conf_dims.get(dim, 0.0):.2f}, "
            f"geo={geo_dims.get(dim, 0.0):.2f}, min={min_dims.get(dim, 0.0):.2f}"
        )

    for step in ev.step_evaluations:
        cells = [
            f"{ds.dimension_name}=s{ds.score}/c{ds.confidence:.1f}"
            for ds in step.dimension_scores
        ]
        print(f"  step {step.step_id}: " + "; ".join(cells))


async def main() -> None:
    task = build_task()
    rubric = build_fixed_rubric(task.task_id)
    trajectories = build_trajectories(task.task_id)

    client = OpenAIClient(
        model=os.environ.get("ADARUBRIC_MODEL", "gpt-4o"),
        base_url=os.environ.get("ADARUBRIC_BASE_URL"),
        api_key=os.environ.get("ADARUBRIC_API_KEY") or os.environ.get("OPENAI_API_KEY"),
    )
    evaluator = LLMTrajectoryEvaluator(
        client,
        aggregator=WeightedMeanAggregator(recency_decay=0.0),
        max_concurrent=1,
        max_tokens=8192,
    )

    print("Model:", os.environ.get("ADARUBRIC_MODEL", "gpt-4o"))
    print("Base URL:", os.environ.get("ADARUBRIC_BASE_URL") or "OpenAI default")
    print(
        "\nExpected ordering: "
        "ideal-clear-pass > partial-middle > obvious-fail ~= irrelevant-actions"
    )

    evaluations = await evaluator.evaluate_batch(
        trajectories,
        rubric,
        temperature=0.0,
        task_instruction=task.instruction,
        max_concurrent=1,
    )

    for ev in evaluations:
        print_evaluation(ev, rubric)

    strict_filter = CompositeFilter(
        [
            AbsoluteThresholdFilter(min_score=3.0),
            DimensionAwareFilter(default_threshold=2.5),
        ]
    )
    strict_passed = strict_filter.filter(evaluations)

    print("\n=== Strict pass/fail check ===")
    for ev in evaluations:
        status = "PASS" if ev.passed_threshold else "FAIL"
        print(f"{ev.trajectory_id}: {status} (global={ev.global_score:.2f})")
    print("Survivors:", [ev.trajectory_id for ev in strict_passed])

    print("\nDiagnostic hints:")
    print(
        "- If ideal-clear-pass is below 3.0, "
        "the evaluator model is under-scoring obvious success."
    )
    print(
        "- If obvious-fail passes, "
        "the threshold/filter configuration is too loose or scores are wrong."
    )
    print(
        "- If irrelevant-actions has mean_conf > 0.5, "
        "confidence is not being treated as applicability."
    )
    print(
        "- If all trajectories are close together, "
        "the local model is likely not following the rubric well."
    )

    await client.close()


if __name__ == "__main__":
    asyncio.run(main())
