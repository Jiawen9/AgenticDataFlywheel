from __future__ import annotations

import json
import re
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
from .response_parser import valid_task_text
from .prompts import dependency_prompt, flywheel_prompt, scene_classification_prompt, system_prompt


Progress = Callable[[dict[str, Any]], None]
SeedProgress = Callable[[dict[str, Any], list[dict[str, Any]] | None], None]


def _now() -> str:
    return datetime.now().astimezone().isoformat()


def _text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    return str(value).strip()


def _model(model: TaskGenerationModel | None, kb_root: Path) -> TaskGenerationModel:
    return model or TaskGenerationModel(diagnostics_dir=kb_root.parent / "model_calls")


class PartialGenerationError(ValueError):
    """Keep verified rows from a unit while surfacing every unresolved failure."""
    def __init__(self, results: list[dict[str, Any]], errors: list[dict[str, Any]]) -> None:
        super().__init__("；".join(item["error"] for item in errors))
        self.results, self.errors = results, errors


def _model_config(model: TaskGenerationModel):
    return getattr(model, "config", None) or load_model_config()


def _trace_hint(raw: str) -> str:
    trace = getattr(raw, "trace_id", None)
    return f"；诊断记录 {trace}.json" if trace else ""


def _checked_json(prompt: str, validate: Callable[[dict[str, Any]], Any], *, model: TaskGenerationModel, stage: str, item_id: str = "") -> Any:
    config = _model_config(model)
    issue = ""
    for attempt in range(config.validation_retries + 1):
        raw = ""
        try:
            correction = "" if not attempt else f"\n上次输出未通过校验：{issue[:300]}。请重新给出严格 JSON 最终答案，不要分析过程。"
            raw = model.complete(prompt + correction, temperature=0.2, max_tokens=config.classification_max_tokens, stage=stage, item_id=item_id)
            return validate(parse_json_value(raw, dict))
        except (ValueError, RuntimeError) as exc:
            issue = f"{exc}{_trace_hint(raw)}"
    raise ValueError(f"{stage} 输出校验失败：{issue}")


def _generate_tasks(prompt: str, count: int, *, model: TaskGenerationModel, item_id: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    config = _model_config(model)
    accepted: list[dict[str, Any]] = []
    seen: set[str] = set()
    last_issue = ""
    for attempt in range(config.validation_retries + 1):
        raw = ""
        issues = []
        try:
            correction = ""
            if attempt:
                correction = f"\n# 校验反馈\n上次输出存在问题：{last_issue[:300]}。本次只生成还缺少的 {count - len(accepted)} 条有效任务，不要重复以下已接受任务：\n{json.dumps([item['task'] for item in accepted], ensure_ascii=False)}\n只返回包含 tasks 数组的 JSON 对象，不要思考说明或格式示例。"
            raw = model.complete(prompt + correction, max_tokens=config.generation_max_tokens, stage="generating", item_id=item_id)
            values = parse_jsonl_tasks(raw)
            for index, item in enumerate(values, 1):
                try:
                    if not isinstance(item, dict):
                        raise ValueError("任务条目必须是 JSON 对象")
                    task = valid_task_text(item.get("task"))
                    key = re.sub(r"\s+", "", task).casefold()
                    if key in seen:
                        raise ValueError("重复任务")
                    if len(accepted) < count:
                        seen.add(key)
                        # Model metadata is not authoritative; context is restored by the caller.
                        accepted.append({"task": task})
                except ValueError as exc:
                    issues.append(f"第 {index} 项：{exc}")
            last_issue = "；".join(issues[:5]) or "有效任务数量不足"
        except (ValueError, RuntimeError) as exc:
            last_issue = str(exc)
        last_issue += _trace_hint(raw)
        if len(accepted) == count:
            return accepted, []
    return accepted, [{"stage": "validation", "error": f"要求 {count} 条主任务，仅得到 {len(accepted)} 条有效且不重复的任务。{last_issue}"}]


def classify_pre_task_scene(task: str, app: str, *, kb_root: Path, model: TaskGenerationModel, item_id: str = "") -> dict[str, str]:
    from .tree_store import _app_nodes, flatten, read_tree
    allowed = {labels for leaf, labels in flatten(read_tree(kb_root)["scenes"]) if any(node["label"] == app for node in _app_nodes(leaf))}
    def validate(value):
        keys = ("scene", "capability", "sub_capability")
        if any(not isinstance(value.get(key), str) or not value[key].strip() for key in keys):
            raise ValueError("场景分类缺少完整的三级名称")
        labels = tuple(value[key].strip() for key in keys)
        if labels not in allowed and labels != ("Unclassified",) * 3:
            raise ValueError("分类路径不属于该 App 的知识库快照")
        return {**dict(zip(keys, labels)), "reason": _text(value.get("reason"))}
    return _checked_json(scene_classification_prompt(scene_tree_text(kb_root), task, app), validate, model=model, stage="classifying", item_id=item_id)


def _dependency_tasks(item: dict[str, Any], *, kb_root: Path, model: TaskGenerationModel) -> list[dict[str, Any]]:
    main = dict(item)
    main_uuid = str(uuid.uuid4())
    main["task_uuid"] = main_uuid
    main["pre_task_uuid"] = None
    app = _text(item.get("app") or item.get("target_app"), "未知应用")
    task = _text(item.get("task"))
    group_id = main_uuid
    def validate(value):
        relationship = value.get("dependency_relationships")
        if not isinstance(relationship, str) or relationship not in {"zero", "weak", "strong"}:
            raise ValueError("dependency_relationships 必须明确为 zero、weak 或 strong 之一")
        if relationship == "weak":
            pre_text = valid_task_text(value.get("pre_task"))
            if pre_text == task:
                raise ValueError("前置任务不能与主任务相同")
            value["pre_task"] = pre_text
        elif value.get("pre_task") is not None:
            raise ValueError("zero/strong 的 pre_task 必须为 JSON null")
        return value
    unit = _text(item.get("execution_unit_id"))
    dependency = _checked_json(dependency_prompt(task, app), validate, model=model, stage="dependency", item_id=unit)
    relationship = dependency["dependency_relationships"]
    if relationship == "weak":
        pre_text = dependency["pre_task"]
        pre_uuid = str(uuid.uuid4())
        scene = classify_pre_task_scene(pre_text, app, kb_root=kb_root, model=model, item_id=unit)
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
    main.update({"app": app, "target_app": app, "pre_dependency": "zero", "pre_task_uuid": None, "dependency_group_id": group_id, "result_id": main_uuid, "deleted": False})
    return [main]


def _initial_node(row: dict[str, Any], *, generate_n: int, kb_root: Path, model: TaskGenerationModel) -> list[dict[str, Any]]:
    prompt = system_prompt(
        row["scene"], row["capability"], row["sub_capability"], row["sub_capability_desc"],
        row["target_app"], row["resource_prior"], row.get("reference_example", ""), generate_n,
    )
    generated, errors = _generate_tasks(prompt, generate_n, model=model, item_id=row["node_id"])
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
        try:
            results.extend(_dependency_tasks(normalized, kb_root=kb_root, model=model))
        except Exception as exc:
            errors.append({"stage": "dependency", "task_index": task_index + 1, "error": f"第 {task_index + 1} 条任务依赖检查失败，未纳入结果：{exc}"})
    if errors:
        raise PartialGenerationError(results, errors)
    return results


def _run_initial_generation(node_ids: list[str], generate_n: int, *, kb_root: Path, progress: Progress, model: TaskGenerationModel) -> dict[str, Any]:
    rows = merged_nodes(kb_root, sample_num=generate_n)
    selected = [row for row in rows if row["node_id"] in set(node_ids)]
    by_id = {row["node_id"]: row for row in selected}
    if any(identifier not in by_id for identifier in node_ids):
        raise ValueError("提交的执行单元不在作业知识库快照中")
    results: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    workers = _model_config(model).max_concurrent
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="task-generation-node") as executor:
        futures = {executor.submit(_initial_node, by_id[node_id_value], generate_n=generate_n, kb_root=kb_root, model=model): node_id_value for node_id_value in node_ids if node_id_value in by_id}
        for completed, future in enumerate(as_completed(futures), start=1):
            current_id = futures[future]
            try:
                results.extend(future.result())
            except PartialGenerationError as exc:
                results.extend(exc.results)
                errors.extend({"item_id": current_id, **error} for error in exc.errors)
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
    classified = classify_pre_task_scene(seed["task"], seed["app"], kb_root=kb_root, model=model, item_id=str(seed.get("source_row", "")))
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
    value, errors = _generate_tasks(flywheel_prompt(context, prior, generate_n), generate_n, model=model, item_id=str(seed.get("source_row", "")))
    short_uuid = secrets.token_hex(3)
    records = []
    for sequence, item in enumerate(value, start=1):
        task = _text(item.get("task")) if isinstance(item, dict) else ""
        if not task:
            continue
        records.append({
            "result_id": uuid.uuid4().hex,
            "seed_id": seed.get("seed_id"),
            "source_row": seed.get("source_row"),
            "source_task": seed["task"],
            "用例编号": generate_case_id(seed["app"], seed.get("scene", "Unclassified"), short_uuid, sequence),
            "源失败任务": seed["task"], "app": seed["app"], "scene": seed.get("scene", "Unclassified"),
            "capability": seed.get("capability", "Unclassified"), "sub_capability": seed.get("sub_capability", "Unclassified"),
            "生成的变体任务": task, "task": task, "run": "flywheel", "审核状态": "待人工Review",
            "deleted": False, "created_at": _now(),
        })
    if errors:
        raise PartialGenerationError(records, errors)
    return records


def prepare_augmentation_seeds(path: Path) -> list[dict[str, Any]]:
    """Keep every usable input row, including duplicate tasks, as its own seed."""
    return [{
        **seed,
        "seed_id": uuid.uuid4().hex,
        "classification_source": "excel" if all(seed.get(key) for key in ("scene", "capability", "sub_capability")) else "model",
        "classification_status": "pending",
        "mapping_status": "pending",
        "node_path_ids": [],
        "generation_status": "waiting",
        "result_count": 0,
    } for seed in _read_seed_workbook(path)]


def _augmentation_paths(kb_root: Path) -> dict[tuple[str, ...], list[str]]:
    from .tree_store import _app_nodes, read_tree
    paths = {}
    for scene in read_tree(kb_root)["scenes"]:
        for capability in scene.get("children", []):
            for task_type in capability.get("children", []):
                for app in _app_nodes(task_type):
                    paths[(scene["label"], capability["label"], task_type["label"], app["label"])] = [
                        scene["id"], capability["id"], task_type["id"], app["id"],
                    ]
    return paths


def _run_augmentation_classification(seeds: list[dict[str, Any]], *, kb_root: Path, progress: Progress,
                                    model: TaskGenerationModel, on_seed: SeedProgress | None = None) -> dict[str, Any]:
    total = len(seeds)
    classified = [dict(seed) for seed in seeds]
    errors: list[dict[str, Any]] = []
    paths = _augmentation_paths(kb_root)

    def classify(seed):
        current = {**seed, "classification_status": "classifying"}
        if on_seed:
            on_seed(current, None)
        try:
            current = _seed_classify(current, kb_root=kb_root, model=model)
            labels = tuple(current.get(key, "") for key in ("scene", "capability", "sub_capability"))
            identifiers = paths.get((*labels, current["app"]), [])
            current.update({"classification_status": "classified", "node_path_ids": identifiers,
                            "mapping_status": "matched" if identifiers else "unclassified" if labels == ("Unclassified",) * 3 else "not_found"})
        except Exception as exc:
            current.update({"classification_status": "failed", "mapping_status": "classification_failed",
                            "node_path_ids": [], "generation_status": "skipped", "error": str(exc)})
        if on_seed:
            on_seed(current, None)
        return current

    workers = _model_config(model).max_concurrent
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="task-augmentation-classify") as executor:
        futures = {executor.submit(classify, seed): index for index, seed in enumerate(seeds)}
        for completed, future in enumerate(as_completed(futures), start=1):
            index = futures[future]
            classified[index] = future.result()
            if classified[index]["classification_status"] == "failed":
                errors.append({"seed_id": classified[index]["seed_id"], "item_id": str(seeds[index].get("source_row", index)),
                               "stage": "classifying", "error": classified[index]["error"]})
            progress({"stage": "classifying", "current_item": str(seeds[index].get("source_row", index)), "completed_items": completed, "total_items": total * 2, "percent": round(completed / max(1, total) * 40)})
    return {"seeds": classified, "errors": errors, "warnings": [], "total_items": total}


def _run_augmentation_generation(seeds: list[dict[str, Any]], generate_n: int, *, kb_root: Path, progress: Progress,
                                model: TaskGenerationModel, on_seed: SeedProgress | None = None) -> dict[str, Any]:
    total = len(seeds)
    valid_seeds = [seed for seed in seeds if seed.get("classification_status") == "classified"]
    records: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    def generate(seed):
        current = {**seed, "generation_status": "generating"}
        if on_seed:
            on_seed(current, None)
        try:
            rows = _variant_records(current, generate_n=generate_n, kb_root=kb_root, model=model)
            issues = []
        except PartialGenerationError as exc:
            rows, issues = exc.results, exc.errors
        except Exception as exc:
            rows, issues = [], [{"stage": "generating", "error": str(exc)}]
        issues = [{"seed_id": current["seed_id"], "item_id": str(current.get("source_row", "")), **error} for error in issues]
        current.update({"generation_status": "succeeded" if not issues else "partial" if rows else "failed", "result_count": len(rows)})
        if issues:
            current["error"] = "；".join(error["error"] for error in issues)
        if on_seed:
            on_seed(current, rows)
        return rows, issues

    workers = _model_config(model).max_concurrent
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="task-augmentation-generate") as executor:
        futures = {executor.submit(generate, seed): seed for seed in valid_seeds}
        for completed, future in enumerate(as_completed(futures), start=1):
            seed = futures[future]
            rows, issues = future.result()
            records.extend(rows)
            errors.extend(issues)
            progress({"stage": "generating", "current_item": str(seed.get("source_row", "")), "completed_items": total + completed, "total_items": total * 2, "percent": 40 + round(completed / max(1, len(valid_seeds)) * 60)})
    records.sort(key=lambda item: (int(item.get("source_row") or 0), item.get("用例编号", "")))
    return {"results": records, "errors": errors, "warnings": [], "total_items": total}


def run_augmentation_classification(seeds: list[dict[str, Any]], *, kb_root: Path, progress: Progress,
                                   on_seed: SeedProgress | None = None, model: TaskGenerationModel | None = None) -> dict[str, Any]:
    active = _model(model, kb_root)
    try:
        return _run_augmentation_classification(seeds, kb_root=kb_root, progress=progress, model=active, on_seed=on_seed)
    finally:
        if model is None:
            active.close()


def run_augmentation_generation(seeds: list[dict[str, Any]], generate_n: int, *, kb_root: Path, progress: Progress,
                               on_seed: SeedProgress | None = None, model: TaskGenerationModel | None = None) -> dict[str, Any]:
    active = _model(model, kb_root)
    try:
        return _run_augmentation_generation(seeds, generate_n, kb_root=kb_root, progress=progress, model=active, on_seed=on_seed)
    finally:
        if model is None:
            active.close()


def _run_augmentation(path: Path, generate_n: int, *, kb_root: Path, progress: Progress, model: TaskGenerationModel) -> dict[str, Any]:
    """Compatibility entry point for callers that still want one automatic run."""
    classified = _run_augmentation_classification(prepare_augmentation_seeds(path), kb_root=kb_root, progress=progress, model=model)
    outcome = _run_augmentation_generation(classified["seeds"], generate_n, kb_root=kb_root, progress=progress, model=model)
    outcome["errors"] = classified["errors"] + outcome["errors"]
    return outcome


def run_initial_generation(node_ids: list[str], generate_n: int, *, kb_root: Path, progress: Progress, model: TaskGenerationModel | None = None) -> dict[str, Any]:
    active = _model(model, kb_root)
    try:
        return _run_initial_generation(node_ids, generate_n, kb_root=kb_root, progress=progress, model=active)
    finally:
        if model is None:
            active.close()


def run_augmentation(path: Path, generate_n: int, *, kb_root: Path, progress: Progress, model: TaskGenerationModel | None = None) -> dict[str, Any]:
    active = _model(model, kb_root)
    try:
        return _run_augmentation(path, generate_n, kb_root=kb_root, progress=progress, model=active)
    finally:
        if model is None:
            active.close()
