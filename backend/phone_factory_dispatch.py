"""Batch workbook dispatch: parse batch tasks by APP and distribute them evenly
across the linked phones under the phone-factory workspace.

Each phone's assigned tasks are written to ``{workspace}/new-guigent-{phone_id}/test.xlsx``
so the device-side agent can pick them up. The existing remote collection flow is
left untouched; this step runs before it on "start selected batch".
"""
from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path
from typing import Any

from .phone_factory import PhoneFactoryError
from .task_generation.constants import ALLOWED_WORKBOOK_SUFFIXES, TASK_GENERATION_DIR

APP_COLUMN = "涉及APP"
CASE_COLUMN = "用例编号"
REPEAT_TIMES = 1

# Workspace that mirrors the whole phone-factory projects (one directory per phone).
PHONE_WORKSPACE_ROOT = Path(os.environ.get("PHONE_FACTORY_WORKSPACE") or r"D:\vlaphonefactory")
PHONE_DIR_PREFIX = "new-guigent-"


def _locate_batch_workbook(upload_dir: Path, batch_id: str) -> tuple[Path, str] | None:
    collections_dir = TASK_GENERATION_DIR / "collection_batches"
    candidates: list[Path] = []
    if collections_dir.is_dir():
        candidates.extend(collections_dir.glob(f"{batch_id}/*.xls*"))
    candidates.append(upload_dir / f"collection-batch-{batch_id}.xlsx")
    for candidate in candidates:
        if candidate.suffix.lower() not in ALLOWED_WORKBOOK_SUFFIXES:
            continue
        if candidate.name.startswith("~$"):
            continue
        if candidate.is_file():
            return candidate, str(candidate)
    return None


def _read_workbook(path: Path) -> list[dict[str, Any]]:
    try:
        import pandas as pd
    except ImportError as exc:
        raise RuntimeError("解析批次工作簿需要 pandas 依赖") from exc
    try:
        frames = pd.read_excel(path, sheet_name=None, dtype=object)
    except Exception as exc:
        raise PhoneFactoryError(f"批次工作簿解析失败：{exc}") from exc
    sheet = next(iter(frames.values())) if frames else None
    if sheet is None or sheet.empty:
        raise PhoneFactoryError("批次工作簿没有数据")
    if APP_COLUMN not in sheet.columns:
        raise PhoneFactoryError(f"批次工作簿缺少列：{APP_COLUMN}")
    return [row for row in sheet.to_dict(orient="records") if isinstance(row, dict)]


def _split_evenly(items: list[dict], phones: list[str]) -> dict[str, list[dict]]:
    buckets: dict[str, list[dict]] = {phone: [] for phone in phones}
    for index, item in enumerate(items):
        buckets[phones[index % len(phones)]].append(item)
    return buckets


def distribute_batch(upload_dir: Path, phone_apps: list[dict], batch_id: str) -> dict[str, Any]:
    """Parse ``collection_batches/{batch_id}`` workbook, group rows by the APP
    column, evenly assign each APP's tasks to its linked phones and persist one
    ``test.xlsx`` per phone under the phone-factory workspace.
    """
    located = _locate_batch_workbook(upload_dir, batch_id)
    if located is None:
        raise PhoneFactoryError(f"批次 {batch_id} 工作簿不存在")
    workbook, workbook_path = located
    rows = _read_workbook(workbook)
    if not rows:
        raise PhoneFactoryError("批次工作簿没有任务数据")

    # Phone ID -> list of apps this phone can run (deduplicated, kept in state order).
    phone_apps_by_app: dict[str, list[str]] = {}
    for row in phone_apps:
        app = str(row.get("app") or "").strip()
        phone_id = str(row.get("phone_id") or "").strip()
        if not app or not phone_id:
            continue
        bucket = phone_apps_by_app.setdefault(app, [])
        if phone_id not in bucket:
            bucket.append(phone_id)

    grouped: dict[str, list[dict]] = {}
    for row in rows:
        app = str(row.get(APP_COLUMN) or "").strip() or "未指定APP"
        grouped.setdefault(app, []).append(row)

    # Per-phone accumulated rows keep the original sheet order per APP.
    per_phone: dict[str, list[dict]] = {}
    assigned: list[dict[str, Any]] = []
    unassigned: list[dict[str, Any]] = []
    for app, app_rows in grouped.items():
        phones = phone_apps_by_app.get(app) or []
        if not phones:
            unassigned.append({"app": app, "count": len(app_rows)})
            continue
        buckets = _split_evenly(app_rows, phones)
        for phone_id, items in buckets.items():
            per_phone.setdefault(phone_id, []).extend(items)
            assigned.append({"phone_id": phone_id, "app": app, "count": len(items)})

    written: list[dict[str, Any]] = []
    import pandas as pd

    columns = list(rows[0].keys()) if rows else []

    def _expand_repeat(items: list[dict]) -> list[dict]:
        if CASE_COLUMN not in columns:
            return [dict(item) for item in items for _ in range(REPEAT_TIMES)]
        expanded: list[dict] = []
        for item in items:
            case_val = item.get(CASE_COLUMN)
            for i in range(1, REPEAT_TIMES + 1):
                dup = dict(item)
                if case_val is None or str(case_val).strip() == "":
                    dup[CASE_COLUMN] = f"unknown-{i:03d}"
                else:
                    dup[CASE_COLUMN] = f"{str(case_val).strip()}-{i:03d}"
                expanded.append(dup)
        return expanded

    for phone_id, items in per_phone.items():
        phone_dir = PHONE_WORKSPACE_ROOT / f"{PHONE_DIR_PREFIX}{phone_id}"
        destination = phone_dir / "test.xlsx"
        expanded_items = _expand_repeat(items)
        frame = pd.DataFrame(expanded_items, columns=columns)
        phone_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            suffix=".xlsx", prefix=f".test-", dir=phone_dir, delete=False
        ) as staging:
            staging_path = Path(staging.name)
        try:
            frame.to_excel(staging_path, index=False, sheet_name="Sheet1")
            staging_path.replace(destination)
        finally:
            staging_path.unlink(missing_ok=True)
        written.append({"phone_id": phone_id, "count": len(expanded_items), "path": str(destination)})

    phone_count = len({item["phone_id"] for item in written})
    message = f"批次 {batch_id} 的 {len(rows)} 条任务已按涉及APP分发到 {phone_count} 台手机"
    if unassigned:
        skipped = sum(item["count"] for item in unassigned)
        message += f"，{skipped} 条任务因无对应手机未分派"
    return {
        "ok": True,
        "message": message,
        "source_workbook": workbook_path,
        "assigned": assigned,
        "unassigned": unassigned,
        "written": written,
    }
