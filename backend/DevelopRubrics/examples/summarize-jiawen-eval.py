"""Summarize Jiawen GUI evaluation JSONL into per-task trajectory rankings.

The evaluator writes one JSONL record per trajectory/run. This script groups
records by task and trajectory, sorts trajectories by score descending, and
saves Markdown/CSV summaries for inspection.
"""

from __future__ import annotations

import argparse
import csv
import json
from dataclasses import dataclass
from pathlib import Path
from statistics import mean, pstdev
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "examples" / "jiawen_rubric_config.json"
DEFAULT_JSONL_PATH = PROJECT_ROOT / "docs" / "evaluation_outputs" / "jiawen_gui_eval.jsonl"
DEFAULT_REPORT_PATH = (
    PROJECT_ROOT / "docs" / "evaluation_outputs" / "jiawen_gui_eval_rankings.md"
)
DEFAULT_CSV_PATH = (
    PROJECT_ROOT / "docs" / "evaluation_outputs" / "jiawen_gui_eval_rankings.csv"
)

Config = dict[str, Any]


@dataclass(frozen=True)
class EvaluationRecord:
    task_id: str
    trajectory_id: str
    run_number: int
    global_score: float
    passed_threshold: bool


@dataclass(frozen=True)
class TrajectorySummary:
    task_id: str
    trajectory_id: str
    runs: int
    mean_score: float
    std_score: float
    min_score: float
    max_score: float
    pass_count: int

    @property
    def pass_fail(self) -> str:
        if self.pass_count == self.runs:
            return "PASS"
        if self.pass_count == 0:
            return "FAIL"
        return f"MIXED ({self.pass_count}/{self.runs})"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create per-task trajectory rankings from Jiawen evaluation JSONL."
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help=f"Config path. Defaults to {DEFAULT_CONFIG_PATH}",
    )
    parser.add_argument(
        "--jsonl",
        type=Path,
        default=None,
        help="Evaluation JSONL path. Defaults to evaluation_jsonl_path in config.",
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=DEFAULT_REPORT_PATH,
        help=f"Markdown report path. Defaults to {DEFAULT_REPORT_PATH}",
    )
    parser.add_argument(
        "--csv",
        type=Path,
        default=DEFAULT_CSV_PATH,
        help=f"CSV output path. Defaults to {DEFAULT_CSV_PATH}",
    )
    return parser.parse_args()


def _resolve_path(path: str | Path, *, base: Path = PROJECT_ROOT) -> Path:
    resolved = Path(path)
    return resolved if resolved.is_absolute() else base / resolved


def load_config(path: Path) -> Config:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def evaluation_jsonl_path(config: Config, explicit_path: Path | None) -> Path:
    if explicit_path is not None:
        return _resolve_path(explicit_path)
    configured = config.get("evaluation_jsonl_path")
    if configured:
        return _resolve_path(str(configured))
    return DEFAULT_JSONL_PATH


def load_records(path: Path) -> list[EvaluationRecord]:
    if not path.exists():
        raise FileNotFoundError(f"Evaluation JSONL not found: {path}")

    records: list[EvaluationRecord] = []
    with path.open("r", encoding="utf-8-sig") as file:
        for line_number, line in enumerate(file, 1):
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
                evaluation = raw["evaluation"]
                records.append(
                    EvaluationRecord(
                        task_id=str(raw.get("task_id") or evaluation["task_id"]),
                        trajectory_id=str(evaluation["trajectory_id"]),
                        run_number=int(raw["run_number"]),
                        global_score=float(evaluation["global_score"]),
                        passed_threshold=bool(evaluation.get("passed_threshold", False)),
                    )
                )
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                print(f"Skipping invalid JSONL line {line_number}: {exc}")
    return records


def summarize(records: list[EvaluationRecord]) -> dict[str, list[TrajectorySummary]]:
    by_task_trajectory: dict[tuple[str, str], list[EvaluationRecord]] = {}
    for record in records:
        by_task_trajectory.setdefault((record.task_id, record.trajectory_id), []).append(record)

    summaries_by_task: dict[str, list[TrajectorySummary]] = {}
    for (task_id, trajectory_id), group in by_task_trajectory.items():
        scores = [record.global_score for record in group]
        summary = TrajectorySummary(
            task_id=task_id,
            trajectory_id=trajectory_id,
            runs=len(group),
            mean_score=mean(scores),
            std_score=pstdev(scores) if len(scores) > 1 else 0.0,
            min_score=min(scores),
            max_score=max(scores),
            pass_count=sum(record.passed_threshold for record in group),
        )
        summaries_by_task.setdefault(task_id, []).append(summary)

    for task_summaries in summaries_by_task.values():
        task_summaries.sort(
            key=lambda item: (item.mean_score, item.pass_count, item.trajectory_id),
            reverse=True,
        )
    return dict(sorted(summaries_by_task.items()))


def build_report(summaries_by_task: dict[str, list[TrajectorySummary]], source_path: Path) -> str:
    lines = [
        "# Jiawen GUI Evaluation Rankings",
        "",
        f"Source JSONL: `{source_path}`",
        "",
    ]

    for task_id, summaries in summaries_by_task.items():
        lines.extend(
            [
                f"## Task `{task_id}`",
                "",
                "| Rank | Trajectory ID | Mean Score | Std | Min | Max | Pass/Fail | Runs |",
                "|---:|---|---:|---:|---:|---:|---|---:|",
            ]
        )
        for rank, summary in enumerate(summaries, 1):
            lines.append(
                f"| {rank} | `{summary.trajectory_id}` | "
                f"{summary.mean_score:.3f} | {summary.std_score:.3f} | "
                f"{summary.min_score:.3f} | {summary.max_score:.3f} | "
                f"{summary.pass_fail} | {summary.runs} |"
            )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def save_report(report: str, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(report, encoding="utf-8")


def save_csv(summaries_by_task: dict[str, list[TrajectorySummary]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(
            [
                "task_id",
                "rank",
                "trajectory_id",
                "mean_score",
                "std_score",
                "min_score",
                "max_score",
                "pass_fail",
                "pass_count",
                "runs",
            ]
        )
        for task_id, summaries in summaries_by_task.items():
            for rank, summary in enumerate(summaries, 1):
                writer.writerow(
                    [
                        task_id,
                        rank,
                        summary.trajectory_id,
                        f"{summary.mean_score:.6f}",
                        f"{summary.std_score:.6f}",
                        f"{summary.min_score:.6f}",
                        f"{summary.max_score:.6f}",
                        summary.pass_fail,
                        summary.pass_count,
                        summary.runs,
                    ]
                )


def main() -> None:
    args = parse_args()
    config = load_config(args.config)
    source_path = evaluation_jsonl_path(config, args.jsonl)
    records = load_records(source_path)
    summaries_by_task = summarize(records)

    report = build_report(summaries_by_task, source_path)
    save_report(report, _resolve_path(args.report))
    save_csv(summaries_by_task, _resolve_path(args.csv))

    print(report)
    print(f"Saved ranking report to {_resolve_path(args.report)}")
    print(f"Saved ranking CSV to {_resolve_path(args.csv)}")


if __name__ == "__main__":
    main()
