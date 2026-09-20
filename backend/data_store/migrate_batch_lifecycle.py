"""Back up SQLite and close unchanged, complete, previously published batches."""
from __future__ import annotations
import argparse
import hashlib
import json
import sqlite3
from contextlib import ExitStack
from pathlib import Path
from uuid import uuid4

from .paths import DATA_ROOT
from .registry import RecordStore, utc_now
from .artifacts import ArtifactStore
from .migrate_root import _no_links
from ..batch_lifecycle import lifecycle, session_batch_id, publication_blockers, published_entry
from ..trajectory_correction.session_state import session_fingerprint


def sha(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def inventory(root):
    return {str(path.relative_to(root).as_posix()): {"size": path.stat().st_size, "sha256": sha(path)}
            for part in ("batches", "releases", "raw") for path in sorted((root / part).rglob("*")) if path.is_file()}


def check(root=DATA_ROOT, *, batch_ids=None):
    root = Path(root).resolve()
    records = RecordStore(root)
    sessions = {item["session_id"]: item for item in records.list("correction_sessions")}
    latest = {}
    for release in sorted(records.list("dataset_releases"), key=lambda item: (item.get("created_at", ""), item["release_id"])):
        for source in release.get("source_refs", []):
            session = sessions.get(source.get("id"))
            batch_id = (source.get("artifact") or {}).get("batch_id") or (session_batch_id(session, root) if session else "")
            if batch_id:
                latest[batch_id] = (release, source, session)
    candidates, skipped, already = [], [], []
    for batch_id, (release, source, session) in sorted(latest.items()):
        if batch_ids is not None and batch_id not in batch_ids:
            continue
        if lifecycle(batch_id, root)["status"] == "published":
            already.append(batch_id)
            continue
        reasons = []
        if not session:
            reasons.append("无法定位发布来源会话")
        else:
            if (session.get("published_release_id") != release["release_id"]
                    or session.get("published_content_fingerprint") != session_fingerprint(session)):
                reasons.append("旧发布后有修改，或缺少可验证的已发布内容指纹")
            if session.get("pending_review") or session.get("stale_tasks"):
                reasons.append("存在未完成或待复核内容")
            try:
                reasons += [item["message"] for item in publication_blockers(batch_id, [session], root)]
            except (OSError, ValueError, KeyError) as exc:
                reasons.append(f"当前过程件无法验证：{exc}")
        result = {"batch_id": batch_id, "release_id": release["release_id"], "published_at": release["created_at"],
                  "session_id": session["session_id"] if session else None}
        if reasons:
            skipped.append({**result, "reasons": list(dict.fromkeys(reasons))})
        else:
            candidates.append(result)
    return {"data_root": str(root), "candidates": candidates, "skipped": skipped, "already_published": already}


def apply(root=DATA_ROOT):
    root = Path(root).resolve()
    _no_links(root, recursive=True)
    records = RecordStore(root)
    report = check(root)
    if not report["candidates"]:
        return {**report, "status": "no_changes", "backup": None}
    store = ArtifactStore(root)
    with ExitStack() as locks:
        for item in report["candidates"]:
            locks.enter_context(store.batch_lock(item["batch_id"]))
        # Recheck under the same batch locks used by publication and mutations.
        current = check(root, batch_ids={item["batch_id"] for item in report["candidates"]})
        if current["candidates"] != report["candidates"]:
            raise ValueError("批次在检查后发生变化，请重新检查")
        before = inventory(root)
        directory = root.parent / ".batch-publication-migration" / root.name / uuid4().hex
        directory.mkdir(parents=True)
        backup = directory / "app.sqlite"
        with records._connection() as source:
            destination = sqlite3.connect(backup)
            try:
                source.backup(destination)
                if destination.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise ValueError("数据库备份完整性检查失败")
            finally:
                destination.close()
        backup_hash = sha(backup)
        state = {**report, "created_at": utc_now(), "status": "backed_up", "backup": str(backup),
                 "backup_sha256": backup_hash, "files": before}
        (directory / "report.json").write_text(json.dumps(state, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
        entries = [published_entry(item["batch_id"], item["release_id"], item["published_at"], root)
                   for item in report["candidates"]]
        originals = {item["batch_id"]: records.get("batch_lifecycle", item["batch_id"]) for item in report["candidates"]}
        # Fail before committing if another writer violates the offline migration requirement.
        if inventory(root) != before:
            raise ValueError("文件在登记前发生变化；请停止所有写入后重试")
        committed = records.put_many(entries)
        try:
            if sha(backup) != backup_hash or inventory(root) != before:
                raise ValueError("过程件、原始文件或发布文件在登记期间发生变化")
        except BaseException as exc:
            restore, deletes = [], []
            for saved in committed:
                original = originals[saved["batch_id"]]
                entry = {"namespace": "batch_lifecycle", "key": saved["batch_id"],
                         "expected_revision": saved["storage_revision"]}
                if original is None:
                    deletes.append(entry)
                else:
                    restore.append({**entry, "payload": original})
            records.put_many(restore, deletes=deletes)
            state.update(status="rolled_back", error=str(exc))
            (directory / "report.json").write_text(json.dumps(state, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
            raise
        state.update(status="verified", closed_batch_ids=[item["batch_id"] for item in report["candidates"]],
                     post_database_sha256=sha(records.database_path), verified_file_count=len(before))
        (directory / "report.json").write_text(json.dumps(state, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
        return {key: value for key, value in state.items() if key != "files"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("check", "apply"))
    parser.add_argument("--data-root", type=Path, default=DATA_ROOT)
    args = parser.parse_args()
    print(json.dumps(globals()[args.command](args.data_root), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
