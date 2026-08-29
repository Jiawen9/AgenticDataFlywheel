"""Python 3.10+ worker used by the FastAPI quality-job manager."""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from trajectory_tools.excel_to_object import load_objects
from trajectory_tools.gui_trajectory_excel import QwenSummarizer, export_trajectory_workbook
from trajectory_tools.settings import DEFAULT_ENV_FILE, configure_model_environment, load_repository_env


HERE = Path(__file__).resolve().parent
REPOSITORY_ROOT = HERE.parents[1]
WORKSPACE = REPOSITORY_ROOT / "backend_workspace"
TREE_RUNS = WORKSPACE / "trajectory_tree_runs"
RESULTS_ROOT = WORKSPACE / "trajectory_quality_results"
CHECKPOINT_ROOT = WORKSPACE / "rubric_outputs" / "evaluations" / "checkpoints"
WORKBOOK = WORKSPACE / "rubric_trajectories.xlsx"
SUMMARY_CACHE = WORKSPACE / "rubric_outputs" / "cache" / "qwen_summaries.json"
RUBRIC_DIR = WORKSPACE / "rubric_outputs" / "rubrics"
CONFIG_PATH = HERE / "examples" / "jiawen_rubric_config.json"


def _load_script(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load module: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


GEN = _load_script("generate_jiawen_rubrics", HERE / "examples" / "generate-jiawen-rubrics.py")
EVAL = _load_script("evaluate_jiawen_rubrics", HERE / "examples" / "evaluate-jiawen-rubrics.py")


def progress(**changes: Any) -> None:
    print("PROGRESS " + json.dumps(changes, ensure_ascii=False), flush=True)


def _now() -> str:
    return datetime.now(ZoneInfo("Asia/Shanghai")).isoformat()


def _tree_manifest(run_id: str) -> dict[str, Any]:
    path = TREE_RUNS / run_id / "manifest.json"
    if not path.is_file():
        raise FileNotFoundError(f"tree run not found: {run_id}")
    return json.loads(path.read_text(encoding="utf-8"))


def _terminal_ids(tree: dict[str, Any]) -> set[str]:
    result: set[str] = set()
    stack = [tree]
    while stack:
        node = stack.pop()
        result.update(str(value) for value in node.get("terminal_trajectories", []))
        stack.extend(node.get("children", []))
    return result


def _rubric_candidates(task_id: str) -> list[Path]:
    safe = GEN._safe_filename_part(task_id)
    return [
        RUBRIC_DIR / f"jiawen_gui_initial_rubric__{safe}.json",
        RUBRIC_DIR / "jiawen_gui_initial_rubric.json",
    ]


def _matching_rubric(task_id: str) -> Path | None:
    for path in _rubric_candidates(task_id):
        if not path.is_file():
            continue
        try:
            if json.loads(path.read_text(encoding="utf-8")).get("task_id") == task_id:
                return path
        except (OSError, ValueError, json.JSONDecodeError):
            continue
    return None


async def _generate_rubric(task: Any, trajectories: list[Any], config: dict[str, Any], workbook: Path) -> Path:
    safe = GEN._safe_filename_part(task.task_id)
    rubric_path = RUBRIC_DIR / f"jiawen_gui_initial_rubric__{safe}.json"
    evidence_path = RUBRIC_DIR / f"jiawen_gui_initial_rubric__{safe}.evidence.md"
    raw_path = RUBRIC_DIR / f"jiawen_gui_initial_rubric__{safe}.raw_response.txt"
    task_config = dict(config)
    task_config.update(
        rubric_path=str(rubric_path), evidence_path=str(evidence_path),
        raw_response_path=str(raw_path), validate_rubric=False,
    )
    dimensions = GEN._int_setting(task_config, "num_dimensions", "ADARUBRIC_NUM_DIMENSIONS", default=5)
    messages = GEN.build_messages(task, trajectories, config=task_config, num_dimensions=dimensions)
    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    evidence_path.write_text(GEN._evidence_from_messages(messages, workbook, task_config, trajectories), encoding="utf-8")
    rubric = await GEN.generate_rubric(task=task, trajectories=trajectories, messages=messages, config=task_config, num_dimensions=dimensions)
    temporary = rubric_path.with_suffix(".json.tmp")
    temporary.write_text(GEN._rubric_text(rubric) + "\n", encoding="utf-8")
    os.replace(temporary, rubric_path)
    return rubric_path


def _ensure_workbook(run_id: str, manifest: dict[str, Any], task_ids: list[str]) -> tuple[Path, dict[str, Any], list[Any]]:
    run_workbook = TREE_RUNS / run_id / str(manifest.get("quality_input_file", "rubric_trajectories.xlsx"))
    if manifest.get("quality_input_file") and run_workbook.is_file():
        tasks, trajectories = load_objects(run_workbook)
        if all(task_id in tasks for task_id in task_ids):
            return run_workbook, tasks, trajectories
    if WORKBOOK.is_file():
        tasks, trajectories = load_objects(WORKBOOK)
        selected = [item for item in trajectories if item.task_id in task_ids]
        observations_ready = all(
            str(step.observation or "").strip()
            and not str(step.observation).startswith("未调用视觉模型")
            for trajectory in selected for step in trajectory.steps
        )
        answers_ready = all(str(item.final_answer or "").strip() for item in selected)
        if all(task_id in tasks for task_id in task_ids) and selected and observations_ready and answers_ready:
            return WORKBOOK, tasks, trajectories
    values = load_repository_env(DEFAULT_ENV_FILE)
    summarizer = QwenSummarizer(values["MODEL_NAME"], values["MODEL_URL"], values["YUNAI_API_KEY"], SUMMARY_CACHE)
    export_trajectory_workbook(WORKSPACE / "rollout_trajectories", WORKBOOK, summarizer)
    tasks, trajectories = load_objects(WORKBOOK)
    return WORKBOOK, tasks, trajectories


async def run(run_id: str, task_ids: list[str], job_id: str) -> dict[str, Any]:
    configure_model_environment(DEFAULT_ENV_FILE)
    manifest = _tree_manifest(run_id)
    manifest_tasks = {str(item["task_id"]): item for item in manifest.get("tasks", [])}
    unknown = [task_id for task_id in task_ids if task_id not in manifest_tasks]
    if unknown:
        raise ValueError(f"tasks not in tree run: {unknown}")
    total = sum(int(manifest_tasks[item].get("trajectory_count", 0)) for item in task_ids)
    progress(stage="preparing", total_trajectories=total, completed_trajectories=0, percent=2)
    workbook, tasks, all_trajectories = _ensure_workbook(run_id, manifest, task_ids)
    config = GEN.load_config(CONFIG_PATH)
    staging = RESULTS_ROOT / f".{run_id}.{job_id}.tmp"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    completed = 0
    summaries: list[dict[str, Any]] = []

    for task_index, task_id in enumerate(task_ids, 1):
        task = tasks.get(task_id)
        if task is None:
            raise ValueError(f"task missing from rubric workbook: {task_id}")
        trajectories = [item for item in all_trajectories if item.task_id == task_id]
        tree_path = TREE_RUNS / run_id / str(manifest_tasks[task_id]["tree_file"])
        terminals = _terminal_ids(json.loads(tree_path.read_text(encoding="utf-8")))
        by_id = {item.trajectory_id: item for item in trajectories}
        if terminals != set(by_id):
            raise ValueError(f"tree/workbook trajectory mismatch for {task_id}: tree={sorted(terminals)}, workbook={sorted(by_id)}")
        rubric_path = _matching_rubric(task_id)
        if rubric_path is None:
            progress(stage="generating_rubric", current_task=task_id, task_index=task_index, percent=max(5, round(15 * completed / max(total, 1))))
            rubric_path = await _generate_rubric(task, trajectories, config, workbook)
        rubric = EVAL._load_rubric(rubric_path, task)
        pipeline = EVAL._build_pipeline(config)
        checkpoint = CHECKPOINT_ROOT / run_id / f"{task_id}.jsonl"
        EVAL.initialize_evaluations_jsonl(checkpoint, resume=True)
        existing = EVAL.load_existing_evaluations_jsonl(checkpoint)
        settings = EVAL._evaluation_settings(config)
        signature = EVAL._settings_signature(settings)
        cached_count = 0
        for trajectory in trajectories:
            key = EVAL._evaluation_key(
                task_id=task_id,
                run_number=1,
                trajectory_id=trajectory.trajectory_id,
                settings_signature=signature,
            )
            if key in existing:
                cached_count += 1
        completed += cached_count
        if cached_count:
            progress(
                stage="evaluating", current_task=task_id, task_index=task_index,
                completed_trajectories=completed, total_trajectories=total,
                percent=20 + round(75 * completed / max(total, 1)),
            )

        def on_trajectory_complete(evaluation: Any) -> None:
            nonlocal completed
            completed += 1
            progress(
                stage="evaluating", current_task=task_id,
                current_trajectory=evaluation.trajectory_id, task_index=task_index,
                completed_trajectories=completed, total_trajectories=total,
                percent=20 + round(75 * completed / max(total, 1)),
            )

        result = await EVAL.evaluate_run_incrementally(
            pipeline=pipeline,
            task=task,
            trajectories=trajectories,
            rubric=rubric,
            rubric_path=rubric_path,
            run_number=1,
            temperature=float(config.get("evaluation_temperature", 0.0)),
            eval_max_tokens=int(config.get("evaluation_max_tokens", 8192)),
            max_concurrent=EVAL._int_setting(
                config, "evaluation_max_concurrent", "ADARUBRIC_EVAL_MAX_CONCURRENT", default=2
            ),
            evaluations_path=checkpoint,
            config=config,
            existing_evaluations=existing,
            on_trajectory_complete=on_trajectory_complete,
        )
        evaluations = result.all_evaluations
        serialized = {}
        for evaluation in evaluations:
            data = json.loads(evaluation.model_dump_json(exclude={"rubric_used"}))
            serialized[evaluation.trajectory_id] = data
        average = sum(item.global_score for item in evaluations) / len(evaluations)
        passed = sum(1 for item in evaluations if item.passed_threshold)
        task_result = {
            "run_id": run_id, "task_id": task_id, "completed_at": _now(),
            "rubric": json.loads(rubric.model_dump_json()), "rubric_path": str(rubric_path),
            "evaluation_settings": settings, "trajectory_count": len(evaluations),
            "average_score": average, "passed_count": passed, "evaluations": serialized,
        }
        (staging / f"{task_id}.json").write_text(json.dumps(task_result, ensure_ascii=False, indent=2), encoding="utf-8")
        summaries.append({key: task_result[key] for key in ("task_id", "completed_at", "trajectory_count", "average_score", "passed_count")})

    progress(stage="publishing", completed_trajectories=completed, total_trajectories=total, percent=97)
    target = RESULTS_ROOT / run_id
    target.mkdir(parents=True, exist_ok=True)
    previous = {}
    old_manifest = target / "manifest.json"
    if old_manifest.is_file():
        previous = {str(item["task_id"]): item for item in json.loads(old_manifest.read_text(encoding="utf-8")).get("tasks", [])}
    for summary in summaries:
        previous[summary["task_id"]] = summary
    for path in staging.glob("*.json"):
        os.replace(path, target / path.name)
    payload = {"run_id": run_id, "updated_at": _now(), "tasks": sorted(previous.values(), key=lambda item: item["task_id"])}
    temporary = target / ".manifest.json.tmp"
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, old_manifest)
    staging.rmdir()
    return payload


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--task-id", action="append", required=True)
    parser.add_argument("--job-id", required=True)
    args = parser.parse_args()
    result = asyncio.run(run(args.run_id, args.task_id, args.job_id))
    print("RESULT " + json.dumps(result, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
