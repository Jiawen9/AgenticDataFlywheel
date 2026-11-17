"""Rebuildable display indexes; never an authority for running/publishing work.

Projections are generated from the payload already in memory at publication.
The original manifest and its fingerprints are deliberately untouched.
"""
from __future__ import annotations

import json
import logging
import math
import sqlite3
import threading
from pathlib import Path

from .data_store import ArtifactStore, RecordStore
from .data_store import summary_index
from .data_store.registry import SummaryUnavailable
from .file_io import io_path

VERSION = 2
LOG = logging.getLogger(__name__)
STAGES = {"00_collection", "01_conversion", "02_annotation", "04_tree", "05_quality"}
TASK_FIELDS = set("task_id goal warning first_trajectory trajectory_count step_count annotated status tree_status quality_status original_step_count tree_step_count ignored_step_count action_node_count scoring_version fusion_complete fusion_eligible_count result_state rubric_ready results_available partial_results_available completed_at updated_at average_score passed_count failed_count threshold total_count".split())


def quality_summary(value):
    value = value if isinstance(value, dict) else {}
    result = summary_index._fields(value, TASK_FIELDS)
    result["results_available"] = bool(value.get("evaluations"))
    result["rubric_ready"] = bool(value.get("rubric"))
    evaluations = value.get("evaluations") if isinstance(value.get("evaluations"), dict) else {}
    result["evaluation_count"] = len(evaluations)
    def score_valid(item):
        if not isinstance(item, dict) or isinstance(item.get("global_score"), bool): return False
        try: return math.isfinite(float(item.get("global_score")))
        except (TypeError, ValueError): return False
    result["rubric_scores_valid"] = bool(evaluations) and all(score_valid(item) for item in evaluations.values())
    result["partial_results_available"] = result["results_available"] and (
        value.get("result_state") == "partial" or value.get("fusion_complete") is False)
    return result


def projection_entries(manifest: dict, payload: dict) -> list[dict]:
    # A display-only projection must not add a new publication gate for legacy
    # payload shapes. Keep a visible diagnostic and let normal detail validation
    # decide whether those artifacts are usable.
    try:
        return _projection_entries(manifest, payload)
    except (TypeError, ValueError, KeyError, AttributeError) as exc:
        return [{'namespace': 'page_stages', 'key': f"{manifest['batch_id']}:{manifest['stage']}",
            'payload': dict(batch_id=manifest['batch_id'], stage=manifest['stage'],
                artifact_version=manifest['version'], content_hash=manifest['content_hash'],
                created_at=manifest['created_at'], updated_at=manifest['created_at'], display_version=VERSION,
                projection_error=summary_index._text(f'页面摘要无法生成：{type(exc).__name__}: {exc}'))}]


def _projection_entries(manifest: dict, payload: dict) -> list[dict]:
    stage, batch = manifest["stage"], manifest["batch_id"]
    if stage not in STAGES or not isinstance(payload, dict):
        return []
    common = dict(batch_id=batch, stage=stage, artifact_version=manifest["version"],
                  content_hash=manifest["content_hash"], created_at=manifest["created_at"],
                  updated_at=manifest["created_at"], display_version=VERSION)
    header = {**common, **summary_index._fields(payload, {
        "kind", "task_count", "batch_role", "task_source_batch_id", "agent_type", "collection_run_id",
        "completed_at", "model_name", "job_id"}), "task_count": 0, "trajectory_count": 0, "step_count": 0}
    tasks = {}
    if stage == "00_collection":
        header["task_count"] = payload.get("task_count", 0)
        for row in payload.get("snapshot", {}).get("tasks", []):
            if row.get("task_id"):
                tasks[row["task_id"]] = {"task_id": row["task_id"], "goal": str(row.get("task") or row["task_id"])}
    elif stage in {"01_conversion", "02_annotation"}:
        from .trajectory_context import row_task_id, row_trajectory_id
        from .batch_results import annotation_task_fingerprints
        fingerprints = annotation_task_fingerprints(payload)
        # Same first-sheet semantics as trajectory_data's list view.
        for row in next(iter(payload.get("sheets", {}).values()), []):
            task, trajectory = row_task_id(row), row_trajectory_id(row)
            if not task:
                continue
            item = tasks.setdefault(task, {"task_id": task, "goal": str(row.get("task") or row.get("goal") or ""),
                "trajectories": set(), "step_count": 0, "annotation_fingerprint": fingerprints.get(task)})
            item["step_count"] += 1
            if trajectory:
                item["trajectories"].add(trajectory)
        for item in tasks.values():
            trajectories = item.pop("trajectories")
            item.update(trajectory_count=len(trajectories), annotated=bool(trajectories),
                        first_trajectory=min(trajectories) if trajectories else "", warning=None)
    elif stage == "04_tree":
        for row in payload.get("tasks", []):
            task = row["task_id"]
            if task not in payload.get("trees", {}):
                continue
            tasks[task] = {**summary_index._fields(row, TASK_FIELDS), "task_id": task,
                "annotation_fingerprint": payload.get("source_task_fingerprints", {}).get(task),
                "tree_hash": payload.get("tree_hashes", {}).get(task)}
    elif stage == "05_quality":
        for row in payload.get("tasks", []):
            task = row["task_id"]
            tasks[task] = {**quality_summary(row), "task_id": task,
                "tree_hash": payload.get("source_tree_hashes", {}).get(task),
                "quality_job_id": row.get("quality_job_id") or payload.get("job_id")}
    if tasks:
        header.update(task_count=len(tasks), trajectory_count=sum(t.get("trajectory_count", 0) for t in tasks.values()),
                      step_count=sum(t.get("step_count", t.get("original_step_count", 0)) for t in tasks.values()))
    entries = [{"namespace": "page_stages", "key": f"{batch}:{stage}", "payload": header}]
    entries.extend({"namespace": "page_stage_tasks", "key": f"{batch}:{stage}:{task}",
                    "payload": {**common, **item}} for task, item in tasks.items())
    return entries


def _envelope(items, total, offset, limit, complete=True, **extra):
    return dict(items=items, total=total if complete else None, filtered_total=total if complete else None,
        latest=items[:3], scope_counts={"total": total if complete else None, "active": 0, "abnormal": 0},
        offset=offset, limit=limit, has_more=offset + len(items) < total,
        index_state="ready" if complete else "building", counts_complete=complete, **extra)


def _validate(offset, limit):
    if offset < 0 or limit not in range(1, 201):
        raise ValueError("分页 offset 必须非负，limit 为 1..200")


def _rows(connection, namespace, batch):
    return [json.loads(row[0]) for row in connection.execute(
        "SELECT payload FROM record_summaries WHERE namespace=? AND batch_id=? AND projection_version=?",
        (namespace, batch, summary_index.VERSION))]


def _choose_references(rows):
    selected = {}
    for key, value in rows:
        if value.get('stage') not in STAGES: continue
        group = (value['batch_id'], value['stage'])
        rank = (key == ArtifactStore._key(*group), value.get('created_at', ''), value.get('version', ''))
        if group not in selected or rank > selected[group][0]: selected[group] = (rank, key, value)
    return {(batch, stage): (key, value) for (batch, stage), (_, key, value) in selected.items()}


def _batch_contexts(connection, batches, *, tasks=False):
    """Read a page/filter's metadata in chunks, with no per-batch DB queries."""
    result = {batch: {'headers': {}, 'references': {}, 'rows': {}} for batch in batches}
    namespaces = ['page_stages', 'collection_runs', 'preprocessing_jobs']
    if tasks: namespaces += ['page_stage_tasks', 'batch_task_states']
    for start in range(0, len(batches), 300):
        ids = batches[start:start + 300]
        marks = ','.join('?' for _ in ids)
        refs = _choose_references((key, json.loads(raw)) for key, raw in connection.execute("SELECT record_key,payload FROM records WHERE namespace='artifacts' AND json_extract(payload,'$.batch_id') IN (" + marks + ")", ids))
        for (_, stage), (_, ref) in refs.items(): result[ref['batch_id']]['references'][stage] = ref
        for ns, batch, raw in connection.execute("SELECT namespace,batch_id,payload FROM record_summaries WHERE projection_version=? AND namespace IN (" + ','.join('?' for _ in namespaces) + ") AND batch_id IN (" + marks + ")", [summary_index.VERSION, *namespaces, *ids]):
            result[batch]['rows'].setdefault(ns, []).append(json.loads(raw))
    for context in result.values():
        refs = context['references']
        context['headers'] = {row['stage']: row for row in context['rows'].get('page_stages', [])
            if row.get('display_version') == VERSION and refs.get(row['stage'], {}).get('version') == row.get('artifact_version')
            and refs.get(row['stage'], {}).get('content_hash') == row.get('content_hash')}
    return result


def _stage_context(connection, batch):
    references = {stage: ref for (_, stage), (_, ref) in _choose_references((key, json.loads(raw)) for key, raw in connection.execute(
        "SELECT record_key,payload FROM records WHERE namespace='artifacts' AND json_extract(payload,'$.batch_id')=?", (batch,))).items()}
    headers = {value["stage"]: value for value in _rows(connection, "page_stages", batch)
        if references.get(value["stage"], {}).get("version") == value["artifact_version"]
        and references.get(value["stage"], {}).get("content_hash") == value["content_hash"]
        and value.get("display_version") == VERSION}
    return headers, references


def _headers(connection, batch):
    return _stage_context(connection, batch)[0]


def _latest_quality_jobs(connection, batch):
    # Once per batch, never one scan of all job JSON for every task. Extract
    # only identities/fingerprints; large configs and logs stay in SQLite.
    rows = connection.execute("""SELECT r.record_key,json_extract(r.payload,'$.status'),
        json_extract(r.payload,'$.task_ids'),json_extract(r.payload,'$.task_fingerprints'),
        json_extract(l.payload,'$.state'),json_extract(l.payload,'$.token'),l.record_key
        FROM records r JOIN record_summaries s ON s.namespace='quality_jobs' AND s.record_key=r.record_key
        LEFT JOIN records l ON l.namespace='quality_executions' AND l.record_key=r.record_key
        WHERE r.namespace='quality_jobs' AND s.batch_id=? AND s.projection_version=?
        ORDER BY json_extract(r.payload,'$.created_at'),r.record_key""", (batch, summary_index.VERSION))
    latest = {}
    for job_id, status, tasks, fingerprints, lease_state, token, lease_key in rows:
        job = dict(job_id=job_id, status=status, task_fingerprints=json.loads(fingerprints or '{}'),
                   lease_state=lease_state, lease_token=token, has_lease=lease_key is not None)
        for task in json.loads(tasks or '[]'): latest[task] = job
    return latest


def _task_rows(connection, batch, context=None):
    headers = context['headers'] if context is not None else _headers(connection, batch)
    rows_for = (lambda namespace: context['rows'].get(namespace, [])) if context is not None else (lambda namespace: _rows(connection, namespace, batch))
    groups = {}
    for row in rows_for("page_stage_tasks"):
        if headers.get(row["stage"], {}).get("artifact_version") == row["artifact_version"] and headers.get(row["stage"], {}).get("content_hash") == row.get("content_hash"):
            groups.setdefault(row["stage"], {})[row["task_id"]] = row
    states = {x["task_id"]: x for x in rows_for("batch_task_states") if x.get("task_id")}
    annotation, trees, quality = (groups.get(stage, {}) for stage in ("02_annotation", "04_tree", "05_quality"))
    partials = {(row.get('task_id'), row.get('job_id')): row for row in rows_for("quality_partial_results")}
    latest_jobs = _latest_quality_jobs(connection, batch) if partials else {}
    result = []
    for task, ann in annotation.items():
        ann = {**ann, "goal": groups.get("00_collection", {}).get(task, {}).get("goal") or ann.get("goal") or task}
        tree, reviewed = trees.get(task), quality.get(task)
        tree_valid = bool(tree and tree.get("annotation_fingerprint") and tree["annotation_fingerprint"] == ann.get("annotation_fingerprint"))
        if not tree_valid:
            tree, reviewed = None, None
        elif not reviewed or reviewed.get("tree_hash") != tree.get("tree_hash"):
            reviewed = None
        if tree:
            job = latest_jobs.get(task) or {}
            job_id = job.get('job_id')
            candidate = partials.get((task, job_id))
            if (candidate and job.get("status") != "stale" and job.get("lease_state") != "committed"
                and (not job.get("has_lease") or job.get("lease_token") == candidate.get("execution_token"))
                and candidate.get("source_tree_hash") == tree.get("tree_hash")
                and candidate.get("input_fingerprint") == job.get("task_fingerprints", {}).get(task)
                and headers.get('05_quality', {}).get('job_id') != job_id
                and (reviewed or {}).get("quality_job_id") != job_id):
                reviewed = {**candidate, "result_state": "partial", "fusion_complete": False, "status": job.get("status"),
                            "partial_results_available": candidate.get("results_available", False)}
        prior = states.get(task, {})
        ts = "succeeded" if tree else prior.get("tree_status", "pending")
        if not tree and ts == "succeeded": ts = "stale"
        qs = prior.get("quality_status", "pending")
        if reviewed:
            expected = (tree or {}).get('trajectory_count', 0)
            complete_scores = (expected > 0 and reviewed.get('rubric_scores_valid')
                and reviewed.get('evaluation_count', 0) >= expected
                and reviewed.get("fusion_complete") is not False and reviewed.get("result_state") != "partial")
            qs = "succeeded" if complete_scores else reviewed.get("status") or "failed"
            if not complete_scores and qs == "succeeded": qs = "failed"
        elif qs == "succeeded": qs = "stale"
        result.append({**ann, **(tree or {}), **(reviewed or {}), "task_id": task,
                       "tree_status": ts, "quality_status": qs, "status": ts,
                       "original_step_count": ann.get("step_count", 0), "tree_step_count": (tree or {}).get("tree_step_count", 0),
                       "tree_trajectory_count": (tree or {}).get('trajectory_count', 0),
                       "ignored_step_count": (tree or {}).get("ignored_step_count", 0), "action_node_count": (tree or {}).get("action_node_count", 0), "tree_file": ""})
    return result, headers


def _read(root, operation, empty):
    store = RecordStore(root)
    if not io_path(store.database_path).exists(): return empty
    try:
        with store._connection() as connection:
            connection.execute("PRAGMA busy_timeout=250")
            connection.execute("BEGIN")
            if not connection.execute("SELECT 1 FROM sqlite_master WHERE name='record_summaries'").fetchone(): return empty
            return operation(connection)
    except sqlite3.OperationalError as exc:
        if "locked" in str(exc) or "busy" in str(exc):
            raise SummaryUnavailable("页面摘要暂时繁忙，保留已有内容稍后刷新") from exc
        raise


def task_page(root, batch_id, *, view="tree", offset=0, limit=10, q="", status="", annotation_version=None, all_ids=False, selected_task_id=None):
    _validate(offset, limit)
    def read(connection):
        rows, heads = _task_rows(connection, batch_id)
        if annotation_version and heads.get("02_annotation", {}).get("artifact_version") != annotation_version:
            raise ValueError("标框版本已变化，请刷新列表")
        selected = [r for r in rows if (not q or q.casefold() in (r["task_id"] + " " + str(r.get("goal", ""))).casefold())
                    and (not status or r.get("quality_status" if view == "quality" else "tree_status") == status)]
        selected.sort(key=lambda r: r["task_id"])
        _, references = _stage_context(connection, batch_id)
        complete = summary_index.ready(connection) and bool(heads.get("02_annotation")) and set(references).issubset(heads) and not any(h.get("projection_error") for h in heads.values())
        items = selected[offset:offset + limit]
        if view == "quality": items = [{**r, "status": r["quality_status"]} for r in items]
        return _envelope(items, len(selected), offset, limit, complete,
            batch_id=batch_id, run_id=batch_id, tasks=items, task_count=len(rows) if complete else None,
            task_ids=[r["task_id"] for r in selected] if all_ids else [r["task_id"] for r in items],
            ready_task_ids=[r["task_id"] for r in rows if r["tree_status"] == "succeeded"] if all_ids else [],
            quality_counts={"completed": sum(r["quality_status"] == "succeeded" for r in rows),
                "stale": sum(r["quality_status"] in {"stale", "invalidated"} for r in rows),
                "pending": sum(r["quality_status"] not in {"succeeded", "stale", "invalidated"} for r in rows)},
            tree_counts={key: count if complete else None for key, count in {
                "completed": sum(r["tree_status"] == "succeeded" for r in rows),
                "stale": sum(r["tree_status"] in {"stale", "invalidated"} for r in rows),
                "pending": sum(r["tree_status"] not in {"succeeded", "stale", "invalidated"} for r in rows)}.items()},
            tree_counts_scope="batch",
            annotation_version=heads.get("02_annotation", {}).get("artifact_version"),
            selected_task=next((row for row in rows if row['task_id'] == selected_task_id), None) if selected_task_id else None,
            revision=heads.get("04_tree", {}).get("artifact_version"),
            quality_revision=heads.get("05_quality", {}).get("artifact_version"),
            completed_at=heads.get("04_tree", {}).get("completed_at", ""), model_name=heads.get("04_tree", {}).get("model_name", ""))
    return _read(root, read, _envelope([], 0, offset, limit, False, tasks=[], batch_id=batch_id, run_id=batch_id,
        task_ids=[], ready_task_ids=[], task_count=None, selected_task=None,
        quality_counts={"completed": None, "stale": None, "pending": None}, annotation_version=None,
        tree_counts={"completed": None, "stale": None, "pending": None}, tree_counts_scope="batch",
        revision=None, quality_revision=None, completed_at="", model_name=""))


def _batch(connection, batch, context=None):
    heads, references = (context['headers'], context['references']) if context is not None else _stage_context(connection, batch)
    rows_for = (lambda namespace: context['rows'].get(namespace, [])) if context is not None else (lambda namespace: _rows(connection, namespace, batch))
    base, preview = heads.get("00_collection", {}), heads.get("02_annotation") or heads.get("01_conversion") or {}
    runs = rows_for("collection_runs")
    completed = [r for r in runs if r.get("status") == "completed"]
    count = sum(r.get("trajectory_count", 0) for r in completed)
    errors = sum(r.get("errors_count", 0) for r in completed)
    running = any(r.get("status") in {"dispatching", "running"} for r in runs)
    jobs = rows_for("preprocessing_jobs")
    job = max(jobs, key=lambda j: (j.get("created_at", ""), j.get("job_id", "")), default=None)
    kind = base.get("kind", "existing_trajectories")
    cs = "partial" if errors and count else "ready" if count else "collecting" if running else "error" if errors or any(r.get("status") == "failed" for r in runs) else "waiting"
    if kind == "existing_trajectories" and preview: cs = "ready"
    preparing = bool(set(references) - set(heads)) or any(h.get("projection_error") for h in heads.values())
    available = bool(count) and not preparing and not (job and job.get("status") in {"queued", "running"})
    return {"batch_id": batch, "kind": kind, "label": {"task_generation": "任务生成", "augmentation": "任务泛化扩增", "manual_collection": "手工采集任务", "rollout_import": "导入 Rollout", "existing_trajectories": "已有轨迹批次"}.get(kind, kind),
        **{k: base[k] for k in ("batch_role", "task_source_batch_id", "agent_type", "collection_run_id") if k in base},
        "task_count": None if preparing else base.get("task_count") or preview.get("task_count", 0),
        "ready_trajectory_count": count or (None if preparing else preview.get("trajectory_count", 0)),
        "ready_step_count": sum(r.get("source_step_count", 0) for r in completed) or (None if preparing else preview.get("step_count", 0)),
        "collection_status": cs, "preprocessing_status": job.get("status") if job else "succeeded" if heads.get("02_annotation") else "not_started",
        "latest_job": job, "annotation_version": heads.get("02_annotation", {}).get("artifact_version"),
        "artifacts": [references[stage] for stage in ('01_conversion', '02_annotation') if stage in references],
        "can_start": available, "reason": "页面摘要正在准备" if preparing else None if available else "等待采集结果或当前预处理完成",
        "collecting": running, "collection_errors": [], "collection_error_count": errors,
        "updated_at": max([h.get("updated_at", "") for h in heads.values()] + [r.get("created_at", "") for r in runs] + [(job or {}).get("updated_at", "")]),
        "summary_preparing": preparing, "summary_error": next((h['projection_error'] for h in heads.values() if h.get('projection_error')), None)}


def _batch_ids(connection, *, q='', batch_id=None):
    sql = """SELECT b.batch_id,MAX(b.updated_at) changed FROM (
        SELECT json_extract(payload,'$.batch_id') batch_id,updated_at FROM records WHERE namespace='artifacts'
            AND json_extract(payload,'$.stage') IN ('00_collection','01_conversion','02_annotation')
        UNION ALL SELECT batch_id,updated_at FROM record_summaries WHERE namespace IN ('collection_runs','preprocessing_jobs')
            AND projection_version=?
        ) b WHERE b.batch_id IS NOT NULL AND b.batch_id<>'' AND NOT EXISTS(SELECT 1 FROM record_summaries l
            WHERE l.namespace='batch_lifecycle' AND l.record_key=b.batch_id AND l.status IN ('published','archived'))"""
    args = [summary_index.VERSION]
    if batch_id is not None: sql += " AND b.batch_id=?"; args.append(batch_id)
    if q:
        sql += """ AND (instr(lower(b.batch_id),lower(?))>0 OR EXISTS (
            SELECT 1 FROM record_summaries d WHERE d.namespace='display_identifiers'
            AND d.projection_version=? AND json_extract(d.payload,'$.kind')='batch'
            AND json_extract(d.payload,'$.id')=b.batch_id AND instr(lower(json_extract(d.payload,'$.display_id')),lower(?))>0))"""
        args.extend([q, summary_index.VERSION, q])
        labels = {'task_generation': '任务生成', 'augmentation': '任务泛化扩增', 'manual_collection': '手工采集任务', 'rollout_import': '导入 Rollout', 'existing_trajectories': '已有轨迹批次'}
        kinds = [kind for kind, label in labels.items() if q.casefold() in label.casefold()]
        if kinds:
            # Broaden the query's final search group with display labels.
            sql = sql[:-1] + " OR EXISTS(SELECT 1 FROM record_summaries p WHERE p.namespace='page_stages' AND p.batch_id=b.batch_id AND json_extract(p.payload,'$.kind') IN (" + ','.join('?' for _ in kinds) + ")))"
            args.extend(kinds)
    sql += " GROUP BY b.batch_id ORDER BY changed DESC,b.batch_id DESC"
    return connection.execute(sql, args).fetchall()


def batch_page(root, *, offset=0, limit=10, q="", status="", batch_id=None, **_options):
    _validate(offset, limit)
    def read(connection):
        ids = _batch_ids(connection, q=q, batch_id=batch_id)
        chosen = ids if status else ids[offset:offset + limit]
        contexts = _batch_contexts(connection, [ident for ident, _ in chosen])
        if status:
            # Filter display metadata before pagination. Never inspect evidence.
            matching = [row for ident, _ in ids if (row := _batch(connection, ident, contexts[ident]))["preprocessing_status"] == status]
            rows, total = matching[offset:offset + limit], len(matching)
        else:
            rows, total = [_batch(connection, ident, contexts[ident]) for ident, _ in chosen], len(ids)
        complete = summary_index.ready(connection) and not any(r["summary_preparing"] for r in rows)
        return _envelope(rows, total, offset, limit, complete, batches=rows)
    return _read(root, read, _envelope([], 0, offset, limit, False, batches=[]))


def quality_candidate_page(root, *, offset=0, limit=10, q="", status="", **options):
    _validate(offset, limit)
    def read(connection):
        candidates, complete = [], summary_index.ready(connection)
        ids = [ident for ident, _ in _batch_ids(connection, q=q)]
        contexts = _batch_contexts(connection, ids, tasks=True)
        for batch in ids:
            tasks, heads = _task_rows(connection, batch, contexts[batch])
            refs = contexts[batch]['references']
            complete = complete and set(refs).issubset(heads) and not any(h.get("projection_error") for h in heads.values())
            tree_tasks = [row for row in tasks if row['tree_status'] == 'succeeded']
            reviewed = [row for row in tree_tasks if row.get('result_state') != 'partial'
                and row.get('rubric_scores_valid') and row.get('evaluation_count', 0) >= row.get('tree_trajectory_count', 0)
                and row.get('tree_trajectory_count', 0) > 0]
            # Preserve the original selector contract: at least one valid task.
            # Partial rows remain visible on quality page, never eligible here.
            if not reviewed: continue
            candidates.append(dict(batch_id=batch, storage_batch_id=batch, tree_run_id=batch,
                tree_completed_at=heads.get('04_tree', {}).get('completed_at', ''),
                quality_completed_at=heads.get('05_quality', {}).get('completed_at', ''),
                total_task_count=len(tree_tasks), reviewed_task_count=len(reviewed), status='ready', is_default=False))
        candidates.sort(key=lambda row: (row['quality_completed_at'], row['tree_completed_at'], row['tree_run_id']), reverse=True)
        default = candidates[0]['tree_run_id'] if candidates else None
        if status: candidates = [row for row in candidates if row['status'] == status]
        rows = candidates[offset:offset + limit]
        for row in rows: row['is_default'] = row['tree_run_id'] == default
        return _envelope(rows, len(candidates), offset, limit, complete, batches=rows,
                         default_batch_id=default, default_tree_run_id=default)
    return _read(root, read, _envelope([], 0, offset, limit, False, batches=[], default_batch_id=None, default_tree_run_id=None))


class StageBackfill:
    def __init__(self, root):
        self.root = Path(root)
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.run, name="page-stage-backfill", daemon=True)

    def run_once(self):
        store = ArtifactStore(self.root)
        if not store.records.database_path.exists(): return
        with store.records._connection() as connection:
            refs = connection.execute("SELECT record_key,payload FROM records WHERE namespace='artifacts' "
                "AND json_extract(payload,'$.stage') IN ('00_collection','01_conversion','02_annotation','04_tree','05_quality')").fetchall()
        chosen = _choose_references((key, json.loads(raw)) for key, raw in refs)
        for key, reference in chosen.values():
            if self.stop.is_set(): return
            old = store.records.get("page_stages", f"{reference['batch_id']}:{reference['stage']}") or {}
            if old.get("content_hash") == reference["content_hash"] and old.get("artifact_version") == reference["version"] and old.get("display_version") == VERSION: continue
            try:
                entries = projection_entries(reference, store.read_payload(reference))
                # CAS and projections share a short transaction. No file work here.
                with store.records._connection(write=True) as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    current = store.records._read(connection, "artifacts", key)
                    if current != reference:
                        connection.rollback(); continue
                    for entry in entries:
                        old_row = store.records._read(connection, entry["namespace"], entry["key"]) or {}
                        store.records._save(connection, entry["namespace"], entry["key"], entry["payload"], old_row.get("storage_revision", 0) + 1)
                    connection.commit()
            except Exception:
                LOG.exception("页面摘要补建失败 batch=%s stage=%s", reference.get("batch_id"), reference.get("stage"))

    def run(self):
        while not self.stop.is_set():
            try: self.run_once()
            except Exception: LOG.exception("页面摘要补建暂时失败")
            self.stop.wait(30)

    def close(self):
        self.stop.set()
        self.thread.join(timeout=2)
