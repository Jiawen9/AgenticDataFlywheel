"""Locate complete upload directories from a release's frozen Excel selection.

Only the workbook and its release-owned provenance are read. No current session,
quality rule, image decoder, raw-directory scan, copy or archive is involved.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path, PurePosixPath
from typing import Any

from openpyxl import load_workbook

from ..data_store.release_provenance import FrozenReleaseSources
from ..file_io import io_path, resolve_path
from ..trajectory_correction.assets import registered_asset_root


class UploadSourceError(ValueError):
    """The published source cannot be located without guessing its scope."""


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _image(value: Any) -> str:
    return _text(value).replace("\\", "/")


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def _component(value: str) -> str:
    """Keep readable identities while preventing slash/dot and name collisions."""
    clean = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value).strip(" .")
    clean = clean or "unnamed"
    if clean != value or len(clean) > 100:
        clean = clean[:80] + "__" + _digest(value)[:16]
    return clean


def _batch_id(release: dict, source: dict, index: int, *, allow_unbatched: bool = False) -> str | None:
    value = _text((source.get("artifact") or {}).get("batch_id") or source.get("batch_id"))
    batches = release.get("batch_ids") or []
    if not value and len(batches) == len(release.get("excel_paths") or []):
        value = _text(batches[index])
    if not value and len(batches) == 1:
        value = _text(batches[0])
    if not value:
        if allow_unbatched:
            return None
        raise UploadSourceError("发布表缺少冻结批次归属，无法确定云道目标目录")
    return value


class _Scope:
    def __init__(self, frozen: FrozenReleaseSources, source: dict, batch_id: str, root: Path, workbook_digest: str):
        self.images: dict[str, dict] = {}
        self.identities: dict[str, list[dict]] = {}
        self.roots: set[Path] = set()
        self.seen: set[tuple[str, str, str]] = set()
        reference = source.get("artifact") or {}
        if not reference:
            raise UploadSourceError("发布表缺少冻结轨迹来源，不能用整个共享轨迹目录代替")
        manifest = frozen.get(reference.get("batch_id"), reference.get("stage"), reference.get("version"))
        exported = next((item for item in (manifest or {}).get("files", [])
                         if item.get("name") == "full_dataset.xlsx"), None)
        if not exported or exported.get("sha256") != workbook_digest:
            raise UploadSourceError("发布表与冻结导出来源不一致")
        self.frozen, self.batch_id, self.root = frozen, batch_id, root
        self._visit(reference)
        # Early correction exports froze the exact tree by run label, even
        # though their 07_cot reference did not yet include the tree manifest.
        run_id = _text(source.get("tree_run_id"))
        if run_id:
            trees = [item for item in frozen.list(batch_id, "04_tree")
                     if _text(item.get("metadata", {}).get("run_id")) == run_id]
            for tree in trees:
                self._visit(tree)
        if not self.roots:
            raise UploadSourceError(f"批次 {batch_id} 缺少冻结原始轨迹根路径")
        if len(self.roots) != 1:
            raise UploadSourceError(f"批次 {batch_id} 的冻结来源包含多个轨迹根路径，无法确定上传来源")
        self.raw_root = next(iter(self.roots))

    def _add_row(self, row: dict, group: dict | None = None) -> None:
        values = {**(group or {}), **(row.get("values") or {}), **row}
        image = _image(values.get("image"))
        if not image:
            return
        # Final frozen rows have priority over upstream rows of the same image.
        self.images.setdefault(image, values)
        for name in ("trajectory_id", "meta_task", "文件夹名", "source_trajectory_id"):
            identity = _text(values.get(name))
            if identity:
                self.identities.setdefault(identity, []).append(values)

    def _visit(self, reference: dict) -> None:
        key = tuple(_text(reference.get(name)) for name in ("batch_id", "stage", "version"))
        if not all(key) or key in self.seen:
            return
        if key[0] != self.batch_id:
            raise UploadSourceError("发布表的冻结来源跨越批次")
        manifest = self.frozen.get(*key)
        if manifest is None:
            raise UploadSourceError("发布引用的冻结来源文件不存在")
        self.seen.add(key)
        payload = json.loads(io_path(self.frozen.resolve_file(manifest, "result.json")).read_text(encoding="utf-8"))
        values = [payload.get("raw_root"), (manifest.get("metadata") or {}).get("raw_root")]
        values += [ref.get("path") for ref in manifest.get("source_refs", [])
                   if ref.get("kind") == "raw_trajectories"]
        for value in values:
            if value:
                self.roots.add(registered_asset_root(str(value), self.root))
        for group in payload.get("groups", []):
            for row in group.get("rows", []):
                self._add_row(row, group)
        for rows in payload.get("sheets", {}).values():
            for row in rows:
                self._add_row(row)
        # Some legacy tree versions embed the source table instead of listing
        # the annotation as a separate parent.
        for rows in (payload.get("source_annotation") or {}).get("sheets", {}).values():
            for row in rows:
                self._add_row(row)
        for parent in manifest.get("source_refs", []):
            self._visit(parent)

    def directory(self, excel_row: dict, label: str) -> tuple[Path, dict]:
        image = _image(excel_row.get("image"))
        frozen_row = self.images.get(image, {}) if image else {}
        if not image:
            # Identity-only workbooks can still locate a precise frozen folder.
            identity = next((_text(excel_row.get(name)) for name in
                             ("trajectory_id", "meta_task", "文件夹名") if _text(excel_row.get(name))), "")
            candidates = self.identities.get(identity, [])
            parent_names = {str(PurePosixPath(_image(row["image"])).parent) for row in candidates}
            if len(parent_names) != 1:
                raise UploadSourceError(f"{label}：无法从冻结轨迹身份确定原目录")
            frozen_row = candidates[0]
            image = _image(frozen_row["image"])
        image_path = Path(image)
        if ".." in PurePosixPath(image).parts or (image_path.drive and not image_path.is_absolute()):
            raise UploadSourceError(f"{label}：轨迹路径越界")
        path = resolve_path(image_path if image_path.is_absolute() else self.raw_root / image_path)
        directory = path.parent
        if directory == self.raw_root or not directory.is_relative_to(self.raw_root):
            raise UploadSourceError(f"{label}：轨迹目录不属于冻结原始轨迹根路径")
        if not io_path(directory).is_dir():
            raise FileNotFoundError(f"{label}：轨迹目录不存在：{directory}")
        # The platform's standard trajectory layout keeps step images directly
        # in the trajectory folder. Missing or damaged images do not block this
        # directory transfer: no image/file-quality check is performed here.
        merged = {**frozen_row, **{key: value for key, value in excel_row.items() if _text(value)}}
        return directory, merged


def resolve_upload_sources(registry, release: dict, *, include_trajectories: bool = True) -> list[dict]:
    """Return serializable upload items; never upload an unselected shared root.

    ``destination_relative`` is relative to the configured cloud prefix. Every
    ``source_dir`` is a complete existing directory; the uploader sends its
    contents to that destination without constructing a local staging copy.
    """
    if include_trajectories and release.get("source_kind") in {"external_import", "external_manual"}:
        raise UploadSourceError("外部 Excel 看板导入不包含本次轨迹上传")
    sources = release.get("source_refs") or []
    root = resolve_path(registry.data_root)
    frozen = None
    items, seen, destinations = [], set(), {}
    for index, file in enumerate(release.get("excel_paths") or []):
        source = sources[index] if index < len(sources) else {}
        if include_trajectories and source.get("kind") == "external_import":
            raise UploadSourceError("外部 Excel 看板导入不包含本次轨迹上传")
        batch_id = _batch_id(release, source, index, allow_unbatched=not include_trajectories)
        cloud_root = _component(batch_id or str(release["release_id"]))
        path = registry.resolve_project_path(str(file.get("path", "")))
        if not io_path(path).is_file() or path.suffix.lower() not in {".xlsx", ".xlsm"}:
            raise FileNotFoundError(f"发布表格不存在或不是 Excel：{path}")
        digest = hashlib.sha256()
        with io_path(path).open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        if digest.hexdigest() != _text(file.get("sha256")).lower():
            raise UploadSourceError(f"发布表格与冻结版本的 SHA256 不一致：{path.name}")
        expected = root / "releases" / str(release["release_id"]) / f"{index + 1:03d}"
        if resolve_path(path.parent) != resolve_path(expected):
            raise UploadSourceError(f"发布表格没有独立的冻结目录，不能上传其共享父目录：{path}")
        sequence = f"{index + 1:03d}"
        item = {"kind": "excel", "source_dir": str(path.parent), "file_path": str(path),
                "batch_id": batch_id, "case_id": None, "trajectory_id": None,
                "destination_relative": f"{cloud_root}/发布表格/{sequence}",
                "filename": _text(file.get("filename")) or path.name, "label": path.name,
                "index": index, "sha256": file.get("sha256"), "size_bytes": io_path(path).stat().st_size,
                "source_identity": _digest(["excel", release["release_id"], index, file.get("sha256")])}
        items.append(item)
        if not include_trajectories:
            continue
        if frozen is None:
            frozen = FrozenReleaseSources(root, release)
        scope = _Scope(frozen, source, batch_id, root, digest.hexdigest())
        book = load_workbook(io_path(path), read_only=True, data_only=False)
        row_count = 0
        try:
            for sheet in book.worksheets:
                rows = sheet.iter_rows(values_only=True)
                headers = [_text(value) for value in next(rows, ())]
                if not any(name in headers for name in ("image", "trajectory_id", "meta_task", "文件夹名")):
                    continue
                for number, cells in enumerate(rows, start=2):
                    row = dict(zip(headers, cells))
                    if not any(_text(row.get(name)) for name in ("image", "trajectory_id", "meta_task", "文件夹名")):
                        continue
                    row_count += 1
                    directory, metadata = scope.directory(row, f"{path.name}/{sheet.title} 第 {number} 行")
                    relative = directory.relative_to(scope.raw_root).as_posix()
                    # Root-relative paths remain stable through supported data
                    # root relocation, and include run/phone/repeat identity.
                    root_identity = scope.raw_root.relative_to(root).as_posix()
                    identity = _digest(["trajectory", batch_id, root_identity, relative])
                    if identity in seen:
                        continue
                    seen.add(identity)
                    case_id = next((_text(metadata.get(name)) for name in
                                    ("collection_case_id", "case_id", "task_id") if _text(metadata.get(name))), "")
                    if not case_id:
                        parts = PurePosixPath(relative).parts
                        case_id = parts[-2] if len(parts) >= 2 else parts[-1]
                    trajectory_id = _text(metadata.get("trajectory_id") or metadata.get("meta_task") or directory.name)
                    folder_name = _component(directory.name) + "__" + identity[:20]
                    destination = f"{_component(batch_id)}/{_component(case_id)}/{folder_name}"
                    previous = destinations.setdefault(destination.casefold(), identity)
                    if previous != identity:
                        raise UploadSourceError(f"轨迹目标目录身份冲突：{destination}")
                    items.append({"kind": "trajectory", "source_dir": str(directory), "batch_id": batch_id,
                                  "case_id": case_id, "trajectory_id": trajectory_id, "source_identity": identity,
                                  "destination_relative": destination, "filename": directory.name,
                                  "label": f"{case_id}/{directory.name}", "index": index})
        finally:
            book.close()
        if not row_count:
            raise UploadSourceError(f"{path.name} 没有可定位的轨迹行，不能猜测上传整个批次")
    if not items:
        raise UploadSourceError("发布记录没有可上传的 Excel")
    return items
