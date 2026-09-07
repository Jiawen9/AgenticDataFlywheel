"""Editable scenario tree and immutable, atomically published knowledge bundles.

The persisted tree has four semantic levels: scene, capability, task type and
App. Older versions stored Apps as ``app_configs`` on the task-type node; the
first read/publication upgrades that shape without changing existing IDs.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

import pandas as pd
from openpyxl import Workbook, load_workbook

from .constants import KNOWLEDGE_BASE_DIR, KNOWLEDGE_BASE_FILES
from .knowledge_base import _clean, _truthy, parse_app_list, validate_workbook


TREE_FILE = "scene_tree.json"
META_SHEET = "_scene_tree_nodes"
PATH_COLUMNS = ["scene", "capability", "sub_capability"]
SCENE_COLUMNS = [*PATH_COLUMNS, "target_app", "use_resource_prior", "reference_example", "description"]
KINDS = ("scene", "capability", "sub_capability", "app")
_LOCK = threading.RLock()


class VersionConflict(ValueError):
    pass


@contextmanager
def write_lock(root: Path) -> Iterator[None]:
    """Serialize publication across threads and local server processes."""
    root.mkdir(parents=True, exist_ok=True)
    with _LOCK, (root / ".publication.lock").open("a+b") as stream:
        stream.seek(0, 2)
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def _pointed_root(root: Path) -> Path | None:
    pointer = root / "current.json"
    if not pointer.exists():
        return None
    version = _json(pointer)["version"]
    if not isinstance(version, str) or uuid.UUID(version).hex != version:
        raise ValueError("知识库当前版本指针无效")
    target = root / "versions" / version
    if not (target / TREE_FILE).is_file():
        raise ValueError("知识库当前版本不完整")
    return target


def _app_nodes(task_type: dict[str, Any]) -> list[dict[str, Any]]:
    """Return App children while accepting the old app_configs shape."""
    children = task_type.get("children")
    if isinstance(children, list):
        result = []
        for item in children:
            if isinstance(item, dict) and item.get("kind") == "app":
                value = dict(item)
                value.setdefault("app", value.get("label", ""))
                result.append(value)
        return result
    result = []
    for config in task_type.get("app_configs", []) or []:
        if not isinstance(config, dict):
            continue
        result.append({
            "id": str(config.get("id") or uuid.uuid4()),
            "kind": "app",
            "label": str(config.get("app") or ""),
            "app": str(config.get("app") or ""),
            "description": str(config.get("description") or ""),
            "reference_example": str(config.get("reference_example") or ""),
            "use_resource_prior": bool(config.get("use_resource_prior", False)),
        })
    return result


def flatten(scenes: list[dict[str, Any]]) -> list[tuple[dict[str, Any], tuple[str, str, str]]]:
    """Return L3 task types and their L1/L2/L3 labels."""
    return [
        (task_type, (scene["label"], capability["label"], task_type["label"]))
        for scene in scenes
        for capability in scene.get("children", [])
        for task_type in capability.get("children", [])
    ]


def _migrate_legacy_scenes(scenes: Any) -> Any:
    """Convert legacy L1/L2/L3 + app_configs to L1/L2/L3/App nodes."""
    migrated = copy.deepcopy(scenes)
    if not isinstance(migrated, list):
        return migrated
    for scene in migrated:
        if not isinstance(scene, dict):
            continue
        scene.setdefault("description", "")
        for capability in scene.get("children", []) or []:
            if not isinstance(capability, dict):
                continue
            capability.setdefault("description", "")
            for task_type in capability.get("children", []) or []:
                if not isinstance(task_type, dict):
                    continue
                task_type.setdefault("description", "")
                if task_type.get("kind") != "sub_capability":
                    continue
                if isinstance(task_type.get("children"), list):
                    for app in task_type["children"]:
                        if isinstance(app, dict):
                            app.setdefault("description", "")
                    task_type.pop("app_configs", None)
                    continue
                children = []
                for config in task_type.pop("app_configs", []) or []:
                    if not isinstance(config, dict):
                        continue
                    children.append({
                        "id": str(config.get("id") or uuid.uuid4()),
                        "kind": "app",
                        "label": str(config.get("app") or "").strip(),
                        "description": str(config.get("description") or ""),
                        "reference_example": str(config.get("reference_example") or ""),
                        "use_resource_prior": bool(config.get("use_resource_prior", False)),
                    })
                task_type["children"] = children
    return migrated


def _has_legacy_apps(scenes: Any) -> bool:
    if not isinstance(scenes, list):
        return False
    for scene in scenes:
        for capability in (scene.get("children", []) if isinstance(scene, dict) else []) or []:
            for task_type in (capability.get("children", []) if isinstance(capability, dict) else []) or []:
                if isinstance(task_type, dict) and "app_configs" in task_type:
                    return True
    return False


def _normalize_tree_payload(scenes: Any) -> Any:
    """Accept the read-only ``app_configs`` projection from older clients.

    The persisted representation is always four-level.  The API still returns
    ``app_configs`` for old consumers, so a client that sends that projection
    back should not accidentally fail just because it has not migrated yet.
    New clients send real L4 children and take the normal strict validation
    path below.
    """
    value = copy.deepcopy(scenes)
    if not isinstance(value, list):
        return value
    for scene in value:
        if not isinstance(scene, dict):
            continue
        for capability in (scene.get("children", []) or []):
            if not isinstance(capability, dict):
                continue
            for task_type in (capability.get("children", []) or []):
                if not isinstance(task_type, dict) or "app_configs" not in task_type:
                    continue
                configs = task_type.pop("app_configs") or []
                children = []
                for config in configs:
                    if not isinstance(config, dict):
                        continue
                    app = str(config.get("app") or config.get("label") or "").strip()
                    children.append({
                        "id": str(config.get("id") or uuid.uuid4()),
                        "kind": "app",
                        "label": app,
                        "description": str(config.get("description") or ""),
                        "reference_example": str(config.get("reference_example") or ""),
                        "use_resource_prior": config.get("use_resource_prior", False),
                    })
                task_type["children"] = children
    return _migrate_legacy_scenes(value)


def _validate_four_level_tree(scenes: Any) -> list[dict[str, Any]]:
    seen_ids: set[str] = set()

    def walk(nodes: Any, depth: int) -> list[dict[str, Any]]:
        if not isinstance(nodes, list):
            raise ValueError("场景树节点必须是数组")
        result = []
        names: set[str] = set()
        for value in nodes:
            if not isinstance(value, dict) or value.get("kind") != KINDS[depth]:
                raise ValueError("场景树必须按 L1 场景、L2 场景、L3 任务类型、L4 App 组织")
            name = value.get("label")
            if not isinstance(name, str) or not name.strip() or len(name.strip()) > 200:
                raise ValueError("节点名称不能为空且不能超过 200 字")
            name = name.strip()
            if name in names:
                raise ValueError(f"同级节点名称重复：{name}")
            names.add(name)
            try:
                identifier = str(uuid.UUID(value.get("id", "")))
            except (ValueError, TypeError, AttributeError) as exc:
                raise ValueError("节点 ID 必须是 UUID") from exc
            if identifier in seen_ids:
                raise ValueError("节点 ID 不能重复")
            seen_ids.add(identifier)
            description = value.get("description", "")
            if not isinstance(description, str) or len(description) > 20000:
                raise ValueError("节点描述必须是文本（最多 20000 字）")
            node = {"id": identifier, "kind": KINDS[depth], "label": name, "description": description}
            if depth < 3:
                if value.get("app_configs"):
                    raise ValueError("App 必须作为 L4 节点配置")
                node["children"] = walk(value.get("children", []), depth + 1)
            else:
                if value.get("children"):
                    raise ValueError("App 不能包含子节点")
                if value.get("app_configs"):
                    raise ValueError("App 节点不能包含 app_configs")
                example = value.get("reference_example", "")
                resource = value.get("use_resource_prior", False)
                if not isinstance(example, str) or len(example) > 20000 or not isinstance(resource, bool):
                    raise ValueError("参考示例必须是文本（最多 20000 字），资源开关必须是布尔值")
                node.update({"reference_example": example, "use_resource_prior": resource})
            result.append(node)
        return result

    return walk(scenes, 0)


def validate_tree(scenes: Any) -> list[dict[str, Any]]:
    """Validate and normalize a tree, including the legacy API projection."""
    return _validate_four_level_tree(_normalize_tree_payload(scenes))


def _paths(scenes: list[dict[str, Any]]) -> dict[tuple[str, ...], dict[str, Any]]:
    found: dict[tuple[str, ...], dict[str, Any]] = {}

    def visit(nodes: list[dict[str, Any]], prefix: tuple[str, ...]) -> None:
        for node in nodes:
            path = (*prefix, node["label"])
            found[path] = node
            visit(node.get("children", []), path)
    visit(scenes, ())
    return found


def import_scene_workbook(path: Path, previous: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    validate_workbook(path, "scene_tree")
    previous = _migrate_legacy_scenes(previous or [])
    previous_paths = _paths(previous)
    scenes: list[dict[str, Any]] = []
    with pd.ExcelFile(path) as book:
        frame = pd.read_excel(book, sheet_name=0).fillna("")
        if META_SHEET in book.sheet_names:
            metadata = pd.read_excel(book, sheet_name=META_SHEET).fillna("")
            required = {"id", "parent_id", "kind", "label"}
            if not required.issubset(metadata.columns):
                raise ValueError("场景树身份元数据缺少必要列")
            by_id: dict[str, dict[str, Any]] = {}
            for _, row in metadata.iterrows():
                identifier = _clean(row["id"])
                if identifier in by_id:
                    raise ValueError("场景树身份元数据 ID 重复")
                kind = _clean(row["kind"])
                node: dict[str, Any] = {
                    "id": identifier,
                    "label": _clean(row["label"]),
                    "kind": kind,
                    "description": _clean(row.get("description")),
                    "children": [],
                }
                if kind == "app":
                    node.update({
                        "reference_example": _clean(row.get("reference_example")),
                        "use_resource_prior": _truthy(row.get("use_resource_prior")),
                    })
                by_id[identifier] = node
            for _, row in metadata.iterrows():
                node = by_id[_clean(row["id"])]
                parent = _clean(row["parent_id"])
                if parent:
                    if parent not in by_id:
                        raise ValueError("场景树身份元数据父节点不存在")
                    by_id[parent]["children"].append(node)
                else:
                    scenes.append(node)
            if len(_paths(scenes)) != len(by_id):
                raise ValueError("场景树身份元数据含有孤立或循环节点")
    path_index = _paths(scenes)
    for row_number, (_, row) in enumerate(frame.iterrows(), start=2):
        if all(not _clean(row.get(key)) for key in SCENE_COLUMNS):
            continue
        parts = tuple(_clean(row.get(key)) for key in PATH_COLUMNS)
        if not all(parts):
            raise ValueError(f"场景树第 {row_number} 行的场景、能力和任务类型不能为空")
        parent_children = scenes
        for depth, label in enumerate(parts):
            key = parts[: depth + 1]
            node = path_index.get(key)
            if node is None:
                node = {
                    "id": previous_paths.get(key, {}).get("id", str(uuid.uuid4())),
                    "label": label,
                    "kind": KINDS[depth],
                    "description": _clean(row.get("description")) if depth == 2 else "",
                    "children": [],
                }
                parent_children.append(node)
                path_index[key] = node
            if depth < 2:
                parent_children = node.setdefault("children", [])
        for app in dict.fromkeys(parse_app_list(row.get("target_app"))):
            existing = next((item for item in node.get("children", []) if item.get("kind") == "app" and item.get("label") == app), None)
            previous_app = previous_paths.get((*parts, app), {})
            config = {
                "id": existing.get("id", str(uuid.uuid4())) if existing else previous_app.get("id", str(uuid.uuid4())),
                "kind": "app",
                "label": app,
                "description": existing.get("description", previous_app.get("description", "")) if existing else previous_app.get("description", ""),
                "reference_example": _clean(row.get("reference_example")),
                "use_resource_prior": _truthy(row.get("use_resource_prior")),
            }
            if existing:
                old_config = {key: existing.get(key) for key in ("reference_example", "use_resource_prior")}
                new_config = {key: config[key] for key in ("reference_example", "use_resource_prior")}
                if old_config != new_config:
                    raise ValueError(f"场景树第 {row_number} 行与同任务类型/App 的配置冲突：{app}；请先合并冲突行")
            else:
                node.setdefault("children", []).append(config)
    return validate_tree(scenes)


def _string_cell(sheet: Any, row: int, column: int, value: Any) -> None:
    cell = sheet.cell(row, column, value)
    if isinstance(value, str):
        cell.data_type = "s"  # User examples and labels must not become Excel formulas.


def write_scene_workbook(path: Path, scenes: list[dict[str, Any]]) -> None:
    book = load_workbook(path) if path.is_file() else Workbook()
    try:
        old = book.worksheets[0]
        title = old.title
        book.remove(old)
        sheet = book.create_sheet(title, 0)
        for column, label in enumerate(SCENE_COLUMNS, 1):
            _string_cell(sheet, 1, column, label)
        row_number = 2
        for task_type, path_labels in flatten(scenes):
            for app in _app_nodes(task_type):
                values = [
                    *path_labels,
                    json.dumps([app["label"]], ensure_ascii=False),
                    app["use_resource_prior"],
                    app["reference_example"],
                    task_type.get("description", ""),
                ]
                for column, value in enumerate(values, 1):
                    _string_cell(sheet, row_number, column, value)
                row_number += 1
        if META_SHEET in book.sheetnames:
            book.remove(book[META_SHEET])
        meta = book.create_sheet(META_SHEET)
        meta.append(["id", "parent_id", "kind", "label", "description", "reference_example", "use_resource_prior"])

        def visit(nodes: list[dict[str, Any]], parent: str = "") -> None:
            for node in nodes:
                index = meta.max_row + 1
                values = [
                    node["id"], parent, node["kind"], node["label"], node.get("description", ""),
                    node.get("reference_example", ""), node.get("use_resource_prior", ""),
                ]
                for column, value in enumerate(values, 1):
                    _string_cell(meta, index, column, value)
                visit(node.get("children", []), node["id"])
        visit(scenes)
        meta.sheet_state = "hidden"
        book.save(path)
    finally:
        book.close()


def _app_paths(scenes: list[dict[str, Any]]) -> dict[tuple[str, str], tuple[tuple[str, str, str], str]]:
    result: dict[tuple[str, str], tuple[tuple[str, str, str], str]] = {}
    for task_type, labels in flatten(scenes):
        for app in _app_nodes(task_type):
            result[(task_type["id"], app["id"])] = (labels, app["label"])
    return result


def _rename_priors(path: Path, before: list[dict[str, Any]], after: list[dict[str, Any]]) -> None:
    if not path.is_file():
        return
    old = _app_paths(before)
    new = _app_paths(after)
    mapping = {
        old[key]: new[key]
        for key in old.keys() & new.keys()
        if old[key] != new[key]
    }
    if not mapping:
        return
    book = load_workbook(path)
    try:
        sheet = book.worksheets[0]
        columns = {cell.value: cell.column for cell in sheet[1]}
        if not set(PATH_COLUMNS).issubset(columns):
            raise ValueError("操控先验列不完整，无法同步重命名")
        app_column = columns.get("target_app")
        for row in range(2, sheet.max_row + 1):
            original_path = tuple(_clean(sheet.cell(row, columns[key]).value) for key in PATH_COLUMNS)
            apps = parse_app_list(sheet.cell(row, app_column).value) if app_column else []
            changed = False
            mapped_path = original_path
            mapped_apps = []
            for app in apps:
                target = mapping.get((original_path, app))
                if target:
                    mapped_path, renamed_app = target
                    mapped_apps.append(renamed_app)
                    changed = True
                else:
                    mapped_apps.append(app)
            if not changed:
                continue
            for key, value in zip(PATH_COLUMNS, mapped_path):
                _string_cell(sheet, row, columns[key], value)
            if app_column:
                value: Any = mapped_apps[0] if len(mapped_apps) == 1 else json.dumps(mapped_apps, ensure_ascii=False)
                _string_cell(sheet, row, app_column, value)
        book.save(path)
    finally:
        book.close()


def _rename_resource_sheets(path: Path, before: list[dict[str, Any]], after: list[dict[str, Any]]) -> list[str]:
    warnings: list[str] = []
    if not path.is_file():
        return warnings
    old = _app_paths(before)
    new = _app_paths(after)
    renames = [
        (old[key][1], new[key][1])
        for key in old.keys() & new.keys()
        if old[key][1] != new[key][1]
    ]
    if not renames:
        return warnings
    book = load_workbook(path)
    try:
        for old_name, new_name in renames:
            if old_name not in book.sheetnames:
                warnings.append(f"资源先验缺少原 App sheet：{old_name}；未执行重命名")
                continue
            if new_name in book.sheetnames:
                warnings.append(f"资源先验 sheet 冲突：{old_name} → {new_name}；保留两份且未覆盖")
                continue
            if not new_name or len(new_name) > 31 or any(char in new_name for char in "[]:*?/\\"):
                warnings.append(f"App 名称无法作为 Excel sheet：{new_name}；保留原 sheet {old_name}")
                continue
            book[old_name].title = new_name
        book.save(path)
    finally:
        book.close()
    return warnings


def _publish(root: Path, source: Path, scenes: list[dict[str, Any]] | None = None,
             replacement: tuple[str, Path] | None = None) -> Path:
    """Caller holds write_lock. No live file changes before the last replace."""
    version = uuid.uuid4().hex
    versions = root / "versions"
    versions.mkdir(exist_ok=True)
    staging = versions / f".staging-{version}"
    staging.mkdir()
    try:
        for filename in KNOWLEDGE_BASE_FILES.values():
            if (source / filename).is_file():
                shutil.copy2(source / filename, staging / filename)
        raw_previous = _json(source / TREE_FILE)["scenes"] if (source / TREE_FILE).exists() else []
        previous = _migrate_legacy_scenes(raw_previous)
        if replacement:
            kind, uploaded = replacement
            shutil.copy2(uploaded, staging / KNOWLEDGE_BASE_FILES[kind])
        scene_path = staging / KNOWLEDGE_BASE_FILES["scene_tree"]
        if scenes is None:
            if previous and not (replacement and replacement[0] == "scene_tree"):
                scenes = previous
            else:
                scenes = import_scene_workbook(scene_path, previous) if scene_path.exists() else []
        scenes = validate_tree(_migrate_legacy_scenes(scenes))
        _rename_priors(staging / KNOWLEDGE_BASE_FILES["control_prior"], previous, scenes)
        publication_warnings = _rename_resource_sheets(staging / KNOWLEDGE_BASE_FILES["resource_prior"], previous, scenes)
        if scene_path.exists() or not replacement:
            write_scene_workbook(scene_path, scenes)
        _write_json(staging / TREE_FILE, {
            "version": version,
            "schema_version": 2,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "scenes": scenes,
            "warnings": publication_warnings,
        })
        published = versions / version
        staging.rename(published)
        pointer = root / f".current-{version}.tmp"
        _write_json(pointer, {"version": version})
        pointer.replace(root / "current.json")
        return published
    finally:
        if staging.is_dir():
            shutil.rmtree(staging)


def current_root(root: Path = KNOWLEDGE_BASE_DIR) -> Path:
    root = Path(root)
    if (root / TREE_FILE).is_file():
        return root
    current = _pointed_root(root)
    if current:
        try:
            tree = _json(current / TREE_FILE)
        except (OSError, ValueError, json.JSONDecodeError):
            return current
        if _has_legacy_apps(tree.get("scenes")):
            with write_lock(root):
                current = _pointed_root(root)
                if current:
                    tree = _json(current / TREE_FILE)
                    if _has_legacy_apps(tree.get("scenes")):
                        _publish(root, current, _migrate_legacy_scenes(tree["scenes"]))
                return _pointed_root(root) or current
        return current
    with write_lock(root):
        return _pointed_root(root) or _publish(root, root)


def read_tree(root: Path = KNOWLEDGE_BASE_DIR) -> dict[str, Any]:
    return _json(current_root(root) / TREE_FILE)


def save_tree(scenes: Any, base_version: str, *, root: Path = KNOWLEDGE_BASE_DIR) -> dict[str, Any]:
    clean = validate_tree(_normalize_tree_payload(scenes))
    current_root(root)
    with write_lock(root):
        source = _pointed_root(root)
        if source is None or source.name != base_version:
            raise VersionConflict("知识库已更新，请保留草稿并刷新最新版本后重试")
        published = _publish(root, source, clean)
    return tree_payload(published)


def replace_workbook(kind: str, source: Path, *, root: Path = KNOWLEDGE_BASE_DIR, base_version: str | None = None) -> Path:
    if kind not in KNOWLEDGE_BASE_FILES:
        raise ValueError(f"未知知识库类型：{kind}")
    validate_workbook(source, kind)
    with write_lock(root):
        current = _pointed_root(root)
        if base_version is not None and (current is None or current.name != base_version):
            raise VersionConflict("知识库已更新，请刷新后重新上传")
        return _publish(root, current or root, replacement=(kind, source))


def prior_status(root: Path) -> tuple[dict[tuple[str, str, str, str], str], dict[str, int], list[str]]:
    controls: dict[tuple[str, str, str, str], list[str]] = {}
    resources: dict[str, int] = {}
    warnings: list[str] = []
    try:
        frame = pd.read_excel(root / KNOWLEDGE_BASE_FILES["control_prior"])
        for _, row in frame.iterrows():
            for app in parse_app_list(row.get("target_app")):
                key = (*(_clean(row.get(part)) for part in PATH_COLUMNS), app)
                desc = _clean(row.get("sub_capability_desc"))
                if desc and desc not in controls.setdefault(key, []):
                    controls[key].append(desc)
    except (OSError, ValueError) as exc:
        warnings.append(f"操控先验不可读：{exc}")
    try:
        with pd.ExcelFile(root / KNOWLEDGE_BASE_FILES["resource_prior"]) as book:
            resources = {sheet: len(pd.read_excel(book, sheet_name=sheet)) for sheet in book.sheet_names}
    except (OSError, ValueError) as exc:
        warnings.append(f"资源先验不可读：{exc}")
    return {key: "\n".join(values) for key, values in controls.items()}, resources, warnings


def tree_payload(root: Path = KNOWLEDGE_BASE_DIR) -> dict[str, Any]:
    source = current_root(root)
    value = copy.deepcopy(_json(source / TREE_FILE))
    controls, resources, warnings = prior_status(source)
    warnings = [*value.get("warnings", []), *warnings]
    leaves = flatten(value["scenes"])
    for leaf, labels in leaves:
        leaf.update(dict(zip(PATH_COLUMNS, labels)))
        apps = []
        for app in _app_nodes(leaf):
            app["app"] = app["label"]
            app["control_prior_available"] = bool(controls.get((*labels, app["label"])))
            app["resource_count"] = resources.get(app["label"], 0)
            apps.append(app)
            if app.get("use_resource_prior") and app["label"] not in resources:
                warnings.append(f"App {app['label']} 缺少资源先验 sheet，仍允许生成")
        leaf["children"] = apps
        leaf["generatable"] = bool(apps)
        # Keep a read-only compatibility projection for older API consumers.
        leaf["app_configs"] = [
            {
                "id": app["id"], "app": app["label"], "reference_example": app["reference_example"],
                "use_resource_prior": app["use_resource_prior"],
                "control_prior_available": app["control_prior_available"], "resource_count": app["resource_count"],
            }
            for app in apps
        ]
    value.update({"schema_version": 2, "leaf_count": len(leaves), "execution_unit_count": sum(len(_app_nodes(leaf)) for leaf, _ in leaves), "warnings": list(dict.fromkeys(warnings))})
    return value


def snapshot(destination: Path, *, root: Path = KNOWLEDGE_BASE_DIR, version: str | None = None) -> dict[str, Any]:
    source = current_root(root)
    value = _json(source / TREE_FILE)
    if version is not None and value["version"] != version:
        raise VersionConflict("知识库已更新，请刷新场景树后重新提交")
    # A pinned immutable source remains valid even if another edit publishes now.
    for kind, filename in KNOWLEDGE_BASE_FILES.items():
        validate_workbook(source / filename, kind)
    destination.mkdir(parents=True, exist_ok=True)
    files = []
    for filename in [*KNOWLEDGE_BASE_FILES.values(), TREE_FILE]:
        target = destination / filename
        shutil.copy2(source / filename, target)
        files.append({"filename": filename, "sha256": hashlib.sha256(target.read_bytes()).hexdigest()})
    return {"directory": str(destination), "version": value["version"], "files": files}
