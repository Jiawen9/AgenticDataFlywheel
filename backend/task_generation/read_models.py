"""Display projections. Authoritative exports and edits still use saved job records."""
from __future__ import annotations

import json
from typing import Any


def project_job(payload: dict) -> dict:
    fields = ("job_id", "kind", "status", "stage", "percent", "created_at", "updated_at",
              "started_at", "completed_at", "current_item", "input_filename", "generate_n", "total_items",
              "completed_items", "result_count", "task_type_count", "expected_main_tasks",
              "knowledge_base_version", "augmentation_preview_version", "augmentation_generation_started")
    value = {key: payload[key] for key in fields if key in payload}
    value["filename"] = payload.get("input_filename") or ""
    value["error_count"] = len(payload.get("errors") or [])
    value["warning_count"] = len(payload.get("warnings") or [])
    value["seed_stats"] = {key: count for key, count in (payload.get("seed_stats") or {}).items()
                           if isinstance(count, (int, float))}
    return value


def result_page(store, job_id: str, *, offset=0, limit=10, app="", task_type="", dependency="",
                show_deleted=False, node_id="", seed_id="", search="") -> dict[str, Any] | None:
    """Page whole dependency/source groups in SQLite, not a complete Python rows list.

    The current record is the source of truth; updates are visible atomically and
    no derived cache can hide edits. Export deliberately does not use this view.
    """
    with store._connection() as connection:
        connection.execute("PRAGMA busy_timeout=250")
        connection.execute("BEGIN")
        job = connection.execute("SELECT json_extract(payload,'$.kind') FROM records WHERE namespace='task_generation.jobs' AND record_key=?", (job_id,)).fetchone()
        if job is None:
            return None
        augmentation = job[0] == "augmentation"
        rows_sql = """SELECT CAST(j.key AS INTEGER) AS position, j.value AS row,
            COALESCE(json_extract(j.value,'$.result_id'),'') AS id,
            COALESCE(json_extract(j.value,'$.pre_dependency'),'') AS dependency,
            COALESCE(json_extract(j.value,'$.app'),'') AS app,
            COALESCE(json_extract(j.value,'$.deleted'),0) AS deleted,
            COALESCE(json_extract(j.value,'$.seed_id'),'') AS seed
            FROM records r, json_each(r.payload,'$.rows') j
            WHERE r.namespace='task_generation.results' AND r.record_key=:job"""
        if augmentation:
            group_sql = """SELECT *, CASE WHEN seed<>'' THEN 'seed:'||seed
                WHEN json_extract(row,'$.source_row') IS NOT NULL THEN 'row:'||json_extract(row,'$.source_row')
                ELSE 'result:'||id END AS group_id FROM rows"""
            mains_sql = "SELECT *, MIN(position) AS first_position FROM grouped GROUP BY group_id"
        else:
            group_sql = "SELECT *, id AS group_id FROM rows"
            mains_sql = """SELECT *,position AS first_position FROM grouped g WHERE dependency<>'pre_node'
                OR NOT EXISTS (SELECT 1 FROM rows m WHERE m.dependency<>'pre_node' AND
                ((json_extract(m.row,'$.pre_task_uuid') IS NOT NULL AND json_extract(m.row,'$.pre_task_uuid')=json_extract(g.row,'$.task_uuid'))
                OR (COALESCE(json_extract(m.row,'$.dependency_group_id'),'')<>'' AND json_extract(m.row,'$.dependency_group_id')=json_extract(g.row,'$.dependency_group_id'))))"""
        cte = f"WITH rows AS ({rows_sql}), grouped AS ({group_sql}), mains AS ({mains_sql}) "
        where = ["1=1"]
        args = {"job": job_id, "offset": offset, "limit": limit}
        if not show_deleted:
            where.append("deleted=0")
        if app:
            where.append("app=:app"); args["app"] = app
        if task_type:
            where.append("json_array(json_extract(row,'$.scene'),json_extract(row,'$.capability'),json_extract(row,'$.sub_capability'))=:task_type")
            args["task_type"] = task_type
        if dependency:
            where.append("COALESCE(json_extract(row,'$.dependency_error'),'')<>''" if dependency == "error" else "dependency=:dependency AND COALESCE(json_extract(row,'$.dependency_error'),'')=''")
            args["dependency"] = dependency
        if seed_id:
            where.append("seed=:seed"); args["seed"] = seed_id
        if node_id:
            where.append("seed IN (SELECT json_extract(s.value,'$.seed_id') FROM records sr,json_each(sr.payload,'$.rows') s,json_each(s.value,'$.node_path_ids') n WHERE sr.namespace='task_generation.seeds' AND sr.record_key=:job AND n.value=:node)")
            args["node"] = node_id
        if search:
            where.append("instr(lower(row),lower(:search))>0"); args["search"] = search
        condition = " AND ".join(where)
        total = connection.execute(cte + "SELECT count(*) FROM mains WHERE " + condition, args).fetchone()[0]
        page = connection.execute(cte + "SELECT row,group_id FROM mains WHERE " + condition + " ORDER BY first_position LIMIT :limit OFFSET :offset", args).fetchall()
        groups = []
        selected = [(json.loads(raw), identity) for raw, identity in page]
        # One bounded selection query for every group on this page, including
        # shared prerequisites. Avoid reparsing the full JSON once per group.
        members = []
        if selected:
            member_args = dict(args)
            if augmentation:
                placeholders = []
                for index, (_, identity) in enumerate(selected):
                    name = f"group_{index}"
                    member_args[name] = identity
                    placeholders.append(":" + name)
                member_rows = connection.execute(cte + "SELECT row,group_id FROM grouped WHERE group_id IN (" + ",".join(placeholders) + ") ORDER BY position", member_args).fetchall()
                by_group = {}
                for raw, identity in member_rows:
                    by_group.setdefault(identity, []).append(json.loads(raw))
                groups = [{"group_id": identity, "results": by_group.get(identity, [])} for _, identity in selected]
            else:
                conditions = []
                for index, (main, _) in enumerate(selected):
                    member_args.update({f"id_{index}": main.get("result_id", ""), f"pre_{index}": main.get("pre_task_uuid") or "", f"dep_{index}": main.get("dependency_group_id") or ""})
                    conditions.append(f"(id=:id_{index} OR (dependency='pre_node' AND ((:pre_{index}<>'' AND json_extract(row,'$.task_uuid')=:pre_{index}) OR (:dep_{index}<>'' AND json_extract(row,'$.dependency_group_id')=:dep_{index}))))")
                members = [json.loads(value[0]) for value in connection.execute(cte + "SELECT row FROM rows WHERE " + " OR ".join(conditions) + " ORDER BY position", member_args)]
                for main, identity in selected:
                    selected_rows = [row for row in members if row.get("result_id") == main.get("result_id") or row.get("pre_dependency") == "pre_node" and
                                     (bool(main.get("pre_task_uuid") and main["pre_task_uuid"] == row.get("task_uuid")) or bool(main.get("dependency_group_id") and main["dependency_group_id"] == row.get("dependency_group_id")))]
                    groups.append({"group_id": identity, "results": selected_rows})
        counts = connection.execute(cte + "SELECT count(*),sum(deleted=0),sum(deleted=0 AND dependency<>'pre_node'),sum(deleted=0 AND dependency='pre_node'),sum(deleted=0 AND dependency='strong') FROM rows", args).fetchone()
        apps = [row[0] for row in connection.execute(cte + "SELECT DISTINCT app FROM mains ORDER BY app", args)]
        type_rows = connection.execute(cte + "SELECT DISTINCT json_array(json_extract(row,'$.scene'),json_extract(row,'$.capability'),json_extract(row,'$.sub_capability')),json_extract(row,'$.scene')||' / '||json_extract(row,'$.capability')||' / '||json_extract(row,'$.sub_capability') FROM mains", args).fetchall()
        return {"groups": groups, "results": [row for group in groups for row in group["results"]], "errors": [],
                "total": total, "offset": offset, "limit": limit, "has_more": offset + limit < total,
                "counts": dict(zip(("all", "active", "main", "prerequisites", "strong"), (value or 0 for value in counts))),
                "apps": apps, "types": [[value or "", label or ""] for value, label in type_rows]}
