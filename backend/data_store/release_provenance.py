"""Self-contained, verified stage lineage owned by an immutable release."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
from typing import Any

from .artifacts import ArtifactStore


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _key(ref: dict) -> tuple[str, str, str]:
    return tuple(str(ref.get(name, "")) for name in ("batch_id", "stage", "version"))


def freeze_release_provenance(root: Path, release: dict, destination: Path, *, store=None) -> dict:
    """Write a release's precise source closure before exposing its manifest.

    ``destination`` is a release staging directory. The returned registry value
    points to its eventual releases/<id> location. Missing legacy lineage is
    retained as an explicit absence; a recorded but broken reference fails.
    """
    root, destination = Path(root).resolve(), Path(destination).resolve()
    store = store or ArtifactStore(root)
    value = deepcopy(release)
    frozen: dict[tuple[str, str, str], dict] = {}
    active: set[tuple[str, str, str]] = set()
    directory = destination / "provenance"
    directory.mkdir(parents=True, exist_ok=True)

    def visit(reference: dict) -> None:
        key = _key(reference)
        if not all(key):
            raise ValueError("发布来源缺少精确阶段标识")
        if key in active:
            raise ValueError("发布来源存在循环")
        if key in frozen:
            return
        manifest = store.get(*key)
        if manifest is None:
            raise ValueError("发布引用的阶段产物不存在或已更新，请重新导出")
        registered = {item["name"]: item for item in manifest.get("files", [])}
        for item in reference.get("files", []):
            actual = registered.get(item.get("name"))
            if actual is None or any(actual.get(field) != item.get(field) for field in ("sha256", "size", "path")):
                raise ValueError("发布引用的阶段产物校验不一致")
        active.add(key)
        files = {}
        # JSON is the authoritative provenance. Excel is copied only for the
        # full final export, whose bytes are independently checked by conversion.
        for item in manifest.get("files", []):
            if item["name"] not in {"result.json", "full_dataset.xlsx"}:
                continue
            data = store.read_file(manifest, item["name"]) if hasattr(store, "read_file") else store.resolve_file(manifest, item["name"]).read_bytes()
            if len(data) != item.get("size") or _digest(data) != item.get("sha256"):
                raise ValueError("发布期间阶段产物发生变化，请重试")
            name = f"{_digest('/'.join(key).encode())[:24]}-{item['name']}"
            (directory / name).write_bytes(data)
            files[item["name"]] = {"name": name, "size": len(data), "sha256": _digest(data)}
        frozen[key] = {"manifest": manifest, "files": files}
        for ref in manifest.get("source_refs", []):
            if isinstance(ref, dict) and all(_key(ref)):
                visit(ref)
        active.remove(key)

    for source in value.get("source_refs", []):
        reference = source.get("artifact")
        if not reference:
            continue
        visit(reference)
        batch_id = reference["batch_id"]
        # Legacy correction manifests referenced tree runs by label only.
        payload = json.loads((directory / frozen[_key(reference)]["files"]["result.json"]["name"]).read_text(encoding="utf-8"))
        run_id = str(payload.get("tree_run_id") or source.get("tree_run_id") or "")
        tree_refs = [ref for ref in reference.get("source_refs", []) if ref.get("stage") == "04_tree"]
        if not tree_refs and run_id:
            tree_refs = [item for item in store.list(batch_id, "04_tree")
                         if str(item.get("metadata", {}).get("run_id", "")) == run_id]
        if run_id and len(tree_refs) != 1:
            raise ValueError("发布的建树来源必须对应唯一有效产物")
        for tree in tree_refs:
            visit(tree)
    bundle = {"schema_version": 1, "release_id": value["release_id"], "artifacts": list(frozen.values())}
    data = (json.dumps(bundle, ensure_ascii=False, allow_nan=False, indent=2) + "\n").encode()
    (directory / "index.json").write_bytes(data)
    value["provenance"] = {"schema_version": 1,
        "path": f"releases/{value['release_id']}/provenance/index.json", "sha256": _digest(data), "size": len(data)}
    return value


def freeze_existing_release(root: Path, release: dict, *, store=None) -> dict:
    """Migration helper; caller commits the returned release registry record."""
    if release.get("provenance"):
        FrozenReleaseSources(root, release)
        return deepcopy(release)
    return freeze_release_provenance(root, release, Path(root) / "releases" / release["release_id"], store=store)


class FrozenReleaseSources:
    """Read-only ArtifactStore interface backed exclusively by release files."""

    def __init__(self, root: Path, release: dict):
        self.directory = (Path(root) / "releases" / str(release["release_id"]) / "provenance").resolve()
        ref = release.get("provenance") or {}
        expected = f"releases/{release['release_id']}/provenance/index.json"
        if ref.get("path") != expected:
            raise ValueError("发布缺少独立冻结来源，请执行数据迁移")
        data = self._bytes(self.directory / "index.json", ref)
        bundle = json.loads(data)
        if bundle.get("release_id") != release["release_id"] or bundle.get("schema_version") != 1:
            raise ValueError("发布冻结来源身份不一致")
        self.entries = {}
        for entry in bundle.get("artifacts", []):
            key = _key(entry["manifest"])
            if not all(key) or key in self.entries:
                raise ValueError("发布冻结来源标识重复或缺失")
            self.entries[key] = entry

    @staticmethod
    def _bytes(path: Path, ref: dict) -> bytes:
        data = path.read_bytes()
        if len(data) != ref.get("size") or _digest(data) != ref.get("sha256"):
            raise ValueError("发布冻结来源校验失败")
        return data

    def get(self, batch_id: str, stage: str, version: str) -> dict | None:
        entry = self.entries.get((batch_id, stage, version))
        return deepcopy(entry["manifest"]) if entry else None

    def list(self, batch_id=None, stage=None) -> list[dict]:
        return [deepcopy(entry["manifest"]) for key, entry in self.entries.items()
                if (batch_id is None or key[0] == batch_id) and (stage is None or key[1] == stage)]

    def resolve_file(self, manifest: dict, filename: str) -> Path:
        entry = self.entries.get(_key(manifest))
        if entry is None or entry["manifest"] != manifest:
            raise ValueError("发布冻结来源未登记或已修改")
        file = entry["files"].get(filename)
        if not file or Path(file["name"]).name != file["name"]:
            raise ValueError("发布冻结来源缺少所需文件")
        path = self.directory / file["name"]
        self._bytes(path, file)
        return path
