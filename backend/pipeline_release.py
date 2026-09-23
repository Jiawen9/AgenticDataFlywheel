"""One-trajectory-per-task publication, owned by a durable Pipeline.

The final workbook and JSON share the same compact row mapping. Full upstream
artifacts are frozen unchanged; no correction session is invented in automatic
mode. All public helpers accept an isolated data root for offline validation.
"""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
import hashlib
import json
import math
import shutil
import uuid

from .batch_lifecycle import ensure_publishable, published_entry, session_batch_id
from .batch_results import current_tree_payload, current_quality_payload
from .data_store import ArtifactStore, DATA_ROOT, RecordStore, RevisionConflict
from .data_store.release_provenance import freeze_release_provenance
from .data_publishing.service import DatasetReleaseRegistry
from .stage_artifacts import write_payload_workbook
from .trajectory_correction.constants import FIXED_SOURCE_ID
from .trajectory_correction.draft_store import utc_now
from .trajectory_correction.session_state import fingerprint, identified, reconcile, session_fingerprint
from .trajectory_correction.workbook import load_snapshot


def _root(root):
    return Path(root or DATA_ROOT).resolve()


def _sessions(batch_id, root):
    return [session for session in RecordStore(root).list("correction_sessions")
            if not session.get("archived") and session_batch_id(session, root) == batch_id]


def _inputs(batch_id, root):
    tree = current_tree_payload(batch_id, root)
    quality = current_quality_payload(batch_id, root)
    table = deepcopy(tree.get("source_annotation") or {})
    if not table.get("sheets"):
        raise ValueError("该批次缺少当前建树步骤来源")
    raw = tree.get("raw_root") or str(root / "raw" / "rollout_trajectories")
    from .trajectory_correction.assets import registered_asset_root
    assets = registered_asset_root(str(raw), root)
    with TemporaryDirectory(prefix="pipeline-selection-") as temporary:
        path = Path(temporary) / "input.xlsx"
        path.with_suffix(".json").write_text(json.dumps(table, ensure_ascii=False), encoding="utf-8")
        snapshot = identified(load_snapshot(path, asset_root=assets, source_kind="annotated_workbook"))
        goals = {str(item["task_id"]): item.get("goal") for item in tree.get("tasks", [])}
        for group in snapshot["groups"]:
            goal = goals.get(group["task_id"])
            if goal:
                group["task"] = str(goal)
                for row in group["rows"]:
                    row["task"] = str(goal)
        snapshot = identified(snapshot)
    return tree, quality, table, snapshot, assets


def build_selection(batch_id, task_ids, mode, threshold=None, *, root=None):
    root = _root(root)
    if mode not in {"manual", "automatic"}:
        raise ValueError("Pipeline 模式必须为 manual 或 automatic")
    if mode == "automatic" and (isinstance(threshold, bool) or not isinstance(threshold, (int, float))
            or not math.isfinite(threshold) or not 0 <= threshold <= 5):
        raise ValueError("自动发布总分阈值必须在 0～5 之间")
    with ArtifactStore(root).batch_lock(batch_id):
        ensure_publishable(batch_id, _sessions(batch_id, root), root)
        tree, quality, table, snapshot, _ = _inputs(batch_id, root)
        expected = sorted(set(map(str, task_ids)))
        if not expected or set(expected) != set(tree.get("task_fingerprints", {})):
            raise ValueError("Pipeline 任务范围与当前整批任务不一致")
        results = {str(item["task_id"]): item for item in quality.get("tasks", [])}
        candidates, selected, excluded = [], [], []
        for task_id in expected:
            evaluations = results.get(task_id, {}).get("evaluations", {})
            if not isinstance(evaluations, dict):
                raise ValueError(f"{task_id} 缺少逐轨迹质检评分")
            groups = [group for group in snapshot["groups"] if group["task_id"] == task_id]
            if not groups or {str(group["meta_task"]) for group in groups} != set(evaluations):
                raise ValueError(f"{task_id} 的步骤来源与质检轨迹不完整匹配")
            choices = []
            for group in groups:
                score = evaluations[str(group["meta_task"])].get("global_score")
                if isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(score) or not 0 <= score <= 5:
                    raise ValueError(f"{task_id} 存在无效轨迹评分")
                choices.append({"task_id": task_id, "trajectory_id": str(group["meta_task"]),
                    "group_id": group["group_id"], "goal": group.get("task", task_id),
                    "global_score": score, "passed_threshold": evaluations[str(group["meta_task"])].get("passed_threshold"),
                    "trajectory_count": 1, "step_count": len(group["rows"])})
            choices.sort(key=lambda item: (-item["global_score"], item["trajectory_id"], item["group_id"]))
            candidates.extend(choices)
            if mode == "manual" or choices[0]["global_score"] >= threshold:
                selected.append(choices[0])
            else:
                excluded.append({"task_id": task_id, "best_score": choices[0]["global_score"],
                                 "reason": "没有达到总分阈值的轨迹"})
        return {"status": "ready", "batch_id": batch_id, "storage_batch_id": batch_id,
                "tree_run_id": batch_id, "run_id": batch_id, "mode": mode, "threshold": threshold,
                "task_ids": expected, "tasks": selected, "candidates": candidates, "excluded": excluded,
                "selected_trajectories": {item["task_id"]: item["trajectory_id"] for item in selected},
                "task_fingerprints": tree["task_fingerprints"], "tree_hashes": tree.get("tree_hashes", {}),
                "source_fingerprint": fingerprint(table),
                "quality_fingerprint": fingerprint(quality.get("tasks", []))}


def _validate_source(selection, root):
    tree, quality, table, snapshot, assets = _inputs(selection["batch_id"], root)
    if (tree.get("task_fingerprints") != selection.get("task_fingerprints")
            or tree.get("tree_hashes", {}) != selection.get("tree_hashes", {})
            or fingerprint(table) != selection.get("source_fingerprint")
            or fingerprint(quality.get("tasks", [])) != selection.get("quality_fingerprint")):
        raise RevisionConflict("Pipeline 入选结果来源已变化，请重新处理")
    return tree, quality, table, snapshot, assets


def pipeline_session_source_current(session, *, root=None):
    """Preserve saved choices after termination until their real source changes."""
    selection = session.get("selection") or {}
    if not session.get("pipeline_selection") or not selection.get("source_fingerprint"):
        return False
    root = _root(root)
    tree = current_tree_payload(session_batch_id(session, root), root)
    return (tree.get("task_fingerprints") == selection.get("task_fingerprints")
            and tree.get("tree_hashes", {}) == selection.get("tree_hashes", {})
            and fingerprint(tree.get("source_annotation") or {}) == selection.get("source_fingerprint"))


def prepare_manual(batch_id, selection, *, root=None):
    """Expose all candidates in the existing editor; default to one Top1 each."""
    root = _root(root)
    if selection.get("batch_id") != batch_id or selection.get("mode") != "manual":
        raise ValueError("人工修正选择与批次或模式不一致")
    records, store = RecordStore(root), ArtifactStore(root)
    with store.batch_lock(batch_id):
        _, _, table, snapshot, assets = _validate_source(selection, root)
        existing = _sessions(batch_id, root)
        ensure_publishable(batch_id, existing, root)
        if len(existing) > 1:
            raise ValueError("同一批次存在多个活动修正会话")
        session = existing[0] if existing else {"session_id": uuid.uuid4().hex[:16], "source_id": FIXED_SOURCE_ID,
            "source_kind": "annotated_workbook", "created_at": utc_now(), "row_edits": {}, "cot": {},
            "group_exports": {}, "exports": []}
        key = fingerprint({"selection": selection, "table": table})
        if session.get("pipeline_input_fingerprint") == key:
            return {"session_id": session["session_id"], "selection": session["selection"],
                    "storage_revision": session["storage_revision"]}
        previous_choices = dict(session.get("group_exports", {}))
        reconcile(session, session.get("snapshot_payload") or {"groups": []}, snapshot, selection["task_fingerprints"])
        defaults = {item["group_id"] for item in selection["tasks"]}
        # Preserve a valid explicit one-per-task selection from an existing draft.
        for task_id in selection["task_ids"]:
            groups = [group for group in snapshot["groups"] if group["task_id"] == task_id]
            previous = [group["group_id"] for group in groups if previous_choices.get(group["group_id"])]
            chosen = set(previous) if len(previous) == 1 else defaults
            for group in groups:
                session["group_exports"][group["group_id"]] = group["group_id"] in chosen
        session.update(batch_id=batch_id, storage_batch_id=batch_id, tree_run_id=batch_id,
            selection=deepcopy(selection), workbook_payload=table, pipeline_selection=True,
            pipeline_input_fingerprint=key, input_fingerprint=key, published=False, updated_at=utc_now())
        directory = root / "system" / "trajectory_correction" / "inputs" / session["session_id"]
        directory.mkdir(parents=True, exist_ok=True)
        write_payload_workbook(directory / "source.xlsx", table)
        for name, value in (("source.json", table), ("snapshot.json", session["snapshot_payload"])):
            (directory / name).write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        session["source_snapshot"] = {"workbook": "source.xlsx", "json": "snapshot.json", "raw_root": str(assets),
            "sha256": _sha(directory / "snapshot.json"), "workbook_json_sha256": _sha(directory / "source.json"),
            "workbook_sha256": _sha(directory / "source.xlsx")}
        session = records.put("correction_sessions", session["session_id"], session, expected_revision=session.get("storage_revision", 0))
        return {"session_id": session["session_id"], "selection": deepcopy(selection), "storage_revision": session["storage_revision"]}


def _manual_fingerprint(session):
    return fingerprint({key: session.get(key) for key in ("snapshot_payload", "workbook_payload", "task_fingerprints",
        "row_edits", "group_exports", "pending_review", "stale_tasks")})


def confirm_manual(session_id, selection=None, *, expected_revision=None, root=None):
    root = _root(root)
    records = RecordStore(root)
    session = records.get("correction_sessions", session_id)
    if session is None:
        raise FileNotFoundError("修正会话不存在")
    batch_id = session_batch_id(session, root)
    with ArtifactStore(root).batch_lock(batch_id):
        session = records.get("correction_sessions", session_id)
        if expected_revision is not None and session["storage_revision"] != expected_revision:
            raise RevisionConflict("修正会话已更新，请刷新后确认")
        choice = deepcopy(selection or session.get("selection"))
        if not choice or choice.get("batch_id") != batch_id or choice.get("mode") != "manual":
            raise ValueError("修正会话缺少本批次 Pipeline 选择")
        _validate_source(choice, root)
        ensure_publishable(batch_id, [session], root)
        selected = []
        for task_id in choice["task_ids"]:
            groups = [group for group in session["snapshot_payload"]["groups"]
                      if group["task_id"] == task_id and session.get("group_exports", {}).get(group["group_id"], False)]
            if len(groups) != 1:
                raise ValueError(f"任务 {task_id} 必须且只能选择一条导出轨迹")
            group = groups[0]
            if not any(not session.get("row_edits", {}).get(str(row["excel_row"]), {}).get("deleted") for row in group["rows"]):
                raise ValueError(f"任务 {task_id} 的入选轨迹没有有效步骤")
            candidate = next((item for item in choice["candidates"] if item["group_id"] == group["group_id"]), None)
            if candidate is None:
                raise ValueError("入选轨迹不属于本次质检候选")
            selected.append(deepcopy(candidate))
        choice.update(tasks=selected, selected_trajectories={item["task_id"]: item["trajectory_id"] for item in selected})
        return {"session_id": session_id, "selection": choice, "manual_fingerprint": _manual_fingerprint(session),
                "group_ids": [item["group_id"] for item in selected],
                "task_fingerprints": deepcopy(session["task_fingerprints"]), "session_revision": session["storage_revision"],
                "confirmed_at": utc_now()}


def validate_confirmation(pipeline, *, root=None):
    root = _root(root)
    confirmation = pipeline.get("confirmation") or {}
    if not confirmation.get("session_id") or confirmation["session_id"] != pipeline.get("session_id"):
        raise ValueError("尚未完成人工修正确认")
    session = RecordStore(root).get("correction_sessions", confirmation["session_id"])
    if session is None or session_batch_id(session, root) != pipeline["batch_id"]:
        raise ValueError("Pipeline 修正会话来源不一致")
    if _manual_fingerprint(session) != confirmation.get("manual_fingerprint"):
        raise RevisionConflict("人工确认后的修改或选择发生变化，请重新确认")
    _validate_source(confirmation["selection"], root)
    return session


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _export_payload(selection, snapshot, table, session, pipeline_id):
    from .trajectory_correction.service import _overlay_row
    selected = {item["group_id"] for item in selection["tasks"]}
    active_sheet = next(iter(table["sheets"]))
    source_rows = table["sheets"][active_sheet]
    columns = list(table.get("columns", {}).get(active_sheet) or list(dict.fromkeys(key for row in source_rows for key in row)))
    for name in ("actions_box", "summary", "thought", "sop"):
        if name not in columns:
            columns.append(name)
    rows, groups, edits, cot = [], [], {}, {}
    for group in snapshot["groups"]:
        if group["group_id"] not in selected:
            continue
        frozen = {**deepcopy(group), "export": True, "rows": []}
        for base in group["rows"]:
            original_number = int(base["excel_row"])
            edit = session.get("row_edits", {}).get(str(original_number), {})
            if edit.get("deleted"):
                continue
            row = _overlay_row(session.get("session_id", ""), session, base)
            value = deepcopy(source_rows[original_number - 2])
            value["actions" if "actions" in columns else "action"] = row["actions"]
            for key in ("actions_box", "summary", "thought", "sop"):
                value[key] = (edit.get("sop", value.get("sop", "")) if key == "sop" else row.get(key, ""))
            number = len(rows) + 2
            row.update(source_excel_row=original_number, excel_row=number)
            row.pop("image_url", None)
            rows.append(value)
            frozen["rows"].append(row)
            if edit:
                edits[str(number)] = deepcopy(edit)
            if session.get("cot", {}).get(str(original_number)) and (row.get("cot_status") == "generated" or any(
                    row.get(field + "_source") == "generated" for field in ("summary", "thought"))):
                cot[str(number)] = deepcopy(session["cot"][str(original_number)])
        if not frozen["rows"]:
            raise ValueError("入选轨迹没有有效步骤")
        groups.append(frozen)
    if len(groups) != len(selected) or not rows:
        raise ValueError("没有完整的入选轨迹可发布")
    return {"schema_version": 2, "pipeline_id": pipeline_id, "source_kind": "pipeline_selection",
        "session_id": session.get("session_id"), "batch_id": selection["batch_id"], "tree_run_id": selection["tree_run_id"],
        "selection": deepcopy(selection), "task_fingerprints": selection["task_fingerprints"], "groups": groups,
        "row_edits": edits, "cot": cot, "columns": {active_sheet: columns}, "sheets": {active_sheet: rows}}


def _existing_edit_view(session, snapshot):
    """Rebase an existing draft onto current rows using stable step identities.

    Automatic selection is determined by quality, independent of saved export
    choices. This copy leaves the user's persisted draft and row positions intact.
    Whole-task source validity has already been checked by ensure_publishable.
    """
    previous = session.get("snapshot_payload")
    if not isinstance(previous, dict):
        raise ValueError("已有修正会话缺少稳定步骤快照，不能忽略已保存修改发布")
    old_rows = {str(row["excel_row"]): row for group in identified(previous).get("groups", [])
                for row in group.get("rows", [])}
    current = identified(snapshot)
    current_rows = {row["step_key"]: row for group in current["groups"] for row in group["rows"]}
    result = deepcopy(session)
    for field in ("row_edits", "cot", "bbox_baselines"):
        mapped = {}
        for number, value in session.get(field, {}).items():
            if not value:
                continue
            old = old_rows.get(str(number))
            row = current_rows.get(old["step_key"]) if old else None
            if row is None:
                raise RevisionConflict("已有修正内容无法关联当前稳定步骤，请先复核")
            mapped[str(row["excel_row"])] = deepcopy(value)
        result[field] = mapped
    result["snapshot_payload"] = current
    return result


def _remove_release_directory(path, release_root):
    # The targets are deterministic children, but verify the resolved path as
    # well before recursive cleanup (including an unexpected directory link).
    resolved, expected = path.resolve(), release_root.resolve()
    if resolved.parent != expected:
        raise ValueError("发布暂存目录越出预期边界")
    shutil.rmtree(resolved)


def publish_pipeline(pipeline, *, root=None, registry=None):
    root = _root(root)
    records, store = RecordStore(root), ArtifactStore(root)
    pipeline_id, batch_id = str(pipeline["pipeline_id"]), str(pipeline["batch_id"])
    release_id = "rel_" + hashlib.sha256(("pipeline:" + pipeline_id).encode()).hexdigest()[:24]
    registry = registry or DatasetReleaseRegistry(data_root=root, releases_file=root / "system" / "dataset_releases.json")
    with store.batch_lock(batch_id):
        existing = records.get("dataset_releases", release_id)
        if existing:
            if existing.get("pipeline_id") != pipeline_id or existing.get("batch_ids") != [batch_id]:
                raise ValueError("Pipeline 发布幂等编号冲突")
            return registry.get(release_id)
        from .pipeline_access import ensure_pipeline_write
        ensure_pipeline_write(batch_id, root, action="publish")
        sessions = _sessions(batch_id, root)
        if len(sessions) > 1:
            raise ValueError("同一批次存在多个活动修正会话")
        ensure_publishable(batch_id, sessions, root)
        manual = pipeline["mode"] == "manual"
        session = validate_confirmation(pipeline, root=root) if manual else {}
        selection = deepcopy((pipeline.get("confirmation") or {}).get("selection") if manual else pipeline.get("selection"))
        if not selection or selection.get("batch_id") != batch_id or selection.get("mode") != pipeline["mode"]:
            raise ValueError("Pipeline 发布缺少有效选择清单")
        if not selection.get("tasks"):
            raise ValueError("没有达到发布条件的轨迹，不创建空发布")
        _, _, table, snapshot, _ = _validate_source(selection, root)
        if manual:
            snapshot = session["snapshot_payload"]
        elif sessions:
            session = _existing_edit_view(sessions[0], snapshot)
        payload = _export_payload(selection, snapshot, table, session, pipeline_id)
        release_root = root / "releases"
        release_root.mkdir(parents=True, exist_ok=True)
        final, staging = release_root / release_id, release_root / ("." + release_id + ".tmp")
        # A durable, stable intent lets retries recover an unregistered rename.
        intent_key = "pipeline:" + pipeline_id
        intent = records.get("release_intents", intent_key)
        request_hash = fingerprint({"pipeline": pipeline_id, "batch": batch_id, "selection": selection,
                                    "confirmation": pipeline.get("confirmation"), "name": pipeline["name"]})
        if intent and intent.get("request_hash") != request_hash:
            raise RevisionConflict("同一 Pipeline 的发布内容不能在重试时改变")
        if not intent:
            intent = records.put("release_intents", intent_key, {"release_id": release_id, "pipeline_id": pipeline_id,
                "batch_id": batch_id, "request_hash": request_hash, "created_at": utc_now(), "status": "preparing"}, expected_revision=0)
        for orphan in (staging, final):
            if orphan.exists():
                _remove_release_directory(orphan, release_root)
        staging.mkdir()
        try:
            filename = "full_dataset.xlsx"
            excel = staging / "001" / filename
            write_payload_workbook(excel, payload)
            refs = [store.get(batch_id, stage) for stage in ("04_tree", "05_quality")]
            artifact = store.publish(batch_id, "07_cot", payload,
                workbooks={filename: excel}, source_refs=[ref for ref in refs if ref],
                metadata={"pipeline_id": pipeline_id, "row_count": sum(len(rows) for rows in payload["sheets"].values())})
            created_at = intent["created_at"]
            release = {"release_id": release_id, "name": pipeline["name"], "created_at": created_at,
                "source_kind": "workflow", "pipeline_id": pipeline_id, "batch_ids": [batch_id], "source_count": 1,
                "source_refs": [{"kind": "pipeline_selection", "id": pipeline_id, "session_id": session.get("session_id"),
                                 "tree_run_id": selection["tree_run_id"], "artifact": artifact}],
                "excel_paths": [{"path": registry.project_path(final / "001" / filename),
                    "data_path": f"releases/{release_id}/001/{filename}", "filename": filename, "sha256": _sha(excel),
                    "rows": sum(len(rows) for rows in payload["sheets"].values()), "created_at": created_at}],
                "trajectory_paths": [], "task_count": len(payload["groups"]), "trajectory_count": len(payload["groups"]),
                "step_count": sum(len(rows) for rows in payload["sheets"].values()), "selection": selection,
                "upload_status": "not_uploaded", "upload_job_id": None, "upload_error": None, "s3_uri": None,
                "uploaded_at": None, "uploaded_files": 0, "uploaded_bytes": 0}
            release = freeze_release_provenance(root, release, staging)
            (staging / "manifest.json").write_text(json.dumps(release, ensure_ascii=False, indent=2), encoding="utf-8")
            staging.rename(final)
            entries = [{"namespace": "dataset_releases", "key": release_id, "payload": release, "expected_revision": 0},
                       published_entry(batch_id, release_id, created_at, root),
                       {"namespace": "release_intents", "key": intent_key, "payload": {**intent, "status": "committed"},
                        "expected_revision": intent["storage_revision"]}]
            for current in sessions:
                current.update(published=True, published_at=created_at, published_release_id=release_id,
                               published_content_fingerprint=session_fingerprint(current))
                entries.append({"namespace": "correction_sessions", "key": current["session_id"], "payload": current,
                                "expected_revision": current["storage_revision"]})
            try:
                records.put_many(entries)
            except Exception:
                # SQLite may have committed before the caller lost its response.
                # A failed read propagates and deliberately keeps the frozen files.
                if not records.get("dataset_releases", release_id):
                    raise
            return registry.get(release_id)
        except Exception:
            if not records.get("dataset_releases", release_id) and final.exists():
                _remove_release_directory(final, release_root)
            raise
        finally:
            if staging.exists():
                _remove_release_directory(staging, release_root)
