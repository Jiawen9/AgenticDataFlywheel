"""Business logic for task-generation Web jobs."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Callable

import pandas as pd

from . import adapter, store


MAX_UPLOAD_BYTES = 50 * 1024 * 1024
SCENE_MATCH_COLUMNS = ["app", "task", "scene", "capability", "sub_capability"]
FLYWHEEL_COLUMNS = [
    "用例编号",
    "源失败任务",
    "app",
    "scene",
    "capability",
    "sub_capability",
    "生成的变体任务",
    "run",
    "审核状态",
]
REQUIRED_INPUT_COLUMNS = {"任务结果", "任务", "涉及APP"}


def source_status() -> dict[str, Any]:
    return adapter.source_status()


def _json_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if isinstance(value, float) and math.isnan(value):
        return None
    if hasattr(value, "item"):
        try:
            return _json_value(value.item())
        except (TypeError, ValueError):
            pass
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _write_rows(job_id: str, rows: list[dict[str, Any]], *, xlsx_columns: list[str] | None = None) -> dict[str, str]:
    safe_rows = [_json_value(row) for row in rows]
    store.save_json_artifact(job_id, "result.json", safe_rows)
    output_path = store.artifact_path(job_id, "result.xlsx")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    frame = pd.DataFrame(safe_rows)
    if xlsx_columns is not None:
        for column in xlsx_columns:
            if column not in frame.columns:
                frame[column] = ""
        frame = frame.reindex(columns=xlsx_columns)
    frame.to_excel(output_path, index=False)
    return {"result_json": "result.json", "result_xlsx": "result.xlsx"}


def _write_scene_matches(job_id: str, rows: list[dict[str, Any]]) -> dict[str, str]:
    safe_rows = [_json_value(row) for row in rows]
    store.save_json_artifact(job_id, "scene_matches.json", safe_rows)
    output_path = store.artifact_path(job_id, "scene_matches.xlsx")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(safe_rows, columns=SCENE_MATCH_COLUMNS).to_excel(output_path, index=False, sheet_name="新场景匹配")
    return {"scene_matches_json": "scene_matches.json", "scene_matches_xlsx": "scene_matches.xlsx"}


def create_input(original_filename: str, content: bytes) -> dict[str, Any]:
    if not original_filename or Path(original_filename).suffix.lower() != ".xlsx":
        raise ValueError("只支持 .xlsx 格式的失败用例文件")
    if not content:
        raise ValueError("上传文件为空")
    if len(content) > MAX_UPLOAD_BYTES:
        raise ValueError("上传文件不能超过 50 MB")

    input_id = store.new_id()
    metadata = store.save_input(input_id, Path(original_filename).name, content)
    try:
        frame = pd.read_excel(store.input_path(input_id), sheet_name=0, nrows=0)
        missing = sorted(REQUIRED_INPUT_COLUMNS.difference(map(str, frame.columns)))
        if missing:
            raise ValueError(f"首个工作表缺少字段：{', '.join(missing)}")
        _, failures = adapter.read_testcase(store.input_path(input_id))
    except Exception as exc:  # noqa: BLE001 - validation boundary
        metadata["status"] = "invalid"
        metadata["error"] = f"无法读取 Excel：{exc}"
        store.save_input_metadata(metadata)
        return metadata
    metadata["failed_count"] = len(failures)
    metadata["status"] = "ready"
    store.save_input_metadata(metadata)
    return metadata


def knowledge_runner(job_id: str, params: dict[str, Any]) -> Callable[[Callable[..., None]], dict[str, Any]]:
    def runner(progress: Callable[..., None]) -> dict[str, Any]:
        status = adapter.source_status()
        if not status["ready"]:
            raise ValueError("知识库不可用：" + "；".join(status["errors"]))
        progress(stage="generating", current="读取三份 KnowledgeBase")
        rows = adapter.task_generate(
            **params,
            progress_callback=lambda completed, total: progress(
                stage="generating",
                current=f"生成场景节点 {completed}/{total}",
                completed=completed,
                total=total,
            ),
        )
        progress(stage="writing", current="保存生成结果")
        artifacts = _write_rows(job_id, rows)
        return {"result_count": len(rows), "artifacts": artifacts}

    return runner


def scene_match_runner(job_id: str, input_id: str) -> Callable[[Callable[..., None]], dict[str, Any]]:
    def runner(progress: Callable[..., None]) -> dict[str, Any]:
        metadata = store.load_input(input_id)
        if not metadata or metadata.get("status") != "ready":
            raise ValueError("上传的失败用例文件不存在或不可用")
        _, failures = adapter.read_testcase(store.input_path(input_id))
        progress(stage="scene_matching", current="匹配失败用例场景", total=len(failures), completed=0)
        rows = adapter.match_scene_by_task(
            failures,
            progress_callback=lambda completed, total: progress(
                stage="scene_matching",
                current=f"匹配失败用例 {completed}/{total}",
                completed=completed,
                total=total,
            ),
        )
        progress(stage="writing", current="保存场景匹配结果", total=len(failures), completed=len(failures))
        artifacts = _write_scene_matches(job_id, rows)
        return {"result_count": len(rows), "input_id": input_id, "artifacts": artifacts}

    return runner


def variant_runner(job_id: str, scene_match_job_id: str, generate_n: int) -> Callable[[Callable[..., None]], dict[str, Any]]:
    def runner(progress: Callable[..., None]) -> dict[str, Any]:
        source_job = store.load_job(scene_match_job_id)
        if not source_job or source_job.get("status") != "succeeded":
            raise ValueError("请先完成场景匹配")
        input_id = str(source_job.get("input_id", ""))
        scene_path = store.artifact_path(scene_match_job_id, "scene_matches.xlsx")
        if not scene_path.is_file():
            raise FileNotFoundError("场景匹配结果文件不存在")
        rows_path = store.artifact_path(scene_match_job_id, "scene_matches.json")
        rows = json.loads(rows_path.read_text(encoding="utf-8"))
        progress(stage="generating", current="生成失败用例变体", total=len(rows), completed=0)
        output_path = store.artifact_path(job_id, "result.xlsx")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        adapter.generate_flywheel_rows(
            scene_path,
            output_path,
            generate_n=generate_n,
            progress_callback=lambda completed, total: progress(
                stage="generating",
                current=f"生成变体 {completed}/{total}",
                completed=completed,
                total=total,
            ),
        )
        generated = pd.read_excel(output_path).to_dict(orient="records") if output_path.is_file() else []
        progress(stage="writing", current="保存变体任务结果", total=len(rows), completed=len(rows))
        artifacts = _write_rows(job_id, generated, xlsx_columns=FLYWHEEL_COLUMNS)
        return {"result_count": len(generated), "input_id": input_id, "scene_match_job_id": scene_match_job_id, "artifacts": artifacts}

    return runner


def preview(job: dict[str, Any]) -> dict[str, Any]:
    filename = "scene_matches.json" if job.get("kind") == "scene_match" else "result.json"
    path = store.artifact_path(str(job["job_id"]), filename)
    rows: list[dict[str, Any]] = []
    if path.is_file():
        payload = json.loads(path.read_text(encoding="utf-8"))
        rows = payload if isinstance(payload, list) else []
    return {"job": job, "total": len(rows), "rows": rows[:2000]}
