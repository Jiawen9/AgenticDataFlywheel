"""Frozen collection options; saved defaults never alter an accepted request."""

COLLECTION_ARTIFACTS_VERSION = 1
MAX_REPEAT_TIMES = 100


def repeat_times(value=1, *, mode="generate"):
    if type(value) is not int or not 1 <= value <= MAX_REPEAT_TIMES:
        raise ValueError("repeat_times 必须为 1 到 100 的整数")
    if mode != "generate" and value != 1:
        raise ValueError("模型迭代评估每条任务只采集一条轨迹，repeat_times 必须为 1")
    return value
