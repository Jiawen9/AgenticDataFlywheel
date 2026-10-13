"""Read-only tree diagnostic for the v5 backend; networking requires --probe-model."""
import argparse
import hashlib
import inspect
import json
import logging
import re
import shutil
import sqlite3
import stat
import sys
import time
from pathlib import Path

sys.dont_write_bytecode = True
repo = Path(__file__).resolve().parent.parent
if (repo / "backend").is_dir():
    sys.path.insert(0, str(repo))

from backend.file_io import io_path, resolve_path
try:
    from backend.pipeline_retry_errors import ModelOutputError
except ImportError:  # Retain safe labels for exceptions without the typed wrapper.
    ModelOutputError = None
from backend.trajectories_tree.tree_builder import parse_action
from backend.trajectories_tree.intermediate_state_classifier import (
    LEGACY_PROMPT_VERSION, PREVIOUS_PROMPT_VERSION, PROMPT_VERSION, QwenIntermediateStateClassifier,
    _image_data_url, _image_digest, parse_cached_classification,
)

KNOWN_PROTOCOLS = {LEGACY_PROMPT_VERSION, PREVIOUS_PROMPT_VERSION, PROMPT_VERSION}


def read(path):
    return json.loads(io_path(path).read_text(encoding="utf-8"))


def parse_failure_reason(exc):
    if ModelOutputError is not None and isinstance(exc, ModelOutputError):
        return {
            "empty_choices": "模型返回 choices 为空",
            "empty_content": "模型返回空内容",
            "invalid_content": "模型 content 类型不符合要求",
            "truncated": "输出被截断，finish_reason=length",
            "invalid_json": "JSON 不完整或格式错误",
            "invalid_schema": "模型字段缺失、类型或取值错误",
            "inconsistent_fields": "分类字段冲突",
            "missing_observation": "observation 为空",
        }[exc.code]
    # Exception messages may contain provider output; expose fixed labels only.
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
        ("Qwen classification response was truncated", "输出被截断，finish_reason=length"),
        ("Qwen returned an empty classification response", "模型返回空内容"),
    )
    return next((safe for prefix, safe in labels if text.startswith(prefix)),
                "字段类型或其他本地校验错误")


def parse_probe(label, item, version):
    if item is None:
        print(label + "：未命中")
        return
    try:
        result = parse_cached_classification(item, version=version)
        print(label + "：解析通过")
        for name, source in (("分类来源", result.classification_source),
                             ("Observation 来源", result.observation_source)):
            # Persisted metadata is untrusted; print only a known version and digest.
            if (isinstance(source, dict) and isinstance(source.get("version"), str)
                    and source["version"] in KNOWN_PROTOCOLS
                    and isinstance(source.get("cache_key"), str)
                    and re.fullmatch(r"[a-f0-9]{64}", source["cache_key"])):
                print(label + " " + name + "：", json.dumps(
                    {"version": source["version"], "cache_key": source["cache_key"]}, ensure_ascii=False))
    except Exception as exc:
        print(label + "：解析失败；" + parse_failure_reason(exc) + "；异常类型 " + type(exc).__name__)


def _safe_request_id(value):
    if (isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,96}", value)
            and not value.lower().startswith(("sk-", "sk_", "bearer"))):
        return value
    return None


def model_probe(job, cache_path, env_path, inputs):
    """Opt-in single model request. Shared data, cache and database stay untouched."""
    from backend.tree_build_service import tree_build_config
    from backend.trajectories_preprocessing import configure_reviewer_environment
    from backend.pipeline_retry_errors import failure_from_exception

    current_config = tree_build_config(env_path)
    frozen = job.get("build_config") or {}
    frozen_version = frozen.get("prompt_version")
    print("原作业分类协议：", frozen_version if isinstance(frozen_version, str) and frozen_version in KNOWN_PROTOCOLS else "未记录或未知")
    print("当前探测分类协议：", PROMPT_VERSION)
    if frozen_version != PROMPT_VERSION:
        print("诊断边界：使用当前分类协议和提示词探测旧作业输入，不是旧模型请求的原样复现")
    if any(current_config.get(k) != frozen.get(k) for k in ("model", "model_url")):
        print("模型或服务地址已变化，停止联网探测")
        return 1
    classifier = None
    calls = 0
    meta = {}
    # SDK child loggers may expose request data when OPENAI_LOG=debug is set.
    logging_threshold = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        model = configure_reviewer_environment(env_path)
        if model != frozen.get("model"):
            print("模型配置与失败作业不一致，停止联网探测")
            return 1
        classifier = QwenIntermediateStateClassifier(model, cache_path)
        if str(classifier.client.base_url).rstrip("/") != str(frozen.get("model_url", "")).rstrip("/"):
            print("实际客户端地址与作业记录不一致，停止联网探测")
            return 1
        classifier.cache = {}  # Request this step afresh, without touching existing cache.
        classifier._save_cache_unlocked = lambda: None
        print("探测使用空内存缓存，仅检查当前协议新回答，不复现旧缓存分类组合")
        classifier.client = classifier.client.with_options(max_retries=0)
        original = classifier.client.chat.completions.create

        def capture(*args, **kwargs):
            nonlocal calls
            if calls:
                raise RuntimeError("diagnostic single-call limit")
            calls += 1
            print("模型请求次数：", calls)
            started = time.monotonic()
            try:
                response = original(*args, **kwargs)
            finally:
                print("模型请求耗时（毫秒）：", round((time.monotonic() - started) * 1000))
            choices = getattr(response, "choices", None)
            count = len(choices) if isinstance(choices, (list, tuple)) else None
            choice = choices[0] if count else None
            finish = getattr(choice, "finish_reason", None)
            finish = finish if isinstance(finish, str) and finish in {
                "stop", "length", "content_filter", "tool_calls", "function_call"
            } else "其他或缺失"
            content = getattr(getattr(choice, "message", None), "content", None)
            kind = type(content).__name__
            kind = kind if kind in {"str", "NoneType", "list", "dict", "int", "float", "bool"} else "other"
            tokens = getattr(getattr(response, "usage", None), "completion_tokens", None)
            meta.update(
                max_tokens=kwargs.get("max_tokens"), choices_count=count,
                finish_reason=finish, content_type=kind,
                content_characters=len(content) if isinstance(content, str) else None,
                completion_tokens=tokens if isinstance(tokens, int) and not isinstance(tokens, bool) else None,
                request_id=_safe_request_id(getattr(response, "_request_id", None)),
            )
            print("模型返回元信息：", json.dumps(meta, ensure_ascii=False))
            return response

        classifier.client.chat.completions.create = capture
        classifier.classify(**inputs)
        print("模型调用及内容校验：通过；未写磁盘缓存")
        print("诊断边界：本次跳过缓存落盘，仍不能排除原作业的缓存写入故障")
        return 0
    except Exception as exc:
        print("模型探测失败；异常类型：", type(exc).__name__)
        failure = failure_from_exception(exc)
        print("安全错误信息：", json.dumps({k: failure.get(k) for k in
              ("category", "http_status", "code", "request_id")}, ensure_ascii=False))
        if meta.get("choices_count") == 0:
            print("定位：模型返回 choices 为空")
        elif meta.get("finish_reason") == "length":
            print("定位：模型输出被截断")
        elif meta and meta.get("content_type") != "str":
            print("定位：模型 content 缺失或类型不符合要求")
        elif isinstance(exc, (ValueError, TypeError)):
            print("定位：", parse_failure_reason(exc))
        return 1
    finally:
        print("模型探测请求总数：", calls)
        if classifier is not None:
            try:
                classifier.client.close()
            except Exception:
                print("模型客户端关闭失败；未影响前述诊断结果")
        logging.disable(logging_threshold)


def local_storage_probe(root, database, cache_path, model):
    """Read-only hints; directory attributes do not prove that a write will succeed."""
    print("缓存保存本地检查（不试写）：")
    for label, path in (("缓存目录", cache_path.parent), ("缓存文件", cache_path),
                        ("缓存临时文件", cache_path.with_name("." + cache_path.name + ".tmp"))):
        try:
            exists = io_path(path).exists()
            print(label + "存在：", exists)
            if exists:
                info = io_path(path).stat()
                attributes = getattr(info, "st_file_attributes", None)
                if attributes is not None:
                    print(label + "只读属性：", bool(attributes & stat.FILE_ATTRIBUTE_READONLY))
                if io_path(path).is_file():
                    print(label + "大小（字节）：", info.st_size)
        except Exception as exc:
            print(label + "检查失败；异常类型：", type(exc).__name__)
    try:
        print("数据磁盘剩余空间（MiB）：", shutil.disk_usage(io_path(root)).free // (1024 * 1024))
    except Exception as exc:
        print("磁盘空间检查失败；异常类型：", type(exc).__name__)
    connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=30)
    try:
        active = [json.loads(row[0]) for row in connection.execute(
            "SELECT payload FROM records WHERE namespace=?", ("tree_jobs",))]
        print("登记为 running 的建树作业数：", sum(job.get("status") == "running" for job in active))
    finally:
        connection.close()
    try:
        source = inspect.getsource(QwenIntermediateStateClassifier.classify)
        validation = source.find("parse_classification_response(raw, require_observation=True)")
        storage = source.find("self.cache[cache_key] =")
        if storage < 0:
            storage = source.find("self._store_result(cache_key, result)")
        if validation >= 0 and storage >= 0:
            print("当前部署的新响应先校验再缓存：", validation < storage)
        response_parser = QwenIntermediateStateClassifier.classify.__globals__.get("response_text")
        response_source = inspect.getsource(response_parser) if callable(response_parser) else ""
        envelope_source = source + response_source
        print("当前部署有 length 截断检查：", 'finish_reason' in envelope_source and '"length"' in envelope_source)
        budget = re.search(r'max_tokens\s*=\s*(\d+)\s+if\s+self\.model\.strip\(\)\.lower\(\)\s*==\s*[\'\"]([^\'\"]+)[\'\"]\s+else\s+(\d+)', source)
        fixed = re.search(r'max_tokens\s*=\s*(\d+)\s*[,)]', source)
        if budget:
            print("当前模型输出 token 上限：", int(budget.group(1) if str(model).strip().lower() == budget.group(2) else budget.group(3)))
        elif fixed:
            print("当前模型输出 token 上限：", int(fixed.group(1)))
    except Exception as exc:
        print("部署代码检查失败；异常类型：", type(exc).__name__)


def probe(root, job_id, *, probe_model=False, env_path=None):
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
    images_ok = True
    for label, path in (("当前截图", current), ("动作后截图", after)):
        try:
            _image_data_url(path)  # Local decoding/encoding only; discard all pixels.
            print(label + "：读取和解码通过")
        except Exception as exc:
            images_ok = False
            print(label + "：读取或解码失败；异常类型 " + type(exc).__name__)
    if probe_model and not images_ok:
        print("截图未通过，停止联网探测")
        return 1
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
    local_storage_probe(root, database, cache_path, job["build_config"]["model"])
    if not io_path(cache_path).is_file():
        print("分类缓存文件不存在")
        cache = {}
    else:
        cache = read(cache_path)
    old_key = QwenIntermediateStateClassifier._key({"version": LEGACY_PROMPT_VERSION, **common})
    print("v2 旧缓存键：", old_key)
    parse_probe("v2 旧缓存", cache.get(old_key), LEGACY_PROMPT_VERSION)
    goals = {}
    for task in read(stage / "00_collection" / "result.json").get("snapshot", {}).get("tasks", []):
        for identifier in (task.get("task_id"), task.get("collection_case_id")):
            if identifier:
                goals[str(identifier)] = str(task.get("task") or identifier)
    task = goals.get(str(row.get("task_id") or "").strip())
    if not task:
        print("来源快照缺少明确任务目标，无法精确计算当前缓存键")
        return 1
    observation_inputs = {**common, "task": task, "after_image_digest": _image_digest(after)}
    for label, version in (("v4 旧缓存", PREVIOUS_PROMPT_VERSION), ("v5 当前缓存", PROMPT_VERSION)):
        key = QwenIntermediateStateClassifier._key({"version": version, **observation_inputs})
        print(label + "键：", key)
        parse_probe(label, cache.get(key), version)
    if probe_model:
        inputs = {
            "trajectory": trajectory, "step_index": step, "current_image_path": current,
            "next_image_path": following, "after_image_path": done if io_path(done).is_file() else None,
            "action": common["action"], "summary": common["summary"],
            "previous_summary": common["previous_summary"], "next_summary": common["next_summary"],
            "task": task,
        }
        return model_probe(job, cache_path, env_path or repo / "backend" / ".env", inputs)
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--probe-model", action="store_true", help="request this step once; never writes cache")
    parser.add_argument("--env-file", type=Path, help="model settings file; default backend/.env")
    options = parser.parse_args()
    try:
        code = probe(options.data_root, options.job_id, probe_model=options.probe_model, env_path=options.env_file)
    except Exception as exc:
        print("诊断未完成；异常类型 " + type(exc).__name__)
        code = 1
    sys.exit(code)
