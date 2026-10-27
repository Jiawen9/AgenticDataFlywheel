"""Independent evaluator settings and run-bound result snapshots."""
from __future__ import annotations

import json
import re
from urllib.parse import urlsplit, urlunsplit

from .data_store.registry import utc_now
from .phone_factory import PhoneFactoryError
from .agent_types import PROVENANCE_FIELDS

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
        return {"ok": True, "supported": False, "offline_evaluation": False, "offline_supported": False,
                "evaluation_profiles": False, "profiles_supported": False, "checklist_policies": [],
                "message": f"评测服务能力检查失败：{exc}"}
    supported = (isinstance(remote, dict) and remote.get("protocol_version") == 1
                 and remote.get("batch_results") is True
                 and type(remote.get("model_iteration_evaluation")) is int
                 and remote["model_iteration_evaluation"] == 1)
    profiles = supported and type(remote.get("evaluation_profiles")) is int and remote["evaluation_profiles"] == 1
    offline = profiles and type(remote.get("offline_evaluation")) is int and remote["offline_evaluation"] == 1
    policies = remote.get("checklist_policies", ["llm"]) if supported else []
    policies = [item for item in policies if item in {"llm", "auto"}] if isinstance(policies, list) else []
    return {"ok": True, "supported": supported, "offline_evaluation": offline, "offline_supported": offline,
            "evaluation_profiles": profiles, "profiles_supported": profiles, "checklist_policies": policies,
            "message": "支持按用例异步评测" if supported else "采集服务尚未升级模型迭代评测，请先部署新版 9011 服务"}


def evaluation_options(store, data, *, offline=False):
    """Keep omitted legacy requests on llm; profile selection opts new jobs into auto."""
    profile_id = data.get("evaluation_profile_id", data.get("profile_id", ""))
    profile_digest = data.get("evaluation_profile_digest", data.get("profile_digest", ""))
    policy = data.get("checklist_source", "auto" if offline or profile_id else "llm")
    if (data.get("evaluation_mode", "checklist") != "checklist" or not isinstance(policy, str)
            or policy not in {"llm", "auto"}):
        raise PhoneFactoryError("评测只支持 checklist 模式与 llm/auto 清单策略")
    if offline and policy != "auto":
        raise PhoneFactoryError("独立评测必须选择规则版本并使用 auto 清单策略")
    if policy == "auto":
        if (not isinstance(profile_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", profile_id)
                or not isinstance(profile_digest, str) or not re.fullmatch(r"[0-9a-f]{64}", profile_digest)):
            raise PhoneFactoryError("请选择有效的评测规则版本及其摘要")
    elif profile_id or profile_digest:
        raise PhoneFactoryError("规则版本仅用于 auto 清单策略")
    options = {"evaluator_config": normalize_config(data.get("evaluator_config", get_config(store)), required=True),
               "checklist_source": policy, "evaluation_mode": "checklist"}
    if policy == "auto":
        options.update(evaluation_profile_id=profile_id, evaluation_profile_digest=profile_digest)
    return options


def require_capability(store, *, offline=False, profiles=False):
    value = capabilities(store)
    if not value["supported"]:
        raise PhoneFactoryError(value["message"], 409)
    if (profiles or offline) and (not value["evaluation_profiles"] or "auto" not in value["checklist_policies"]):
        raise PhoneFactoryError("采集服务尚未升级规则版本与自动匹配清单能力，请先部署新版 9011 服务", 409)
    if offline and not value["offline_evaluation"]:
        raise PhoneFactoryError("采集服务尚未升级独立评测能力，请先部署新版 9011 服务", 409)
    return value


def get_profiles(store):
    require_capability(store, profiles=True)
    value = store._remote_result(["evaluation-profiles"])
    if not isinstance(value, dict) or not isinstance(value.get("profiles"), list) or not isinstance(value.get("errors", []), list):
        raise PhoneFactoryError("远端规则版本列表格式无效", 502)
    seen = set()
    for row in value["profiles"]:
        if (not isinstance(row, dict) or not isinstance(row.get("id"), str)
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", row["id"])
                or row["id"] in seen or not isinstance(row.get("name"), str)
                or not isinstance(row.get("digest"), str) or not re.fullmatch(r"[0-9a-f]{64}", row["digest"])):
            raise PhoneFactoryError("远端规则版本身份或摘要无效", 502)
        seen.add(row["id"])
    return value


def verify_profile(store, options):
    if options["checklist_source"] != "auto":
        return
    profile = next((row for row in get_profiles(store)["profiles"] if row["id"] == options["evaluation_profile_id"]), None)
    if profile is None or profile["digest"] != options["evaluation_profile_digest"]:
        raise PhoneFactoryError("评测规则版本不存在或已变化，请刷新版本列表后重新选择", 409)


def get_sources(store):
    require_capability(store, offline=True)
    value = store._remote_result(["evaluation-sources"])
    if (not isinstance(value, dict) or not isinstance(value.get("sources"), list)
            or not isinstance(value.get("allowed_roots"), list)
            or any(not isinstance(path, str) for path in value["allowed_roots"])):
        raise PhoneFactoryError("远端评测来源列表格式无效", 502)
    seen = set()
    for row in value["sources"]:
        if (not isinstance(row, dict) or not isinstance(row.get("run_id"), str) or not row["run_id"]
                or row["run_id"] in seen or row.get("run_mode") not in {"generate", "modeliter"}
                or not isinstance(row.get("status"), str) or not isinstance(row.get("created_at"), str)
                or type(row.get("trajectory_count")) is not int or row["trajectory_count"] < 0):
            raise PhoneFactoryError("远端评测来源身份或计数无效", 502)
        seen.add(row["run_id"])
    return value


def preview_source(store, data):
    from .collection_runs import _identifier
    require_capability(store, offline=True)
    if set(data) - {"source_run_id", "source_path"}:
        raise PhoneFactoryError("预览只接受已有运行编号或服务器轨迹目录")
    source = {}
    for key in ("source_run_id", "source_path"):
        raw = data.get(key, "")
        if not isinstance(raw, str) or any(ord(c) < 32 for c in raw):
            raise PhoneFactoryError("评测来源必须是有效文本")
        if raw.strip():
            source[key] = raw.strip()
    if len(source) != 1:
        raise PhoneFactoryError("请选择一个已有采集运行，或填写一个服务器轨迹目录")
    value = store._remote_result(["evaluation-preview", "--payload", json.dumps(source, ensure_ascii=False)])
    if (not isinstance(value, dict) or type(value.get("valid")) is not bool
            or not isinstance(value.get("errors"), list) or not isinstance(value.get("cases"), list)
            or type(value.get("trajectory_count")) is not int or value["trajectory_count"] < 0):
        raise PhoneFactoryError("远端轨迹预览格式无效", 502)
    if not value["valid"]:
        return value
    try:
        preview_id = _identifier(value.get("preview_id"), "预览编号")
    except Exception as exc:
        raise PhoneFactoryError("远端预览编号无效", 502) from exc
    if (not isinstance(value.get("source"), dict) or not isinstance(value.get("source_digest"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", value["source_digest"])
            or value["trajectory_count"] != len(value["cases"]) or not value["cases"] or value["errors"]):
        raise PhoneFactoryError("远端轨迹预览身份或数量无效", 502)
    seen = set()
    for row in value["cases"]:
        if (not isinstance(row, dict) or any(not isinstance(row.get(key), str) or not row[key].strip()
                for key in ("source_trajectory_id", "case_id", "task"))
                or row["source_trajectory_id"] in seen or not isinstance(row.get("phone_id", ""), str)):
            raise PhoneFactoryError("远端轨迹预览存在缺失或重复的轨迹身份", 502)
        seen.add(row["source_trajectory_id"])
    # A server preview is an immutable manifest. Keep it locally so later result
    # rows are checked against source identities, not against selected phones.
    existing = store.records.get("model_iter_previews", preview_id)
    record = {"preview_id": preview_id, "request": source, "preview": value, "created_at": utc_now()}
    if existing and existing["preview"] != value:
        raise PhoneFactoryError("远端复用了不同内容的预览编号", 502)
    if not existing:
        store.records.put("model_iter_previews", preview_id, record, expected_revision=0)
    return value


def validate_results(value, run_id, expected_phones, *, expected_sources=None, metadata=None):
    if (not isinstance(value, dict) or value.get("run_id") != run_id
            or value.get("run_mode") != "modeliter" or not isinstance(value.get("cases"), list)
            or not isinstance(value.get("summary"), dict)):
        raise PhoneFactoryError("远端评测结果身份或格式不匹配", 502)
    if (metadata or {}).get("agent_type") and value.get("agent_type", metadata["agent_type"]) != metadata["agent_type"]:
        raise PhoneFactoryError("远端评测结果的采集引擎不匹配", 502)
    summary = value["summary"]
    if any(type(summary.get(key)) is not int or summary[key] < 0 for key in COUNTS):
        raise PhoneFactoryError("远端评测计数无效", 502)
    cases, seen = [], set()
    for row in value["cases"]:
        if (not isinstance(row, dict) or not isinstance(row.get("case_key"), str)
                or not row["case_key"] or row["case_key"] in seen
                or (expected_sources is None and row.get("phone_id") not in expected_phones)
                or not isinstance(row.get("evaluation_status"), str) or row["evaluation_status"] not in CASE_STATUSES
                or not isinstance(row.get("collection_status"), str) or row["collection_status"] not in {"pending", "running", "completed", "failed"}
                or not isinstance(row.get("case_id"), str)
                or row.get("verdict") not in (None, "", "success", "partial_success", "fail", "unchecked")):
            raise PhoneFactoryError("远端用例结果身份或状态无效", 502)
        if expected_sources is not None:
            source_id = row.get("source_trajectory_id")
            expected = expected_sources.get(source_id) if isinstance(source_id, str) else None
            if (not expected or row.get("case_id") != expected["case_id"]
                    or row.get("phone_id", "") != expected.get("phone_id", "")):
                raise PhoneFactoryError("远端独立评测结果与输入轨迹身份不匹配", 502)
            if any(case.get("source_trajectory_id") == source_id for case in cases):
                raise PhoneFactoryError("远端独立评测重复返回同一条输入轨迹", 502)
        if row["evaluation_status"] == "completed" and (
                row["collection_status"] != "completed" or row.get("verdict") not in VERDICTS):
            raise PhoneFactoryError("已评测用例必须采集完成并包含有效结论", 502)
        if row["evaluation_status"] != "completed" and row.get("verdict") in VERDICTS:
            raise PhoneFactoryError("未完成评测的用例不能包含最终结论", 502)
        seen.add(row["case_key"])
        cases.append(row)
    if summary["total"] != len(cases):
        raise PhoneFactoryError("远端评测总数与用例明细不一致", 502)
    if expected_sources is not None and len(cases) != len(expected_sources):
        raise PhoneFactoryError("远端独立评测用例总数与输入快照不一致", 502)
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
    return {**{key: value[key] for key in PROVENANCE_FIELDS if key in value}, "run_id": run_id, "run_mode": "modeliter", "summary": summary, "cases": cases, **(metadata or {})}


def capture_results(runtime, run, *, final=False, remote_status=None):
    record = runtime.records.get("model_iter_results", run["run_id"])
    if record and record.get("final"):
        return record["result"]
    request = run.get("request", {})
    expected_sources = ({row["source_trajectory_id"]: row for row in request["source_cases"]}
                        if request.get("execution_mode") == "evaluate_only" else None)
    metadata = {key: request[key] for key in ("execution_mode", "source", "source_digest", "evaluation_profile_id",
                                             "evaluation_profile_digest", "checklist_source", "agent_type") if key in request}
    value = validate_results(runtime.store._remote_result(["run-evaluations", run["run_id"]]),
                             run["run_id"], request.get("phone_ids", []),
                             expected_sources=expected_sources, metadata=metadata)
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
