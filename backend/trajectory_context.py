"""Resolve one immutable annotation version and its registered raw input root."""
from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .data_store import ArtifactStore, DATA_ROOT, rebase_data_path

_LOCKS_GUARD = threading.Lock()
_LOCKS: dict[tuple[str, str], Any] = {}
IDENTITY_FIELDS = ("task_id", "trajectory_id", "source_trajectory_id", "collection_run_id", "collected_at")


class AnnotationVersionConflict(ValueError):
    """A caller edited a version which is no longer current."""


def annotation_batch_lock(batch_id: str, root: Path | None = None):
    key = (str(Path(root or DATA_ROOT).resolve()), batch_id)
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(key, threading.RLock())


def row_task_id(row: dict[str, Any]) -> str:
    explicit = str(row.get("task_id") or "").strip()
    if explicit:
        return explicit
    image = str(row.get("image") or "").replace("\\", "/").strip("/")
    if image.startswith("runs/") or row.get("collection_run_id"):
        raise ValueError("采集轨迹 JSON 缺少 task_id")
    return image.split("/", 1)[0] if image else ""


def row_trajectory_id(row: dict[str, Any]) -> str:
    explicit = str(row.get("trajectory_id") or "").strip()
    if explicit:
        return explicit
    if row.get("collection_run_id") or str(row.get("image") or "").replace("\\", "/").startswith("runs/"):
        raise ValueError("采集轨迹 JSON 缺少稳定 trajectory_id")
    return str(row.get("文件夹名") or "").strip()


def row_identity_metadata(row: dict[str, Any]) -> dict[str, Any]:
    return {key: row[key] for key in IDENTITY_FIELDS if key in row}


@dataclass(frozen=True)
class TrajectoryBatchContext:
    batch_id: str
    annotation_ref: dict[str, Any]
    json_path: Path
    raw_root: Path
    payload: dict[str, Any]
    task_goals: dict[str, str]
    data_root: Path | None = None
    input_snapshot_refs: tuple[dict[str, str], ...] = ()

    @property
    def annotation_version(self) -> str:
        return self.annotation_ref["version"]


def resolve_batch_context(batch_id: str, annotation_version: str | None = None,
                          root: Path | None = None) -> TrajectoryBatchContext:
    """Read registered JSON only; never import Excel or select another batch.

    Missing batch/version/JSON raises FileNotFoundError. Invalid JSON, source
    lineage, paths, and checksums raise ValueError. Historical new-store rows
    without identity fields retain their existing task/trajectory identifiers.
    """
    data_root = Path(root or DATA_ROOT).resolve()
    store = ArtifactStore(data_root)
    if annotation_version is None:
        versions = store.list(batch_id, "02_annotation")
        if not versions:
            raise FileNotFoundError("该批次尚未完成轨迹预处理")
        manifest = max(versions, key=lambda item: (item["created_at"], item["version"]))
    else:
        manifest = store.get(batch_id, "02_annotation", annotation_version)
        if manifest is None:
            raise FileNotFoundError("该批次的标框版本不存在")
    path = store.resolve_file(manifest, "result.json")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("sheets"), dict):
        raise ValueError("标框 JSON 快照格式无效")
    roots: set[Path] = set()
    goals: dict[str, str] = {}
    input_refs: dict[tuple[str, str], dict[str, str]] = {}
    visited: set[tuple[str, str, str]] = set()

    def add_root(value: str) -> None:
        target = rebase_data_path(value, data_root)
        if not target.is_relative_to((data_root / "raw").resolve()):
            raise ValueError("轨迹来源目录必须在当前 data/raw 下")
        roots.add(target)

    def visit(artifact: dict[str, Any]) -> None:
        key = (artifact["batch_id"], artifact["stage"], artifact["version"])
        if key in visited:
            return
        if key[0] != batch_id:
            raise ValueError("轨迹来源引用跨越批次")
        visited.add(key)
        input_ref = artifact.get("metadata", {}).get("input_snapshot_ref")
        if input_ref is not None:
            if not isinstance(input_ref, dict) or not isinstance(input_ref.get("path"), str) or not isinstance(input_ref.get("sha256"), str):
                raise ValueError("预处理输入快照引用格式无效")
            reference_path = Path(input_ref["path"])
            if reference_path.is_absolute() or not (data_root / reference_path).resolve().is_relative_to(data_root):
                raise ValueError("预处理输入快照路径超出数据目录")
            if len(input_ref["sha256"]) != 64 or any(value not in "0123456789abcdef" for value in input_ref["sha256"].lower()):
                raise ValueError("预处理输入快照校验值无效")
            input_refs[(input_ref["path"], input_ref["sha256"])] = dict(input_ref)
        if artifact.get("metadata", {}).get("raw_root"):
            add_root(artifact["metadata"]["raw_root"])
        if artifact["stage"] == "00_collection":
            value = json.loads(store.resolve_file(artifact, "result.json").read_text(encoding="utf-8"))
            for task in value.get("snapshot", {}).get("tasks", []):
                for identifier in (task.get("task_id"), task.get("collection_case_id")):
                    if identifier:
                        goals[str(identifier)] = str(task.get("task") or identifier)
        for reference in artifact.get("source_refs", []):
            if all(reference.get(key) for key in ("batch_id", "stage", "version")):
                ancestor = store.get(reference["batch_id"], reference["stage"], reference["version"])
                if ancestor is None:
                    raise FileNotFoundError("轨迹来源产物版本不存在")
                visit(ancestor)
            elif reference.get("kind") == "raw_trajectories" and reference.get("path"):
                add_root(reference["path"])
    visit(manifest)
    if not roots:
        raise ValueError("该标框版本没有登记原始轨迹根目录")
    if len(roots) != 1:
        raise ValueError("同一标框版本引用了不同的原始轨迹根目录")
    raw_root = next(iter(roots))
    if not raw_root.is_dir():
        raise FileNotFoundError("已登记的原始轨迹根目录不存在")
    seen = set()
    for rows in payload["sheets"].values():
        for row in rows:
            task_id, trajectory_id = row_task_id(row), row_trajectory_id(row)
            if not task_id or not trajectory_id:
                raise ValueError("标框 JSON 缺少任务或轨迹编号")
            key = (trajectory_id, str(row.get("image") or ""))
            if key in seen:
                raise ValueError("标框 JSON 包含重复轨迹步骤")
            seen.add(key)
            image = str(row.get("image") or "").replace("\\", "/")
            candidate = (raw_root / image).resolve()
            if Path(image).is_absolute() or not candidate.is_relative_to(raw_root):
                raise ValueError("轨迹图片路径超出批次原始目录")
    return TrajectoryBatchContext(batch_id, manifest, path, raw_root, payload, goals, data_root,
                                  tuple(input_refs.values()))



def validate_batch_sources(context: TrajectoryBatchContext, root: Path | None = None) -> None:
    """Validate frozen raw bytes at model-work boundaries, not ordinary GETs.

    Already registered historical artifacts without an input snapshot reference
    keep their original read behavior; no validation evidence is manufactured.
    """
    from .preprocessing_service import verify_input
    from .stage_artifacts import fingerprint
    data_root = Path(root or context.data_root or DATA_ROOT).resolve()
    for reference in context.input_snapshot_refs:
        path = (data_root / reference["path"]).resolve()
        if not path.is_relative_to(data_root):
            raise ValueError("预处理输入快照路径超出数据目录")
        if not path.is_file():
            raise FileNotFoundError("已登记的预处理输入 JSON 不存在")
        if fingerprint(path) != reference["sha256"]:
            raise ValueError("已登记的预处理输入 JSON 校验失败")
        snapshot = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(snapshot, dict) or snapshot.get("batch_id") != context.batch_id:
            raise ValueError("预处理输入快照批次不匹配")
        if rebase_data_path(snapshot.get("raw_root", ""), data_root) != context.raw_root:
            raise ValueError("预处理输入快照原始目录不匹配")
        if not isinstance(snapshot.get("trajectories"), list):
            raise ValueError("预处理输入快照缺少轨迹清单")
        verify_input(snapshot, data_root)
