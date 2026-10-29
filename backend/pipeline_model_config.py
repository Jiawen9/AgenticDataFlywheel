"""Public PipelineAgent model overrides, independent of sampling and evaluator settings."""
from __future__ import annotations

import re
from urllib.parse import urlsplit, urlunsplit

MODEL_OVERRIDE_FIELDS = ("execution_model", "planner_config")


def default_models():
    return {"vla": "", "execution_model": "", "planner_config": None}


def model_name(value, label):
    if not isinstance(value, str) or any(ord(char) < 32 for char in value):
        raise ValueError(f"{label} 必须是不含控制字符的文本")
    return value.strip()


def model_url(value, label):
    address = model_name(value, label)
    if not address:
        return ""
    try:
        parsed = urlsplit(address if "://" in address else "http://" + address)
        parsed.port
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.query or parsed.fragment or "\\" in address
                or any(char.isspace() for char in address)):
            raise ValueError()
    except ValueError as exc:
        raise ValueError(f"{label} 必须是有效 HTTP(S) 地址，不能包含凭据、查询参数或片段") from exc
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/") or "/v1", "", ""))


def planner_config(value):
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) - {"base_url", "model", "api_key_env"}:
        raise ValueError("规划配置只接受 base_url、model 和 api_key_env，禁止传入密钥原文")
    address = model_url(value.get("base_url", ""), "规划模型地址")
    name = model_name(value.get("model", ""), "规划模型名")
    variable = model_name(value.get("api_key_env", ""), "api_key_env")
    if not address or not name:
        raise ValueError("规划模型地址与模型名必须同时填写；沿用采集端配置时请清空规划配置")
    result = {"base_url": address, "model": name}
    if variable:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", variable):
            raise ValueError("api_key_env 必须是采集服务器的环境变量名，不能填写密钥值")
        result["api_key_env"] = variable
    return result


def normalize_defaults(value):
    if not isinstance(value, dict) or set(value) - set(default_models()):
        raise ValueError("模型默认配置只接受 vla、execution_model 和 planner_config")
    return {"vla": model_url(value.get("vla", ""), "执行模型地址"),
            "execution_model": model_name(value.get("execution_model", ""), "执行模型名"),
            "planner_config": planner_config(value.get("planner_config"))}


def normalize_overrides(data, agent_type):
    """Missing/empty overrides preserve old request identities; never load defaults here."""
    result = {}
    execution = model_name(data.get("execution_model", ""), "执行模型名")
    planner = planner_config(data.get("planner_config"))
    if execution:
        result["execution_model"] = execution
    if planner is not None:
        result["planner_config"] = planner
    if result and agent_type != "pipelineagent":
        raise ValueError("执行模型名和规划模型覆盖配置仅支持 PipelineAgent")
    return result
