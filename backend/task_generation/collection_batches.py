"""Immutable collection batches projected from validated generation results."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Literal

from openpyxl import Workbook
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
from openpyxl.styles import Font
from openpyxl.utils import get_column_letter
from pydantic import BaseModel, ConfigDict

from .collection_input import CollectionInput, CollectionInputError, CollectionTask


COLLECTION_COLUMNS = [
    "用例编号", "单APP跨APP", "涉及APP", "一级场景", "二级场景", "三级场景", "任务",
    "难易程度", "页面关键元素", "预期步数", "用例来源", "构建人", "任务结束标志",
    "任务复杂度", "关键动作", "SOP", "动态变量说明",
]


class CollectionBatchTask(CollectionTask):
    collection_case_id: str


class CollectionBatchSnapshot(CollectionInput):
    tasks: list[CollectionBatchTask]


class CollectionBatchSummary(BaseModel):
    model_config = ConfigDict(strict=True)

    schema_version: Literal[1] = 1
    batch_id: str
    source_job_id: str
    kind: Literal["task_generation", "augmentation"]
    job_status: Literal["succeeded", "partial"]
    knowledge_base_version: str | None
    created_at: str
    task_count: int
    apps: list[str]
    filename: str
    download_url: str


class CollectionBatchDetail(CollectionBatchSummary):
    snapshot: CollectionBatchSnapshot


class CollectionBatchList(BaseModel):
    batches: list[CollectionBatchSummary]


def validate_batch_id(batch_id: str) -> None:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", batch_id):
        raise CollectionInputError("采集批次编号无效")


def payload_digest(payload: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def workbook_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def collection_batch_payload(snapshot: dict[str, Any], created_at: str) -> dict[str, Any]:
    if not snapshot["tasks"]:
        raise CollectionInputError("没有未删除任务，无法提交采集批次")
    batch_id = snapshot["job_id"]
    validate_batch_id(batch_id)
    seen: set[str] = set()
    tasks = []
    for task in snapshot["tasks"]:
        case_id = task.get("case_id") if snapshot["kind"] == "augmentation" else None
        case_id = case_id if isinstance(case_id, str) and case_id.strip() else task["task_id"]
        if case_id in seen:
            raise CollectionInputError(f"采集用例编号重复：{case_id}")
        seen.add(case_id)
        tasks.append({**task, "collection_case_id": case_id})
    return CollectionBatchDetail(
        batch_id=batch_id,
        source_job_id=batch_id,
        kind=snapshot["kind"],
        job_status=snapshot["job_status"],
        knowledge_base_version=snapshot["knowledge_base_version"],
        created_at=created_at,
        task_count=len(tasks),
        apps=list(dict.fromkeys(task["app"] for task in tasks)),
        filename=f"collection-batch-{batch_id}.xlsx",
        download_url=f"/api/task-generation/collection-batches/{batch_id}/workbook",
        snapshot={**snapshot, "tasks": tasks},
    ).model_dump()


def write_collection_workbook(snapshot: dict[str, Any], path: Path) -> None:
    """Keep all 17 headers; write the first seven values as literal text."""
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Sheet1"
    sheet.append(COLLECTION_COLUMNS)
    for header in sheet[1]:
        header.font = Font(bold=True)
    for row_number, task in enumerate(snapshot["tasks"], start=2):
        values = [task["collection_case_id"], "单APP", task["app"], task["scene"],
                  task["capability"], task["sub_capability"], task["task"]]
        for column, value in enumerate(values, start=1):
            if value is not None:
                if len(value) > 32767 or ILLEGAL_CHARACTERS_RE.search(value):
                    raise CollectionInputError(f"第 {row_number - 1} 条任务的 {COLLECTION_COLUMNS[column - 1]} 无法完整写入 Excel")
                cell = sheet.cell(row_number, column, value=value)
                cell.data_type = "s"
                cell.number_format = "@"
        # Persist blank cells too, so readers see the exact 17-column contract.
        for column in range(8, len(COLLECTION_COLUMNS) + 1):
            sheet.cell(row_number, column, value=None)
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = sheet.dimensions
    for column in range(1, len(COLLECTION_COLUMNS) + 1):
        sheet.column_dimensions[get_column_letter(column)].width = 70 if column == 7 else 24
    try:
        workbook.save(path)
    finally:
        workbook.close()
