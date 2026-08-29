"""Build an immutable AdaRubric workbook from tree-classified steps."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from .model_config import load_env_values, load_model_config
from .DevelopRubrics.trajectory_tools.gui_trajectory_excel import (
    QwenSummarizer, StepRecord, TaskRecord, TrajectoryRecord, write_workbook,
)

FINAL_ANSWER_CACHE = Path(__file__).resolve().parent.parent / "backend_workspace" / "rubric_outputs" / "cache" / "qwen_tree_final_answers.json"

def _env(path: Path) -> dict[str, str]:
    values = load_env_values(path)
    config = load_model_config(path, module="quality")
    values["MODEL_NAME"] = config.model
    values["MODEL_URL"] = config.base_url
    values["YUNAI_API_KEY"] = config.api_key
    return values

def build_quality_workbook(*, grouped: dict[str, list[tuple[str, list[Any]]]], task_goals: dict[str, str],
                           trajectory_root: Path, output: Path, env_path: Path,
                           progress: Callable[[dict[str, Any]], None] | None = None,
                           summarizer: Any | None = None) -> tuple[int, int, int]:
    values = _env(env_path) if summarizer is None else {"MODEL_NAME": getattr(summarizer, "model", "test-model")}
    if summarizer is None:
        config = load_model_config(env_path, module="quality")
        summarizer = QwenSummarizer(
            config.model,
            config.base_url,
            config.api_key or "EMPTY",
            FINAL_ANSWER_CACHE,
            timeout=config.timeout,
            max_retries=config.max_retries,
            verify=config.verify,
            proxy=config.proxy,
            trust_env=config.trust_env,
        )
    tasks = {task_id: TaskRecord(task_id, task_goals.get(task_id, task_id)) for task_id in grouped}
    trajectories: list[TrajectoryRecord] = []
    total = sum(len(items) for items in grouped.values()); completed = 0
    for task_id, items in grouped.items():
        task_text = tasks[task_id].task_text
        for trajectory_id, source_steps in items:
            records: list[StepRecord] = []
            retained_steps = [step for step in source_steps if step.counted_in_tree]
            if not retained_steps:
                raise ValueError(f"trajectory has no quality-evaluation steps: {trajectory_id}")
            for step in retained_steps:
                if not step.observation:
                    raise ValueError(f"missing observation: {trajectory_id} step {step.step_index}")
                action = json.dumps(step.action, ensure_ascii=False, separators=(",", ":"))
                screenshot = str(Path(step.image))
                prefix = f"step{step.step_index:03d}_vla"
                records.append(StepRecord(trajectory_id, task_id, step.step_index, action,
                    {"summary": step.summary, "screenshot": screenshot}, step.observation,
                    str(Path(screenshot).with_name(f"{prefix}_model_request.json")),
                    str(Path(screenshot).with_name(f"{prefix}_model_response.json")), screenshot, ""))
            final_answer = summarizer.summarize_trajectory(task_text, trajectory_id, records)
            source_directory = str(Path(retained_steps[0].image).parent)
            trajectories.append(TrajectoryRecord(trajectory_id, task_id, source_directory, final_answer,
                {"source_directory": source_directory, "observation_model": values["MODEL_NAME"],
                 "observation_prompt_version": "trajectory-intermediate-observation-v4"}, records))
            completed += 1
            if progress:
                progress({"stage": "summarizing_trajectories", "summarized_trajectories": completed,
                          "total_trajectories": total})
    write_workbook(output, tasks, trajectories)
    return len(tasks), len(trajectories), sum(len(item.steps) for item in trajectories)
