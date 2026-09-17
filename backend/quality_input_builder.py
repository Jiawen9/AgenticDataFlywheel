"""Build an immutable AdaRubric workbook from tree-classified steps."""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable

from .DevelopRubrics.trajectory_tools.gui_trajectory_excel import (
    QwenSummarizer, StepRecord, TaskRecord, TrajectoryRecord, write_workbook,
    SYSTEM_PROMPT, DOMAIN, EXPECTED_TOOLS,
)
from .data_store import DATA_ROOT
from .stage_artifacts import write_sidecar

FINAL_ANSWER_CACHE = DATA_ROOT / "cache" / "rubric_outputs" / "qwen_tree_final_answers.json"

def _env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1); values[key.strip()] = value.strip().strip('"').strip("'")
    missing = [key for key in ("YUNAI_API_KEY", "MODEL_URL", "MODEL_NAME") if not values.get(key)]
    if missing:
        raise ValueError(f"missing required model settings: {', '.join(missing)}")
    return values

def build_quality_workbook(*, grouped: dict[str, list[tuple[str, list[Any]]]], task_goals: dict[str, str],
                           trajectory_root: Path, output: Path, env_path: Path,
                           progress: Callable[[dict[str, Any]], None] | None = None,
                           summarizer: Any | None = None,
                           max_concurrent: int = 1) -> tuple[int, int, int]:
    values = _env(env_path) if summarizer is None else {"MODEL_NAME": getattr(summarizer, "model", "test-model")}
    if summarizer is None:
        summarizer = QwenSummarizer(values["MODEL_NAME"], values["MODEL_URL"], values["YUNAI_API_KEY"], FINAL_ANSWER_CACHE)
    tasks = {task_id: TaskRecord(task_id, task_goals.get(task_id, task_id)) for task_id in grouped}
    prepared: list[tuple[str, str, str, str, list[StepRecord]]] = []
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
                    {"summary": step.summary, "screenshot": screenshot,
                     "source_identity": step.identity_dict() if hasattr(step, "identity_dict") else {}}, step.observation,
                    str(Path(screenshot).with_name(f"{prefix}_model_request.json")),
                    str(Path(screenshot).with_name(f"{prefix}_model_response.json")), screenshot, ""))
            source_directory = str(Path(retained_steps[0].image).parent)
            prepared.append((task_id, task_text, trajectory_id, source_directory, records))

    total = len(prepared)
    final_answers: list[str | None] = [None] * total

    def _summarize(item: tuple[str, str, str, str, list[StepRecord]]) -> str:
        _, task_text, trajectory_id, _, records = item
        return summarizer.summarize_trajectory(task_text, trajectory_id, records)

    worker_count = max(1, int(max_concurrent))
    completed = 0
    if worker_count == 1:
        for index, item in enumerate(prepared):
            final_answers[index] = _summarize(item)
            completed += 1
            if progress:
                progress({"stage": "summarizing_trajectories", "summarized_trajectories": completed,
                          "total_trajectories": total})
    else:
        with ThreadPoolExecutor(
            max_workers=worker_count,
            thread_name_prefix="tree-summary",
        ) as executor:
            pending = {
                executor.submit(_summarize, item): index
                for index, item in enumerate(prepared)
            }
            for future in as_completed(pending):
                final_answers[pending[future]] = future.result()
                completed += 1
                if progress:
                    progress({"stage": "summarizing_trajectories", "summarized_trajectories": completed,
                              "total_trajectories": total})

    trajectories: list[TrajectoryRecord] = []
    for index, (task_id, _, trajectory_id, source_directory, records) in enumerate(prepared):
        final_answer = final_answers[index]
        if final_answer is None:
            raise RuntimeError(f"missing trajectory summary: {trajectory_id}")
        trajectories.append(TrajectoryRecord(trajectory_id, task_id, source_directory, final_answer,
            {"source_directory": source_directory, "observation_model": values["MODEL_NAME"],
             "observation_prompt_version": "trajectory-intermediate-observation-v4",
             "source_identity": records[0].action_input.get("source_identity", {})}, records))
    write_workbook(output, tasks, trajectories)
    write_sidecar(output, _quality_payload(tasks, trajectories))
    return len(tasks), len(trajectories), sum(len(item.steps) for item in trajectories)


def _quality_payload(tasks: dict[str, TaskRecord], trajectories: list[TrajectoryRecord]) -> dict[str, Any]:
    """Keep structured identity in memory; Excel is only the corresponding view."""
    encode = lambda value: json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    columns = {
        "Tasks": ["task_id", "task_text", "instruction", "domain", "complexity", "context_json", "expected_tools_json"],
        "Trajectories": ["trajectory_id", "task_id", "source_directory", "step_count", "final_answer", "metadata_json"],
        "Steps": ["trajectory_id", "task_id", "step_id", "action", "action_input_json", "observation",
                  "request_file", "response_file", "screenshot_path", "source_warning"],
    }
    values = {
        "Tasks": [[task.task_id, task.task_text, f"{SYSTEM_PROMPT}\n\nTask:\n{task.task_text}", DOMAIN, "complex",
                   encode({"task_description": task.task_text}), encode(EXPECTED_TOOLS)] for task in tasks.values()],
        "Trajectories": [[item.trajectory_id, item.task_id, item.source_directory, len(item.steps), item.final_answer,
                          encode(item.metadata)] for item in trajectories],
        "Steps": [[step.trajectory_id, step.task_id, step.step_id, step.action, encode(step.action_input), step.observation,
                   step.request_file, step.response_file, step.screenshot_path, step.source_warning]
                  for item in trajectories for step in item.steps],
    }
    return {"schema_version": 1, "columns": columns,
            "sheets": {name: [dict(zip(columns[name], row)) for row in rows] for name, rows in values.items()}}
