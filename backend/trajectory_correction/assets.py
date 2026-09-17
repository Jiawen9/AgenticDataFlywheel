"""Allow-listed project assets and safe image resolution."""

from __future__ import annotations

from pathlib import Path, PurePosixPath
from ..stage_artifacts import structured_input_exists, sidecar_path
from ..data_store import rebase_data_path

from .constants import (
    FIXED_ANNOTATED_XLSX,
    FIXED_SOURCE_ID,
    FIXED_TRAJECTORY_ROOT,
    IMAGE_SUFFIXES,
    PROJECT_ROOT,
)

def annotated_source() -> Path:
    return FIXED_ANNOTATED_XLSX


def trajectory_source() -> Path:
    return FIXED_TRAJECTORY_ROOT


def registered_asset_root(value: str, data_root: Path) -> Path:
    """Resolve an explicitly frozen raw root, including project-external data."""
    data_root = Path(data_root).resolve()
    try:
        candidate = rebase_data_path(value, data_root)
    except ValueError as exc:
        raise ValueError("修正资源目录必须位于当前数据根目录的 raw 下") from exc
    if not candidate.is_relative_to(data_root) or not candidate.is_relative_to((data_root / "raw").resolve()):
        raise ValueError("修正资源目录必须位于当前数据根目录的 raw 下")
    if not candidate.is_dir():
        raise FileNotFoundError("已登记的修正原始轨迹目录不存在")
    return candidate


def _safe_path(value: str, root: Path) -> Path:
    normalized = value.replace("\\", "/").strip("/")
    if not normalized or normalized.startswith("..") or "/../" in f"/{normalized}/":
        raise ValueError("资源路径无效")
    candidate = (root / PurePosixPath(normalized)).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError as exc:
        raise ValueError("资源路径超出允许目录") from exc
    return candidate


def source_from_id(source_id: str) -> tuple[Path, Path]:
    """Return ``(workbook, asset root)`` for a correction source ID."""
    if source_id.replace("\\", "/") == FIXED_SOURCE_ID:
        if not structured_input_exists(annotated_source()):
            raise FileNotFoundError("轨迹 JSON 输入不存在")
        return annotated_source(), trajectory_source()
    raise ValueError("当前修正流程只允许使用项目内置标注表和质检 Top-1 结果")


def fixed_source() -> dict[str, object]:
    """Describe the only source allowed for a quality-driven session."""
    workbook, trajectories = source_from_id(FIXED_SOURCE_ID)
    if not structured_input_exists(workbook):
        raise FileNotFoundError("轨迹 JSON 输入不存在")
    return {
        "source_id": FIXED_SOURCE_ID,
        "name": "项目内置 annotated_trajectories.xlsx",
        "kind": "annotated_workbook",
        "relative_path": workbook.relative_to(PROJECT_ROOT).as_posix() if workbook.is_relative_to(PROJECT_ROOT) else workbook.name,
        "size_bytes": (sidecar_path(workbook) if sidecar_path(workbook).is_file() else workbook).stat().st_size,
        "package_root": trajectories.relative_to(PROJECT_ROOT).as_posix() if trajectories.is_relative_to(PROJECT_ROOT) else trajectories.name,
    }


def resolve_asset(package_root: Path, image_value: str) -> Path:
    """Resolve an image path without allowing traversal outside a package."""
    normalized = image_value.replace("\\", "/").strip("/")
    if not normalized:
        raise FileNotFoundError("图片路径为空")
    candidate = _safe_path(normalized, package_root)
    if candidate.suffix.lower() in IMAGE_SUFFIXES and candidate.is_file():
        return candidate
    if Path(normalized).suffix.lower() not in IMAGE_SUFFIXES:
        raise ValueError("仅允许访问截图资源")
    raise FileNotFoundError(image_value)
