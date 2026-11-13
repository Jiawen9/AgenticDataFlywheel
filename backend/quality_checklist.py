"""Platform bridge to the existing collector Checklist evaluator (no local fork)."""
from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import sys
import time
from pathlib import Path

from .data_store import RecordStore
from .file_io import io_path, logical_path, resolve_path
from .quality_fusion import SCORING_VERSION

TERMINAL = {"complete", "completed", "succeeded", "partial", "failed", "cancelled", "error"}


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _store(root):
    from .phone_factory import PhoneFactoryStore
    return PhoneFactoryStore(root)


def settings(root, *, store=None):
    from .model_iteration import get_profiles
    store = store or _store(root)
    saved = store.records.get("quality_settings", "checklist") or {}
    capability = store._remote_result(["capabilities"])
    supported = capability.get("quality_checklist_jobs", {}).get("version") == 1
    profiles = get_profiles(store)["profiles"] if supported else []
    selected = next((p for p in profiles if p["id"] == saved.get("evaluation_profile_id") and p["digest"] == saved.get("evaluation_profile_digest")), None)
    if not saved and len(profiles) == 1:
        selected = profiles[0]
    return {"evaluation_profile_id": selected["id"] if selected else "",
            "evaluation_profile_digest": selected["digest"] if selected else "",
            "profiles": profiles, "supported": supported,
            "reason": "" if supported else "请先升级采集端的质检 Checklist 能力"}


def save_settings(root, value, *, store=None):
    from .phone_factory import PhoneFactoryError
    if not isinstance(value, dict) or set(value) != {"evaluation_profile_id", "evaluation_profile_digest"}:
        raise PhoneFactoryError("质检设置只接受评测规则版本及摘要")
    store = store or _store(root)
    current = settings(root, store=store)
    if not any(p["id"] == value["evaluation_profile_id"] and p["digest"] == value["evaluation_profile_digest"] for p in current["profiles"]):
        raise PhoneFactoryError("规则版本不存在或已变化，请刷新后选择", 409)
    store.records.put("quality_settings", "checklist", dict(value))
    return {**current, **value}


def freeze_options(root, *, store=None):
    from .model_iteration import get_config, normalize_config
    from .evaluation_job_previews import endpoint
    from .phone_factory import PhoneFactoryError
    store = store or _store(root)
    current = settings(root, store=store)
    if not current["supported"]:
        raise PhoneFactoryError(current["reason"], 409)
    if not current["evaluation_profile_id"]:
        raise PhoneFactoryError("请先在轨迹质检中选择 Checklist 规则版本", 409)
    return {"version": 1, "scoring_version": SCORING_VERSION,
            "collector_server": endpoint(), "evaluator_config": normalize_config(get_config(store), required=True),
            "evaluation_profile_id": current["evaluation_profile_id"],
            "evaluation_profile_digest": current["evaluation_profile_digest"]}


def _source_error(code, task_id, trajectory_id, message):
    return ValueError(f"[{code}] 质检任务 {task_id} / 轨迹 {trajectory_id}：{message}")


def _source_directory(task_id, trajectory, raw_root):
    """Resolve the frozen source only; never guess from cwd or change metadata."""
    ident = trajectory.trajectory_id
    metadata = trajectory.metadata or {}
    directory = metadata.get("source_directory")
    if not isinstance(directory, str) or not directory.strip():
        raise _source_error("source_directory_missing", task_id, ident, "冻结质检输入缺少原件目录")
    source = metadata.get("source_identity") or {}
    # source_identity.task_id may be the original case ID before platform
    # numbering, so compare the quality task itself, not that source alias.
    if ((getattr(trajectory, "task_id", None) and str(trajectory.task_id) != str(task_id))
            or (source.get("trajectory_id") and str(source["trajectory_id"]) != str(ident))):
        raise _source_error("source_identity_mismatch", task_id, ident, "原件身份与冻结任务或轨迹不一致")
    try:
        if "\x00" in directory:
            raise ValueError("原件目录包含无效字符")
        path = logical_path(directory)
        root = None
        if raw_root is not None:
            root = logical_path(raw_root)
            if not root.is_absolute():
                raise ValueError("冻结原件根目录不是绝对路径")
            root = resolve_path(root)
        if not path.is_absolute():
            if path.anchor:
                raise ValueError("原件目录不是完整的绝对路径或相对路径")
            if root is None:
                raise _source_error("source_root_missing", task_id, ident,
                                    "原件目录为相对路径，但冻结输入没有登记原件根目录")
            path = root / path
        path = resolve_path(path)
        if root is not None and not path.is_relative_to(root):
            raise ValueError("原件目录超出冻结输入登记的根目录")
    except OSError as exc:
        raise _source_error("source_directory_unreadable", task_id, ident,
                            f"原件目录无法解析或访问：{exc}") from exc
    except (TypeError, ValueError) as exc:
        if str(exc).startswith("[source_root_missing]"):
            raise
        raise _source_error("source_directory_invalid", task_id, ident, str(exc)) from exc
    try:
        if not stat.S_ISDIR(io_path(path).stat().st_mode):
            raise _source_error("source_directory_invalid", task_id, ident, f"原件路径不是目录：{path}")
        # Read one entry at most, solely to distinguish an unreadable directory.
        with os.scandir(io_path(path)) as entries:
            next(entries, None)
    except FileNotFoundError as exc:
        raise _source_error("source_directory_not_found", task_id, ident, f"原件目录不存在：{path}") from exc
    except OSError as exc:
        raise _source_error("source_directory_unreadable", task_id, ident,
                            f"原件目录不可读：{path}；{exc}") from exc
    return path


def build_task(task_id, task, trajectories, *, raw_root=None):
    """Map frozen quality identities to original complete trajectory directories."""
    rows = []
    case_ids = set()
    key_elements = set()
    for trajectory in trajectories:
        metadata = trajectory.metadata or {}
        directory = _source_directory(task_id, trajectory, raw_root)
        source = metadata.get("source_identity") or {}
        case_ids.add(str(source.get("case_id") or source.get("task_id") or task_id))
        elements = metadata.get("key_elements") or (getattr(task, "context", {}) or {}).get("key_elements")
        if not elements:
            original = io_path(directory / "_trajectory_for_evaluate.json")
            try:
                original_value = json.loads(original.read_text(encoding="utf-8-sig"))
            except FileNotFoundError:
                original_value = {}
            except (OSError, ValueError) as exc:
                raise _source_error("source_metadata_unreadable", task_id, trajectory.trajectory_id,
                                    f"原件元数据无法读取：{original.name}；{exc}") from exc
            elements = original_value.get("key_elements")
        key_elements.add(elements if isinstance(elements, str) else
                         json.dumps(elements, ensure_ascii=False, sort_keys=True) if elements else "")
        rows.append({"trajectory_id": trajectory.trajectory_id, "source_directory": str(directory)})
    if len(case_ids) != 1 or len(key_elements) != 1:
        raise ValueError("同任务轨迹的原用例或验收条件不一致，不能共用 Checklist")
    return {"task_id": task_id, "case_id": next(iter(case_ids)),
            "task": str(getattr(task, "instruction", None) or getattr(task, "description", None) or getattr(task, "task", None) or ""),
            "key_elements": next(iter(key_elements)), "trajectories": rows}


def _control_store(root):
    """Use the same CLI/authentication with a short independent cleanup budget."""
    from .phone_factory import PhoneFactoryStore, CLIENT_SCRIPT, _client_failure
    def client(args):
        try:
            result = subprocess.run([sys.executable, str(CLIENT_SCRIPT), *args], capture_output=True,
                text=True, encoding="utf-8", errors="replace", check=False, timeout=5,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                env={**os.environ, "PHONE_FACTORY_TIMEOUT": "3", "PHONE_FACTORY_CLIENT_ERROR_METADATA": "1"})
        except (OSError, subprocess.TimeoutExpired):
            return {"ok": False, "output": "Checklist 停止对账暂未确认", "remote_kind": "timeout"}
        return {"ok": True, "output": result.stdout.strip()} if result.returncode == 0 else _client_failure(result.stderr or result.stdout)
    return PhoneFactoryStore(root, run_client_fn=client)


def cancel_remote_jobs(root, job_id, *, store=None):
    """Best-effort bounded compensation after a local worker has been fenced.

    Only this quality job's unfinished remote generation is addressed. A late
    cancellation cannot stop a retried generation or any phone collection.
    """
    records = RecordStore(root)
    index = records.get("quality_checklist_by_job", job_id) or {}
    control = store or _control_store(root)
    deadline = time.monotonic() + 10
    for key in index.get("keys", []):
        row = records.get("quality_checklist_jobs", key) or {}
        if row.get("last_quality_job_id") != job_id or row.get("complete") or row.get("remote_terminal"):
            continue
        remote_id, server = row.get("remote_job_id"), row.get("collector_server")
        if not remote_id or not server or time.monotonic() >= deadline:
            # An accepted response may have been lost. Replaying the frozen
            # request will recover its ID; cleanup must not create new work.
            continue
        changes = {"retryable": True}
        try:
            generation = row.get("remote_generation")
            if type(generation) is not int:
                state = control._remote_result(["quality-checklist-status", remote_id, "--server", server])
                generation = state.get("generation")
            if type(generation) is not int:
                raise ValueError("Checklist 服务未返回执行代次，停止待对账")
            payload = {"request_id": "stop_" + digest({"job_id": job_id, "key": key, "generation": generation}),
                       "expected_generation": generation}
            state = control._remote_result(["quality-checklist-cancel", remote_id, "--payload", json.dumps(payload), "--server", server])
            changes.update(remote_generation=state.get("generation", generation),
                           remote_terminal=state.get("status") in TERMINAL, cleanup_error=None)
        except Exception as exc:
            from .pipeline_diagnostics import format_exception
            changes["cleanup_error"] = (format_exception(exc).get("message") or type(exc).__name__)[:500]
        def update(current):
            if current.get("last_quality_job_id") == job_id:
                current.update(changes)
        records.update("quality_checklist_jobs", key, update)


def evaluate_task(root, job, task_manifest, input_digest, *, progress=None, check=None, store=None, poll_seconds=2):
    """Resume one deterministic remote job; completed candidates stay immutable."""
    store = store or _store(root)
    frozen = job["checklist_options"]
    key = digest({"input": input_digest, "options": frozen, "task": task_manifest})
    records = RecordStore(root)
    saved = records.get("quality_checklist_jobs", key) or {}
    server = frozen["collector_server"]

    def remote(args):
        if check:
            check()
        return store._remote_result([*args, "--server", server])

    def persist(**changes):
        nonlocal saved
        if check:
            check()
        saved = records.update("quality_checklist_jobs", key, lambda row: row.update(**changes), default={"request_id": "qc_" + key, "input_digest": input_digest})

    if saved.get("complete"):
        return saved["results"]
    request_id = "qc_" + key
    # Paths are short and contain only small descriptors, never copied evidence.
    directory = Path(root) / "system" / "quality_checklist" / key[:24]
    io_path(directory).mkdir(parents=True, exist_ok=True)
    manifest = {"schema_version": 1, "quality_job_id": request_id, "input_digest": input_digest, "tasks": [task_manifest]}
    request = {"request_id": request_id, **{name: frozen[name] for name in ("evaluator_config", "evaluation_profile_id", "evaluation_profile_digest")}}
    request_path, manifest_path = directory / "request.json", directory / "manifest.json"
    for path, payload in ((request_path, request), (manifest_path, manifest)):
        content = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        if io_path(path).exists():
            if io_path(path).read_bytes() != content:
                raise ValueError("frozen Checklist request descriptor differs")
        else:
            io_path(path).write_bytes(content)
    persist(last_quality_job_id=job["job_id"], collector_server=server)
    records.update("quality_checklist_by_job", job["job_id"],
                   lambda row: row.update(keys=list(dict.fromkeys([*row.get("keys", []), key]))), default={})
    accepted = remote(["quality-checklist-start", "--payload", json.dumps(request, ensure_ascii=False), "--manifest", str(manifest_path)])
    remote_id = accepted.get("job_id") or accepted.get("request_id") or request_id
    persist(remote_job_id=remote_id, remote_generation=accepted.get("generation"), remote_terminal=accepted.get("status") in TERMINAL)
    if saved.get("retryable"):
        payload = {"request_id": "retry_" + digest({"job_id": job["job_id"], "key": key})}
        if type(saved.get("remote_generation")) is int:
            payload["expected_generation"] = saved["remote_generation"]
        state = remote(["quality-checklist-retry", remote_id, "--payload", json.dumps(payload)])
        persist(remote_generation=state.get("generation"), remote_terminal=state.get("status") in TERMINAL)
    try:
        while True:
            state = remote(["quality-checklist-status", remote_id])
            if state.get("generation") != saved.get("remote_generation") or (state.get("status") in TERMINAL) != saved.get("remote_terminal"):
                persist(remote_generation=state.get("generation"), remote_terminal=state.get("status") in TERMINAL)
            if progress:
                progress(stage="checklist_evaluating", current_task=task_manifest["task_id"])
            if state.get("status") in TERMINAL:
                break
            time.sleep(poll_seconds)
        results, offset = {}, 0
        expected = {row["trajectory_id"] for row in task_manifest["trajectories"]}
        while True:
            page = remote(["quality-checklist-results", remote_id, "--offset", str(offset), "--limit", "100"])
            items = page.get("items", [])
            for row in items:
                ident = row.get("trajectory_id")
                if row.get("task_id") != task_manifest["task_id"] or ident not in expected or ident in results:
                    raise ValueError("Checklist result identity differs from frozen input")
                results[ident] = row.get("checklist") or {"status": "error", "error": row.get("error") or "checklist_result_missing"}
            offset += len(items)
            if not page.get("has_more"):
                break
            if not items:
                raise ValueError("Checklist result pagination did not advance")
        for ident in expected - results.keys():
            results[ident] = {"status": "error", "error": state.get("error") or "checklist_result_missing"}
        complete = all(row.get("status") == "complete" for row in results.values())
        persist(results=results, complete=complete, retryable=not complete, remote_terminal=True)
        return results
    except BaseException:
        # Cancel only this read-only evaluation job, never a collection run.
        try:
            cancel_remote_jobs(root, job["job_id"], store=store)
        except Exception:
            pass
        raise
