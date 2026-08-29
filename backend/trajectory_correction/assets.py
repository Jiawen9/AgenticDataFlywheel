"""Allow-listed project assets and safe image resolution."""

from __future__ import annotations

from pathlib import Path, PurePosixPath

from .constants import (
    FIXED_ANNOTATED_XLSX,
    FIXED_SOURCE_ID,
    FIXED_TRAJECTORY_ROOT,
    IMAGE_SUFFIXES,
)


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
        if not FIXED_ANNOTATED_XLSX.is_file():
            raise FileNotFoundError("项目内置 annotated_trajectories.xlsx 不存在")
        return FIXED_ANNOTATED_XLSX, FIXED_TRAJECTORY_ROOT
    raise ValueError("当前修正流程只允许使用项目内置标注表和质检 Top-1 结果")


def fixed_source() -> dict[str, object]:
    """Describe the only source allowed for a quality-driven session."""
    if not FIXED_ANNOTATED_XLSX.is_file():
        raise FileNotFoundError("项目内置 annotated_trajectories.xlsx 不存在")
    return {
        "source_id": FIXED_SOURCE_ID,
        "name": "项目内置 annotated_trajectories.xlsx",
        "kind": "annotated_workbook",
        "relative_path": "backend_workspace/annotated_trajectories.xlsx",
        "size_bytes": FIXED_ANNOTATED_XLSX.stat().st_size,
        "package_root": "backend_workspace/rollout_trajectories",
    }


def resolve_asset(package_root: Path, image_value: str) -> Path:
    """Resolve an image path without allowing traversal outside a package."""
    normalized = image_value.replace("\\", "/").strip("/")
    if not normalized:
        raise FileNotFoundError("图片路径为空")
    candidates = [
        _safe_path(normalized, package_root),
        _safe_path(Path(normalized).name, package_root),
    ]
    for candidate in candidates:
        if candidate.suffix.lower() in IMAGE_SUFFIXES and candidate.is_file():
            return candidate
    # Keep the basename fallback inside the allow-listed package root for
    # workbook paths that include an extra top-level directory.
    image_name = Path(normalized).name
    for candidate in package_root.rglob(image_name):
        if candidate.is_file() and candidate.suffix.lower() in IMAGE_SUFFIXES:
            return candidate.resolve()
    if Path(normalized).suffix.lower() not in IMAGE_SUFFIXES:
        raise ValueError("仅允许访问截图资源")
    raise FileNotFoundError(image_value)
