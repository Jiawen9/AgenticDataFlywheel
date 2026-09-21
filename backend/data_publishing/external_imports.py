"""Validated external workbooks become immutable releases without workflow sessions."""
from __future__ import annotations

from copy import deepcopy

from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
from typing import BinaryIO
import uuid

from ..batch_lifecycle import published_entry
from ..data_store import RecordStore
from ..data_store.artifacts import _filename, _identifier, _json_bytes, _sha256, _write_bytes
from ..data_store.locking import batch_lock
from ..data_store.paths import contained_path
from ..data_store.registry import utc_now

IMPORTS = "dataset_release_imports"
REQUESTS = "dataset_import_requests"
FILE_INDEX = "dataset_import_files"
CONTENT_INDEX = "dataset_import_contents"
MAX_UPLOAD_BYTES = 50 * 1024 * 1024
TTL_HOURS = 24
LOCK_ID = "external_release_imports"


class ImportFailure(ValueError):
    def __init__(self, message: str, *, status: int = 422, code: str = "invalid_import", **details):
        super().__init__(message)
        self.status = status
        self.detail = {"code": code, "message": message, **details}


def _hash(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, allow_nan=False,
                                     sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _expired(record: dict) -> bool:
    return datetime.fromisoformat(record["expires_at"].replace("Z", "+00:00")) <= datetime.now(timezone.utc)


def _metadata_signature(metadata: dict) -> str:
    return _hash({name: metadata.get(name, "") for name in
                  ("data_source", "data_date", "app", "level1", "level2", "sheet_name")})


class ExternalReleaseImporter:
    def __init__(self, registry, *, max_upload_bytes: int = MAX_UPLOAD_BYTES):
        self.registry = registry
        self.root = registry.data_root
        self.records = RecordStore(self.root)
        self.max_upload_bytes = max_upload_bytes
        self.temporary_root = contained_path(self.root, "tmp", "dataset_release_imports")
        self.release_root = contained_path(self.root, "releases")

    def _directory(self, import_id: str) -> Path:
        _identifier(import_id, "import ID")
        if not import_id.startswith("imp_"):
            raise ImportFailure("导入编号无效")
        return contained_path(self.temporary_root, import_id)

    @staticmethod
    def _remove(path: Path, base: Path) -> None:
        if not path.exists():
            return
        resolved, boundary = path.resolve(), base.resolve()
        if resolved == boundary or not resolved.is_relative_to(boundary) or path.is_symlink():
            raise ImportFailure("导入清理路径无效", status=409)
        shutil.rmtree(path)

    def _release_paths(self, record: dict) -> tuple[Path, Path]:
        release_id = _identifier(record["target_release_id"], "release ID")
        if not release_id.startswith("rel_"):
            raise ImportFailure("发布恢复编号无效", status=409)
        return (contained_path(self.release_root, "." + release_id + ".importing"),
                contained_path(self.release_root, release_id))

    def _recover_record(self, record: dict) -> dict:
        if record.get("status") == "publishing":
            release = self.records.get("dataset_releases", record["target_release_id"])
            if release:
                record = self.records.update(IMPORTS, record["import_id"], lambda item: item.update(
                    status="published", release_id=release["release_id"]))
            else:
                staging, final = self._release_paths(record)
                self._remove(staging, self.release_root)
                self._remove(final, self.release_root)
                record = self.records.update(IMPORTS, record["import_id"],
                                             lambda item: item.update(status="validated"))
        if record.get("status") == "published":
            self._remove(self._directory(record["import_id"]), self.temporary_root)
        elif _expired(record):
            self._remove(self._directory(record["import_id"]), self.temporary_root)
            if record.get("status") != "expired":
                record = self.records.update(IMPORTS, record["import_id"],
                                             lambda item: item.update(status="expired"))
        return record

    def recover(self) -> None:
        """Only import-owned journals and temporary directories are examined."""
        if not self.records.list(IMPORTS) and not self.temporary_root.exists():
            return
        with batch_lock(self.root, LOCK_ID):
            records = self.records.list(IMPORTS)
            registered = {record["import_id"] for record in records}
            for record in records:
                self._recover_record(record)
            if self.temporary_root.exists():
                deadline = datetime.now(timezone.utc).timestamp() - TTL_HOURS * 3600
                for path in self.temporary_root.iterdir():
                    if (path.name.startswith("imp_") and path.name not in registered
                            and path.is_dir() and path.stat().st_mtime <= deadline):
                        self._remove(path, self.temporary_root)

    def _duplicate(self, record: dict) -> tuple[dict | None, str | None]:
        existing = self.records.get(FILE_INDEX, record["file_key"])
        if existing:
            release = self.registry.get(existing["release_id"])
            if release is None:
                raise ImportFailure("已有导入索引缺少发布记录，请检查存储", status=409)
            if existing["metadata_signature"] != record["metadata_signature"]:
                return release, "同一文件的工作表已发布，数据日期、来源或补充分类与历史记录不同；请查看已有发布。"
            return release, None
        existing = self.records.get(CONTENT_INDEX, record["content_hash"])
        if existing:
            release = self.registry.get(existing["release_id"])
            if release is None:
                raise ImportFailure("已有内容索引缺少发布记录，请检查存储", status=409)
            return release, None
        # A downloaded platform release must not be counted again as a manual batch.
        for release in self.records.list("dataset_releases"):
            if release.get("source_kind") == "external_manual":
                continue
            if any(item.get("sha256") == record["source_sha256"] for item in release.get("excel_paths", [])):
                return self.registry.get(release["release_id"]), "该文件已在平台发布，请查看已有发布，不能再次作为外部数据累计。"
        return None, None

    @staticmethod
    def _duplicate_summary(release: dict | None) -> dict | None:
        return {key: release[key] for key in ("release_id", "name")} if release else None

    def preview(self, filename: str, stream: BinaryIO, **options) -> dict:
        from ..training_data_overview.external_workbook import parse_external_workbook

        try:
            _filename(filename)
        except ValueError as exc:
            raise ImportFailure("上传文件名无效") from exc
        suffix = Path(filename).suffix.lower()
        if suffix not in {".xlsx", ".xlsm"}:
            raise ImportFailure("仅支持 .xlsx 或 .xlsm 完整数据表")
        self.recover()
        import_id = "imp_" + uuid.uuid4().hex
        directory = self._directory(import_id)
        directory.mkdir(parents=True, exist_ok=False)
        source = directory / ("source" + suffix)
        keep = False
        try:
            size = 0
            with source.open("xb") as output:
                while chunk := stream.read(1024 * 1024):
                    size += len(chunk)
                    if size > self.max_upload_bytes:
                        raise ImportFailure("上传文件超过 50 MiB 上限", status=413, code="file_too_large")
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            if not size:
                raise ImportFailure("上传文件为空")
            parsed = parse_external_workbook(source, **options)
            response = {key: deepcopy(parsed[key]) for key in (
                "valid", "sheets", "sheet_name", "summary", "errors", "warnings")}
            response.update(import_id=None, filename=filename, duplicate_release=None)
            if not parsed["valid"]:
                return response
            metadata = parsed["metadata"]
            canonical = {"schema_version": 1, "parser_version": 1, "metadata": metadata,
                         "content_hash": parsed["content_hash"], "rows": parsed["rows"],
                         "warnings": parsed["warnings"]}
            _write_bytes(directory / "canonical.json", _json_bytes(canonical))
            expires = (datetime.now(timezone.utc) + timedelta(hours=TTL_HOURS)).isoformat()
            source_hash = _sha256(source)
            record = {
                "import_id": import_id, "status": "validated", "created_at": utc_now(), "expires_at": expires,
                "filename": filename, "suffix": suffix, "size": size, "metadata": metadata,
                "source_sha256": source_hash, "canonical_sha256": _sha256(directory / "canonical.json"),
                "content_hash": parsed["content_hash"], "metadata_signature": _metadata_signature(metadata),
                "file_key": _hash({"sha256": source_hash, "sheet_name": metadata["sheet_name"]}),
                "summary": parsed["summary"], "warnings": parsed["warnings"],
            }
            with batch_lock(self.root, LOCK_ID):
                duplicate, conflict = self._duplicate(record)
                response["duplicate_release"] = self._duplicate_summary(duplicate)
                if conflict:
                    response["valid"] = False
                    response["errors"].append({"sheet": metadata["sheet_name"], "row": None,
                                               "field": "metadata", "message": conflict})
                    return response
                self.records.put(IMPORTS, import_id, record, expected_revision=0)
            keep = True
            response.update(import_id=import_id, expires_at=expires)
            return response
        finally:
            if not keep:
                self._remove(directory, self.temporary_root)

    def _verify(self, record: dict) -> tuple[Path, Path]:
        directory = self._directory(record["import_id"])
        source, canonical = directory / ("source" + record["suffix"]), directory / "canonical.json"
        for path, digest in ((source, record["source_sha256"]), (canonical, record["canonical_sha256"])):
            if not path.is_file() or not path.resolve().is_relative_to(directory.resolve()) or _sha256(path) != digest:
                raise ImportFailure("上传暂存文件缺失或发生变化，请重新校验", status=409, code="import_changed")
        data = json.loads(canonical.read_text(encoding="utf-8"))
        if data.get("metadata") != record["metadata"] or data.get("content_hash") != record["content_hash"]:
            raise ImportFailure("上传预览来源不一致，请重新校验", status=409, code="import_changed")
        return source, canonical

    def _request_entry(self, request_id: str, fingerprint: str, release_id: str) -> dict:
        return {"namespace": REQUESTS, "key": request_id, "expected_revision": 0,
                "payload": {"request_id": request_id, "fingerprint": fingerprint, "release_id": release_id}}

    def _index_entries(self, record: dict, release_id: str) -> list[dict]:
        entries = []
        for namespace, key in ((FILE_INDEX, record["file_key"]), (CONTENT_INDEX, record["content_hash"])):
            current = self.records.get(namespace, key)
            if current is None:
                entries.append({"namespace": namespace, "key": key, "expected_revision": 0,
                    "payload": {"release_id": release_id, "metadata_signature": record["metadata_signature"],
                                "source_sha256": record["source_sha256"], "content_hash": record["content_hash"]}})
            elif current["release_id"] != release_id:
                raise ImportFailure("导入内容已由另一份发布登记，请重新校验", status=409)
        return entries

    def _reuse(self, record: dict, release: dict, request_id: str, fingerprint: str) -> dict:
        payload = dict(record, status="published", release_id=release["release_id"])
        self.records.put_many([
            {"namespace": IMPORTS, "key": record["import_id"], "payload": payload,
             "expected_revision": record["storage_revision"]},
            self._request_entry(request_id, fingerprint, release["release_id"]),
            *self._index_entries(record, release["release_id"]),
        ])
        self._remove(self._directory(record["import_id"]), self.temporary_root)
        return release

    def publish(self, import_id: str, name: str, request_id: str) -> dict:
        try:
            _identifier(import_id, "import ID")
            _identifier(request_id, "request ID")
        except ValueError as exc:
            raise ImportFailure("导入编号或幂等请求编号无效") from exc
        name = str(name or "").strip()
        if not name or len(name) > 120:
            raise ImportFailure("发布名称必须为 1–120 个字符")
        fingerprint = _hash({"import_id": import_id, "name": name})
        with batch_lock(self.root, LOCK_ID):
            previous = self.records.get(REQUESTS, request_id)
            if previous:
                if previous["fingerprint"] != fingerprint:
                    raise ImportFailure("请求编号已用于另一项发布，请重新提交", status=409, code="idempotency_conflict",
                                        release_id=previous["release_id"])
                release = self.registry.get(previous["release_id"])
                if release is None:
                    raise ImportFailure("发布记录缺失，请检查存储", status=409)
                return release
            record = self.records.get(IMPORTS, import_id)
            if record is None:
                raise ImportFailure("导入预览不存在或已过期，请重新校验", status=410, code="import_expired")
            record = self._recover_record(record)
            if record["status"] == "published":
                release = self.registry.get(record["release_id"])
                if release is None:
                    raise ImportFailure("发布记录缺失，请检查存储", status=409)
                self.records.put_many([self._request_entry(request_id, fingerprint, release["release_id"])])
                return release
            if record["status"] == "expired":
                raise ImportFailure("导入预览已过期，请重新校验", status=410, code="import_expired")
            source, canonical = self._verify(record)
            duplicate, conflict = self._duplicate(record)
            if conflict:
                raise ImportFailure(conflict, status=409, code="duplicate_metadata",
                                    release_id=duplicate["release_id"])
            if duplicate:
                return self._reuse(record, duplicate, request_id, fingerprint)
            release_id = record.get("target_release_id") or "rel_" + uuid.uuid4().hex[:16]
            batch_id = record.get("target_batch_id") or "external_" + uuid.uuid4().hex
            record = self.records.update(IMPORTS, import_id, lambda item: item.update(
                status="publishing", target_release_id=release_id, target_batch_id=batch_id, publish_name=name))
            staging, final = self._release_paths(record)
            with batch_lock(self.root, batch_id):
                try:
                    release = self._prepare(record, source, canonical, staging, final)
                    payload = dict(record, status="published", release_id=release_id)
                    self.records.put_many([
                        {"namespace": "dataset_releases", "key": release_id, "payload": release, "expected_revision": 0},
                        published_entry(batch_id, release_id, release["created_at"], self.root),
                        {"namespace": IMPORTS, "key": import_id, "payload": payload,
                         "expected_revision": record["storage_revision"]},
                        self._request_entry(request_id, fingerprint, release_id),
                        *self._index_entries(record, release_id),
                    ])
                except Exception:
                    # An uncertain commit must never delete registered release files.
                    if self.records.get("dataset_releases", release_id) is None:
                        self._remove(staging, self.release_root)
                        self._remove(final, self.release_root)
                        self.records.update(IMPORTS, import_id, lambda item: item.update(status="validated"))
                    raise
            self._remove(self._directory(import_id), self.temporary_root)
            return self.registry.get(release_id)

    def _prepare(self, record: dict, source: Path, canonical: Path, staging: Path, final: Path) -> dict:
        staging.mkdir(parents=True, exist_ok=False)
        (staging / "001").mkdir()
        raw = staging / "001" / record["filename"]
        shutil.copyfile(source, raw)
        data = staging / "external-data.json"
        shutil.copyfile(canonical, data)
        if _sha256(raw) != record["source_sha256"] or _sha256(data) != record["canonical_sha256"]:
            raise ImportFailure("冻结期间源文件发生变化，请重新校验", status=409)
        # Flush the immutable copies before making the database record visible.
        for path in (raw, data):
            with path.open("rb+") as stream:
                os.fsync(stream.fileno())
        created_at = utc_now()
        release_id, batch_id = record["target_release_id"], record["target_batch_id"]
        raw_ref = {"path": self.registry.project_path(final / "001" / record["filename"]),
                   "sha256": record["source_sha256"]}
        canonical_ref = {"path": self.registry.project_path(final / "external-data.json"),
                         "sha256": record["canonical_sha256"]}
        metadata = record["metadata"]
        manifest = {"schema_version": 1, "parser_version": 1, "release_id": release_id, "batch_id": batch_id,
                    "import_id": record["import_id"], "filename": record["filename"], "sheet_name": metadata["sheet_name"],
                    "metadata": metadata, "source": raw_ref, "canonical": canonical_ref,
                    "content_hash": record["content_hash"], "created_at": created_at}
        _write_bytes(staging / "import-manifest.json", _json_bytes(manifest))
        external = {**metadata, "filename": record["filename"], "import_id": record["import_id"], "parser_version": 1,
                    "source_sha256": record["source_sha256"], "content_hash": record["content_hash"],
                    "canonical": canonical_ref,
                    "manifest": {"path": self.registry.project_path(final / "import-manifest.json"),
                                 "sha256": _sha256(staging / "import-manifest.json")}}
        release = {"release_id": release_id, "name": record["publish_name"], "source_kind": "external_manual",
                   "created_at": created_at, "batch_ids": [batch_id], "external_import": external,
                   "excel_paths": [{**raw_ref, "filename": record["filename"], "rows": record["summary"]["step_count"],
                                   "created_at": created_at,
                                   "data_path": (Path("releases") / release_id / "001" / record["filename"]).as_posix()}],
                   "trajectory_paths": [], "source_count": 1, "task_count": 0,
                   "trajectory_count": record["summary"]["trajectory_count"], "step_count": record["summary"]["step_count"],
                   "source_refs": [{"kind": "external_import", "id": record["import_id"], "batch_id": batch_id}],
                   "upload_status": "not_uploaded", "upload_job_id": None, "upload_error": None,
                   "s3_uri": None, "uploaded_at": None, "uploaded_files": 0, "uploaded_bytes": 0}
        _write_bytes(staging / "manifest.json", _json_bytes(release))
        staging.rename(final)
        return release
