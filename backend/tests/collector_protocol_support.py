"""Capabilities for current-collector transport doubles, not legacy fixtures."""


def current_capabilities(value):
    value["collection_artifacts_version"] = 1
    value["collection_artifacts_versions"] = [1, 2]
    declared = value.setdefault("agent_types", {"newagent": {
        "available": True, "collection": True, "capture_logprobs": True,
        "model_iteration_evaluation": value.get("model_iteration_evaluation") == 1,
        "offline_evaluation": value.get("offline_evaluation") == 1}})
    for capability in declared.values():
        capability.update(collection_artifacts_version=1, collection_artifacts_versions=[1, 2], repeat_times=True, max_repeat_times=100)
    return value
