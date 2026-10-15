"""Read or bundle one Pipeline's diagnostic history without running business code.

Only --bundle writes files (a unique ZIP beneath logs/diagnostic_bundles). Missing
historical logs are reported, never reconstructed as if they had been observed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import uuid
import zipfile
from datetime import datetime, timezone

sys.dont_write_bytecode = True
REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from backend.file_io import io_path, is_link_or_junction, logical_path
from backend.pipeline_diagnostics import sanitize, sanitize_partial

STAGES = {"collection": "采集与回传", "preprocessing": "预处理与标框", "tree": "轨迹树构建",
          "quality": "轨迹质检", "correction": "人工修正", "cot": "COT 生成",
          "publication": "数据发布", "overview": "看板汇总"}
JOB_NAMESPACES = {"collection": "collection_runs", "preprocessing": "preprocessing_jobs",
                  "tree": "tree_jobs", "quality": "quality_jobs", "cot": "correction_cot_jobs"}
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,191}\Z")
TERMINAL = {"succeeded", "completed", "failed", "terminated", "cancelled", "interrupted", "no_publishable_data"}


def identifier(value: str) -> str:
    if (not IDENTIFIER.fullmatch(value) or value in {".", ".."} or value.endswith(".")
            or re.fullmatch(r"(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", value, re.I)):
        raise ValueError("无效的诊断编号")
    return value


def safe_path(root: Path, *parts: str) -> Path:
    """Check lexical bounds and every ancestor before accessing a file/link."""
    root = Path(os.path.abspath(logical_path(root)))
    path = root.joinpath(*parts)
    if not path.is_relative_to(root) or ".." in path.parts:
        raise ValueError("拒绝读取诊断目录以外的路径")
    for candidate in [*reversed(path.parents), path]:
        if is_link_or_junction(candidate):
            raise ValueError("拒绝读取链接或目录联接")
    return path


def now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def encoded(value) -> bytes:
    return (json.dumps(sanitize(value), ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")


def read_records(root: Path, pipeline_id: str) -> tuple[dict, list[dict], list[str]]:
    """SQLite snapshot; no RecordStore initialization, recovery or latest-job lookup."""
    database = safe_path(root, "system", "app.sqlite")
    if not io_path(database).is_file():
        return {}, [], ["数据库不存在；仅使用已有诊断日志"]
    rows, issues = [], []
    connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=10, isolation_level=None)
    try:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='records'").fetchone():
            return {}, [], ["数据库没有 records 表"]
        seen = set()

        def get(namespace, key, *, required=False):
            if not key or (namespace, key) in seen:
                return None
            seen.add((namespace, key))
            row = connection.execute("SELECT payload FROM records WHERE namespace=? AND record_key=?",
                                     (namespace, str(key))).fetchone()
            if row is None:
                if required:
                    issues.append(f"关联记录缺失：{namespace}/{key}")
                return None
            try:
                value = json.loads(row[0])
            except (ValueError, TypeError):
                issues.append(f"关联记录无法解析：{namespace}/{key}")
                return None
            rows.append({"namespace": namespace, "record_key": key, "payload": sanitize(value)})
            return value

        pipeline = get("pipelines", pipeline_id, required=True) or {}
        if not isinstance(pipeline, dict):
            return {}, rows, [*issues, "Pipeline 登记记录不是对象，无法读取关联"]
        for step in pipeline.get("steps", []):
            namespace = JOB_NAMESPACES.get(step.get("id"))
            if not namespace:
                continue
            ids = list(step.get("job_ids") or []) + list(step.get("previous_job_ids") or [])
            if step.get("retry_job_id"):
                ids.append(step["retry_job_id"])
            for job_id in dict.fromkeys(ids):
                get(namespace, job_id, required=True)
                if namespace == "collection_runs":
                    get("collection_transfers", job_id)
                if namespace == "quality_jobs":
                    get("quality_executions", job_id)
        for run_id in pipeline.get("collection_run_ids") or []:
            get("collection_runs", run_id, required=True)
            get("collection_transfers", run_id)
        get("correction_sessions", pipeline.get("session_id"), required=True)
        get("dataset_releases", pipeline.get("release_id"), required=True)
        get("training_overview_conversions", pipeline.get("release_id"))
        intent = get("release_intents", "pipeline:" + pipeline_id)
        if isinstance(intent, dict) and intent.get("pipeline_id") == pipeline_id:
            get("dataset_releases", intent.get("release_id"))
            get("training_overview_conversions", intent.get("release_id"))
        return sanitize(pipeline), rows, issues
    finally:
        connection.close()


def read_files(root: Path, pipeline_id: str) -> tuple[dict[str, bytes], list[dict], list[str]]:
    """Read only allowlisted text logs, each bounded by its initial file size."""
    base = safe_path(root, "logs", "pipelines", pipeline_id)
    files, observations, issues = {}, [], []
    if not io_path(base).is_dir():
        return files, observations, ["历史明细缺失：该 Pipeline 尚无统一日志目录"]
    pending = [base]
    while pending:
        directory = pending.pop()
        try:
            children = list(io_path(directory).iterdir())
        except OSError as exc:
            issues.append(f"目录读取失败：{directory.relative_to(base).as_posix()} ({type(exc).__name__})")
            continue
        for physical in children:
            child = logical_path(physical)
            relative = child.relative_to(base).as_posix()
            try:
                safe_path(base, relative)
                if child.parent == base and child.name not in {"index.json", "events", "jobs", "requests"}:
                    continue
                if io_path(child).is_dir():
                    pending.append(child)
                    continue
                if child.suffix not in {".json", ".jsonl", ".txt", ".log"}:
                    continue
                with io_path(child).open("rb") as handle:
                    before = os.fstat(handle.fileno())
                    raw = handle.read(before.st_size)
                    after = os.fstat(handle.fileno())
                changed = (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns)
                partial = changed or len(raw) != before.st_size
                text = raw.decode("utf-8", errors="replace")
                if child.suffix == ".jsonl":
                    lines = []
                    for position, line in enumerate(text.splitlines()):
                        if not line.strip():
                            continue
                        try:
                            lines.append(json.dumps(sanitize(json.loads(line)), ensure_ascii=False))
                        except (ValueError, TypeError):
                            partial = True
                            issues.append(f"跳过未完成或损坏的日志行：{relative}:{position + 1}")
                    data = (("\n".join(lines) + "\n") if lines else "").encode("utf-8")
                elif child.suffix == ".json":
                    try:
                        data = encoded(json.loads(text))
                    except (ValueError, TypeError):
                        partial = True
                        # Incomplete JSON may hide a credential split across fields.
                        data = encoded({"incomplete_or_invalid_json": True, "captured_bytes": len(raw)})
                        issues.append(f"JSON 尚未完成或无法解析：{relative}")
                else:
                    data = (sanitize_partial(text) if partial else sanitize(text)).encode("utf-8")
                files[relative] = data
                observations.append({"path": relative, "captured_at": now(), "source_size_bytes": before.st_size,
                                     "captured_bytes": len(raw), "partial": partial,
                                     "source_changed_during_read": changed, "export_sanitized": True})
            except (OSError, ValueError) as exc:
                issues.append(f"未读取文件：{relative} ({type(exc).__name__})")
    return files, observations, issues


def context_of(value: dict) -> dict:
    context = value.get("context")
    return {**(context if isinstance(context, dict) else {}), **{k: v for k, v in value.items() if k != "context"}}


def matches(value: dict, filters: dict, job_stages: dict | None = None) -> bool:
    value = context_of(value)
    stage = value.get("step_id") or value.get("stage") or (job_stages or {}).get(value.get("job_id"))
    requests = {str(value.get(k)) for k in ("logical_id", "logical_request_id", "request_id", "http_attempt_id", "attempt_id") if value.get(k)}
    return (not filters.get("stage") or stage == filters["stage"]) and (
        not filters.get("job_id") or value.get("job_id") == filters["job_id"]) and (
        not filters.get("request_id") or filters["request_id"] in requests)


def collect(root: Path, pipeline_id: str, *, stage=None, job_id=None, request_id=None) -> dict:
    identifier(pipeline_id)
    for value in (job_id, request_id):
        if value:
            identifier(value)
    root = safe_path(Path(root))
    captured_at = now()
    files, observations, issues = read_files(root, pipeline_id)
    try:
        pipeline, records, db_issues = read_records(root, pipeline_id)
        issues += db_issues
    except (sqlite3.Error, OSError, ValueError) as exc:
        pipeline, records = {}, []
        issues.append(f"SQLite 只读快照不可用：{type(exc).__name__}")
    index = json.loads(files.get("index.json", b"{}"))
    if not isinstance(index, dict):
        index = {}
        issues.append("日志索引不是对象，无法读取索引信息")
    if not pipeline:
        pipeline = index.get("pipeline") or {}
    if not isinstance(pipeline, dict):
        pipeline = {}
    if not pipeline and not files:
        raise ValueError("没有找到该 Pipeline 的登记记录或统一日志；未创建任何数据目录")
    job_stages = {}
    for item in pipeline.get("steps", []):
        ids = list(item.get("job_ids") or []) + list(item.get("previous_job_ids") or [])
        if item.get("retry_job_id"):
            ids.append(item["retry_job_id"])
        for current_id in ids:
            job_stages[current_id] = item.get("id")
    for current_id in pipeline.get("collection_run_ids") or []:
        job_stages[current_id] = "collection"
    if pipeline.get("release_id"):
        job_stages[pipeline["release_id"]] = "overview"
    filters = {"stage": stage, "job_id": job_id, "request_id": request_id}
    events = []
    request_contexts = {}
    for path, data in files.items():
        if path.startswith("events/") and path.endswith(".jsonl"):
            for line in data.decode("utf-8").splitlines():
                value = json.loads(line)
                if isinstance(value, dict) and matches(value, filters, job_stages):
                    events.append(value)
        elif path.startswith("requests/") and path.rsplit("/", 1)[-1] in {"call.json", "attempt.json"}:
            value = json.loads(data)
            if isinstance(value, dict):
                parts = path.split("/")
                value = context_of(value)
                value.setdefault("request_id", parts[1])
                if len(parts) == 5 and parts[2] == "attempts":
                    value.setdefault("attempt_id", parts[3])
                request_contexts["/".join(parts[:-1])] = value
    # HTTP attempt files can inherit the logical call's trusted association.
    for path, value in request_contexts.items():
        parent = request_contexts.get("/".join(path.split("/")[:2]), {})
        request_contexts[path] = {**parent, **value}
    events.sort(key=lambda e: (str(e.get("at", "")), str(e.get("writer_id", "")), e.get("sequence", 0)))
    if any(filters.values()):
        filtered = {"index.json": files["index.json"]} if "index.json" in files else {}
        for path, data in files.items():
            parts = path.split("/")
            if parts[0] == "events":
                values = [json.loads(line) for line in data.decode().splitlines()]
                values = [v for v in values if isinstance(v, dict) and matches(v, filters, job_stages)]
                if values:
                    filtered[path] = b"".join((json.dumps(v, ensure_ascii=False) + "\n").encode() for v in values)
            elif parts[0] == "jobs" and len(parts) > 2 and not request_id:
                if matches({"job_id": parts[1]}, filters, job_stages):
                    filtered[path] = data
            elif parts[0] == "requests" and len(parts) > 2:
                value = dict(request_contexts.get("/".join(parts[:-1])) or request_contexts.get("/".join(parts[:2])) or {})
                value.setdefault("request_id", parts[1])
                if len(parts) > 4 and parts[2] == "attempts":
                    value.setdefault("attempt_id", parts[3])
                if matches(value, filters, job_stages):
                    filtered[path] = data
        files = filtered
        observations = [item for item in observations if item["path"] in files]
    requests = [{"path": path, **value} for path, value in request_contexts.items()
                if matches(value, filters, job_stages)]
    if any(filters.values()):
        # Keep Pipeline identity; other records must be within selected associations.
        records = [record for record in records if record["namespace"] == "pipelines" or (
            not request_id and matches({"job_id": record["record_key"],
                                       "step_id": next((s for s, ns in JOB_NAMESPACES.items() if ns == record["namespace"]),
                                                       {"correction_sessions": "correction", "dataset_releases": "publication", "release_intents": "publication", "collection_transfers": "collection", "training_overview_conversions": "overview"}.get(record["namespace"]))},
                                      filters, job_stages))]
    return {"schema_version": 1, "pipeline_id": pipeline_id, "captured_at": captured_at,
            "snapshot_finished_at": now(), "coverage": "snapshot_only" if not index else index.get("coverage", "recorded"),
            "pipeline": pipeline, "runtime": index.get("runtime", {}), "records": records, "events": events,
            "requests": requests, "files": files, "file_observations": observations, "issues": sanitize(issues),
            "filters": filters, "active": bool(pipeline and pipeline.get("status") not in TERMINAL),
            "snapshot_note": "各文件按初始长度截取，数据库为独立只读事务快照；不是跨文件全局一致快照"}


def brief_error(error) -> str:
    if isinstance(error, dict):
        error = {key: error[key] for key in ("type", "category", "message", "code", "http_status", "request_id") if error.get(key) is not None}
        text = json.dumps(error, ensure_ascii=False)
    else:
        text = str(error)
    text = sanitize(text).replace("\r", " ").replace("\n", " ")
    return text[:1000] + ("…（完整信息见日志）" if len(text) > 1000 else "")


def summary(report: dict) -> str:
    pipeline = report["pipeline"]
    lines = [f"Pipeline：{report['pipeline_id']}", f"批次：{pipeline.get('batch_id') or '未知'}",
             f"状态：{pipeline.get('status') or '未知'}；当前阶段：{STAGES.get(pipeline.get('current_step'), pipeline.get('current_step') or '未知')}",
             f"截取时间：{report['captured_at']}", f"记录覆盖：{report['coverage']}", report["snapshot_note"]]
    if report["active"]:
        lines.append("运行中快照：本次读取不会暂停作业，未完成请求可能在截取后继续更新。")
    for step in pipeline.get("steps", []):
        if report["filters"].get("stage") and step.get("id") != report["filters"]["stage"]:
            continue
        ids = list(dict.fromkeys(list(step.get("previous_job_ids") or []) + list(step.get("job_ids") or [])))
        if step.get("retry_job_id") and step["retry_job_id"] not in ids:
            ids.append(step["retry_job_id"])
        if report["filters"].get("job_id") and report["filters"]["job_id"] not in ids:
            continue
        retry = step.get("retry_info") or {}
        lines.append(f"- {step.get('label') or STAGES.get(step.get('id'), step.get('id'))}：{step.get('status', '未知')}；作业：{', '.join(ids) or '尚未绑定/复用已有产物'}")
        if retry.get("attempts"):
            lines.append(f"  尝试：{retry['attempts']}/{retry.get('max_attempts', '未知')}；下次执行：{retry.get('next_retry_at') or '无'}")
        if step.get("error"):
            lines.append("  错误：" + brief_error(step["error"]))
    failures = [e for e in report["events"] if e.get("failure") or e.get("error") or e.get("exception") or e.get("status") in {"failed", "timeout"} or any(x in str(e.get("event", "")) for x in ("failed", "exception", "timeout"))]
    lines.append(f"事件数：{len(report['events'])}；失败/异常事件数：{len(failures)}")
    for event in failures:
        error = event.get("failure") or event.get("error") or event.get("exception") or event.get("message") or event.get("event")
        lines.append(f"- {event.get('at', '')} 作业 {event.get('job_id', '-')}：{brief_error(error)}")
    for record in report["records"]:
        value = record["payload"]
        if record["namespace"] in set(JOB_NAMESPACES.values()) and isinstance(value, dict):
            lines.append(f"登记作业 {record['record_key']}：{value.get('status', '未知')}")
            if value.get("failure") or value.get("error"):
                lines.append("  失败记录：" + brief_error(value.get("failure") or value["error"]))
    for request in report["requests"]:
        elapsed = request.get("elapsed_ms")
        if elapsed is None and isinstance(request.get("elapsed_seconds"), (int, float)):
            elapsed = request["elapsed_seconds"] * 1000
        status = request.get("status") or request.get("outcome") or "未知/未完成"
        lines.append(f"请求 {request['path']}：{status}；耗时 {elapsed if elapsed is not None else '未知'} ms" + ("（慢请求 ≥30秒）" if isinstance(elapsed, (int, float)) and elapsed >= 30000 else ""))
        if request.get("exception") or request.get("failure"):
            lines.append("  失败：" + brief_error(request.get("exception") or request["failure"]))
        if request.get("body_capture_gap"):
            lines.append("  正文证据缺口：" + brief_error(request["body_capture_gap"]))
        if "/attempts/" in request["path"]:
            lines.append(f"  响应头耗时：{request.get('headers_elapsed_ms') if request.get('headers_elapsed_ms') is not None else '未知'} ms；"
                         f"首段正文耗时：{request.get('first_body_elapsed_ms') if request.get('first_body_elapsed_ms') is not None else '未知'} ms")
            if not request.get("response_headers_received"):
                lines.append("  观测位置：未收到响应头" + ("；已失败/结束" if request.get("failure") or request.get("exception") or request.get("finished_at") else "；可能仍在等待，请核对作业状态"))
            elif not request.get("response_body_complete"):
                lines.append("  观测位置：已收到响应头，正文接收尚未完成")
            else:
                lines.append("  观测位置：正文接收完成；解析/校验结果见逻辑请求记录")
        else:
            lines.append("  解析/校验结束时间：" + str(request.get("validation_finished_at") or "未知/未完成"))
    lines.append("证据边界：远端排队时间和模型内部推理时间未提供时均为未知；等待响应头不等于模型推理耗时。")
    if report["coverage"] == "snapshot_only":
        lines.append("历史明细缺失：只能导出现存登记记录，历史请求正文和已丢弃异常无法补回。")
    for issue in report["issues"]:
        lines.append("注意：" + issue)
    for observation in report["file_observations"]:
        if observation["partial"]:
            lines.append("未完成/变化文件：" + observation["path"])
    return sanitize("\n".join(lines) + "\n")


def bundle(root: Path, report: dict) -> Path:
    target = safe_path(root, "logs", "diagnostic_bundles")
    io_path(target).mkdir(parents=True, exist_ok=True)
    name = f"{identifier(report['pipeline_id'])}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}.zip"
    path = safe_path(root, "logs", "diagnostic_bundles", name)
    contents = {"诊断摘要.txt": summary(report).encode("utf-8"),
                "snapshot.json": encoded({k: v for k, v in report.items() if k not in {"files", "events", "records"}}),
                "records.json": encoded(report["records"]), "timeline.json": encoded(report["events"])}
    contents.update({"logs/" + name: data for name, data in report["files"].items()})
    manifest = {"schema_version": 1, "pipeline_id": report["pipeline_id"], "captured_at": report["captured_at"],
                "files": [{"path": key, "size_bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
                          for key, data in sorted(contents.items())]}
    contents["manifest.json"] = encoded(manifest)
    try:
        with io_path(path).open("xb") as output, zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for key, data in contents.items():
                archive.writestr(key, data)
    except BaseException:
        io_path(path).unlink(missing_ok=True)
        raise
    return path


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="只读查看 Pipeline 统一诊断；仅 --bundle 写入独立 ZIP，不调用模型或恢复作业")
    parser.add_argument("--pipeline-id", required=True)
    parser.add_argument("--data-root", type=Path, default=Path(os.environ.get("ADF_DATA_ROOT") or REPO / "backend_workspace"))
    parser.add_argument("--timeline", action="store_true")
    parser.add_argument("--stage", choices=list(STAGES))
    parser.add_argument("--job-id")
    parser.add_argument("--request-id")
    parser.add_argument("--bundle", action="store_true")
    args = parser.parse_args(argv)
    try:
        report = collect(args.data_root, args.pipeline_id, stage=args.stage, job_id=args.job_id, request_id=args.request_id)
        print(summary(report), end="")
        if args.timeline:
            for event in report["events"]:
                print(json.dumps(event, ensure_ascii=False))
        if args.bundle:
            print("诊断包：" + str(bundle(args.data_root, report)))
        return 0
    except (ValueError, OSError, sqlite3.Error) as exc:
        print("诊断失败：" + sanitize(str(exc)), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
