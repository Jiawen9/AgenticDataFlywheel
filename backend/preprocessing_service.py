"""Pure conversion of frozen collection inputs and the shared annotation adapter."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any, Callable

from .data_store.paths import contained_path, rebase_data_path
from .export_vla_trajectories import STEP_RESPONSE_RE, extract_response_fields
from .stage_artifacts import fingerprint
from .trajectories_preprocessing import DEFAULT_ENV_FILE, read_env_file

SHEET = "VLA trajectories"
COLUMNS = ["文件夹名", "image", "xml", "action", "summary"]


class PreprocessingError(ValueError):
    def __init__(self, message: str, status: int = 409):
        super().__init__(message)
        self.status = status


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")).encode("utf-8")).hexdigest()


def processing_config(env_file: Path = DEFAULT_ENV_FILE) -> dict:
    # Credentials never enter job records or cache fingerprints.
    try:
        values = read_env_file(env_file)
        error = None
    except (OSError, ValueError) as exc:
        values, error = {}, str(exc)
    code_files = ["export_vla_trajectories.py", "trajectories_preprocessing.py",
                  "preprocessing_service.py", "bounding_box/qwen_reviewer.py",
                  "bounding_box/build_annotations.py", "bounding_box/action_box.py"]
    code = {name: fingerprint(Path(__file__).parent / name) for name in code_files}
    return {"model": values.get("MODEL_NAME"), "base_url": values.get("MODEL_URL"),
            "max_review_rounds": 4, "pipeline_revision": digest(code),
            "configuration_error": error}


def verify_input(snapshot: dict, root: Path) -> None:
    raw_root = rebase_data_path(snapshot["raw_root"], root)
    if not raw_root.is_relative_to((Path(root) / "raw").resolve()):
        raise PreprocessingError("原始轨迹目录不在当前数据目录内")
    seen = set()
    for trajectory in snapshot["trajectories"]:
        run_id = trajectory["collection_run_id"]
        for item in trajectory["files"]:
            path = contained_path(raw_root, "runs", run_id, item["path"])
            if path in seen:
                continue
            seen.add(path)
            if not path.is_file() or fingerprint(path) != item["sha256"]:
                raise PreprocessingError(f"采集原文件缺失或校验值变化：{run_id}/{item['path']}")


def convert_input(snapshot: dict, progress: Callable[[dict], None], *, data_root: Path | None = None) -> tuple[dict, dict]:
    trajectories = sorted(snapshot["trajectories"],
                          key=lambda item: (item.get("collected_at") or "", item["collection_run_id"],
                                            item["task_id"], item["relative_dir"]))
    work = []
    for trajectory in trajectories:
        directory = Path(trajectory["relative_dir"])
        for item in trajectory["files"]:
            relative = Path(item["path"])
            match = STEP_RESPONSE_RE.match(relative.name)
            if relative.parent == directory and match:
                work.append((trajectory, int(match.group(1)), item))
    work.sort(key=lambda item: (item[0].get("collected_at") or "", item[0]["collection_run_id"],
                               item[0]["task_id"], item[0]["relative_dir"], item[1]))
    rows, warnings, skipped = [], [], []
    raw_root = rebase_data_path(snapshot["raw_root"], data_root) if data_root is not None else Path(snapshot["raw_root"])
    progress({"stage": "converting", "total_steps": len(work), "completed_steps": 0})
    for index, (trajectory, step, response) in enumerate(work):
        run_id = trajectory["collection_run_id"]
        source_id = trajectory["source_trajectory_id"]
        event = {"stage": "converting", "total_steps": len(work), "completed_steps": index,
                 "current_task": trajectory["task_id"], "current_trajectory": source_id,
                 "current_step": step}
        progress(event)
        files = {Path(item["path"]).as_posix(): item for item in trajectory["files"]}
        directory = Path(trajectory["relative_dir"])
        image = (directory / f"step{step:03d}_vla_input.jpg").as_posix()
        xml = (directory / f"step{step:03d}_vla_input_ui.xml").as_posix()
        fallback = (directory / f"step{step-1:03d}_vla_done_ui.xml").as_posix()
        reason = None
        if image not in files:
            reason = "缺少输入截图"
        elif xml not in files:
            if step > 1 and fallback in files:
                xml = fallback
                warnings.append(f"{run_id}/{source_id}/step{step:03d}：采用前一步 done XML")
            else:
                reason = "缺少输入 XML 及可用的前一步 done XML"
        if reason:
            skipped.append({"collection_run_id": run_id, "trajectory_id": source_id,
                            "step": step, "reason": reason})
        else:
            response_path = contained_path(raw_root, "runs", run_id, response["path"])
            try:
                action, summary = extract_response_fields(response_path)
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                action, summary = "", ""
                warnings.append(f"{run_id}/{source_id}/step{step:03d}：{exc}")
            identity = [snapshot["batch_id"], run_id, trajectory["task_id"], trajectory["relative_dir"]]
            trajectory_id = "tr_" + hashlib.sha256(json.dumps(
                identity, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()
            rows.append({"文件夹名": source_id,
                         "image": (Path("runs") / run_id / image).as_posix(),
                         "xml": (Path("runs") / run_id / xml).as_posix(),
                         "action": action, "summary": summary,
                         "task_id": trajectory["task_id"], "trajectory_id": trajectory_id,
                         "source_trajectory_id": source_id, "collection_run_id": run_id,
                         "collected_at": trajectory.get("collected_at"),
                         "collection_case_id": trajectory["collection_case_id"],
                         "source_result_id": trajectory.get("source_result_id"), "step": step,
                         **({"source_row_id": trajectory["source_row_id"], "source_kind": "manual_collection"}
                            if trajectory.get("source_kind") == "manual_collection" else {})})
        progress({**event, "completed_steps": index + 1})
    if not rows:
        raise PreprocessingError("所选采集结果没有可转换步骤")
    payload = {"schema_version": 1, "batch_id": snapshot["batch_id"],
               "raw_root": snapshot["raw_root"], "columns": {SHEET: COLUMNS},
               "sheets": {SHEET: rows}}
    metadata = {"response_count": len(work), "row_count": len(rows),
                "trajectory_count": len({row["trajectory_id"] for row in rows}),
                "task_count": len({row["task_id"] for row in rows}),
                "warnings": warnings, "skipped_steps": skipped}
    return payload, metadata


def annotate_input(json_path: Path, payload: dict, output_path: Path, *, snapshot: dict,
                   config: dict, cache_path: Path, env_file: Path,
                   progress: Callable[[dict], None], reviewer_factory=None, data_root: Path | None = None) -> tuple[dict, dict]:
    from .bounding_box.qwen_reviewer import QwenBoxReviewer, qwen_settings
    from .trajectories_preprocessing import annotate_trajectory_workbook

    if reviewer_factory is None:
        values = read_env_file(env_file)
        if not values.get("YUNAI_API_KEY") or not config.get("model") or not config.get("base_url"):
            raise PreprocessingError("标框模型尚未配置；初始转换结果已保存")
        if values.get("MODEL_NAME") != config["model"] or values.get("MODEL_URL") != config["base_url"]:
            raise PreprocessingError("排队后模型配置已变化，请重新开始预处理")
        settings = qwen_settings(api_key=values["YUNAI_API_KEY"], base_url=config["base_url"])
        reviewer = QwenBoxReviewer(model=config["model"], cache_path=cache_path, client_settings=settings)
    else:
        reviewer = reviewer_factory(config=config, cache_path=cache_path)
    result = copy.deepcopy(payload)
    rows = result["sheets"][SHEET]
    if "actions_box" not in result["columns"][SHEET]:
        result["columns"][SHEET].append("actions_box")

    def row_progress(event):
        row = rows[event["excel_row"] - 2]
        if event["phase"] == "done":
            row["actions_box"] = event["actions_box"]
        progress({"stage": "annotating", "completed_steps": event["completed_steps"],
                  "total_steps": event["total_steps"], "current_task": row["task_id"],
                  "current_trajectory": row.get("source_trajectory_id", row["文件夹名"]),
                  "current_step": row.get("step")})

    try:
        counts = annotate_trajectory_workbook(json_path, output_path, reviewer=reviewer,
            trajectory_root=rebase_data_path(snapshot["raw_root"], data_root) if data_root is not None else Path(snapshot["raw_root"]), allow_excel_import=False,
            max_review_rounds=config["max_review_rounds"], progress=row_progress)
    finally:
        if reviewer_factory is None:
            reviewer.client.close()
    return result, counts
