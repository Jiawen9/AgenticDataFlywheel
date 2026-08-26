"""Tests for OpenAI-compatible client configuration helpers."""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from adarubric.core.exceptions import LLMClientError
from adarubric.llm.openai_client import OpenAIClient, _extra_body_from_env, parse_extra_body


class _TinyResponse(BaseModel):
    trajectory_id: str
    task_id: str


class _FakeOpenAIClient(OpenAIClient):
    def __init__(self, raw: str) -> None:
        self.model = "fake"
        self._max_retries = 1
        self._extra_body = None
        self.raw = raw

    async def _chat(self, *_args, **_kwargs) -> str:
        return self.raw


def test_extra_body_from_env_json_object():
    assert _extra_body_from_env('{"chat_template_kwargs": {"enable_thinking": true}}') == {
        "chat_template_kwargs": {"enable_thinking": True}
    }


def test_parse_extra_body_accepts_config_object():
    value = {"chat_template_kwargs": {"enable_thinking": True}}
    assert parse_extra_body(value) == value


def test_extra_body_from_env_rejects_non_object():
    with pytest.raises(ValueError, match="must be a JSON object"):
        _extra_body_from_env("[1, 2, 3]")


async def test_generate_structured_skips_non_schema_json_candidates():
    client = _FakeOpenAIClient(
        'Clicked coordinate [384, 483]\n{"trajectory_id": "traj-1", "task_id": "task-1"}'
    )

    response = await client.generate_structured([], _TinyResponse)

    assert response == _TinyResponse(trajectory_id="traj-1", task_id="task-1")


async def test_generate_structured_does_not_report_coordinate_array_as_primary_error():
    client = _FakeOpenAIClient("Clicked coordinate [384, 483]\ntruncated final JSON")

    with pytest.raises(LLMClientError) as exc_info:
        await client.generate_structured([], _TinyResponse)

    assert "no JSON object candidate matched schema" in str(exc_info.value)
    assert exc_info.value.context["candidate_count"] == 0
