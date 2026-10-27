"""Collection engine identities, distinct from platform Pipeline orchestration."""
AGENT_TYPES = ("newagent", "pipelineagent")
PROVENANCE_FIELDS = ("agent_type", "agent_version", "trajectory_format", "normalizer_version", "runtime_digest", "configuration_digest")


def normalize_agent_type(value="newagent"):
    if not isinstance(value, str) or value not in AGENT_TYPES:
        raise ValueError("agent_type 必须为 newagent 或 pipelineagent")
    return value


def engine_capabilities(remote):
    """Legacy collectors advertise only the original engine implicitly."""
    declared = remote.get("agent_types") if isinstance(remote, dict) else None
    if isinstance(declared, dict):
        return {name: {"available": False, "collection": False, "capture_logprobs": False,
                       "model_iteration_evaluation": False, "offline_evaluation": False,
                       "reason": "采集电脑未声明此引擎", **(declared.get(name) or {})}
                for name in AGENT_TYPES if isinstance(declared.get(name, {}), dict)}
    return {
        "newagent": {"available": True, "collection": True, "capture_logprobs": True,
                     "model_iteration_evaluation": remote.get("model_iteration_evaluation") == 1,
                     "offline_evaluation": remote.get("offline_evaluation") == 1,
                     "reason": "现有采集服务"},
        "pipelineagent": {"available": False, "collection": False, "capture_logprobs": False,
                          "model_iteration_evaluation": False, "offline_evaluation": False,
                          "reason": "请先升级采集电脑并配置 PipelineAgent"},
    }
