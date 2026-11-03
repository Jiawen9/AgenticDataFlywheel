"""Bounded background retrieval of immutable, paged evaluation previews."""
from __future__ import annotations

from .collection_runs import _identifier
from .data_store.registry import utc_now
from .phone_factory import PhoneFactoryError


def public(value, offset=0, limit=50):
    offset, limit = int(offset), int(limit)
    if offset < 0 or not 1 <= limit <= 100:
        raise PhoneFactoryError("预览分页无效")
    cases = value.get("cases", [])
    return {**value, "cases": cases[offset:offset + limit], "case_offset": offset,
            "has_more": offset + limit < len(cases)}


def validate(value, *, complete=False):
    import re
    if (not isinstance(value, dict) or type(value.get("valid")) is not bool
            or not isinstance(value.get("cases"), list) or not isinstance(value.get("errors"), list)
            or type(value.get("trajectory_count")) is not int or value["trajectory_count"] < 0):
        raise PhoneFactoryError("远端轨迹预览格式无效", 502)
    _identifier(value.get("preview_id"), "预览编号")
    if value.get("status") == "preparing":
        return
    if not value["valid"]:
        return
    if (not isinstance(value.get("source"), dict)
            or not re.fullmatch(r"[0-9a-f]{64}", str(value.get("source_digest", "")))
            or value["trajectory_count"] <= 0
            or complete and value["trajectory_count"] != len(value["cases"])):
        raise PhoneFactoryError("远端轨迹预览身份或数量无效", 502)
    seen = set()
    for row in value["cases"]:
        if (not isinstance(row, dict) or any(not isinstance(row.get(k), str) or not row[k].strip()
                for k in ("source_trajectory_id", "case_id", "task"))
                or row["source_trajectory_id"] in seen or not isinstance(row.get("phone_id", ""), str)):
            raise PhoneFactoryError("远端轨迹预览存在缺失或重复的轨迹身份", 502)
        seen.add(row["source_trajectory_id"])


def accept(store, source, value):
    validate(value)
    preview_id = value["preview_id"]
    pending = value.get("status") == "preparing" or value.get("has_more") is True
    saved = {**value, "valid": False, "status": "preparing", "cases": []} if pending else value
    if not pending:
        validate(saved, complete=True)
    existing = store.records.get("model_iter_previews", preview_id)
    if existing and existing.get("request") != source:
        raise PhoneFactoryError("远端复用了其他来源的预览编号", 502)
    if existing and existing["preview"].get("status") != "preparing":
        if not pending and existing["preview"] != saved:
            raise PhoneFactoryError("远端复用了不同内容的预览编号", 502)
        return public(existing["preview"])
    if not existing:
        store.records.put("model_iter_previews", preview_id, {"preview_id": preview_id,
            "request": source, "preview": saved, "created_at": utc_now()}, expected_revision=0)
    elif not pending:
        commit(store, preview_id, source, saved)

    if pending:
        schedule(store, preview_id)
    return public(saved)


def commit(store, preview_id, source, value):
    def update(row):
        if row.get("request") != source:
            raise PhoneFactoryError("预览来源不可修改", 502)
        current = row["preview"]
        if current.get("status") != "preparing" and current != value:
            raise PhoneFactoryError("已完成预览内容不可修改", 502)
        row.update(preview=value, sync_error=None)
    return store.records.update("model_iter_previews", preview_id, update)


def schedule(store, preview_id):
    store.runtime._preview_pool.submit(preview_id, lambda: refresh(store, preview_id))


def refresh(store, preview_id):
    saved = store.records.get("model_iter_previews", preview_id)
    if not saved or saved["preview"].get("status") != "preparing":
        return
    try:
        value = store._remote_result(["evaluation-preview-status", preview_id, "--limit", "100"])
        validate(value)
        if value.get("preview_id") != preview_id or value.get("case_offset", 0) != 0:
            raise PhoneFactoryError("远端预览首页身份或页码不匹配", 502)
        if value.get("status") == "preparing":
            return
        cases = list(value["cases"])
        while value.get("has_more"):
            if len(cases) >= value["trajectory_count"]:
                raise PhoneFactoryError("远端预览分页超过声明的轨迹数量", 502)
            page = store._remote_result(["evaluation-preview-status", preview_id,
                "--offset", str(len(cases)), "--limit", "100"])
            validate(page)
            if (page.get("source_digest") != value.get("source_digest") or page.get("preview_id") != preview_id
                    or page.get("case_offset") != len(cases) or not page["cases"]
                    or page.get("trajectory_count") != value["trajectory_count"]
                    or page.get("source") != value.get("source")
                    or len(cases) + len(page["cases"]) > value["trajectory_count"]):
                raise PhoneFactoryError("远端预览分页身份或摘要变化", 502)
            cases.extend(page["cases"])
            value["has_more"] = page.get("has_more", False)
        value.update(cases=cases, case_offset=0, has_more=False)
        validate(value, complete=True)
        commit(store, preview_id, saved["request"], value)
    except Exception as exc:
        # A transient fetch failure does not discard the durable remote preview.
        store.records.update("model_iter_previews", preview_id, lambda row: row.update(sync_error=str(exc)))


def state(store, preview_id, offset=0, limit=50):
    preview_id = _identifier(preview_id, "预览编号")
    saved = store.records.get("model_iter_previews", preview_id)
    if not saved:
        raise PhoneFactoryError("预览不存在", 404)
    if saved["preview"].get("status") == "preparing":
        schedule(store, preview_id)
    from .collection_resume_snapshots import evaluation_snapshot_status
    return {**public(saved["preview"], offset, limit),
            **evaluation_snapshot_status(store.records,saved.get("request",{})),
            **({"sync_error": saved["sync_error"]} if saved.get("sync_error") else {})}
