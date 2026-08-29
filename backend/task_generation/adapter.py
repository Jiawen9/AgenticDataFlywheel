"""Safe adapter around the local task-generation draft scripts."""

from __future__ import annotations

import importlib
import sys
import threading
from pathlib import Path
from typing import Any

import pandas as pd

from ..model_config import DEFAULT_ENV_FILE, load_env_values


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DRAFT_DIR = PROJECT_ROOT / "任务生成草稿"
DEFAULT_KNOWLEDGE_BASE_DIR = PROJECT_ROOT / "backend_workspace" / "task_generation" / "KnowledgeBase"
KNOWLEDGE_BASE_FILES = (
    "VLA场景树.xlsx",
    "APP操控先验知识库.xlsx",
    "APP资源先验知识库.xlsx",
)
REQUIRED_VLA_COLUMNS = {
    "scene",
    "capability",
    "sub_capability",
    "target_app",
    "use_resource_prior",
    "reference_example",
}
REQUIRED_CONTROL_COLUMNS = {
    "scene",
    "capability",
    "sub_capability",
    "target_app",
    "sub_capability_desc",
}
MODULE_LOCK = threading.RLock()


def knowledge_base_dir() -> Path:
    try:
        values = load_env_values(DEFAULT_ENV_FILE)
    except (FileNotFoundError, ValueError):
        values = {}
    configured = values.get("TASK_GENERATION_KNOWLEDGE_BASE_DIR", "").strip()
    root = Path(configured).expanduser() if configured else DEFAULT_KNOWLEDGE_BASE_DIR
    return root if root.is_absolute() else (PROJECT_ROOT / root).resolve()


def _load_draft_modules() -> tuple[Any, Any]:
    if not DRAFT_DIR.is_dir():
        raise FileNotFoundError(f"任务生成草稿目录不存在：{DRAFT_DIR}")
    with MODULE_LOCK:
        draft_text = str(DRAFT_DIR)
        if draft_text not in sys.path:
            sys.path.insert(0, draft_text)
        try:
            task_module = importlib.import_module("task_generate")
            flywheel_module = importlib.import_module("flywheel_task")
        except (ImportError, ModuleNotFoundError) as exc:
            raise RuntimeError(f"任务生成草稿脚本无法加载：{exc}") from exc
        return task_module, flywheel_module


def read_testcase(path: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    # Keep upload validation independent of model-client imports.  This is
    # equivalent to the draft script's read_testcase implementation and lets
    # the Web layer reject malformed workbooks before loading the generators.
    frame = pd.read_excel(path, sheet_name=0)
    passed: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    for _, row in frame.iterrows():
        item = row.to_dict()
        (passed if row.get("任务结果") == "TRUE" else failed).append(item)
    return passed, failed


def match_scene_by_task(rows: list[dict[str, Any]], progress_callback=None) -> list[dict[str, Any]]:
    _, flywheel = _load_draft_modules()
    return flywheel.match_scene_by_task_new(rows, progress_callback=progress_callback)


def task_generate(**kwargs: Any) -> list[dict[str, Any]]:
    task_module, _ = _load_draft_modules()
    result = task_module.task_generate(**kwargs)
    return result if isinstance(result, list) else []


def generate_flywheel_rows(scene_match_path: Path, output_path: Path, generate_n: int = 10, progress_callback=None) -> None:
    _, flywheel = _load_draft_modules()
    try:
        flywheel.process_flywheel_export(
            str(scene_match_path),
            str(output_path),
            generate_n=generate_n,
            progress_callback=progress_callback,
        )
    except TypeError:
        # Compatibility with an older local draft before the optional
        # generate_n parameter is added.
        flywheel.process_flywheel_export(str(scene_match_path), str(output_path))


def _cell_value(value: Any) -> Any:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(value, dict):
        return {str(key): _cell_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_cell_value(item) for item in value]
    return value


def source_status() -> dict[str, Any]:
    root = knowledge_base_dir()
    files: list[dict[str, Any]] = []
    errors: list[str] = []
    frames: dict[str, pd.DataFrame] = {}
    for name in KNOWLEDGE_BASE_FILES:
        path = root / name
        info: dict[str, Any] = {
            "name": name,
            "exists": path.is_file(),
            "relative_path": f"backend_workspace/task_generation/KnowledgeBase/{name}",
            "size_bytes": path.stat().st_size if path.is_file() else 0,
            "sheets": [],
            "rows": 0,
            "columns": [],
            "error": None,
        }
        if not path.is_file():
            errors.append(f"缺少 {name}")
            files.append(info)
            continue
        try:
            workbook = pd.ExcelFile(path)
            try:
                info["sheets"] = workbook.sheet_names
                if name == "APP资源先验知识库.xlsx":
                    info["rows"] = sum(len(pd.read_excel(workbook, sheet_name=sheet)) for sheet in workbook.sheet_names)
                else:
                    frame = pd.read_excel(workbook, sheet_name=0)
                    frames[name] = frame
                    info["rows"] = len(frame)
                    info["columns"] = [str(column) for column in frame.columns]
            finally:
                workbook.close()
        except Exception as exc:
            info["error"] = str(exc)
            errors.append(f"{name}：{exc}")
        files.append(info)

    vla = frames.get("VLA场景树.xlsx")
    control = frames.get("APP操控先验知识库.xlsx")
    if vla is not None:
        missing = sorted(REQUIRED_VLA_COLUMNS.difference(map(str, vla.columns)))
        if missing:
            errors.append(f"VLA场景树.xlsx 缺少字段：{', '.join(missing)}")
    if control is not None:
        missing = sorted(REQUIRED_CONTROL_COLUMNS.difference(map(str, control.columns)))
        if missing:
            errors.append(f"APP操控先验知识库.xlsx 缺少字段：{', '.join(missing)}")

    def values(column: str) -> list[str]:
        if vla is None or column not in vla.columns:
            return []
        result: set[str] = set()
        for value in vla[column].tolist():
            if isinstance(value, (list, tuple)):
                result.update(str(item).strip() for item in value if str(item).strip())
            else:
                text = str(value).strip()
                if text and text.lower() != "nan":
                    result.add(text)
        return sorted(result, key=str.casefold)

    return {
        "ready": not errors and len(files) == len(KNOWLEDGE_BASE_FILES),
        "location": "backend_workspace/task_generation/KnowledgeBase",
        "files": files,
        "errors": errors,
        "filters": {
            "apps": values("target_app"),
            "scenes": values("scene"),
            "capabilities": values("capability"),
            "sub_capabilities": values("sub_capability"),
        },
    }
