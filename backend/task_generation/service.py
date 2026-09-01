from __future__ import annotations

import json
import logging
import secrets
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

try:
    from pypinyin import Style, pinyin
except ImportError:  # Keep the API usable before optional dependencies are installed.
    Style = None  # type: ignore[assignment]
    pinyin = None  # type: ignore[assignment]

from .config import load_model_config
from .knowledge_base import merged_nodes, node_id, scene_tree_text
from .model_client import TaskGenerationModel, parse_json_value, parse_jsonl_tasks
from .prompts import dependency_prompt, flywheel_prompt, scene_classification_prompt, system_prompt


Progress = Callable[[dict[str, Any]], None]


def _now() -> str:
    return datetime.now().astimezone().isoformat()


def _text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    return str(value).strip()


def _model(model: TaskGenerationModel | None) -> TaskGenerationModel:
    return model or TaskGenerationModel()


def classify_pre_task_scene(task: str, app: str, *, kb_root: Path, model: TaskGenerationModel) -> dict[str, str]:
    raw = model.complete(scene_classification_prompt(scene_tree_text(kb_root), task, app), temperature=0.2, max_tokens=600)
    value = parse_json_value(raw, dict)
    return {
        "scene": _text(value.get("scene"), "Unclassified"),
        "capability": _text(value.get("capability"), "Unclassified"),
        "sub_capability": _text(value.get("sub_capability"), "Unclassified"),
        "reason": _text(value.get("reason")),
    }


def _dependency_tasks(item: dict[str, Any], *, kb_root: Path, model: TaskGenerationModel) -> list[dict[str, Any]]:
    main = dict(item)
    main_uuid = str(uuid.uuid4())
    main["task_uuid"] = main_uuid
    main["pre_task_uuid"] = None
    app = _text(item.get("app") or item.get("target_app"), "未知应用")
    task = _text(item.get("task"))
    group_id = main_uuid
    try:
        dependency = parse_json_value(model.complete(dependency_prompt(task, app), temperature=0.2, max_tokens=700), dict)
        relationship = _text(dependency.get("dependency_relationships"), "zero").lower()
        if relationship == "weak" and _text(dependency.get("pre_task")).lower() not in {"", "null"}:
            pre_text = _text(dependency.get("pre_task"))
            pre_uuid = str(uuid.uuid4())
            scene = classify_pre_task_scene(pre_text, app, kb_root=kb_root, model=model)
            pre = dict(item)
            pre.update({
                "app": app, "target_app": app, "task": pre_text, "task_uuid": pre_uuid,
                "pre_task_uuid": None, "pre_dependency": "pre_node", "dependency_group_id": group_id,
                "status": pre.get("status"), "scene": scene["scene"], "capability": scene["capability"],
                "sub_capability": scene["sub_capability"], "result_id": pre_uuid, "deleted": False,
            })
            main.update({"app": app, "target_app": app, "pre_dependency": "weak", "pre_task_uuid": pre_uuid, "dependency_group_id": group_id, "result_id": main_uuid, "deleted": False})
            return [pre, main]
        if relationship == "strong":
            main.update({"app": app, "target_app": app, "pre_dependency": "strong", "pre_task_uuid": None, "status": "-2", "dependency_group_id": group_id, "result_id": main_uuid, "deleted": False})
            return [main]
    except Exception as exc:
        logging.warning("依赖判定失败，任务回退为 zero：%s", exc)
        main["dependency_error"] = str(exc)
    main.update({"app": app, "target_app": app, "pre_dependency": "zero", "pre_task_uuid": None, "dependency_group_id": group_id, "result_id": main_uuid, "deleted": False})
    return [main]


def _initial_node(row: dict[str, Any], *, generate_n: int, kb_root: Path, model: TaskGenerationModel) -> list[dict[str, Any]]:
    prompt = system_prompt(
        row["scene"], row["capability"], row["sub_capability"], row["sub_capability_desc"],
        row["target_app"], row["resource_prior"], row.get("reference_example", ""), generate_n,
    )
    raw = model.complete(prompt, max_tokens=max(2048, generate_n * 450))
    generated = parse_jsonl_tasks(raw)
    if not generated:
        preview = " ".join(raw.split())[:800] if raw else "<空响应>"
        raise ValueError(f"生成节点没有返回可解析的任务；模型响应片段：{preview}")
    results: list[dict[str, Any]] = []
    for task_index, value in enumerate(generated):
        task = _text(value.get("task"))
        if not task:
            continue
        normalized = dict(value)
        normalized.update({
            "app": row["target_app"],
            "target_app": row["target_app"],
            "scene": row["scene"],
            "capability": row["capability"],
            "sub_capability": row["sub_capability"],
            "task": task,
            "source_node_id": row["task_type_id"],
            "execution_unit_id": row["node_id"],
            "task_index": task_index,
            "created_at": _now(),
        })
        results.extend(_dependency_tasks(normalized, kb_root=kb_root, model=model))
    if not results:
        raise ValueError("生成节点未返回非空任务文本")
    return results


def run_initial_generation(node_ids: list[str], generate_n: int, *, kb_root: Path, progress: Progress, model: TaskGenerationModel | None = None) -> dict[str, Any]:
    model = _model(model)
    rows = merged_nodes(kb_root, sample_num=generate_n)
    selected = [row for row in rows if row["node_id"] in set(node_ids)]
    by_id = {row["node_id"]: row for row in selected}
    if any(identifier not in by_id for identifier in node_ids):
        raise ValueError("提交的执行单元不在作业知识库快照中")
    results: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    workers = load_model_config().max_concurrent
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="task-generation-node") as executor:
        futures = {executor.submit(_initial_node, by_id[node_id_value], generate_n=generate_n, kb_root=kb_root, model=model): node_id_value for node_id_value in node_ids if node_id_value in by_id}
        for completed, future in enumerate(as_completed(futures), start=1):
            current_id = futures[future]
            try:
                results.extend(future.result())
            except Exception as exc:
                errors.append({"item_id": current_id, "error": str(exc)})
            progress({"stage": "generating", "current_item": current_id, "completed_items": completed, "total_items": len(futures), "percent": round(completed / max(1, len(futures)) * 100)})
    order = {value: index for index, value in enumerate(node_ids)}
    results.sort(key=lambda item: (order.get(item.get("execution_unit_id", ""), len(order)), item.get("task_index", 0), 0 if item.get("pre_dependency") == "pre_node" else 1))
    return {"results": results, "errors": errors, "warnings": [], "total_items": len(futures)}


def get_chinese_initials(value: Any) -> str:
    text = _text(value)
    if not text:
        return ""
    if pinyin is not None and Style is not None:
        return "".join(item[0].upper() for item in pinyin(text, style=Style.FIRST_LETTER, strict=False) if item and item[0].isalpha())
    # pypinyin is declared in requirements.txt. This fallback keeps local
    # smoke tests and a newly-created environment functional until install.
    return "".join(char.upper() for char in text if char.isascii() and char.isalpha())


def generate_case_id(app: str, scene: str, short_uuid: str, sequence: int) -> str:
    scene_prefix = str(scene).split("--", 1)[0].split("-", 1)[0].strip()
    return f"{get_chinese_initials(scene_prefix)}-{get_chinese_initials(app)}-{short_uuid}-{sequence}"


def _read_seed_workbook(path: Path) -> list[dict[str, Any]]:
    import pandas as pd

    with pd.ExcelFile(path) as excel:
        sheet = "新场景匹配" if "新场景匹配" in excel.sheet_names else excel.sheet_names[0]
        frame = pd.read_excel(excel, sheet_name=sheet)
    columns = set(str(column) for column in frame.columns)
    classified = {"app", "task", "scene", "capability", "sub_capability"}.issubset(columns)
    if not classified and not {"任务", "涉及APP"}.issubset(columns):
        raise ValueError("种子 Excel 必须包含 任务/涉及APP，或 app/task/scene/capability/sub_capability")
    rows = []
    for index, row in frame.iterrows():
        if classified:
            value = {key: _text(row.get(key)) for key in ("app", "task", "scene", "capability", "sub_capability")}
            value["source_row"] = int(index) + 2
        else:
            result = _text(row.get("任务结果")).upper()
            if "任务结果" in columns and result == "TRUE":
                continue
            value = {"app": _text(row.get("涉及APP")), "task": _text(row.get("任务")), "source_row": int(index) + 2}
        if value.get("app") and value.get("task"):
            rows.append(value)
    if not rows:
        raise ValueError("Excel 中没有可扩增的种子任务")
    return rows


def _seed_classify(seed: dict[str, Any], *, kb_root: Path, model: TaskGenerationModel) -> dict[str, Any]:
    if all(seed.get(key) for key in ("scene", "capability", "sub_capability")):
        return seed
    classified = classify_pre_task_scene(seed["task"], seed["app"], kb_root=kb_root, model=model)
    return {**seed, **classified}


def _variant_records(seed: dict[str, Any], *, generate_n: int, kb_root: Path, model: TaskGenerationModel) -> list[dict[str, Any]]:
    nodes = merged_nodes(kb_root, sample_num=generate_n)
    node = next((item for item in nodes if item["node_id"] == node_id(seed["app"], seed["scene"], seed["capability"], seed["sub_capability"])), None)
    context = dict(seed)
    if node:
        context.update({"sub_capability_desc": node["sub_capability_desc"], "reference_example": node.get("reference_example", "")})
        prior = node["resource_prior"]
    else:
        prior = []
    value = parse_json_value(model.complete(flywheel_prompt(context, prior, generate_n), max_tokens=max(2048, generate_n * 350)))
    if isinstance(value, dict):
        value = [value]
    if not isinstance(value, list):
        raise ValueError("扩增模型返回必须是 JSON 数组")
    short_uuid = secrets.token_hex(3)
    records = []
    for sequence, item in enumerate(value, start=1):
        task = _text(item.get("task")) if isinstance(item, dict) else ""
        if not task:
            continue
        records.append({
            "result_id": uuid.uuid4().hex,
            "source_row": seed.get("source_row"),
            "source_task": seed["task"],
            "用例编号": generate_case_id(seed["app"], seed.get("scene", "Unclassified"), short_uuid, sequence),
            "源失败任务": seed["task"], "app": seed["app"], "scene": seed.get("scene", "Unclassified"),
            "capability": seed.get("capability", "Unclassified"), "sub_capability": seed.get("sub_capability", "Unclassified"),
            "生成的变体任务": task, "task": task, "run": "flywheel", "审核状态": "待人工Review",
            "deleted": False, "created_at": _now(),
        })
    if not records:
        raise ValueError("扩增节点没有返回可解析的变体任务")
    return records


def run_augmentation(path: Path, generate_n: int, *, kb_root: Path, progress: Progress, model: TaskGenerationModel | None = None) -> dict[str, Any]:
    model = _model(model)
    seeds = _read_seed_workbook(path)
    total = len(seeds)
    classified: list[dict[str, Any] | None] = [None] * total
    errors: list[dict[str, Any]] = []
    workers = load_model_config().max_concurrent
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="task-augmentation-classify") as executor:
        futures = {executor.submit(_seed_classify, seed, kb_root=kb_root, model=model): index for index, seed in enumerate(seeds)}
        for completed, future in enumerate(as_completed(futures), start=1):
            index = futures[future]
            try:
                classified[index] = future.result()
            except Exception as exc:
                errors.append({"item_id": str(seeds[index].get("source_row", index)), "stage": "classifying", "error": str(exc)})
            progress({"stage": "classifying", "current_item": str(seeds[index].get("source_row", index)), "completed_items": completed, "total_items": total * 2, "percent": round(completed / max(1, total) * 40)})
    valid_seeds = [item for item in classified if item is not None]
    records: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="task-augmentation-generate") as executor:
        futures = {executor.submit(_variant_records, seed, generate_n=generate_n, kb_root=kb_root, model=model): seed for seed in valid_seeds}
        for completed, future in enumerate(as_completed(futures), start=1):
            seed = futures[future]
            try:
                records.extend(future.result())
            except Exception as exc:
                errors.append({"item_id": str(seed.get("source_row", "")), "stage": "generating", "error": str(exc)})
            progress({"stage": "generating", "current_item": str(seed.get("source_row", "")), "completed_items": total + completed, "total_items": total * 2, "percent": 40 + round(completed / max(1, len(valid_seeds)) * 60)})
    records.sort(key=lambda item: (int(item.get("source_row") or 0), item.get("用例编号", "")))
    return {"results": records, "errors": errors, "warnings": [], "total_items": total}
