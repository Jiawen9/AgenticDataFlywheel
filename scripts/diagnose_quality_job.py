"""Export quality diagnostics; --probe makes ONE model request, without redaction.

Run on the platform server with its Python environment, from any working directory.
Only this script's unique output directory is written. Production jobs, artifacts,
and caches are never updated. Historical discarded error bodies cannot be recovered.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import time
import traceback
import uuid
from datetime import datetime

sys.dont_write_bytecode = True
REPO = Path(__file__).resolve().parents[1]
RUBRICS = REPO / "backend" / "DevelopRubrics"
for directory in (REPO, RUBRICS):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))


def write_json(output: Path, name: str, value) -> None:
    from backend.file_io import io_path
    io_path(output / name).write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def read_artifact(root: Path, records, batch: str, stage: str) -> dict:
    """Read a checksum-verified copy without invoking artifact recovery or locks."""
    from backend.data_store.paths import contained_path
    from backend.file_io import io_path
    key = f"{batch}/{stage}"
    ref = records.get("artifacts", key)
    if not ref:
        raise ValueError(f"没有登记的当前过程件: {key}")
    entry = next(item for item in ref["files"] if item["name"] == "result.json")
    path = contained_path(root, entry["path"])
    expected_parent = contained_path(root, "batches", batch, stage)
    if ref.get("schema_version", 1) < 2:
        expected_parent = contained_path(expected_parent, ref["version"])
    if path.parent != expected_parent:
        raise ValueError("过程件路径与登记阶段不一致")
    raw = io_path(path).read_bytes()
    if len(raw) != entry["size"] or hashlib.sha256(raw).hexdigest() != entry["sha256"]:
        raise ValueError("过程件校验失败或正在更新，未发送模型请求")
    if records.get("artifacts", key) != ref:
        raise ValueError("读取时过程件修订发生变化，未发送模型请求")
    return json.loads(raw)


def load_example(name: str):
    module_name = "quality_diagnostic_" + name.replace("-", "_")
    spec = importlib.util.spec_from_file_location(module_name, RUBRICS / "examples" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def prepare_score(root, generator, config, task, selected, job, output, trajectory_id, step):
    from backend.file_io import io_path
    from adarubric.evaluator.trajectory_evaluator import LLMTrajectoryEvaluator, _EvaluationResponse
    evaluator_script = load_example("evaluate-jiawen-rubrics")
    failure = job.get("failure") or {}
    trajectory_id = trajectory_id or failure.get("trajectory_id")
    step = step if step is not None else failure.get("step")
    trajectory = next((item for item in selected if item.trajectory_id == trajectory_id), None)
    if trajectory is None:
        raise ValueError("找不到失败轨迹，请用 --trajectory-id 指定该任务内的轨迹")
    if type(step) is not int or step not in {item.step_id for item in trajectory.steps}:
        raise ValueError("找不到失败评分 step_id，请用 --step 指定；它不是截图文件编号")

    parent = root / "system" / "rubric_outputs" / "rubrics"
    candidates = [parent / f"jiawen_gui_initial_rubric__{generator._safe_filename_part(task.task_id)}.json",
                  parent / "jiawen_gui_initial_rubric.json"]
    rubric_path, raw = None, None
    for path in candidates:
        try:
            data = io_path(path).read_bytes()
            if json.loads(data).get("task_id") == task.task_id:
                rubric_path, raw = path, data
                break
        except (OSError, ValueError):
            continue
    if rubric_path is None:
        raise ValueError("没有匹配的已有 Rubric，评分探测不会额外调用模型生成它")
    io_path(output / "rubric.json").write_bytes(raw)
    rubric = evaluator_script._load_rubric(output / "rubric.json", task)
    trajectory = evaluator_script._trajectory_for_evaluation(trajectory,
        include_action=evaluator_script._bool_setting(config, "evaluation_include_action", "ADARUBRIC_EVAL_INCLUDE_ACTION", default=True),
        include_action_input=evaluator_script._bool_setting(config, "evaluation_include_action_input", "ADARUBRIC_EVAL_INCLUDE_ACTION_INPUT", default=True))
    enabled = evaluator_script._bool_setting(config, "evaluation_chunk_enabled", "ADARUBRIC_EVAL_CHUNK_ENABLED", default=False)
    threshold = evaluator_script._int_setting(config, "evaluation_chunk_threshold", "ADARUBRIC_EVAL_CHUNK_THRESHOLD", default=10)
    lookahead = evaluator_script._int_setting(config, "evaluation_step_lookahead", "ADARUBRIC_EVAL_STEP_LOOKAHEAD", default=1, minimum=0)
    target_ids = None
    if enabled and len(trajectory.steps) > threshold:
        chunk = next(item for item in evaluator_script._trajectory_chunks(trajectory, lookahead_steps=lookahead)
                     if step in item.core_step_ids)
        trajectory, target_ids = chunk.trajectory, chunk.core_step_ids
    # No clients are created here; this only uses the production prompt builder.
    evaluator = LLMTrajectoryEvaluator(None, aggregator=evaluator_script._build_aggregator(config))
    messages = evaluator._build_messages(trajectory, rubric, task.instruction, target_step_ids=target_ids)
    instruction = "\n\nYou MUST respond with valid JSON matching this schema:\n" + json.dumps(_EvaluationResponse.model_json_schema(), indent=2)
    if messages and messages[-1]["role"] == "user":
        messages[-1] = {**messages[-1], "content": messages[-1]["content"] + instruction}
    else:
        messages.append({"role": "user", "content": instruction})
    context = {
        "rubric": json.loads(rubric.model_dump_json()),
        "trajectory": json.loads(trajectory.model_dump_json()),
        "target_step_ids": sorted(target_ids) if target_ids is not None else None,
        "config": config,
    }
    return {"messages": messages, "response_format": {"type": "json_object"},
            # Match the explicit overrides made by quality_job_runner, not CLI defaults.
            "temperature": float(config.get("evaluation_temperature", 0.0)),
            "max_tokens": int(config.get("evaluation_max_tokens", 8192)),
            "validation_context": context}, {
        "trajectory_id": trajectory_id, "requested_step_id": step,
        "target_step_ids": context["target_step_ids"],
        "context_step_ids": [item.step_id for item in trajectory.steps],
        "scope": "single_step_chunk" if target_ids is not None else "whole_trajectory_single_request",
        "rubric_source": str(rubric_path), "rubric_sha256": hashlib.sha256(raw).hexdigest(),
        "rubric_is_current_copy_not_historical_frozen_snapshot": True,
    }


def prepare_request(root: Path, records, job: dict, task_id: str | None, output: Path,
                    *, operation="rubric", trajectory_id=None, step=None) -> dict:
    from backend.batch_results import annotation_task_fingerprints, quality_task_fingerprints
    from backend.stage_artifacts import load_quality_objects
    from trajectory_tools.settings import configure_model_environment, DEFAULT_ENV_FILE
    from adarubric.llm.http_options import resolve_http_options

    if job.get("status") in {"queued", "running"}:
        raise ValueError("所选质检作业仍在执行；请选择已失败作业，避免与原作业同时调用模型")
    task_id = task_id or job.get("current_task") or (job.get("failure") or {}).get("task_id")
    if not task_id:
        tasks = job.get("task_ids") or []
        if len(tasks) != 1:
            raise ValueError("无法确定失败任务，请用 --task-id 指定该作业中的任务")
        task_id = tasks[0]
    if task_id not in (job.get("task_ids") or []):
        raise ValueError("指定任务不属于所选质检作业")
    batch = job.get("batch_id") or job.get("run_id")
    if not batch or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for char in batch):
        raise ValueError("无法确定有效业务批次编号")
    trees = read_artifact(root, records, batch, "04_tree")
    annotation = read_artifact(root, records, batch, "02_annotation")
    expected = trees.get("source_task_fingerprints", {}).get(task_id)
    if not expected or annotation_task_fingerprints(annotation).get(task_id) != expected:
        raise ValueError("当前标框与轨迹树不匹配，不能用已失效输入复现")
    original_hash = job.get("source_tree_hashes", {}).get(task_id)
    if original_hash and trees.get("tree_hashes", {}).get(task_id) != original_hash:
        raise ValueError("该任务的树已经变化，不能冒充旧作业输入重新请求")

    quality = trees["quality_input"]
    # The diagnostic copy contains no source_ref that could redirect to live storage.
    copy = {key: quality[key] for key in ("schema_version", "columns", "sheets") if key in quality}
    write_json(output, "quality_input.json", copy)
    tasks, trajectories = load_quality_objects(output / "quality_input.json")
    selected = [item for item in trajectories if item.task_id == task_id]
    if task_id not in tasks or not selected:
        raise ValueError("质检输入缺少指定任务或轨迹")
    if any(not str(item.final_answer or "").strip() or
           any(not str(step.observation or "").strip() for step in item.steps) for item in selected):
        raise ValueError("质检输入缺少最终摘要或 Observation")

    configure_model_environment(DEFAULT_ENV_FILE)
    generator = load_example("generate-jiawen-rubrics")
    config = generator.load_config(RUBRICS / "examples" / "jiawen_rubric_config.json")
    config["validate_rubric"] = False
    options = {
        "model": str(generator._setting(config, "model", "ADARUBRIC_MODEL", "gpt-4o")),
        "base_url": generator._setting(config, "base_url", "ADARUBRIC_BASE_URL", None),
        "api_key": str(generator._setting(config, "api_key", "ADARUBRIC_API_KEY", None)
                       or os.environ.get("OPENAI_API_KEY") or "EMPTY"),
        "extra_body": generator._extra_body_setting(config),
        "http_options": resolve_http_options(),
    }
    score_context = {}
    if operation == "score":
        scoring, score_context = prepare_score(root, generator, config, tasks[task_id], selected,
                                              job, output, trajectory_id, step)
        options.update(scoring)
    elif operation == "rubric":
        dimensions = generator._int_setting(config, "num_dimensions", "ADARUBRIC_NUM_DIMENSIONS", default=5)
        options.update({
            "messages": generator.build_messages(tasks[task_id], selected, config=config, num_dimensions=dimensions),
            "temperature": generator._float_setting(config, "temperature", "ADARUBRIC_TEMPERATURE", default=0.0),
            "max_tokens": generator._int_setting(config, "max_tokens", "ADARUBRIC_MAX_TOKENS", default=4096),
        })
    else:
        raise ValueError("未知诊断操作")
    frozen = job.get("task_fingerprints", {}).get(task_id)
    current = quality_task_fingerprints(trees).get(task_id)
    from urllib.request import getproxies
    from importlib.metadata import version
    write_json(output, "probe_context.json", {
        "job_id": job["job_id"], "batch_id": batch, "task_id": task_id,
        "operation": "trajectory_score" if operation == "score" else "rubric_generate",
        "trajectory_count": len(selected), **score_context,
        "input_frozen_hash_available": bool(original_hash),
        "current_quality_fingerprint_matches_job": current == frozen if frozen else None,
        "note": "按当前服务器代码、配置和已验证输入重建一次请求；不是历史请求的逐字节回放，不执行 JSON 修复，不写业务结果。",
        "config_path": str(RUBRICS / "examples" / "jiawen_rubric_config.json"),
        "env_path": str(DEFAULT_ENV_FILE), "python": sys.executable, "python_version": sys.version,
        "openai_version": version("openai"), "httpx_version": version("httpx"),
        "detected_system_proxies": getproxies(),
    })
    write_json(output, "effective_options.json", options)
    return options


async def validate_score_response(response, context):
    """Run production parsing against recorded content, without another HTTP call."""
    from adarubric.llm.openai_client import OpenAIClient
    from adarubric.core.exceptions import LLMClientError
    from adarubric.core.models import DynamicRubric, Trajectory
    from adarubric.evaluator.trajectory_evaluator import LLMTrajectoryEvaluator, _EvaluationResponse
    content = response.choices[0].message.content
    if content is None:
        raise LLMClientError("LLM returned empty content")
    class RecordedClient:
        async def _chat(self, *args, **kwargs):
            return content
    parsed = await OpenAIClient.generate_structured(RecordedClient(), [], _EvaluationResponse)
    script = load_example("evaluate-jiawen-rubrics")
    evaluator = LLMTrajectoryEvaluator(None, aggregator=script._build_aggregator(context["config"]))
    trajectory = Trajectory.model_validate(context["trajectory"])
    rubric = DynamicRubric.model_validate(context["rubric"])
    targets = set(context["target_step_ids"]) if context["target_step_ids"] is not None else None
    evaluator._convert_raw(parsed, trajectory, rubric, target_step_ids=targets)
    expected = targets if targets is not None else {item.step_id for item in trajectory.steps}
    actual = {item.step_id for item in parsed.step_evaluations}
    return {"status": "passed", "finish_reason": response.choices[0].finish_reason,
            "expected_step_ids": sorted(expected), "returned_step_ids": sorted(actual),
            "missing_step_ids": sorted(expected - actual), "extra_step_ids": sorted(actual - expected)}


async def perform_request(options: dict, output: Path, transport=None, timeout_seconds: float = 300,
                          show_progress=False) -> dict:
    """Capture raw SDK HTTP data. No retries, redirects, JSON repair or cache writes."""
    import httpx
    from openai import AsyncOpenAI, DefaultAsyncHttpxClient
    from backend.file_io import io_path

    report = {"request_count": 0, "status_code": None, "outcome": "starting",
              "timeout_seconds": timeout_seconds, "redaction": False,
              "sdk_retries": 0, "follow_redirects": False}
    started = time.monotonic()
    async def waiting():
        while True:
            await asyncio.sleep(10)
            stage = "等待响应头" if report["status_code"] is None else "接收/解析响应"
            print(f"{stage}；已等待 {time.monotonic() - started:.0f} 秒，请求上限 {timeout_seconds:g} 秒", flush=True)
    heartbeat = asyncio.create_task(waiting()) if show_progress else None

    async def request_hook(request):
        if report["request_count"]:
            raise RuntimeError("诊断脚本禁止第二次 HTTP 请求")
        report["request_count"] += 1
        write_json(output, "request.json", {"method": request.method, "url": str(request.url),
                                              "headers": list(request.headers.multi_items())})
        io_path(output / "request_body.bin").write_bytes(request.content)
        io_path(output / "request_body.txt").write_bytes(request.content.decode("utf-8", errors="replace").encode("utf-8"))
        write_json(output, "report.json", report)

    async def response_hook(response):
        report["status_code"] = response.status_code
        if show_progress:
            print(f"收到 HTTP {response.status_code} 响应头，正在读取正文", flush=True)
        write_json(output, "response.json", {"status_code": response.status_code,
                                               "headers": list(response.headers.multi_items())})
        # Preserve even a partial decoded body when the peer stalls midway through it.
        pieces = []
        with io_path(output / "response_body.bin").open("wb") as stream:
            try:
                async for piece in response.aiter_bytes():
                    pieces.append(piece)
                    stream.write(piece)
                    stream.flush()
            finally:
                raw = b"".join(pieces)
                io_path(output / "response_body.txt").write_bytes(raw.decode("utf-8", errors="replace").encode("utf-8"))
        # Restore consumed content so the SDK sees exactly the captured body.
        response._content = raw

    network = dict(options["http_options"])
    if transport is not None:
        network["transport"] = transport
    http = DefaultAsyncHttpxClient(**network, timeout=httpx.Timeout(timeout_seconds, connect=10),
        follow_redirects=False, event_hooks={"request": [request_hook], "response": [response_hook]})
    client = None
    try:
        client = AsyncOpenAI(
            api_key=options["api_key"], base_url=options["base_url"], max_retries=0, http_client=http)
        kwargs = {key: options[key] for key in ("model", "messages", "temperature", "max_tokens")}
        if options.get("extra_body"):
            kwargs["extra_body"] = options["extra_body"]
        if options.get("response_format"):
            kwargs["response_format"] = options["response_format"]
        response = await asyncio.wait_for(client.chat.completions.create(**kwargs), timeout=timeout_seconds)
        if options.get("validation_context"):
            try:
                report["response_validation"] = await validate_score_response(response, options["validation_context"])
            except Exception as exc:
                report["response_validation"] = {"status": "failed", "exception_type": type(exc).__name__}
                write_json(output, "validation_error.json", {"exception": str(exc), "context": getattr(exc, "context", None)})
                raise
        report["outcome"] = "success"
    except Exception as exc:
        report.update(outcome="failed", exception_type=type(exc).__name__, exception=str(exc))
        io_path(output / "exception.txt").write_text(traceback.format_exc(), encoding="utf-8")
    finally:
        if heartbeat is not None:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
        report["elapsed_ms"] = round((time.monotonic() - started) * 1000)
        write_json(output, "report.json", report)
        try:
            await asyncio.wait_for(client.close() if client else http.aclose(), timeout=5)
        except Exception as exc:
            report["cleanup_error"] = str(exc)
            write_json(output, "report.json", report)
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path)
    parser.add_argument("--job-id", help="不指定时选最近的质检作业")
    parser.add_argument("--task-id", help="默认使用作业当前任务")
    parser.add_argument("--operation", choices=("rubric", "score"), default="rubric", help="生成Rubric或探测失败评分步骤")
    parser.add_argument("--trajectory-id", help="score模式默认使用failure.trajectory_id")
    parser.add_argument("--step", type=int, help="score模式默认使用failure.step，是评分step_id")
    parser.add_argument("--probe", action="store_true", help="发送一次所选模型请求并保存原始错误")
    args = parser.parse_args(argv)
    from backend.data_store import DATA_ROOT, RecordStore
    from backend.file_io import io_path, resolve_path
    root = resolve_path(args.data_root or DATA_ROOT)
    records = RecordStore(root)
    jobs = sorted(records.list("quality_jobs"), key=lambda item: str(item.get("created_at", "")), reverse=True)
    job = records.get("quality_jobs", args.job_id) if args.job_id else (jobs[0] if jobs else None)
    if not job:
        print("没有找到质检作业，数据目录:", root)
        return 1
    output = root / "logs" / "quality_diagnostics" / (datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8])
    io_path(output).mkdir(parents=True, exist_ok=False)
    write_json(output, "job.json", job)
    write_json(output, "recent_jobs.json", [{key: item.get(key) for key in (
        "job_id", "batch_id", "current_task", "stage", "status", "failure", "created_at")} for item in jobs[:5]])
    print("诊断目录:", output)
    print("质检作业:", job["job_id"], "状态:", job.get("status"), "任务:", job.get("current_task"))
    print("已保存错误:", json.dumps(job.get("failure"), ensure_ascii=False))
    if not args.probe:
        print("只导出了已有记录；历史未保存的错误正文无法还原。加 --probe 才会发出一次请求。")
        return 0
    try:
        options = prepare_request(root, records, job, args.task_id, output,
                                  operation=args.operation, trajectory_id=args.trajectory_id, step=args.step)
        raw_timeout = os.environ.get("ADARUBRIC_REQUEST_TIMEOUT_SECONDS", "300")
        timeout = float(raw_timeout)
        import math
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("ADARUBRIC_REQUEST_TIMEOUT_SECONDS 必须是有限正数")
        print(f"开始单次 {'轨迹评分' if args.operation == 'score' else 'Rubric生成'} 请求；不自动修复 JSON、不重试。", flush=True)
        # Last resort for an SDK that ignores cancellation: only this diagnostic
        # interpreter is stopped, after its request deadline and cleanup allowance.
        import threading
        def abort():
            io_path(output / "watchdog.txt").write_text("诊断超过请求期限与15秒清理宽限，已退出诊断进程。", encoding="utf-8")
            os._exit(124)
        watchdog = threading.Timer(timeout + 15, abort)
        watchdog.daemon = True
        watchdog.start()
        try:
            report = asyncio.run(perform_request(options, output, timeout_seconds=timeout, show_progress=True))
        finally:
            watchdog.cancel()
        print("HTTP状态:", report.get("status_code"), "请求次数:", report["request_count"])
        body_path = output / "response_body.txt"
        if io_path(body_path).is_file() and report["outcome"] != "success":
            print("服务端原始返回:\n" + io_path(body_path).read_text(encoding="utf-8"))
        elif report.get("exception"):
            print("原始异常:", report["exception"])
        print("完整记录已保存到:", output)
        return 0 if report["outcome"] == "success" else 2
    except Exception:
        detail = traceback.format_exc()
        io_path(output / "preparation_error.txt").write_text(detail, encoding="utf-8")
        print(detail)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
