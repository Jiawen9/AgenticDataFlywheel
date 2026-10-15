"""Offline, read-only probe for one failed tree classification; never calls a model."""
import argparse
import hashlib
import json
import re
import sqlite3
import sys
from pathlib import Path

sys.dont_write_bytecode = True
repo = Path(__file__).resolve().parent.parent
if (repo / "backend").is_dir():
    sys.path.insert(0, str(repo))

from backend.file_io import io_path, resolve_path
from backend.trajectories_tree.tree_builder import parse_action
from backend.trajectories_tree.intermediate_state_classifier import (
    LEGACY_PROMPT_VERSION, PROMPT_VERSION, QwenIntermediateStateClassifier,
    _image_data_url, _image_digest, parse_classification_response,
)


def read(path):
    return json.loads(io_path(path).read_text(encoding="utf-8"))


def parse_probe(label, raw, observation):
    try:
        parse_classification_response(raw, require_observation=observation)
        print(label + "：解析通过")
    except Exception as exc:
        # Exception messages can include a provider response. Only fixed labels escape.
        text = str(exc)
        labels = (
            ("Qwen response does not contain JSON", "没有 JSON"),
            ("Qwen response contains invalid JSON", "JSON 不完整或格式错误"),
            ("Qwen classification must contain boolean", "is_intermediate 类型错误"),
            ("invalid intermediate-state category", "category 无效"),
            ("intermediate state must use", "分类字段冲突"),
            ("stable state must use", "分类字段冲突"),
            ("confidence must be", "confidence 越界"),
            ("classification reason must", "reason 为空"),
            ("observation must", "observation 为空"),
        )
        label_text = next((safe for prefix, safe in labels if text.startswith(prefix)),
                          "字段类型或其他解析错误")
        print(label + "：解析失败；" + label_text + "；异常类型 " + type(exc).__name__)


def probe(root, job_id):
    root = resolve_path(root)
    database = root / "system" / "app.sqlite"
    if not io_path(database).is_file():
        print("数据库不存在，请核对数据目录")
        return 1
    connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=30)
    try:
        found = connection.execute(
            "SELECT payload FROM records WHERE namespace=? AND record_key=?",
            ("tree_jobs", job_id),
        ).fetchone()
    finally:
        connection.close()
    if not found:
        print("未找到指定建树作业")
        return 1
    job = json.loads(found[0])
    batch = str(job["batch_id"])
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", batch):
        print("批次编号格式异常")
        return 1
    location = job.get("failure") or {}
    trajectory, step = location.get("trajectory_id"), location.get("step")
    if not trajectory or not isinstance(step, int) or isinstance(step, bool):
        print("作业没有明确的轨迹及步骤定位")
        return 1
    stage = root / "batches" / batch
    annotation = stage / "02_annotation"
    manifest = read(annotation / "manifest.json")
    if manifest.get("version") != job.get("annotation_version"):
        print("标框修订已变化，不能用当前输入复盘此作业")
        return 1
    expected = next((item.get("sha256") for item in job.get("annotation_ref", {}).get("files", [])
                     if item.get("name") == "result.json"), None)
    payload_bytes = io_path(annotation / "result.json").read_bytes()
    if not expected or hashlib.sha256(payload_bytes).hexdigest() != expected:
        print("无法确认标框 JSON 与失败作业的输入一致")
        return 1
    payload = json.loads(payload_bytes)
    rows = next(iter(payload["sheets"].values()))
    target = []
    for order, row in enumerate(rows):
        if str(row.get("trajectory_id") or "").strip() != trajectory:
            continue
        match = re.search(r"step(\d+)", Path(str(row.get("image") or "")).name, re.I)
        number = int(match.group(1)) if match else len(target) + 1
        target.append((number, order, row))
    target.sort(key=lambda item: (item[0], item[1]))
    positions = [i for i, item in enumerate(target) if item[0] == step]
    if len(positions) != 1:
        print("目标步骤不存在或不唯一")
        return 1
    pos = positions[0]
    row = target[pos][2]
    nxt = target[pos + 1][2] if pos + 1 < len(target) else None
    raw_root = resolve_path(Path(job["raw_root"]))
    if not raw_root.is_relative_to(root / "raw"):
        print("作业来源不在当前数据目录，需核对部署路径")
        return 1

    def resource(value):
        path = resolve_path(raw_root / str(value["image"]).strip())
        if not path.is_relative_to(raw_root):
            raise ValueError("resource boundary")
        return path

    current = resource(row)
    following = resource(nxt) if nxt else None
    done = current.with_name(re.sub(r"_input(?:_stability)?\.jpg$", "_done.jpg", current.name, flags=re.I))
    after = done if io_path(done).is_file() else following or current
    print("标框输入一致；目标步骤：", step)
    for label, path in (("当前截图", current), ("动作后截图", after)):
        try:
            _image_data_url(path)  # Local decoding/encoding only; discard all pixels.
            print(label + "：读取和解码通过")
        except Exception as exc:
            print(label + "：读取或解码失败；异常类型 " + type(exc).__name__)
    summary = lambda value: str(value.get("summary") or "")
    common = {
        "model": job["build_config"]["model"], "trajectory": trajectory, "step_index": step,
        "current_image_digest": _image_digest(current),
        "next_image_digest": _image_digest(following) if following else "",
        "action": parse_action(str(row.get("action") or "")), "summary": summary(row),
        "previous_summary": summary(target[pos - 1][2]) if pos else "",
        "next_summary": summary(nxt) if nxt else "",
    }
    cache_path = root / "cache" / "trajectories_tree" / "qwen_intermediate_state_cache.json"
    if not io_path(cache_path).is_file():
        print("分类缓存文件不存在")
        return 0
    cache = read(cache_path)
    old_key = QwenIntermediateStateClassifier._key({"version": LEGACY_PROMPT_VERSION, **common})
    old = cache.get(old_key)
    print("旧缓存键：", old_key)
    if isinstance(old, str):
        parse_probe("旧缓存", old, False)
    else:
        print("旧缓存：无可用字符串条目")
    goals = {}
    for task in read(stage / "00_collection" / "result.json").get("snapshot", {}).get("tasks", []):
        for identifier in (task.get("task_id"), task.get("collection_case_id")):
            if identifier:
                goals[str(identifier)] = str(task.get("task") or identifier)
    task = goals.get(str(row.get("task_id") or "").strip())
    if not task:
        print("来源快照缺少明确任务目标，无法精确计算当前缓存键")
        return 1
    current_key = QwenIntermediateStateClassifier._key({
        "version": PROMPT_VERSION, **common, "task": task, "after_image_digest": _image_digest(after),
    })
    item = cache.get(current_key)
    print("当前缓存键：", current_key)
    if item is None:
        print("当前缓存：未命中")
    else:
        raw = str(item.get("raw_response", "")) if isinstance(item, dict) else str(item)
        parse_probe("当前缓存", raw, True)
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--job-id", required=True)
    options = parser.parse_args()
    try:
        code = probe(options.data_root, options.job_id)
    except Exception as exc:
        print("诊断未完成；异常类型 " + type(exc).__name__)
        code = 1
    sys.exit(code)
