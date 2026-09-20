"""Persistent dataset release registry built from completed correction drafts."""

from __future__ import annotations

from copy import deepcopy
from contextlib import ExitStack
import hashlib
import json
from pathlib import Path, PurePosixPath
import secrets
import threading
import shutil
from typing import Any, Callable, Optional

from ..trajectory_correction.constants import CORRECTION_EXPORTS_DIR, FIXED_TRAJECTORY_ROOT
from ..trajectory_correction.draft_store import list_sessions, load_session, save_session, storage_root, utc_now
from .constants import PROJECT_ROOT, RELEASES_FILE, ensure_release_dirs
from ..data_store import RecordStore, ArtifactStore, DATA_ROOT, rebase_data_path
from ..data_store.release_provenance import freeze_release_provenance
from ..trajectory_correction.session_state import session_fingerprint
from ..batch_lifecycle import (is_batch_active, session_batch_id, ensure_publishable, published_entry,
                               BatchNotReadyError, BatchPublishedError, ensure_batch_active)


SessionLoader = Callable[[str], Optional[dict[str, Any]]]
SessionLister = Callable[[], list[dict[str, Any]]]
SessionSaver = Callable[[dict[str, Any]], dict[str, Any]]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class DatasetReleaseRegistry:
    """Atomic local registry for immutable publication records.

    New releases own immutable file copies and explicit internal lineage.
    No filesystem registry is imported into current state.
    """

    def __init__(
        self,
        *,
        releases_file: Path = RELEASES_FILE,
        project_root: Path = PROJECT_ROOT,
        trajectory_root: Path = FIXED_TRAJECTORY_ROOT,
        correction_exports_dir: Path = CORRECTION_EXPORTS_DIR,
        session_loader: SessionLoader = load_session,
        session_lister: SessionLister = list_sessions,
        session_saver: SessionSaver = save_session,
        data_root: Path | None = None,
    ) -> None:
        self.releases_file = releases_file
        self.project_root = project_root.resolve()
        self.data_root = Path(data_root or (DATA_ROOT if project_root == PROJECT_ROOT else project_root / "backend_workspace")).resolve()
        self._records = RecordStore(self.data_root)
        self.trajectory_root = trajectory_root.resolve()
        self.correction_exports_dir = correction_exports_dir.resolve()
        self.session_loader = session_loader
        self.session_lister = session_lister
        self.session_saver = session_saver
        self._lock = threading.RLock()
        self.releases_file.parent.mkdir(parents=True, exist_ok=True)

    def _read(self) -> dict[str, Any]:
        return {"schema_version": 1, "releases": self._records.list("dataset_releases")}

    def project_path(self, path: Path) -> str:
        resolved = path.resolve()
        if not resolved.is_relative_to(self.data_root):
            raise ValueError("发布文件必须位于当前数据目录内")
        try:
            relative = resolved.relative_to(self.project_root)
        except ValueError as exc:
            if resolved.is_relative_to(self.data_root):
                return "@data/" + resolved.relative_to(self.data_root).as_posix()
            raise ValueError("发布文件必须位于当前数据目录内") from exc
        return PurePosixPath(self.project_root.name, *relative.parts).as_posix()

    def resolve_project_path(self, value: str) -> Path:
        normalized = str(value or "").replace("\\", "/").strip("/")
        parts = PurePosixPath(normalized).parts
        if parts and parts[0] == "@data":
            if ".." in parts:
                raise ValueError("发布文件路径越出数据目录")
            return rebase_data_path(Path(*parts[1:]), self.data_root)
        if not parts or parts[0] != self.project_root.name or ".." in parts:
            raise ValueError("发布文件路径无效")
        try:
            candidate = rebase_data_path(self.project_root.joinpath(*parts[1:]), self.data_root)
        except ValueError as exc:
            raise ValueError("发布文件路径越出当前数据目录") from exc
        return candidate

    def _latest_full_export(
        self, session: dict[str, Any], *, require_file: bool
    ) -> tuple[dict[str, Any], Path]:
        session_id = str(session.get("session_id", "")).strip()
        exports = [
            item
            for item in session.get("exports", [])
            if isinstance(item, dict) and item.get("kind") == "full_dataset"
        ]
        if not exports:
            raise ValueError("尚未导出完整数据集 Excel")
        latest = max(exports, key=lambda item: str(item.get("created_at", "")))
        current_fingerprint = session_fingerprint(session)
        if latest.get("content_fingerprint") and latest["content_fingerprint"] != current_fingerprint:
            raise ValueError("修正内容或来源已更新，请重新导出当前完整数据集")
        if session.get("published_content_fingerprint") == current_fingerprint or (
            session.get("published") and not session.get("published_content_fingerprint")):
            raise ValueError("当前批次内容已经发布，处理已结束")
        filename = str(latest.get("filename", "")).strip()
        if not filename or Path(filename).name != filename:
            raise ValueError("完整数据集 Excel 文件名无效")
        path = (self.correction_exports_dir / session_id / filename).resolve()
        try:
            path.relative_to((self.correction_exports_dir / session_id).resolve())
        except ValueError as exc:
            raise ValueError("完整数据集 Excel 路径无效") from exc
        if require_file and not path.is_file():
            raise ValueError(f"完整数据集 Excel 不存在：{filename}")
        if require_file and (not latest.get("sha256") or _sha256(path) != latest["sha256"]):
            raise ValueError("完整数据集 Excel 与导出记录的 SHA256 不一致，请重新导出")
        return latest, path

    @staticmethod
    def _selection_stats(session: dict[str, Any], rows: int) -> dict[str, int]:
        selection = session.get("selection") if isinstance(session.get("selection"), dict) else {}
        tasks = selection.get("tasks") if isinstance(selection.get("tasks"), list) else []
        trajectories = 0
        for task in tasks:
            if not isinstance(task, dict):
                continue
            try:
                trajectories += int(task.get("trajectory_count") or 0)
            except (TypeError, ValueError):
                continue
        return {
            "task_count": len([item for item in tasks if isinstance(item, dict)]),
            "trajectory_count": trajectories,
            "step_count": rows,
        }

    def candidates(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for session in self.session_lister():
            if not isinstance(session, dict) or session.get("archived"):
                continue
            batch_id = session_batch_id(session, self.data_root)
            if not is_batch_active(batch_id, self.data_root):
                continue
            ready = True
            reason = ""
            latest: dict[str, Any] = {}
            path: Optional[Path] = None
            try:
                latest, path = self._latest_full_export(session, require_file=True)
                ensure_publishable(batch_id, [session], self.data_root)
            except BatchPublishedError:
                continue
            except (OSError, ValueError, BatchNotReadyError) as exc:
                ready = False
                reason = str(exc)
            sheets = latest.get("sheets") if isinstance(latest.get("sheets"), dict) else {}
            rows = sum(int(value or 0) for value in sheets.values()) if sheets else 0
            stats = self._selection_stats({**session, "selection": latest.get("selection", session.get("selection"))}, rows)
            result.append(
                {
                    "session_id": str(session.get("session_id", "")),
                    "tree_run_id": str(session.get("tree_run_id", "")),
                    "batch_id": batch_id,
                    "created_at": session.get("created_at"),
                    "updated_at": session.get("updated_at"),
                    "ready": ready,
                    "reason": reason,
                    "latest_excel": {
                        "filename": str(latest.get("filename", "")),
                        "created_at": latest.get("created_at"),
                        "rows": rows,
                        "path": self.project_path(path) if path is not None and path.is_file() else "",
                    },
                    **stats,
                }
            )
        return sorted(result, key=lambda item: str(item.get("updated_at", "")), reverse=True)

    def _with_availability(self, release: dict[str, Any]) -> dict[str, Any]:
        value = deepcopy(release)
        value.setdefault("source_kind", "workflow")
        if "batch_ids" not in value:
            value["batch_ids"] = sorted({str((source.get("artifact") or {}).get("batch_id")
                or session_batch_id(self.session_loader(str(source.get("id"))) or {}, self.data_root))
                for source in value.get("source_refs", [])
                if source.get("kind") in (None, "correction_session")} - {""})
        available = True
        for item in value.get("excel_paths", []):
            try:
                item["available"] = self.resolve_project_path(str(item.get("path", ""))).is_file()
            except ValueError:
                item["available"] = False
            available = available and bool(item["available"])
        for item in value.get("trajectory_paths", []):
            try:
                available = available and self.resolve_project_path(str(item)).is_dir()
            except ValueError:
                available = False
        value["local_available"] = available
        return value

    def list_releases(self) -> list[dict[str, Any]]:
        with self._lock:
            releases = self._read()["releases"]
            return [
                self._with_availability(item)
                for item in sorted(
                    (item for item in releases if isinstance(item, dict)),
                    key=lambda item: str(item.get("created_at", "")),
                    reverse=True,
                )
            ]

    def get(self, release_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            release = next(
                (
                    item
                    for item in self._read()["releases"]
                    if isinstance(item, dict) and item.get("release_id") == release_id
                ),
                None,
            )
            return self._with_availability(release) if release is not None else None

    def create(self, name: str, session_ids: list[str]) -> dict[str, Any]:
        display_name = str(name or "").strip()
        if not display_name:
            raise ValueError("数据集名称不能为空")
        if len(display_name) > 120:
            raise ValueError("数据集名称不能超过 120 个字符")
        unique_ids = list(dict.fromkeys(str(item or "").strip() for item in session_ids if str(item or "").strip()))
        if not unique_ids:
            raise ValueError("至少选择一个纠偏会话")
        if not self.trajectory_root.is_dir():
            raise ValueError("原始轨迹根目录不存在，无法创建数据集发布")

        with self._lock, ExitStack() as locks:
            initial = [self.session_loader(key) for key in unique_ids]
            if any(item is None for item in initial):
                raise FileNotFoundError("纠偏会话不存在")
            expected_batches = {item["session_id"]: session_batch_id(item, self.data_root) for item in initial}
            for batch_id in sorted(set(expected_batches.values())):
                locks.enter_context(ArtifactStore(self.data_root).batch_lock(batch_id))
                ensure_batch_active(batch_id, self.data_root)
            sessions: list[dict[str, Any]] = []
            excel_paths: list[dict[str, Any]] = []
            sources: list[dict[str, Any]] = []
            totals = {"task_count": 0, "trajectory_count": 0, "step_count": 0}
            for session_id in unique_ids:
                session = self.session_loader(session_id)
                if session is None:
                    raise FileNotFoundError(f"纠偏会话不存在：{session_id}")
                if session.get("archived"):
                    raise ValueError("所选纠偏会话已归档")
                batch_id = session_batch_id(session, self.data_root)
                if expected_batches[session_id] != batch_id:
                    raise ValueError("会话来源已变化，请刷新后重试")
                ensure_publishable(batch_id, [session], self.data_root)
                latest, path = self._latest_full_export(session, require_file=True)
                sheets = latest.get("sheets") if isinstance(latest.get("sheets"), dict) else {}
                rows = sum(int(value or 0) for value in sheets.values()) if sheets else 0
                stats = self._selection_stats({**session, "selection": latest.get("selection", session.get("selection"))}, rows)
                for key in totals:
                    totals[key] += stats[key]
                excel_paths.append(
                    {
                        "path": self.project_path(path),
                        "filename": path.name,
                        "sha256": _sha256(path),
                        "rows": rows,
                        "created_at": latest.get("created_at"),
                    }
                )
                sessions.append(session)
                sources.append({"kind": "correction_session", "id": session_id,
                                "revision": session.get("storage_revision"), "tree_run_id": session.get("tree_run_id"),
                                "export_id": latest.get("export_id"), "artifact": latest.get("artifact")})

            batch_ids = [session_batch_id(item, self.data_root) for item in sessions]
            if len(set(batch_ids)) != len(batch_ids):
                raise ValueError("同一业务批次只能选择一次")
            release_id = f"rel_{secrets.token_hex(8)}"
            created_at = utc_now()
            release: dict[str, Any] = {
                "release_id": release_id,
                "name": display_name,
                "created_at": created_at,
                "excel_paths": excel_paths,
                "trajectory_paths": [self.project_path(self.trajectory_root)],
                "batch_ids": batch_ids,
                "source_count": len(sessions),
                "source_refs": sources,
                **totals,
                "upload_status": "not_uploaded",
                "upload_job_id": None,
                "upload_error": None,
                "s3_uri": None,
                "uploaded_at": None,
                "uploaded_files": 0,
                "uploaded_bytes": 0,
            }
            # Finish all immutable files before exposing the release in SQLite.
            release_root = self.data_root / "releases"
            release_root.mkdir(parents=True, exist_ok=True)
            final = release_root / release_id
            staging = release_root / f".{release_id}.tmp"
            staging.mkdir()
            originals = [deepcopy(session) for session in sessions]
            try:
                for index, item in enumerate(excel_paths, start=1):
                    source = self.resolve_project_path(item["path"])
                    relative = Path(f"{index:03d}") / item["filename"]
                    target = staging / relative
                    target.parent.mkdir()
                    shutil.copyfile(source, target)
                    if _sha256(target) != item["sha256"]:
                        raise ValueError("发布期间源表内容发生变化，请重新导出后重试")
                    item["source_path"] = item["path"]
                    item["path"] = self.project_path(final / relative)
                    item["data_path"] = (Path("releases") / release_id / relative).as_posix()
                release = freeze_release_provenance(self.data_root, release, staging)
                (staging / "manifest.json").write_text(json.dumps(release, ensure_ascii=False, indent=2), encoding="utf-8")
                staging.rename(final)
                for session in sessions:
                    session["published"] = True
                    session["published_content_fingerprint"] = session_fingerprint(session)
                    session["published_at"] = created_at
                    session["published_release_id"] = release_id
                entries = [{"namespace": "dataset_releases", "key": release_id, "payload": release, "expected_revision": 0}]
                entries.extend({"namespace": "correction_sessions", "key": item["session_id"], "payload": item,
                                "expected_revision": item.get("storage_revision", 0)} for item in sessions)
                entries.extend(published_entry(batch_id, release_id, created_at, self.data_root) for batch_id in batch_ids)
                # Injected mirrors are retained for embedded consumers; SQLite owns the atomic state.
                if self.session_saver is not save_session:
                    for session in sessions:
                        self.session_saver(session)
                release = self._records.put_many(entries)[0]
            except Exception:
                if self.session_saver is not save_session:
                    for original in originals:
                        try:
                            self.session_saver(original)
                        except Exception:
                            pass
                if final.exists():
                    shutil.rmtree(final)
                raise
            finally:
                if staging.exists():
                    shutil.rmtree(staging)
            return self._with_availability(release)

    def update(self, release_id: str, changes: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            registry = self._read()
            release = next(
                (
                    item
                    for item in registry["releases"]
                    if isinstance(item, dict) and item.get("release_id") == release_id
                ),
                None,
            )
            if release is None:
                raise FileNotFoundError("数据集发布记录不存在")
            release = self._records.update("dataset_releases", release_id,
                                           lambda current: current.update(changes), default=release)
            return self._with_availability(release)

    def excel_file(self, release_id: str, index: int) -> tuple[Path, str]:
        release = self.get(release_id)
        if release is None:
            raise FileNotFoundError("数据集发布记录不存在")
        excel_paths = release.get("excel_paths", [])
        if index < 0 or index >= len(excel_paths):
            raise FileNotFoundError("数据集 Excel 不存在")
        item = excel_paths[index]
        path = self.resolve_project_path(str(item.get("path", "")))
        if not path.is_file() or path.suffix.lower() not in {".xlsx", ".xlsm"}:
            raise FileNotFoundError("数据集 Excel 文件不存在")
        if not item.get("sha256") or _sha256(path) != item["sha256"]:
            raise ValueError("数据集 Excel 与发布版本的 SHA256 不一致")
        return path, str(item.get("filename") or path.name)


def default_registry() -> DatasetReleaseRegistry:
    ensure_release_dirs()
    return DatasetReleaseRegistry()
