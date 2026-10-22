"""Independent evaluator settings and run-bound result snapshots."""
from __future__ import annotations

import re
from urllib.parse import urlsplit, urlunsplit

from .data_store.registry import utc_now
from .phone_factory import PhoneFactoryError

ROLES = ("text", "vision")
COUNTS = ("total", "collection_completed", "collection_failed", "evaluation_completed",
          "evaluation_failed", "evaluation_running", "evaluation_pending", "success", "partial_success", "fail")
CASE_STATUSES = {"pending", "running", "retry_wait", "completed", "needs_review", "collection_failed"}
TERMINAL_CASE_STATUSES = {"completed", "needs_review", "collection_failed"}
VERDICTS = {"success", "partial_success", "fail"}


def default_config():
    return {role: {"base_url": "", "model": "", "api_key_env": f"PHONE_FACTORY_EVALUATOR_{role.upper()}_API_KEY"}
            for role in ROLES}


def normalize_config(value, *, required=False):
    if not isinstance(value, dict) or set(value) != set(ROLES):
        raise PhoneFactoryError("评测配置必须包含 text 和 vision")
    result = {}
    for role in ROLES:
        item = value[role]
        if not isinstance(item, dict) or set(item) != {"base_url", "model", "api_key_env"}:
            raise PhoneFactoryError(f"{role} 评测配置只接受 base_url、model、api_key_env；不要传入密钥")
        if any(not isinstance(v, str) for v in item.values()):
            raise PhoneFactoryError(f"{role} 评测配置必须是文本")
        item = {k: v.strip() for k, v in item.items()}
        if required and (not item["base_url"] or not item["model"]):
            raise PhoneFactoryError("请先保存文本和视觉评测模型的地址与模型名")
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", item["api_key_env"]):
            raise PhoneFactoryError("api_key_env 必须是采集服务器的环境变量名，不能填写密钥值")
        if any(ord(char) < 32 for char in item["model"]):
            raise PhoneFactoryError("评测模型名不能包含控制字符")
        address = item["base_url"]
        if address:
            try:
                parsed = urlsplit(address if "://" in address else "http://" + address)
                parsed.port
                if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                        or parsed.username or parsed.password or parsed.query or parsed.fragment
                        or "\\" in address or any(c.isspace() or ord(c) < 32 for c in address)):
                    raise ValueError()
            except ValueError as exc:
                raise PhoneFactoryError("评测模型地址必须是有效 HTTP(S) API 基地址，不包含密钥、查询参数或片段") from exc
            item["base_url"] = urlunsplit((parsed.scheme, parsed.netloc, (parsed.path.rstrip("/") or "/v1") + "/", "", ""))
        result[role] = item
    return result


def get_config(store):
    record = store.records.get("model_iter_settings", "evaluator")
    return normalize_config(record["config"] if record else default_config())


def save_config(store, value):
    config = normalize_config(value)
    store.records.update("model_iter_settings", "evaluator", lambda item: item.update(config=config), default={})
    return config


def capabilities(store):
    try:
        remote = store._remote_result(["capabilities"])
    except PhoneFactoryError as exc:
        return {"ok": True, "supported": False, "message": f"评测服务能力检查失败：{exc}"}
    supported = (isinstance(remote, dict) and remote.get("protocol_version") == 1
                 and remote.get("batch_results") is True
                 and type(remote.get("model_iteration_evaluation")) is int
                 and remote["model_iteration_evaluation"] == 1)
    return {"ok": True, "supported": supported,
            "message": "支持按用例异步评测" if supported else "采集服务尚未升级模型迭代评测，请先部署新版 9011 服务"}


def validate_results(value, run_id, expected_phones):
    if (not isinstance(value, dict) or value.get("run_id") != run_id
            or value.get("run_mode") != "modeliter" or not isinstance(value.get("cases"), list)
            or not isinstance(value.get("summary"), dict)):
        raise PhoneFactoryError("远端评测结果身份或格式不匹配", 502)
    summary = value["summary"]
    if any(type(summary.get(key)) is not int or summary[key] < 0 for key in COUNTS):
        raise PhoneFactoryError("远端评测计数无效", 502)
    cases, seen = [], set()
    for row in value["cases"]:
        if (not isinstance(row, dict) or not isinstance(row.get("case_key"), str)
                or not row["case_key"] or row["case_key"] in seen
                or row.get("phone_id") not in expected_phones
                or row.get("evaluation_status") not in CASE_STATUSES
                or row.get("collection_status") not in {"pending", "running", "completed", "failed"}
                or not isinstance(row.get("case_id"), str)
                or row.get("verdict") not in {None, "", "success", "partial_success", "fail", "unchecked"}):
            raise PhoneFactoryError("远端用例结果身份或状态无效", 502)
        if row["evaluation_status"] == "completed" and (
                row["collection_status"] != "completed" or row.get("verdict") not in VERDICTS):
            raise PhoneFactoryError("已评测用例必须采集完成并包含有效结论", 502)
        if row["evaluation_status"] != "completed" and row.get("verdict") in VERDICTS:
            raise PhoneFactoryError("未完成评测的用例不能包含最终结论", 502)
        seen.add(row["case_key"])
        cases.append(row)
    if summary["total"] != len(cases):
        raise PhoneFactoryError("远端评测总数与用例明细不一致", 502)
    # Derive every count from validated identities, never trust a stale aggregate.
    summary = {key: 0 for key in COUNTS}
    summary["total"] = len(cases)
    for row in cases:
        summary["collection_completed"] += row["collection_status"] == "completed"
        summary["collection_failed"] += row["collection_status"] == "failed"
        status = row["evaluation_status"]
        if status == "completed":
            summary["evaluation_completed"] += 1
            summary[row["verdict"]] += 1
        elif status in {"needs_review", "collection_failed"}:
            summary["evaluation_failed"] += 1
        elif status == "running":
            summary["evaluation_running"] += 1
        else:
            summary["evaluation_pending"] += 1
    denominator = sum(summary[key] for key in VERDICTS)
    summary["success_rate"] = summary["success"] / denominator if denominator else None
    return {"run_id": run_id, "run_mode": "modeliter", "summary": summary, "cases": cases}


def capture_results(runtime, run, *, final=False, remote_status=None):
    record = runtime.records.get("model_iter_results", run["run_id"])
    if record and record.get("final"):
        return record["result"]
    value = validate_results(runtime.store._remote_result(["run-evaluations", run["run_id"]]),
                             run["run_id"], run.get("request", {}).get("phone_ids", []))
    if final and any(row["evaluation_status"] not in TERMINAL_CASE_STATUSES
                     or row["collection_status"] not in {"completed", "failed"} for row in value["cases"]):
        raise PhoneFactoryError("远端运行已结束但仍有未完成用例，暂不冻结评测结果", 502)
    if remote_status == "succeeded" and (not value["cases"] or any(
            row["evaluation_status"] != "completed" for row in value["cases"])):
        raise PhoneFactoryError("远端成功状态与用例评测结果不一致", 502)
    def update(current):
        if not current.get("final"):
            current.update(result=value, final=final, updated_at=utc_now())
    return runtime.records.update("model_iter_results", run["run_id"], update, default={})["result"]
