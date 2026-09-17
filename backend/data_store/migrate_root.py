"""Offline, journalled data -> backend_workspace root migration.

check is strictly read-only. apply retains the old target and the original DB;
finalize deletes that retained target only after verification. rollback is
available before finalization and refuses to discard post-migration DB writes.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import uuid
from contextlib import contextmanager, closing
from pathlib import Path

from .artifacts import ArtifactStore
from .paths import PROJECT_ROOT, resolved_path
from .registry import RecordStore, utc_now

STATE_DIR = ".data-root-migration"
MUTABLE_PATH_FIELDS = {
    "trajectory_annotations": ("path",), "tree_jobs": ("raw_root",),
    "collection_runs": ("output_dir",), "quality_results": ("rubric_path",),
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _no_links(path: Path, *, recursive: bool = False) -> None:
    candidates = [path, *path.parents]
    if recursive and path.is_dir():
        for directory, dirs, files in os.walk(path, followlinks=False):
            candidates.extend(Path(directory) / name for name in dirs + files)
    for candidate in candidates:
        if candidate.is_symlink():
            raise ValueError(f"Migration does not allow symbolic links: {candidate}")
        if candidate.exists() and getattr(candidate.lstat(), "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 1024):
            raise ValueError(f"Migration does not allow directory junctions/reparse points: {candidate}")


def _paths(project_root: Path) -> tuple[Path, Path, Path, Path]:
    project = Path(os.path.abspath(project_root))
    _no_links(project)
    project = resolved_path(project)
    if not project.is_dir():
        raise ValueError("Project root does not exist")
    source, target, journal = project / "data", project / "backend_workspace", project / STATE_DIR
    for item in (source, target, journal):
        _no_links(item)
        if item.resolve().parent != project:
            raise ValueError("Migration targets must be immediate children of the selected project")
    return project, source, target, journal


def _inventory(root: Path) -> dict:
    _no_links(root, recursive=True)
    return {path.relative_to(root).as_posix(): {"size": path.stat().st_size, "sha256": sha256(path)}
            for path in sorted(root.rglob("*")) if path.is_file() and not path.name.startswith("~$")}


def _verify_inventory(root: Path, inventory: dict, *, database: bool = False, allow_removed: bool = False) -> None:
    _no_links(root, recursive=True)
    for name, expected in inventory.items():
        if not database and name == "system/app.sqlite":
            continue
        path = root / name
        if not path.is_file():
            if allow_removed:
                continue
            raise ValueError(f"Migration file is missing: {name}")
        if path.stat().st_size != expected["size"] or sha256(path) != expected["sha256"]:
            raise ValueError(f"Migration checksum changed: {name}")


def _read_state(journal: Path, source: Path, target: Path) -> dict | None:
    path = journal / "state.json"
    if not path.exists():
        return None
    state = json.loads(path.read_text(encoding="utf-8"))
    if state.get("source") != str(source) or state.get("target") != str(target):
        raise ValueError("Migration journal does not match the selected project")
    if state.get("staged_target") != str(journal / "legacy_backend_workspace"):
        raise ValueError("Migration staging path does not match the journal directory")
    _no_links(journal, recursive=True)
    return state


def _save_state(journal: Path, state: dict, phase: str) -> None:
    state.update(phase=phase, updated_at=utc_now())
    temporary = journal / "state.json.tmp"
    with temporary.open("w", encoding="utf-8", newline="\n") as output:
        json.dump(state, output, ensure_ascii=False, indent=2)
        output.write("\n"); output.flush(); os.fsync(output.fileno())
    temporary.replace(journal / "state.json")


@contextmanager
def _mutation_lock(journal: Path):
    # OS locks are released after a crash, so a retained audit file cannot
    # prevent resuming the journal. Do not unlink a lock another process may open.
    journal.mkdir(exist_ok=True)
    with (journal / "command.lock").open("a+b") as lock:
        if lock.seek(0, 2) == 0:
            lock.write(b"0"); lock.flush()
        lock.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise ValueError("Another migration command is running") from exc
        try:
            yield
        finally:
            lock.seek(0)
            if os.name == "nt":
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _database_records(database: Path) -> list[tuple]:
    if not database.is_file():
        raise ValueError("Source data has no system/app.sqlite")
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as connection:
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("SQLite integrity check failed")
        return [(namespace, key, json.loads(payload)) for namespace, key, payload in
                connection.execute("SELECT namespace, record_key, payload FROM records")]


def _schema_digest(database: Path) -> str:
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as connection:
        rows = list(connection.execute("SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name"))
    return hashlib.sha256(json.dumps(rows, separators=(",", ":")).encode()).hexdigest()


def _records(root: Path) -> list[tuple]:
    return _database_records(root / "system/app.sqlite")


def _records_digest(rows: list[tuple]) -> str:
    return hashlib.sha256(json.dumps(sorted(rows, key=lambda row: (row[0], row[1])),
        sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()


def _registered_files(root: Path) -> int:
    store = ArtifactStore(root)
    total = 0
    for manifest in store.list():
        for file in manifest["files"]:
            store.resolve_file(manifest, file["name"]); total += 1
    return total


def check(project_root: Path = PROJECT_ROOT) -> dict:
    _, source, target, journal = _paths(project_root)
    state = _read_state(journal, source, target)
    if state and state["phase"] not in {"rolled_back"}:
        if state["phase"] in {"applied", "finalizing", "finalized"}:
            verification = verify(project_root)
            return {"ready": True, "phase": state["phase"], "source": str(source), "target": str(target), **verification}
        return {"ready": False, "phase": state["phase"], "reason": "Interrupted migration; resume apply or rollback", "source": str(source), "target": str(target)}
    if state:
        return {"ready": False, "phase": "rolled_back", "reason": "This migration was rolled back; retain its journal for audit"}
    if not source.is_dir():
        raise ValueError(f"Source data directory does not exist: {source}")
    records = _records(source)
    blockers = []
    for namespace, key, payload in records:
        statuses = [payload.get("status"), payload.get("upload_status")]
        if isinstance(payload.get("internal_upload"), dict):
            statuses.append(payload["internal_upload"].get("status"))
        active = {"queued", "running", "dispatching", "uploading", "classifying", "generating", "annotating", "publishing"}
        if any(status in active for status in statuses if isinstance(status, str)):
            blockers.append(f"Active record: {namespace}/{key} ({next(status for status in statuses if status in active)})")
    for root in (source, target):
        if not root.exists():
            continue
        _no_links(root, recursive=True)
        blockers.extend(f"Office lock file: {path}" for path in root.rglob("~$*") if path.is_file())
        blockers.extend(f"SQLite journal is present: {path}" for pattern in ("app.sqlite-wal", "app.sqlite-shm", "app.sqlite-journal") for path in root.rglob(pattern))
    count = _registered_files(source)
    return {"ready": not blockers, "phase": "not_started", "source": str(source), "target": str(target),
            "target_exists": target.exists(), "registered_file_count": count, "blockers": blockers}


def _move_path(value: str, source: Path, target: Path) -> str:
    path = Path(value)
    if path.is_absolute() and path.resolve().is_relative_to(source):
        return str(target / path.resolve().relative_to(source))
    return value


def _expected_records(state: dict, original: list[tuple]) -> list[tuple]:
    source, target = Path(state["source"]), Path(state["target"])
    result = []
    keys = {(namespace, key) for namespace, key, _ in original}
    for namespace, key, value in original:
        payload = json.loads(json.dumps(value))
        changed = False; new_key = key
        for field in MUTABLE_PATH_FIELDS.get(namespace, ()):
            if isinstance(payload.get(field), str):
                replacement = _move_path(payload[field], source, target)
                if replacement != payload[field]:
                    payload[field] = replacement; changed = True
        if namespace == "trajectory_annotations" and changed:
            new_key = hashlib.sha256(payload["path"].encode("utf-8")).hexdigest()
            if new_key != key and (namespace, new_key) in keys:
                raise ValueError("Annotation path index conflicts at the new root")
        if namespace == "data_root_relocations" and payload.get("new_root") == str(source):
            payload["new_root"] = str(target); changed = True
        if changed: payload["storage_revision"] = payload.get("storage_revision", 0) + 1
        result.append((namespace, new_key, payload))
    result.append(("data_root_relocations", state["migration_id"],
                   {"old_root": str(source), "new_root": str(target), "status": "applied",
                    "migration_id": state["migration_id"], "created_at": state["created_at"], "storage_revision": 1}))
    return result


def _apply_records(root: Path, state: dict) -> None:
    store = RecordStore(root)
    backup = Path(state["staged_target"]).parent / "original.sqlite"
    if sha256(backup) != state["original_database_sha256"]:
        raise ValueError("Original database backup checksum failed")
    original = _database_records(backup)
    if _schema_digest(store.database_path) != state["schema_digest"]:
        raise ValueError("Database schema changed during migration; refusing to overwrite later writes")
    expected = _expected_records(state, original)
    if _records_digest(original) != state["source_records_digest"] or _records_digest(expected) != state["expected_records_digest"]:
        raise ValueError("Migration record fingerprints do not match the journal")
    if _records_digest(_records(root)) not in {state["source_records_digest"], state["expected_records_digest"]}:
        raise ValueError("Database changed during migration; refusing to overwrite later writes")
    with store._connection(write=True) as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            current = [(ns, key, json.loads(value)) for ns, key, value in
                       connection.execute("SELECT namespace,record_key,payload FROM records")]
            if _records_digest(current) == state["expected_records_digest"]:
                connection.rollback(); return
            if _records_digest(current) != state["source_records_digest"]:
                raise ValueError("Database changed during migration; refusing to overwrite later writes")
            old_values = {(ns, key): value for ns, key, value in original}
            new_keys = {(ns, key) for ns, key, _ in expected}
            for ns, key in set(old_values) - new_keys:
                connection.execute("DELETE FROM records WHERE namespace=? AND record_key=?", (ns, key))
            for ns, key, payload in expected:
                if old_values.get((ns, key)) != payload:
                    store._save(connection, ns, key, payload, payload["storage_revision"])
            connection.commit()
        except BaseException:
            connection.rollback(); raise


def apply(project_root: Path = PROJECT_ROOT) -> dict:
    _, source, target, journal = _paths(project_root)
    state = _read_state(journal, source, target)
    if state and state["phase"] in {"applied", "finalized"}:
        return {"phase": state["phase"], **verify(project_root)}
    if state and state["phase"] in {"rolled_back", "rolling_back", "finalizing"}:
        raise ValueError(f"Cannot apply a migration in phase {state['phase']}")
    if state is None:
        preflight = check(project_root)
        if not preflight["ready"]:
            raise ValueError("Migration is blocked: " + "; ".join(preflight["blockers"]))
    with _mutation_lock(journal):
        state = _read_state(journal, source, target)
        if state is None:
            state = {"schema_version": 1, "migration_id": uuid.uuid4().hex,
                     "source": str(source), "target": str(target), "staged_target": str(journal / "legacy_backend_workspace"),
                     "target_existed": target.exists(), "source_files": _inventory(source),
                     "legacy_files": _inventory(target) if target.exists() else {}, "created_at": utc_now()}
            original = _records(source)
            state["source_records_digest"] = _records_digest(original)
            state["schema_digest"] = _schema_digest(source / "system/app.sqlite")
            state["expected_records_digest"] = _records_digest(_expected_records(state, original))
            backup = journal / "original.sqlite"
            if backup.exists():
                raise ValueError("Unjournalled database backup exists; inspect it before retrying")
            shutil.copy2(source / "system/app.sqlite", backup)
            state["original_database_sha256"] = sha256(backup)
            if _records_digest(_database_records(backup)) != state["source_records_digest"]:
                raise ValueError("Source database changed during backup")
            _save_state(journal, state, "prepared")
        staged = Path(state["staged_target"])
        if source.exists():
            _verify_inventory(source, state["source_files"], database=True)
            if state["target_existed"] and not staged.exists():
                _verify_inventory(target, state["legacy_files"], database=True)
                target.rename(staged)
            _save_state(journal, state, "old_staged")
            if target.exists():
                raise ValueError("Destination unexpectedly exists; refusing to overwrite it")
            source.rename(target)
            _save_state(journal, state, "moved")
        elif not target.exists():
            raise ValueError("Both migration source and target are missing")
        _verify_inventory(target, state["source_files"])
        _apply_records(target, state)
        state["applied_database_sha256"] = sha256(target / "system/app.sqlite")
        _save_state(journal, state, "applied")
    return {"phase": "applied", **verify(project_root)}


def verify(project_root: Path = PROJECT_ROOT) -> dict:
    _, source, target, journal = _paths(project_root)
    state = _read_state(journal, source, target)
    if state is None or state["phase"] not in {"applied", "finalizing", "finalized"}:
        raise ValueError("Migration has not been applied")
    if source.exists():
        raise ValueError("Original data path reappeared after migration")
    _verify_inventory(target, state["source_files"])
    records = _records(target)
    mapping = next((payload for namespace, key, payload in records
                    if namespace == "data_root_relocations" and key == state["migration_id"]), None)
    if not mapping or mapping.get("old_root") != str(source) or mapping.get("new_root") != str(target):
        raise ValueError("Committed root mapping is missing")
    return {"verified": True, "file_count": len(state["source_files"]), "registered_file_count": _registered_files(target),
            "migration_id": state["migration_id"]}


def finalize(project_root: Path = PROJECT_ROOT) -> dict:
    _, source, target, journal = _paths(project_root)
    result = verify(project_root)
    with _mutation_lock(journal):
        state = _read_state(journal, source, target)
        if state["phase"] == "finalized":
            return {"phase": "finalized", **result}
        staged = Path(state["staged_target"])
        for candidate in (target, staged):
            if candidate.exists() and any(path.is_file() for path in candidate.rglob("~$*")):
                raise ValueError("Office lock file appeared after migration; close the workbook before finalizing")
        if staged.exists():
            _verify_inventory(staged, state["legacy_files"], database=True, allow_removed=state["phase"] == "finalizing")
            current = _inventory(staged)
            if set(current) - set(state["legacy_files"]):
                raise ValueError("Unexpected files appeared in the retained old workspace")
        elif state["target_existed"] and state["phase"] != "finalizing":
            raise ValueError("Retained old workspace is missing before finalization")
        _save_state(journal, state, "finalizing")
        if staged.exists():
            # These checked absolute paths are fixed children of this journal.
            if staged.resolve() != (journal / "legacy_backend_workspace").resolve() or not staged.resolve().is_relative_to(journal.resolve()):
                raise ValueError("Unsafe finalization target")
            _no_links(staged, recursive=True)
            shutil.rmtree(staged)
        _save_state(journal, state, "finalized")
    return {"phase": "finalized", **result}


def rollback(project_root: Path = PROJECT_ROOT) -> dict:
    _, source, target, journal = _paths(project_root)
    with _mutation_lock(journal):
        state = _read_state(journal, source, target)
        if state is None:
            raise ValueError("No migration journal to roll back")
        if state["phase"] == "rolled_back":
            return {"phase": "rolled_back"}
        if state["phase"] in {"finalizing", "finalized"}:
            raise ValueError("Finalized migration cannot restore the deleted old workspace")
        staged = Path(state["staged_target"])
        if not source.exists():
            _verify_inventory(target, state["source_files"])
            current_digest = _records_digest(_records(target))
            if current_digest not in {state["source_records_digest"], state["expected_records_digest"]}:
                raise ValueError("Database changed after migration; refusing to discard later writes")
            if _schema_digest(target / "system/app.sqlite") != state["schema_digest"]:
                raise ValueError("Database schema changed after migration; refusing to discard later writes")
            backup = journal / "original.sqlite"
            if sha256(backup) != state["original_database_sha256"]:
                raise ValueError("Original database backup checksum failed")
            _save_state(journal, state, "rolling_back")
            shutil.copy2(backup, target / "system/app.sqlite")
            target.rename(source)
        if staged.exists():
            if target.exists():
                raise ValueError("Cannot restore old workspace over an existing target")
            staged.rename(target)
        _verify_inventory(source, state["source_files"], database=True)
        if state["target_existed"]:
            _verify_inventory(target, state["legacy_files"], database=True)
        _save_state(journal, state, "rolled_back")
        return {"phase": "rolled_back", "source": str(source), "target": str(target)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("check", "apply", "verify", "finalize", "rollback"))
    parser.add_argument("--project-root", type=Path, default=PROJECT_ROOT)
    args = parser.parse_args()
    try:
        result = globals()[args.action](args.project_root)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("ready", True) else 2
    except (OSError, ValueError, sqlite3.Error) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
