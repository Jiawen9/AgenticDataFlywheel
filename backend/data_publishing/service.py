"""Persistent dataset release registry built from completed correction drafts."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path, PurePosixPath
import secrets
import threading
from typing import Any, Callable, Optional

from ..trajectory_correction.constants import CORRECTION_EXPORTS_DIR, FIXED_TRAJECTORY_ROOT
from ..trajectory_correction.draft_store import list_sessions, load_session, save_session, utc_now
from .constants import PROJECT_ROOT, RELEASES_FILE, ensure_release_dirs


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

    Session identifiers are accepted transiently when a release is created,
    but are deliberately never persisted in the release registry.
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
    ) -> None:
        self.releases_file = releases_file
        self.project_root = project_root.resolve()
        self.trajectory_root = trajectory_root.resolve()
        self.correction_exports_dir = correction_exports_dir.resolve()
        self.session_loader = session_loader
        self.session_lister = session_lister
        self.session_saver = session_saver
        self._lock = threading.RLock()
        self.releases_file.parent.mkdir(parents=True, exist_ok=True)

    def _empty(self) -> dict[str, Any]:
        return {"schema_version": 1, "releases": []}

    def _read(self) -> dict[str, Any]:
        if not self.releases_file.is_file():
            return self._empty()
        try:
            payload = json.loads(self.releases_file.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError(f"发布记录无法读取：{exc}") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("releases"), list):
            raise ValueError("发布记录格式无效")
        return payload

    def _write(self, payload: dict[str, Any]) -> None:
        self.releases_file.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.releases_file.with_name(
            f".{self.releases_file.name}.{secrets.token_hex(6)}.tmp"
        )
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temporary.replace(self.releases_file)

    def project_path(self, path: Path) -> str:
        resolved = path.resolve()
        try:
            relative = resolved.relative_to(self.project_root)
        except ValueError as exc:
            raise ValueError("发布文件必须位于项目目录内") from exc
        return PurePosixPath(self.project_root.name, *relative.parts).as_posix()

    def resolve_project_path(self, value: str) -> Path:
        normalized = str(value or "").replace("\\", "/").strip("/")
        parts = PurePosixPath(normalized).parts
        if not parts or parts[0] != self.project_root.name or ".." in parts:
            raise ValueError("发布文件路径无效")
        candidate = self.project_root.joinpath(*parts[1:]).resolve()
        try:
            candidate.relative_to(self.project_root)
        except ValueError as exc:
            raise ValueError("发布文件路径越出项目目录") from exc
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
            if not isinstance(session, dict) or session.get("published"):
                continue
            ready = True
            reason = ""
            latest: dict[str, Any] = {}
            path: Optional[Path] = None
            try:
                latest, path = self._latest_full_export(session, require_file=True)
            except (OSError, ValueError) as exc:
                ready = False
                reason = str(exc)
            sheets = latest.get("sheets") if isinstance(latest.get("sheets"), dict) else {}
            rows = sum(int(value or 0) for value in sheets.values()) if sheets else 0
            stats = self._selection_stats(session, rows)
            result.append(
                {
                    "session_id": str(session.get("session_id", "")),
                    "tree_run_id": str(session.get("tree_run_id", "")),
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

        with self._lock:
            sessions: list[dict[str, Any]] = []
            excel_paths: list[dict[str, Any]] = []
            totals = {"task_count": 0, "trajectory_count": 0, "step_count": 0}
            for session_id in unique_ids:
                session = self.session_loader(session_id)
                if session is None:
                    raise FileNotFoundError(f"纠偏会话不存在：{session_id}")
                if session.get("published"):
                    raise ValueError("所选纠偏会话已经发布")
                latest, path = self._latest_full_export(session, require_file=True)
                sheets = latest.get("sheets") if isinstance(latest.get("sheets"), dict) else {}
                rows = sum(int(value or 0) for value in sheets.values()) if sheets else 0
                stats = self._selection_stats(session, rows)
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

            release_id = f"rel_{secrets.token_hex(8)}"
            created_at = utc_now()
            release: dict[str, Any] = {
                "release_id": release_id,
                "name": display_name,
                "created_at": created_at,
                "excel_paths": excel_paths,
                "trajectory_paths": [self.project_path(self.trajectory_root)],
                "source_count": len(sessions),
                **totals,
                "upload_status": "not_uploaded",
                "upload_job_id": None,
                "upload_error": None,
                "s3_uri": None,
                "uploaded_at": None,
                "uploaded_files": 0,
                "uploaded_bytes": 0,
            }
            registry = self._read()
            registry["releases"].append(release)
            self._write(registry)

            originals = [deepcopy(session) for session in sessions]
            try:
                for session in sessions:
                    session["published"] = True
                    session["published_at"] = created_at
                    session["published_release_id"] = release_id
                    self.session_saver(session)
            except Exception:
                rollback = self._read()
                rollback["releases"] = [
                    item for item in rollback["releases"] if item.get("release_id") != release_id
                ]
                self._write(rollback)
                for original in originals:
                    try:
                        self.session_saver(original)
                    except Exception:
                        pass
                raise
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
            release.update(changes)
            self._write(registry)
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
        return path, str(item.get("filename") or path.name)


def default_registry() -> DatasetReleaseRegistry:
    ensure_release_dirs()
    return DatasetReleaseRegistry()
