"""Resolve display/edit coordinates from registered, step-local evidence.

This does not change actions or historical scoring. XML bounds are deliberately
not used as a screen size: a UI tree may cover only part of the input image.
"""
from __future__ import annotations

from collections import OrderedDict
from copy import deepcopy
from concurrent.futures import Future
import hashlib
import json
import math
from pathlib import Path, PurePosixPath
import re
import threading

from PIL import Image

from .data_store import ArtifactStore, DATA_ROOT
from .file_io import io_path, resolve_path
from .trajectory_context import AnnotationVersionConflict, resolve_batch_context, row_trajectory_id

_INDEX_LIMIT = 8
_ROW_LIMIT = 100_000
_INDEXES: OrderedDict = OrderedDict()
_INDEX_LOCK = threading.Lock()
_INFLIGHT: dict[tuple, Future] = {}
_INPUT = re.compile(r"^(step(\d+)_vla)_input\.(?:jpg|jpeg|png)$", re.I)
_POINT_KEYS = {"click": ("coordinate",), "long_press": ("coordinate",),
               "swipe": ("start_coordinate", "end_coordinate")}


def action_object(value):
    if isinstance(value, dict):
        return value
    try:
        value = json.loads(str(value))
    except (TypeError, ValueError):
        return {}
    if isinstance(value, list) and len(value) == 1:
        value = value[0]
    return value if isinstance(value, dict) else {}


def action_digest(value):
    return hashlib.sha256(json.dumps(action_object(value), ensure_ascii=False,
                                    sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def coordinate_changed(before, after):
    before, after = action_object(before), action_object(after)
    keys = _POINT_KEYS.get(after.get("action"), ())
    return bool(keys) and (_POINT_KEYS.get(before.get("action"), ()) != keys
                           or any(before.get(key) != after.get(key) for key in keys))


def _unknown(reason):
    return {"space": "unknown", "reason": reason}


def _safe_path(root, image):
    value = str(image or "").replace("\\", "/")
    if not value or PurePosixPath(value).is_absolute() or ":" in value:
        raise ValueError("图片必须是已登记的相对路径")
    root = resolve_path(root)
    target = resolve_path(root / value)
    if not target.is_relative_to(root):
        raise ValueError("图片路径超出已登记的原始目录")
    return target


def _read_json(path):
    # A response is step-local; never read the whole trajectory or batch here.
    with io_path(path).open("rb") as stream:
        content = stream.read(4 * 1024 * 1024 + 1)
    if len(content) > 4 * 1024 * 1024:
        raise ValueError("模型响应文件过大")
    return json.loads(content.decode("utf-8-sig"))


def resolve_coordinate_info(row, raw_root, *, current_action=None, saved_contract=None,
                            legacy_native=False):
    base = action_object(row.get("actions", row.get("action")))
    current = base if current_action is None else action_object(current_action)
    image = str(row.get("image") or "").replace("\\", "/")
    contract = saved_contract or row.get("coordinate_info") or row.get("values", {}).get("coordinate_info")
    if isinstance(contract, str):
        try:
            contract = json.loads(contract)
        except ValueError:
            contract = None
    bound_contract = (isinstance(contract, dict) and contract.get("contract_version") == 1
                      and contract.get("action_sha256") == action_digest(current)
                      and contract.get("image") == image)
    native_source = action_object(contract.get("source_action")) if bound_contract else base
    agent = str(row.get("agent_type") or "").lower()
    if agent == "newagent":
        return {"space": "normalized", "reason": "newagent_legacy", "normalization_base": 1000, "source_action": native_source}
    if agent and agent != "pipelineagent":
        return _unknown("unrecognized_agent")
    try:
        path = _safe_path(raw_root, image)
        match = _INPUT.fullmatch(path.name)
        response_path = _safe_path(raw_root, str(PurePosixPath(image).with_name(match.group(1) + "_model_response.json"))) if match else None
        response = _read_json(response_path) if response_path else {}
        if not isinstance(response, dict):
            response = {}
    except (OSError, UnicodeError, ValueError):
        response, match = {}, None
    response_agent = str(response.get("agent_type") or "").lower()
    if agent != "pipelineagent" and response_agent != "pipelineagent":
        # Explicit legacy human8.0 schema, or native tool-call evidence. Merely
        # omitting agent_type from an external row is not a coordinate contract.
        if legacy_native or (row.get("trajectory_format") == "newagent-compatible-v1"
                             and row.get("normalizer_version") == "native-v1"):
            return {"space": "normalized", "reason": "legacy_native_format", "normalization_base": 1000, "source_action": native_source}
        content = response.get("content")
        if response_agent in {"", "newagent"} and isinstance(content, str):
            found = re.search(r"<tool_call>\s*", content)
            if found:
                try:
                    native, _ = json.JSONDecoder().raw_decode(content[found.end():])
                    native = action_object(native)
                    if native == base or (bound_contract and contract.get("space") == "normalized" and native == native_source):
                        return {"space": "normalized", "reason": "native_response", "normalization_base": 1000, "source_action": native}
                except ValueError:
                    pass
        return _unknown("coordinate_source_unconfirmed")
    if not match or response_agent != "pipelineagent":
        return _unknown("pipeline_execution_evidence_missing")
    if (response.get("actual_action_coordinate_system") != "pixel"
            or type(response.get("global_step")) is not int
            or response["global_step"] != int(match.group(2))):
        return _unknown("pipeline_pixel_contract_missing")
    executed = action_object(response.get("executed_action"))
    edited_contract = (bound_contract
                       and action_object(contract.get("source_action")) == executed)
    if not executed or (executed != base and not edited_contract):
        return _unknown("executed_action_mismatch")
    if current != base and not edited_contract:
        return _unknown("edited_action_contract_missing")
    try:
        with Image.open(io_path(path)) as screenshot:
            width, height = screenshot.size
        if width <= 0 or height <= 0:
            raise ValueError("invalid image dimensions")
    except (OSError, ValueError):
        return _unknown("original_input_image_unreadable")
    if edited_contract and (contract.get("space") != "pixel"
                            or contract.get("width") != width or contract.get("height") != height):
        return _unknown("edited_action_contract_mismatch")
    return {"space": "pixel", "width": width, "height": height,
            "reason": "pipeline_executed_input_image", "source_action": executed}


def edit_contract(info, action, image):
    """Validate new coordinates; never clamp or rewrite stored actions."""
    action = action_object(action)
    if info.get("space") not in {"pixel", "normalized"}:
        raise ValueError("坐标来源尚未确认，不能修改坐标；仍可保存文字或未变动作")
    for key in _POINT_KEYS.get(action.get("action"), ()):
        point = action.get(key)
        if (not isinstance(point, list) or len(point) != 2
                or any(type(n) not in {int, float} or not math.isfinite(n) for n in point)):
            raise ValueError("坐标必须是两个有限数值")
        limits = (info["width"], info["height"]) if info["space"] == "pixel" else (1000, 1000)
        if any(n < 0 or n >= limit for n, limit in zip(point, limits)):
            raise ValueError("修改的坐标超出当前坐标系范围")
    return {**deepcopy(info), "contract_version": 1, "image": str(image).replace("\\", "/"),
            "action_sha256": action_digest(action)}


def _batch_index(key, batch_id, version, root):
    with _INDEX_LOCK:
        cached = _INDEXES.get(key)
        if cached is not None:
            _INDEXES.move_to_end(key)
            return cached
        pending = _INFLIGHT.get(key)
        owner = pending is None
        if owner:
            pending = _INFLIGHT[key] = Future()
    if not owner:
        return pending.result()
    try:
        context = resolve_batch_context(batch_id, version, root)
        rows = {(row_trajectory_id(row), str(row.get("image") or "").replace("\\", "/")):
                {name: row[name] for name in ("image", "actions", "action", "agent_type", "trajectory_format", "normalizer_version", "coordinate_info") if name in row}
                for sheet in context.payload["sheets"].values() for row in sheet}
        cached = (context.raw_root, rows)
        with _INDEX_LOCK:
            _INDEXES[key] = cached
            while len(_INDEXES) > _INDEX_LIMIT or (len(_INDEXES) > 1 and sum(len(item[1]) for item in _INDEXES.values()) > _ROW_LIMIT):
                _INDEXES.popitem(last=False)
        pending.set_result(cached)
        return cached
    except BaseException as exc:
        pending.set_exception(exc)
        raise
    finally:
        with _INDEX_LOCK:
            _INFLIGHT.pop(key, None)


def batch_coordinate_info(batch_id, annotation_version, trajectory_id, image, *, root=None):
    root = resolve_path(Path(root or DATA_ROOT))
    manifest = ArtifactStore(root).get(batch_id, "02_annotation", annotation_version)
    if manifest is None:
        if annotation_version:
            raise AnnotationVersionConflict("标注版本已变化，请刷新后重试")
        raise FileNotFoundError("该批次尚未完成轨迹预处理")
    version = manifest["version"]
    # Revalidate registry identity even when the index is already cached.
    key = (str(root), batch_id, version, hashlib.sha256(json.dumps(manifest, sort_keys=True,
                                                                default=str).encode()).hexdigest())
    raw_root, rows = _batch_index(key, batch_id, version, root)
    row = rows.get((trajectory_id, image.replace("\\", "/")))
    if row is None:
        raise FileNotFoundError("当前标注版本中不存在该轨迹图片")
    # Image I/O is outside annotation/index locks; image is a registered row key.
    return {"coordinate_info": resolve_coordinate_info(row, raw_root), "annotation_version": version}
