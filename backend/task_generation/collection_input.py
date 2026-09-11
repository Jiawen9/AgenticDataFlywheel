"""Read-only contract for future collection consumers of saved generation results."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError


class CollectionInputError(ValueError):
    """The persisted job cannot currently provide a valid collection input."""


class CollectionTask(BaseModel):
    model_config = ConfigDict(strict=True)

    task_id: str = Field(description="原 task_uuid；缺失时使用原 result_id，不创建新编号")
    source_result_id: str = Field(description="生成侧原 result_id")
    task: str = Field(description="当前保存的任务文本，包含人工修改")
    app: str
    scene: str | None
    capability: str | None
    sub_capability: str | None
    pre_dependency: Literal["zero", "weak", "strong", "pre_node", "unknown"] = Field(
        description="原依赖类型；没有判定记录时为 unknown"
    )
    pre_task_id: str | None = Field(description="原 pre_task_uuid；缺失时为 null")
    source_status: Any = Field(description="原行级 status，保留原类型；缺失时为 null，不推断审核状态")
    source_seed_id: str | None = Field(description="原 seed_id；历史结果可能缺失")
    source_row: int | str | None
    source_task: str | None
    case_id: str | None = Field(description="原用例编号；缺失时为 null")
    dependency_error: Any = Field(description="原 dependency_error；缺失时为 null")


class CollectionInput(BaseModel):
    model_config = ConfigDict(strict=True)

    schema_version: Literal[1] = 1
    job_id: str
    kind: Literal["task_generation", "augmentation"]
    job_status: Literal["succeeded", "partial"]
    knowledge_base_version: str | None
    task_count: int
    tasks: list[CollectionTask]
    errors: list[Any]
    warnings: list[Any]


def _optional_identifier(value: Any, field: str, row: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise CollectionInputError(f"第 {row} 条结果的 {field} 必须为字符串")
    return value if value.strip() else None


def _required_text(value: Any, field: str, row: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CollectionInputError(f"第 {row} 条结果的 {field} 缺失或不是非空字符串")
    return value


def build_collection_input(job: dict[str, Any], records: Any) -> dict[str, Any]:
    """Project and validate saved values without modifying either input object."""
    if not isinstance(records, list) or any(not isinstance(item, dict) for item in records):
        raise CollectionInputError("作业结果格式无效，必须为结果记录数组")

    tasks: list[dict[str, Any]] = []
    by_id: dict[str, dict[str, Any]] = {}
    result_ids: set[str] = set()
    for row, record in enumerate(records, start=1):
        deleted = record.get("deleted")
        if deleted is not None and not isinstance(deleted, bool):
            raise CollectionInputError(f"第 {row} 条结果的 deleted 必须为布尔值")
        if deleted is True:
            continue
        result_id = _required_text(record.get("result_id"), "result_id", row)
        task_id = _optional_identifier(record.get("task_uuid"), "task_uuid", row) or result_id
        if task_id in by_id:
            raise CollectionInputError(f"任务编号重复：{task_id}")
        if result_id in result_ids:
            raise CollectionInputError(f"来源结果编号重复：{result_id}")
        dependency = _optional_identifier(record.get("pre_dependency"), "pre_dependency", row) or "unknown"
        if dependency not in {"zero", "weak", "strong", "pre_node", "unknown"}:
            raise CollectionInputError(f"第 {row} 条结果的 pre_dependency 无效：{dependency}")
        task = {
            "task_id": task_id,
            "source_result_id": result_id,
            "task": _required_text(record.get("task"), "task", row),
            "app": _required_text(record.get("app"), "app", row),
            "scene": record.get("scene"),
            "capability": record.get("capability"),
            "sub_capability": record.get("sub_capability"),
            "pre_dependency": dependency,
            "pre_task_id": _optional_identifier(record.get("pre_task_uuid"), "pre_task_uuid", row),
            "source_status": record.get("status"),
            "source_seed_id": record.get("seed_id"),
            "source_row": record.get("source_row"),
            "source_task": record.get("source_task"),
            "case_id": record.get("用例编号"),
            "dependency_error": record.get("dependency_error"),
        }
        tasks.append(task)
        by_id[task_id] = task
        result_ids.add(result_id)

    for task in tasks:
        task_id, pre_id = task["task_id"], task["pre_task_id"]
        if pre_id is not None:
            if pre_id == task_id:
                raise CollectionInputError(f"任务 {task_id} 的前置任务不能引用自身")
            if pre_id not in by_id:
                raise CollectionInputError(f"任务 {task_id} 的前置任务不存在或已删除：{pre_id}")
        if task["pre_dependency"] == "weak" and (pre_id is None or by_id[pre_id]["pre_dependency"] != "pre_node"):
            raise CollectionInputError(f"弱依赖任务 {task_id} 必须关联有效的 pre_node 前置任务")

    try:
        return CollectionInput(
            job_id=job.get("job_id"),
            kind=job.get("kind"),
            job_status=job.get("status"),
            knowledge_base_version=job.get("knowledge_base_version"),
            task_count=len(tasks),
            tasks=tasks,
            errors=job.get("errors", []),
            warnings=job.get("warnings", []),
        ).model_dump()
    except ValidationError as exc:
        fields = ", ".join(".".join(map(str, error["loc"])) for error in exc.errors())
        raise CollectionInputError(f"作业保存的数据不符合任务读取协议：{fields}") from exc
