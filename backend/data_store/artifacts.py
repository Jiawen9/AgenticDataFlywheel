"""Immutable JSON/Excel stage snapshots, published only when complete."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import uuid
from itertools import chain
from pathlib import Path
from typing import Any

from .paths import DATA_ROOT, contained_path
from .registry import RecordStore, utc_now

_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
_RESERVED = re.compile(r"(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?\Z", re.IGNORECASE)


def _identifier(value: str, label: str) -> str:
    if not isinstance(value, str) or not _SAFE_ID.fullmatch(value) or value.endswith(".") or _RESERVED.fullmatch(value):
        raise ValueError(f"Invalid {label}")
    return value


def _filename(value: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 180 or value in {".", ".."} or re.search(r'[<>:"/\\|?*\x00-\x1f]', value) or value.endswith((".", " ")) or _RESERVED.fullmatch(value):
        raise ValueError("Invalid artifact filename")
    return value


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2) + "\n").encode("utf-8")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _write_bytes(path: Path, value: bytes) -> None:
    with path.open("xb") as output:
        output.write(value)
        output.flush()
        os.fsync(output.fileno())


def _write_tables(path: Path, tables: dict[str, list[dict]]) -> None:
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill

    if not tables:
        raise ValueError("Excel tables must contain at least one worksheet")
    workbook = Workbook()
    workbook.remove(workbook.active)
    used_names = set()
    try:
        for sheet_name, rows in tables.items():
            if not isinstance(sheet_name, str) or not sheet_name or len(sheet_name) > 31 or re.search(r"[\\/*?:\[\]]", sheet_name) or sheet_name.lower() in used_names:
                raise ValueError("Invalid or duplicate Excel worksheet name")
            used_names.add(sheet_name.lower())
            sheet = workbook.create_sheet(sheet_name)
            columns: list[str] = []
            for row in rows:
                if not isinstance(row, dict):
                    raise TypeError("Excel table rows must be dictionaries")
                for column in row:
                    if not isinstance(column, str):
                        raise TypeError("Excel column names must be strings")
                    if column not in columns:
                        columns.append(column)
            if len(rows) > 1048575 or len(columns) > 16384:
                raise ValueError("Excel worksheet exceeds its row or column limit")
            values_iter = chain([columns], ([row.get(column) for column in columns] for row in rows))
            for row_number, values in enumerate(values_iter, start=1):
                for column_number, value in enumerate(values, start=1):
                    if isinstance(value, (dict, list, tuple)):
                        value = json.dumps(value, ensure_ascii=False, allow_nan=False)
                    if isinstance(value, str) and len(value) > 32767:
                        raise ValueError(f"Excel cell {sheet_name}!R{row_number}C{column_number} exceeds 32767 characters; original JSON text must be retained")
                    cell = sheet.cell(row_number, column_number, value)
                    # Treat arbitrary tasks, identifiers and model text as literal
                    # strings, including =, +, -, and @ prefixes.
                    if isinstance(value, str):
                        cell.data_type = "s"
                        cell.number_format = "@"
                    if row_number == 1:
                        cell.font = Font(bold=True, color="FFFFFF")
                        cell.fill = PatternFill("solid", fgColor="0F766E")
            sheet.freeze_panes = "A2"
            if columns:
                sheet.auto_filter.ref = sheet.dimensions
        workbook.save(path)
    finally:
        workbook.close()
    with path.open("r+b") as output:
        os.fsync(output.fileno())


class ArtifactStore:
    def __init__(self, root: Path = DATA_ROOT):
        self.root = contained_path(Path(root))
        self.records = RecordStore(self.root)

    @staticmethod
    def _key(batch_id: str, stage: str, version: str) -> str:
        return "/".join((_identifier(batch_id, "batch ID"), _identifier(stage, "stage"), _identifier(version, "version")))

    def publish(
        self,
        batch_id: str,
        stage: str,
        payload: Any,
        workbooks: dict[str, Path] | None = None,
        tables: dict[str, list[dict]] | None = None,
        source_refs: list | None = None,
        metadata: dict | None = None,
    ) -> dict:
        """Freeze one JSON payload and corresponding Excel files as a new version.

        ``tables`` generates result.xlsx from in-memory rows. ``workbooks`` maps
        destination filenames to existing Excel files and copies their exact
        bytes (for legacy exporters). Failed publication is never returned by
        list/get; callers can safely retry without rerunning their model work.
        """
        _identifier(batch_id, "batch ID")
        _identifier(stage, "stage")
        payload_bytes = _json_bytes(payload)
        metadata = json.loads(_json_bytes(metadata or {}))
        source_refs = json.loads(_json_bytes(source_refs or []))
        workbooks = workbooks or {}
        names = {"result.json", "manifest.json"}
        if tables is not None:
            names.add("result.xlsx")
        for name, source in workbooks.items():
            _filename(name)
            if not name.lower().endswith(".xlsx") or name.casefold() in {value.casefold() for value in names}:
                raise ValueError("Artifact workbooks require unique .xlsx filenames")
            names.add(name)
            if not Path(source).is_file():
                raise FileNotFoundError(f"Excel artifact source does not exist: {source}")
        version = utc_now().replace("-", "").replace(":", "").replace(".", "") + "-" + uuid.uuid4().hex
        key = self._key(batch_id, stage, version)
        final = contained_path(self.root, "batches", batch_id, stage, version)
        staging = contained_path(self.root, "tmp", "artifacts", uuid.uuid4().hex)
        staging.mkdir(parents=True, exist_ok=False)
        published = False
        try:
            _write_bytes(staging / "result.json", payload_bytes)
            if tables is not None:
                _write_tables(staging / "result.xlsx", tables)
            for name, source in workbooks.items():
                with Path(source).open("rb") as incoming, (staging / name).open("xb") as output:
                    shutil.copyfileobj(incoming, output)
                    output.flush()
                    os.fsync(output.fileno())
            files = []
            for file in sorted(staging.iterdir(), key=lambda item: item.name):
                files.append({
                    "name": file.name,
                    "kind": "json" if file.suffix == ".json" else "excel",
                    "path": (final / file.name).relative_to(self.root).as_posix(),
                    "sha256": _sha256(file),
                    "size": file.stat().st_size,
                })
            manifest = {
                "schema_version": 1,
                "batch_id": batch_id,
                "stage": stage,
                "version": version,
                "created_at": utc_now(),
                "source_refs": source_refs,
                "metadata": metadata,
                "files": files,
            }
            _write_bytes(staging / "manifest.json", _json_bytes(manifest))
            final.parent.mkdir(parents=True, exist_ok=True)
            # Unique destination; a complete directory becomes visible at once.
            staging.rename(final)
            published = True
            self.records.put("artifacts", key, manifest, expected_revision=0)
            return manifest
        except BaseException:
            # Delete only this invocation's newly-created, validated directory.
            target = final if published else staging
            if target.is_dir() and target.resolve().is_relative_to(self.root):
                shutil.rmtree(target)
            raise

    def list(self, batch_id: str | None = None, stage: str | None = None) -> list[dict]:
        if batch_id is not None:
            _identifier(batch_id, "batch ID")
        if stage is not None:
            _identifier(stage, "stage")
        manifests = self.records.list("artifacts")
        return [self._manifest(item) for item in manifests if (batch_id is None or item["batch_id"] == batch_id) and (stage is None or item["stage"] == stage)]

    def get(self, batch_id: str, stage: str, version: str) -> dict | None:
        record = self.records.get("artifacts", self._key(batch_id, stage, version))
        return self._manifest(record) if record else None

    @staticmethod
    def _manifest(record: dict) -> dict:
        return {key: value for key, value in record.items() if key != "storage_revision"}

    def resolve_file(self, manifest: dict, filename: str) -> Path:
        """Resolve only a registered snapshot file and verify its frozen bytes."""
        _filename(filename)
        registered = self.get(manifest["batch_id"], manifest["stage"], manifest["version"])
        if registered is None:
            raise FileNotFoundError("Artifact snapshot is not registered")
        file = next((item for item in registered["files"] if item["name"] == filename), None)
        if file is None:
            raise FileNotFoundError("Artifact file is not in the snapshot manifest")
        path = contained_path(self.root, file["path"])
        expected_parent = contained_path(self.root, "batches", registered["batch_id"], registered["stage"], registered["version"])
        if path.parent != expected_parent:
            raise ValueError("Artifact file is outside its snapshot directory")
        if not path.is_file():
            raise FileNotFoundError("Artifact file is missing")
        if path.stat().st_size != file["size"] or _sha256(path) != file["sha256"]:
            raise ValueError("Artifact file checksum does not match the frozen snapshot")
        return path
