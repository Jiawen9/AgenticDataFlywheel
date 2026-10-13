"""Validate the model response envelope without exposing provider content."""

from backend.pipeline_retry_errors import ModelOutputError


def response_text(response) -> str:
    choices = getattr(response, "choices", None)
    if not isinstance(choices, (list, tuple)) or not choices:
        raise ModelOutputError("empty_choices")
    choice = choices[0]
    if getattr(choice, "finish_reason", None) == "length":
        raise ModelOutputError("truncated")
    content = getattr(getattr(choice, "message", None), "content", None)
    if content is None:
        raise ModelOutputError("empty_content")
    if not isinstance(content, str):
        raise ModelOutputError("invalid_content")
    raw = content.strip()
    if not raw:
        raise ModelOutputError("empty_content")
    return raw
