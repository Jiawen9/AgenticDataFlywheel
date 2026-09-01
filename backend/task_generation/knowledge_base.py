from __future__ import annotations

import ast
import hashlib
import math
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from .constants import ALLOWED_WORKBOOK_SUFFIXES, KNOWLEDGE_BASE_DIR, KNOWLEDGE_BASE_FILES


REQUIRED_COLUMNS = {
    "scene_tree": {"scene", "capability", "sub_capability", "target_app", "use_resource_prior", "reference_example"},
    "control_prior": {"scene", "capability", "sub_capability", "target_app", "sub_capability_desc"},
}


def parse_app_list(value: Any) -> list[str]:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value).strip()
    if not text:
        return []
    if text.startswith("[") and text.endswith("]"):
        try:
            parsed = ast.literal_eval(text.replace("，", ","))
            if isinstance(parsed, (list, tuple)):
                return [str(item).strip() for item in parsed if str(item).strip()]
        except (ValueError, SyntaxError):
            text = text[1:-1]
        return [item.strip().strip("'\"") for item in text.split(",") if item.strip()]
    return [text]


def _clean(value: Any, fallback: str = "") -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return fallback
    return str(value).strip()


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return _clean(value).lower() in {"true", "1", "yes", "y", "是"}


def node_id(app: str, scene: str, capability: str, sub_capability: str) -> str:
    raw = "\x1f".join((app, scene, capability, sub_capability))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _path(kind: str, root: Path = KNOWLEDGE_BASE_DIR) -> Path:
    if kind not in KNOWLEDGE_BASE_FILES:
        raise ValueError(f"未知知识库类型：{kind}")
    return root / KNOWLEDGE_BASE_FILES[kind]


def validate_workbook(path: Path, kind: str) -> dict[str, Any]:
    if path.suffix.lower() not in ALLOWED_WORKBOOK_SUFFIXES:
        raise ValueError("知识库只支持 .xlsx 或 .xlsm 文件")
    if not path.is_file():
        raise FileNotFoundError(path.name)
    try:
        with pd.ExcelFile(path) as excel:
            sheets = list(excel.sheet_names)
            if not sheets:
                raise ValueError("工作簿没有可读取的 sheet")
            if kind in REQUIRED_COLUMNS:
                df = pd.read_excel(excel, sheet_name=sheets[0], nrows=0)
                missing = sorted(REQUIRED_COLUMNS[kind] - set(str(column) for column in df.columns))
                if missing:
                    raise ValueError(f"{KNOWLEDGE_BASE_FILES[kind]} 缺少列：{', '.join(missing)}")
                rows = len(pd.read_excel(excel, sheet_name=sheets[0]))
            else:
                rows = sum(len(pd.read_excel(excel, sheet_name=sheet)) for sheet in sheets)
        return {"valid": True, "kind": kind, "filename": path.name, "sheets": sheets, "rows": rows}
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(f"无法读取知识库：{exc}") from exc


def list_knowledge_bases(root: Path = KNOWLEDGE_BASE_DIR) -> list[dict[str, Any]]:
    from .tree_store import current_root
    try:
        source = current_root(root)
    except (OSError, ValueError):
        source = root
    result = []
    for kind, filename in KNOWLEDGE_BASE_FILES.items():
        path = source / filename
        item: dict[str, Any] = {"kind": kind, "filename": filename, "exists": path.is_file(), "valid": False, "size_bytes": path.stat().st_size if path.is_file() else 0}
        if path.is_file():
            try:
                item.update(validate_workbook(path, kind))
                item["modified_at"] = datetime.fromtimestamp(path.stat().st_mtime).isoformat()
            except (OSError, ValueError) as exc:
                item["error"] = str(exc)
        result.append(item)
        item["version"] = source.name if source != root else None
    return result


def replace_knowledge_base(kind: str, source: Path, *, root: Path = KNOWLEDGE_BASE_DIR, base_version: str | None = None) -> dict[str, Any]:
    from .tree_store import replace_workbook
    replace_workbook(kind, source, root=root, base_version=base_version)
    return next(item for item in list_knowledge_bases(root) if item["kind"] == kind)


def snapshot_knowledge_base(destination: Path, *, root: Path = KNOWLEDGE_BASE_DIR, version: str | None = None) -> dict[str, Any]:
    from .tree_store import snapshot
    return snapshot(destination, root=root, version=version)


def scene_tree_text(root: Path) -> str:
    from .tree_store import flatten, read_tree
    lines = []
    for leaf, labels in flatten(read_tree(root)["scenes"]):
        apps = ", ".join(config["app"] for config in leaf["app_configs"])
        if apps:
            lines.append(f"{' > '.join(labels)} | 涵盖App：{apps}")
    return "\n".join(lines)


def merged_nodes(root: Path = KNOWLEDGE_BASE_DIR, *, sample_num: int = 1) -> list[dict[str, Any]]:
    from .tree_store import current_root, flatten, prior_status, read_tree
    root = current_root(root)
    controls, _, _ = prior_status(root)
    with pd.ExcelFile(_path("resource_prior", root)) as resource_book:
        resources = {sheet: pd.read_excel(resource_book, sheet_name=sheet) for sheet in resource_book.sheet_names}
    result = []
    for leaf, labels in flatten(read_tree(root)["scenes"]):
        for config in leaf["app_configs"]:
            app = config["app"]
            item = dict(zip(("scene", "capability", "sub_capability"), labels))
            item.update(config)
            item.update({"target_app": app, "task_type_id": leaf["id"], "sub_capability_desc": controls.get((*labels, app)) or "无"})
            selected: list[dict[str, Any]] = []
            if config["use_resource_prior"] and app in resources and not resources[app].empty:
                count = min(max(0, int(sample_num)), len(resources[app]))
                if count:
                    selected = resources[app].sample(n=count).to_dict(orient="records")
            item["resource_prior"] = selected
            item["resource_count"] = len(resources.get(app, pd.DataFrame()))
            item["node_id"] = node_id(app, *labels)
            result.append(item)
    return result


def tree_payload(root: Path = KNOWLEDGE_BASE_DIR) -> dict[str, Any]:
    from .tree_store import tree_payload as payload
    return payload(root)
