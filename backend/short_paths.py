"""Versioned local archive layouts, stable identities and full-hash cache aliases.

The collection application's legacy wire protocol remains untouched. Consumers
can validate the remote manifest first and use this module for local storage.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import secrets
from contextlib import contextmanager
from pathlib import Path

from .data_store.paths import contained_path
from .data_store.registry import RecordStore, RevisionConflict
from .file_io import io_path, is_link_or_junction
from .identifiers import ID_SCHEME, allocate_id, batch_uses_short_ids

STORAGE_LAYOUT = "short-v1"


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _trajectory_id(root, batch_id, run_id, item):
    original = item.get("source_relative_dir") or item["relative_dir"]
    return allocate_id("tr", root, key=["trajectory", batch_id, run_id,
                       item["task_id"], item.get("phone_id"), original])


def map_collection_manifest(root, run, source_manifest):
    """Return a local manifest plus a durable source->destination file mapping.

    Source manifests must be in the existing collection wire layout. No file
    bytes are modified, and this does not mark a run complete.
    """
    from .collection_runs import CollectionRunStore
    from .data_store.registry import utc_now
    store = RecordStore(root)
    normalized = CollectionRunStore(root)._normalize(
        run, source_manifest, run.get("completed_at") or run.get("created_at") or utc_now())
    batch_id, run_id = run["batch_id"], run["collection_run_id"]
    if not batch_uses_short_ids(batch_id, root):
        files = {f["path"]: f["path"] for t in normalized["trajectories"] for f in t["files"]}
        return {"manifest": normalized, "storage_layout": "legacy", "mapping": {"files": files}}
    source_hash = _digest(normalized)
    original_hash = _digest(source_manifest)
    key = _digest([batch_id, run_id, source_hash, original_hash])
    previous = store.get("collection_path_mappings", key)
    if previous is not None:
        value = previous["value"]
        if previous["source_hash"] != source_hash or previous["value_hash"] != _digest(value):
            raise RevisionConflict("原始轨迹路径映射校验失败")
        return copy.deepcopy(value)
    manifest = copy.deepcopy(normalized)
    files, seen = {}, set()
    for trajectory in manifest["trajectories"]:
        original = trajectory["relative_dir"]
        identifier = _trajectory_id(root, batch_id, run_id, trajectory)
        task_id = trajectory["task_id"]
        from .collection_runs import _identifier
        _identifier(task_id, "任务编号")
        local = task_id + "/" + identifier
        trajectory.update(source_relative_dir=original, relative_dir=local,
                          trajectory_id=identifier, storage_layout=STORAGE_LAYOUT)
        for item in trajectory["files"]:
            source = item["path"]
            target = local + source[len(original):]
            if target.casefold() in seen:
                raise ValueError("短归档路径重复")
            seen.add(target.casefold())
            files[source] = target
            item["source_path"], item["path"] = source, target
    manifest["trajectories"].sort(key=lambda item: item["relative_dir"])
    value = {"manifest": manifest, "storage_layout": STORAGE_LAYOUT,
             "mapping": {"key": key, "source_sha256": source_hash, "files": files,
                         "source_manifest": normalized, "original_manifest": copy.deepcopy(source_manifest),
                         "original_sha256": original_hash}}
    try:
        store.put("collection_path_mappings", key,
                  {"source_hash": source_hash, "value_hash": _digest(value), "value": value}, expected_revision=0)
    except RevisionConflict:
        previous = store.get("collection_path_mappings", key)
        if previous is None or previous["value"] != value:
            raise
    return value


def validate_local_manifest(root, run, mapped, *, root_override=None):
    """Validate mapping identity and every local byte before completed registration."""
    from .collection_runs import CollectionRunStore
    if mapped["storage_layout"] == STORAGE_LAYOUT:
        mapping = mapped["mapping"]
        saved = RecordStore(root).get("collection_path_mappings", mapping["key"])
        if (not saved or saved["value_hash"] != _digest(mapped) or saved["value"] != mapped
                or mapped["manifest"]["batch_id"] != run["batch_id"]
                or mapped["manifest"]["collection_run_id"] != run["collection_run_id"]):
            raise ValueError("本地轨迹清单与冻结路径映射不一致")
    elif mapped["storage_layout"] != "legacy":
        raise ValueError("未知轨迹存储布局")
    CollectionRunStore(root)._validate_source_files(run, mapped["manifest"], root_override=root_override)


def adapt_conversion_identity(payload, snapshot, root):
    """Only new batches opt in; the legacy converter and its code hash stay intact."""
    batch_id = snapshot["batch_id"]
    if not batch_uses_short_ids(batch_id, root):
        return payload
    sources = {}
    for item in snapshot["trajectories"]:
        run_id = item["collection_run_id"]
        identifier = _trajectory_id(root, batch_id, run_id, item)
        if item.get("trajectory_id") and item["trajectory_id"] != identifier:
            raise ValueError("冻结轨迹身份与短编号映射不一致")
        sources[(run_id, item["task_id"], item["relative_dir"])] = identifier
    result = copy.deepcopy(payload)
    for rows in result.get("sheets", {}).values():
        for row in rows:
            run_id = row.get("collection_run_id")
            if not run_id:
                continue
            image = str(row.get("image", "")).replace("\\", "/")
            prefix = "runs/" + run_id + "/"
            if not image.startswith(prefix):
                raise ValueError("轨迹截图不属于登记运行")
            relative = image[len(prefix):].rsplit("/", 1)[0]
            key = (run_id, row["task_id"], relative)
            if key not in sources:
                raise ValueError("轨迹步骤缺少冻结身份映射")
            row["trajectory_id"] = sources[key]
    result["identity_scheme"] = ID_SCHEME
    return result


def short_cache_directory(root, batch_id, namespace, fingerprint, parent):
    """Shorten a directory component, retaining the full fingerprint as authority."""
    root = Path(root)
    relative = Path(parent).relative_to(root)
    original = root
    for part in relative.parts:
        if part in {".", ".."}:
            raise ValueError("缓存路径不允许越界")
        original /= part
        if is_link_or_junction(original):
            raise ValueError("缓存路径不允许链接")
    parent = contained_path(root, str(relative))
    if not batch_uses_short_ids(batch_id, root):
        return contained_path(parent, fingerprint)
    if not isinstance(fingerprint, str) or len(fingerprint) != 64 or any(c not in "0123456789abcdef" for c in fingerprint):
        raise ValueError("缓存指纹必须为完整 SHA256")
    identifier = allocate_id("c", root, key=["cache", batch_id, namespace, str(parent.relative_to(root)), fingerprint])
    unresolved = parent / identifier
    if is_link_or_junction(unresolved):
        raise ValueError("缓存路径不允许链接")
    path = contained_path(parent, identifier)
    identity = {"schema_version": 1, "batch_id": batch_id, "namespace": namespace, "fingerprint": fingerprint}
    io_path(path).mkdir(parents=True, exist_ok=True)
    target = path / "identity.json"
    if is_link_or_junction(path) or is_link_or_junction(target):
        raise ValueError("缓存路径不允许链接")
    # The batch lock also protects readers from observing a half-created marker.
    from .data_store.locking import batch_lock
    with batch_lock(root, batch_id):
        if io_path(target).exists():
            if json.loads(io_path(target).read_text(encoding="utf-8")) != identity:
                raise ValueError("缓存目录的完整指纹不匹配")
        else:
            # A process can die before replacing the first marker. Only our
            # exclusively reserved sibling temporary files may be discarded;
            # actual cache content without an identity remains invalid.
            abandoned = list(io_path(path).iterdir())
            if any(is_link_or_junction(item) or not item.is_file()
                   or not re.fullmatch(r"\.[0-9a-f]{12}\.tmp", item.name) for item in abandoned):
                raise ValueError("缓存目录缺少完整指纹，不复用未知缓存")
            for item in abandoned:
                item.unlink()
            with short_temporary_file(target) as temporary:
                io_path(temporary).write_text(json.dumps(identity, ensure_ascii=False), encoding="utf-8")
                io_path(temporary).replace(io_path(target))
    return path


@contextmanager
def short_temporary_file(target):
    """Reserve a short sibling name exclusively, without repeating the target name."""
    target = Path(target)
    io_path(target.parent).mkdir(parents=True, exist_ok=True)
    for _ in range(100):
        temporary = target.parent / ("." + secrets.token_hex(6) + ".tmp")
        try:
            fd = os.open(io_path(temporary), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            continue
        os.close(fd)
        break
    else:
        raise FileExistsError("无法分配临时文件")
    try:
        yield temporary
    finally:
        io_path(temporary).unlink(missing_ok=True)
