"""Python 3.10+ worker used by the FastAPI quality-job manager."""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from trajectory_tools.settings import DEFAULT_ENV_FILE, configure_model_environment


HERE = Path(__file__).resolve().parent
REPOSITORY_ROOT = HERE.parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))
from backend.batch_operations import batch_operation
from backend.data_store import DATA_ROOT, ArtifactStore, RecordStore
from backend.stage_artifacts import load_quality_objects, quality_tables, write_payload_workbook, write_sidecar
from backend.batch_results import (current_tree_payload, current_tree_batch, merge_quality_results,
                                   quality_task_fingerprints, StaleTaskInput)
from backend.trajectory_data import resolve_tree_run_dir
from backend.quality_data import quality_manifest

WORKSPACE = DATA_ROOT / "system"
TREE_RUNS = WORKSPACE / "trajectory_tree_runs"
RESULTS_ROOT = WORKSPACE / "trajectory_quality_results"
CHECKPOINT_ROOT = DATA_ROOT / "cache" / "rubric_outputs" / "evaluations" / "checkpoints"
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
    path = resolve_tree_run_dir(run_id, TREE_RUNS) / "manifest.json"
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
    """Read only the quality JSON frozen by this tree run."""
    run_root = resolve_tree_run_dir(run_id, TREE_RUNS)
    name = manifest.get("quality_input_json")
    if not name:
        raise ValueError(f"tree run {run_id} has no required quality_input_json")
    snapshot = (run_root / str(name)).resolve()
    if not snapshot.is_relative_to(run_root.resolve()):
        raise ValueError("quality input JSON must belong to its tree run")
    tasks, trajectories = load_quality_objects(snapshot)
    if not all(task_id in tasks for task_id in task_ids):
        raise ValueError("quality JSON snapshot is missing selected tasks")
    selected = [item for item in trajectories if item.task_id in task_ids]
    if not selected or any(not str(item.final_answer or "").strip() for item in selected):
        raise ValueError("quality JSON snapshot is missing final answers")
    if any(not str(step.observation or "").strip() for item in selected for step in item.steps):
        raise ValueError("quality JSON snapshot is missing observations")
    workbook = run_root / str(manifest.get("quality_input_file", "rubric_trajectories.xlsx"))
    return workbook, tasks, trajectories


async def run(run_id: str, task_ids: list[str], job_id: str) -> dict[str, Any]:
    alias = RecordStore(DATA_ROOT).get("batch_run_aliases", run_id)
    operation_batch = alias["batch_id"] if alias else run_id
    with batch_operation(operation_batch, "quality", DATA_ROOT):
        current = current_tree_payload(run_id, DATA_ROOT)
        if current.get("trees"):
            job = RecordStore(DATA_ROOT).get("quality_jobs", job_id) or {}
            expected = job.get("source_tree_hashes") or {task: current["tree_hashes"].get(task) for task in task_ids}
            if any(current["tree_hashes"].get(task) != value for task, value in expected.items()):
                raise StaleTaskInput("所选任务的轨迹树已变化，请重新质检")
            parent = DATA_ROOT / "tmp" / "quality-jobs"
            parent.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix=f"{job_id}-", dir=parent) as directory:
                work = Path(directory)
                quality_path = work / "rubric_trajectories.xlsx"
                write_payload_workbook(quality_path, current["quality_input"])
                write_sidecar(quality_path, current["quality_input"])
                return await _run_frozen(run_id, task_ids, job_id, current, work)
        if (RecordStore(DATA_ROOT).get("quality_jobs", job_id) or {}).get("batch_id"):
            raise StaleTaskInput("当前批次轨迹树已失效，请重新建树")
        manifest = _tree_manifest(run_id)
        with batch_operation(str(manifest.get("batch_id") or operation_batch), "quality_legacy", DATA_ROOT):
            return await _run_frozen(run_id, task_ids, job_id)


async def _run_frozen(run_id: str, task_ids: list[str], job_id: str,
                      current: dict | None = None, work: Path | None = None) -> dict[str, Any]:
    configure_model_environment(DEFAULT_ENV_FILE)
    manifest = current or _tree_manifest(run_id)
    manifest_tasks = {str(item["task_id"]): item for item in manifest.get("tasks", [])}
    unknown = [task_id for task_id in task_ids if task_id not in manifest_tasks]
    if unknown:
        raise ValueError(f"tasks not in tree run: {unknown}")
    total = sum(int(manifest_tasks[item].get("trajectory_count", 0)) for item in task_ids)
    progress(stage="preparing", total_trajectories=total, completed_trajectories=0, percent=2)
    if current is not None:
        workbook = work / "rubric_trajectories.xlsx"
        tasks, all_trajectories = load_quality_objects(workbook.with_suffix(".json"))
        selected = [item for item in all_trajectories if item.task_id in task_ids]
        if not selected or any(not str(item.final_answer or "").strip() for item in selected):
            raise ValueError("quality JSON snapshot is missing final answers")
        if any(not str(step.observation or "").strip() for item in selected for step in item.steps):
            raise ValueError("quality JSON snapshot is missing observations")
    else:
        workbook, tasks, all_trajectories = _ensure_workbook(run_id, manifest, task_ids)
    config = GEN.load_config(CONFIG_PATH)
    input_fingerprints = quality_task_fingerprints(current) if current is not None else {}
    queued = RecordStore(DATA_ROOT).get("quality_jobs", job_id) or {}
    if current is not None and queued.get("task_fingerprints") and any(
            queued["task_fingerprints"].get(task) != input_fingerprints.get(task) for task in task_ids):
        raise StaleTaskInput("质检配置已变化，请重新提交")
    completed = 0
    summaries: list[dict[str, Any]] = []
    task_results: list[dict[str, Any]] = []

    for task_index, task_id in enumerate(task_ids, 1):
        task = tasks.get(task_id)
        if task is None:
            raise ValueError(f"task missing from rubric workbook: {task_id}")
        trajectories = [item for item in all_trajectories if item.task_id == task_id]
        if current is not None:
            tree = current["trees"][task_id]
        else:
            tree_path = resolve_tree_run_dir(run_id, TREE_RUNS) / str(manifest_tasks[task_id]["tree_file"])
            tree = json.loads(tree_path.read_text(encoding="utf-8"))
        terminals = _terminal_ids(tree)
        by_id = {item.trajectory_id: item for item in trajectories}
        if terminals != set(by_id):
            raise ValueError(f"tree/workbook trajectory mismatch for {task_id}: tree={sorted(terminals)}, workbook={sorted(by_id)}")
        rubric_path = _matching_rubric(task_id)
        if rubric_path is None:
            progress(stage="generating_rubric", current_task=task_id, task_index=task_index, percent=max(5, round(15 * completed / max(total, 1))))
            rubric_path = await _generate_rubric(task, trajectories, config, workbook)
        rubric = EVAL._load_rubric(rubric_path, task)
        pipeline = EVAL._build_pipeline(config)
        checkpoint_key = quality_task_fingerprints(current).get(task_id) if current is not None else None
        checkpoint = (CHECKPOINT_ROOT / run_id / checkpoint_key / f"{task_id}.jsonl" if checkpoint_key
                      else CHECKPOINT_ROOT / run_id / f"{task_id}.jsonl")
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
        task_results.append(task_result)
        summaries.append({key: task_result[key] for key in ("task_id", "completed_at", "trajectory_count", "average_score", "passed_count")})

    progress(stage="publishing", completed_trajectories=completed, total_trajectories=total, percent=97)
    if current is not None:
        fingerprints = quality_task_fingerprints(current)
        if any(fingerprints.get(task) != input_fingerprints.get(task) for task in task_ids):
            raise StaleTaskInput("质检期间模型配置已变化，请重新提交")
        return merge_quality_results(run_id, task_results,
            {task: current["tree_hashes"][task] for task in task_ids},
            {task: fingerprints[task] for task in task_ids}, job_id=job_id, completed_at=_now(), root=DATA_ROOT)
    artifact = ArtifactStore(DATA_ROOT).publish(str(manifest.get("batch_id") or run_id), "05_quality",
        {"schema_version": 1, "run_id": run_id, "job_id": job_id, "tasks": task_results},
        tables=quality_tables(task_results),
        source_refs=[item for item in manifest.get("artifacts", []) if item.get("stage") == "04_tree"]
            or [{"kind": "tree_run", "run_id": run_id}],
        metadata={"run_id": run_id, "job_id": job_id, "task_ids": task_ids})
    records = RecordStore(DATA_ROOT)
    entries = []
    for task_result in task_results:
        task_result["artifact"] = artifact
        entries.append({"namespace": "quality_results", "key": f"{run_id}:{task_result['task_id']}", "payload": task_result})
    previous_manifest = quality_manifest(run_id)
    previous = {str(item["task_id"]): item for item in previous_manifest.get("tasks", [])}
    for summary in summaries:
        previous[summary["task_id"]] = summary
    payload = {"run_id": run_id, "updated_at": _now(), "artifact": artifact, "tasks": sorted(previous.values(), key=lambda item: item["task_id"])}
    entries.append({"namespace": "quality_manifests", "key": run_id, "payload": payload,
                    "expected_revision": previous_manifest.get("storage_revision", 0)})
    saved = records.put_many(entries)
    payload = saved[-1]
    return payload


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--task-id", action="append", required=True)
    parser.add_argument("--job-id", required=True)
    args = parser.parse_args()
    try:
        result = asyncio.run(run(args.run_id, args.task_id, args.job_id))
    except StaleTaskInput as exc:
        print("ERROR " + json.dumps({"kind": "stale", "message": str(exc)}, ensure_ascii=False), flush=True)
        return 2
    print("RESULT " + json.dumps(result, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
