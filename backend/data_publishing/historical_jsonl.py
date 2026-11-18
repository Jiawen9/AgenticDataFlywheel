"""Register completed historical JSONL releases from an operator manifest."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from ..batch_lifecycle import published_entry
from ..data_store import DATA_ROOT, RecordStore
from ..data_store.artifacts import _filename, _json_bytes, _sha256, _write_bytes
from ..data_store.locking import batch_lock
from ..data_store.paths import contained_path
from ..data_store.registry import utc_now
from ..file_io import io_path, is_link_or_junction, resolve_path
from ..identifiers import BATCH_IDENTITIES, allocate_id
from ..training_data_overview.external_jsonl import parse_external_jsonl


VERSION_INDEX = "historical_jsonl_versions"
ADAPTERS = {"data_flywheel", "factory", "health", "middle"}
MANIFEST_FIELDS = {"file", "name", "version", "data_source", "cloud_uri", "adapter", "data_date", "app", "level1", "level2"}


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _text(item: dict, key: str, *, required: bool = False, limit: int = 240) -> str:
    value = item.get(key, "")
    if not isinstance(value, str):
        raise ValueError(f"{key} 必须为文本")
    value = value.strip()
    if (required and not value) or len(value) > limit or any(ord(char) < 32 for char in value):
        raise ValueError(f"{key} 格式无效")
    return value


def _entry(item: dict, directory: Path, registered_day: str) -> dict:
    if not isinstance(item, dict) or set(item) - MANIFEST_FIELDS:
        raise ValueError("清单条目包含未知字段或不是对象")
    path = Path(_text(item, "file", required=True, limit=32767))
    if path.suffix.lower() != ".jsonl":
        raise ValueError("历史训练文件必须是 JSONL")
    filename = _filename(path.name)
    path = resolve_path(directory / path)
    adapter = _text(item, "adapter") or Path(filename).stem.casefold()
    if adapter not in ADAPTERS:
        raise ValueError("adapter 必须为 data_flywheel、factory、health 或 middle")
    name = _text(item, "name", required=True)
    version = _text(item, "version", required=True)
    data_source = _text(item, "data_source", required=True, limit=120)
    cloud_uri = _text(item, "cloud_uri", required=True, limit=4096)
    cloud = urlsplit(cloud_uri)
    if (cloud.scheme.lower() != "s3" or not cloud.netloc or cloud.query or cloud.fragment
            or "@" in cloud.netloc or ":" in cloud.netloc or any(char.isspace() for char in cloud.netloc)):
        raise ValueError("cloud_uri 必须为无认证信息的 s3://bucket/对象或目录")
    requested_date = _text(item, "data_date")
    if requested_date and date.fromisoformat(requested_date).isoformat() != requested_date:
        raise ValueError("data_date 必须为 YYYY-MM-DD")
    metadata = {
        "data_source": data_source, "data_date": requested_date or registered_day,
        "app": _text(item, "app"), "level1": _text(item, "level1"), "level2": _text(item, "level2"),
        "version": version, "cloud_uri": cloud_uri, "adapter": adapter, "date_defaulted": not requested_date,
    }
    parsed = parse_external_jsonl(path, source_format=adapter, **{
        key: metadata[key] for key in ("data_source", "data_date", "app", "level1", "level2")
    })
    registration_signature = _digest({
        "name": name, "filename": filename, "source_sha256": parsed["source_sha256"],
        "metadata": {**metadata, "data_date": requested_date},
    })
    return {"path": path, "filename": filename, "name": f"{name} · {version}",
            "version_key": _digest([data_source, version]), "metadata": metadata, "parsed": parsed,
            "signature": registration_signature}


def _check_existing(records: RecordStore, entry: dict) -> dict | None:
    indexed = records.get(VERSION_INDEX, entry["version_key"])
    if not indexed:
        return None
    release = records.get("dataset_releases", indexed["release_id"])
    if release is None:
        raise ValueError("历史版本索引缺少发布记录，请检查数据目录")
    if indexed["signature"] != entry["signature"]:
        raise ValueError(f"来源与版本已登记，内容或配置不同（发布编号 {release['release_id']}）")
    return release


def _public_report(entry: dict) -> dict:
    parsed, metadata = entry["parsed"], entry["metadata"]
    return {"file": entry["filename"], "name": entry["name"], **metadata,
            "source_sha256": parsed["source_sha256"], "source_size": parsed["source_size"],
            "summary": parsed["summary"], "warnings": parsed["warnings"], "errors": parsed["errors"],
            "valid": parsed["valid"]}


def _copy_source(source: Path, target: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with io_path(source).open("rb") as input_file, io_path(target).open("xb") as output_file:
        for block in iter(lambda: input_file.read(1024 * 1024), b""):
            output_file.write(block)
            digest.update(block)
            size += len(block)
        output_file.flush()
        os.fsync(output_file.fileno())
    return digest.hexdigest(), size


def _freeze(registry, entry: dict, staging: Path, final: Path, batch_id: str) -> dict:
    report = _public_report(entry)
    parsed, metadata = entry["parsed"], entry["metadata"]
    source_folder = Path("training") / "001"
    source_file = source_folder / "source.jsonl"
    report_file = source_folder / "import-report.json"
    io_path(staging / source_folder).mkdir(parents=True)
    if _copy_source(entry["path"], staging / source_file) != (report["source_sha256"], report["source_size"]):
        raise ValueError("原始 JSONL 在校验后发生变化，请重新预检")
    source = {"path": registry.project_path(final / source_file), "sha256": report["source_sha256"]}
    warnings = [*parsed["warnings"], *parsed["errors"]]
    if metadata["date_defaulted"]:
        warnings.append({"row": None, "field": "data_date", "message": "未填写数据日期，使用登记日期"})
    canonical_data = {"schema_version": 1, "parser_version": parsed["parser_version"], "metadata": metadata,
                      "rows": parsed["rows"], "warnings": warnings, "summary": parsed["summary"]}
    _write_bytes(staging / "external-data.json", _json_bytes(canonical_data))
    canonical = {"path": registry.project_path(final / "external-data.json"), "sha256": _sha256(staging / "external-data.json")}
    manifest = {"schema_version": 1, "parser_version": parsed["parser_version"], "metadata": metadata,
                "release_id": final.name, "batch_id": batch_id, "filename": entry["filename"],
                "source": source, "canonical": canonical}
    _write_bytes(staging / "import-manifest.json", _json_bytes(manifest))
    _write_bytes(staging / report_file, _json_bytes({**report, "warnings": warnings, "release_id": final.name}))
    summary = parsed["summary"]
    training = {**source, "index": 0, "batch_id": batch_id, "filename": entry["filename"],
                "size": report["source_size"], "status": "ready", "available": True,
                "included_count": summary["step_count"], "excluded_count": summary["excluded_rows"],
                "report_path": registry.project_path(final / report_file), "report_filename": "import-report.json",
                "report_sha256": _sha256(staging / report_file)}
    release = {
        "release_id": final.name, "name": entry["name"], "source_kind": "external_manual", "created_at": utc_now(),
        "batch_ids": [batch_id], "excel_paths": [], "trajectory_paths": [], "training_files": [training],
        "source_count": 1, "task_count": 0, "trajectory_count": summary["trajectory_count"],
        "step_count": summary["step_count"], "source_refs": [{"kind": "historical_jsonl", "batch_id": batch_id}],
        "external_import": {**metadata, "format": "jsonl", "filename": entry["filename"], "sheet_name": "JSONL",
                            "parser_version": parsed["parser_version"], "source_sha256": source["sha256"],
                            "source": source, "canonical": canonical,
                            "registration_signature": entry["signature"],
                            "manifest": {"path": registry.project_path(final / "import-manifest.json"),
                                         "sha256": _sha256(staging / "import-manifest.json")}},
        "upload_status": "not_uploaded", "upload_job_id": None, "upload_error": None, "s3_uri": None,
        "uploaded_at": None, "uploaded_files": 0, "uploaded_bytes": 0,
    }
    _write_bytes(staging / "manifest.json", _json_bytes(release))
    return release


def _recover_prepared(registry, final: Path, entry: dict, batch_id: str) -> dict:
    """Complete an interrupted registration of this version using its frozen files."""
    try:
        frozen = json.loads(io_path(final / "manifest.json").read_text(encoding="utf-8"))
        imported = frozen["external_import"]
        if (frozen["release_id"] != final.name or frozen["batch_ids"] != [batch_id]
                or imported["format"] != "jsonl" or imported["registration_signature"] != entry["signature"]
                or imported["source"]["sha256"] != entry["parsed"]["source_sha256"]):
            raise ValueError("已冻结的历史训练版本与本次配置不同")
        for name in ("source", "canonical", "manifest"):
            ref = imported[name]
            path = registry.resolve_project_path(ref["path"])
            if not path.is_relative_to(final) or _sha256(path) != ref["sha256"]:
                raise ValueError("已冻结的历史训练文件校验失败")
        training = frozen["training_files"][0]
        if any(training[key] != value for key, value in imported["source"].items()):
            raise ValueError("训练文件与发布来源不一致")
        report = registry.resolve_project_path(training["report_path"])
        if not report.is_relative_to(final) or _sha256(report) != training["report_sha256"]:
            raise ValueError("已冻结的历史训练导入报告校验失败")
        return frozen
    except (TypeError, KeyError, IndexError, OSError) as exc:
        raise ValueError("已冻结的历史训练发布资料不完整，请检查数据目录") from exc


def _register(registry, records: RecordStore, entry: dict) -> tuple[dict, str]:
    root, key = records.root, entry["version_key"]
    with batch_lock(root, "historical-jsonl-" + key):
        existing = _check_existing(records, entry)
        if existing is not None:
            registry.training_file(existing["release_id"], 0)
            return existing, "existing"
        release_id = allocate_id("rel", root, key=[VERSION_INDEX, "release", key])
        batch_id = allocate_id("b", root, key=[VERSION_INDEX, "batch", key])
        if is_link_or_junction(root / "releases" / release_id):
            raise ValueError("历史发布目录不能为链接")
        final = contained_path(root, "releases", release_id)
        parent = final.parent
        io_path(parent).mkdir(parents=True, exist_ok=True)
        if io_path(final).exists():
            release = _recover_prepared(registry, final, entry, batch_id)
        else:
            with TemporaryDirectory(prefix=".jsonl-", dir=io_path(parent)) as temporary:
                staging = resolve_path(temporary) / "release"
                release = _freeze(registry, entry, staging, final, batch_id)
                io_path(staging).rename(io_path(final))
        identity = records.get(BATCH_IDENTITIES, batch_id)
        records.put_many([
            {"namespace": "dataset_releases", "key": release_id, "payload": release, "expected_revision": 0},
            published_entry(batch_id, release_id, release["created_at"], root),
            {"namespace": VERSION_INDEX, "key": key,
             "payload": {"release_id": release_id, "signature": entry["signature"]}, "expected_revision": 0},
            {"namespace": BATCH_IDENTITIES, "key": batch_id,
             "payload": {**identity, "source_kind": "external_manual"}, "expected_revision": identity["storage_revision"]},
        ])
        return release, "registered"


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        return None


def _overview_endpoint(root: Path, url: str):
    base = urlsplit(url)
    if (base.scheme != "http" or base.hostname not in {"127.0.0.1", "::1", "localhost"}
            or base.username is not None or base.path not in {"", "/"} or base.query or base.fragment):
        raise ValueError("--api-url 必须为本机后端地址，例如 http://127.0.0.1:8765")
    url = url.rstrip("/")
    opener = build_opener(ProxyHandler({}), _NoRedirect)
    try:
        with opener.open(url + "/api/data-storage", timeout=5) as response:
            data = json.load(response)
    except HTTPError as exc:
        raise ValueError(f"本机地址未返回平台数据目录（HTTP {exc.code}），请核对 --api-url") from exc
    except (URLError, TimeoutError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("data_root"), str):
        raise ValueError("本机地址未返回平台数据目录，请核对 --api-url")
    if resolve_path(data["data_root"]) != root:
        raise ValueError("CLI 数据目录与运行中的后端不同，请核对 ADF_DATA_ROOT 和 --api-url")
    return opener, url


def _queue_overview(endpoint, release_id: str) -> str:
    if endpoint is None:
        return "pending_server"
    opener, url = endpoint
    request = Request(url + "/api/training-data-overview/releases/" + quote(release_id, safe="") + "/retry", method="POST")
    try:
        with opener.open(request, timeout=10) as response:
            conversion = json.load(response).get("conversion", {})
            return conversion.get("status") or "queued"
    except (URLError, TimeoutError, ValueError, AttributeError):
        return "pending_retry"


def run_import(manifest_path: Path, *, apply: bool = False, api_url: str = "http://127.0.0.1:8765",
               data_root: Path = DATA_ROOT, registered_day: str | None = None) -> dict:
    """Parse read-only first, then independently commit the verified versions."""
    manifest_path = resolve_path(manifest_path)
    manifest = json.loads(io_path(manifest_path).read_text(encoding="utf-8-sig"))
    items = manifest.get("datasets") if isinstance(manifest, dict) else None
    if not isinstance(items, list) or not items:
        raise ValueError("清单需要非空的 datasets 数组")
    root = resolve_path(data_root)
    records = RecordStore(root)
    registered_day = registered_day or datetime.now(timezone(timedelta(hours=8))).date().isoformat()
    reports, prepared, seen = [], [], set()
    for number, item in enumerate(items, 1):
        report = {"entry": number}
        try:
            entry = _entry(item, manifest_path.parent, registered_day)
            report.update(_public_report(entry))
            key = entry["version_key"]
            if key in seen:
                raise ValueError("清单内重复指定同一数据来源及版本")
            seen.add(key)
            if report["valid"]:
                existing = _check_existing(records, entry)
                if existing:
                    report["release_id"] = existing["release_id"]
                    for field in ("data_date", "date_defaulted"):
                        report[field] = existing["external_import"][field]
                prepared.append((entry, report))
        except (OSError, ValueError, sqlite3.Error) as exc:
            report["valid"] = False
            report.setdefault("errors", []).append({"message": str(exc)})
        reports.append(report)
    result = {"mode": "apply" if apply else "preview", "data_root": str(root), "ok": all(row["valid"] for row in reports),
              "datasets": reports}
    if not apply or not result["ok"]:
        return result
    endpoint = _overview_endpoint(root, api_url)
    from .service import DatasetReleaseRegistry

    registry = DatasetReleaseRegistry(data_root=root, releases_file=root / "system" / "dataset_release" / "releases.json")
    for entry, report in prepared:
        try:
            release, registration = _register(registry, records, entry)
            report.update(release_id=release["release_id"], registration=registration,
                          overview=_queue_overview(endpoint, release["release_id"]))
            for field in ("data_date", "date_defaulted"):
                report[field] = release["external_import"][field]
        except (OSError, ValueError, sqlite3.Error) as exc:
            result["ok"] = report["valid"] = False
            report["errors"].append({"message": str(exc)})
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="预检或登记内网历史训练 JSONL")
    parser.add_argument("manifest", type=Path, help="历史训练 JSON 清单")
    parser.add_argument("--apply", action="store_true", help="明确登记历史发布并安排训练数据汇总")
    parser.add_argument("--api-url", default="http://127.0.0.1:8765", help="运行中的本机平台后端地址")
    options = parser.parse_args(argv)
    try:
        report = run_import(options.manifest, apply=options.apply, api_url=options.api_url)
    except (OSError, ValueError, sqlite3.Error) as exc:
        report = {"ok": False, "errors": [{"message": str(exc)}]}
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
