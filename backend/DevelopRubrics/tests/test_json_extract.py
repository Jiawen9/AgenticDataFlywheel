"""Tests for JSON substring extraction from LLM outputs."""

from __future__ import annotations

from adarubric.llm.json_extract import (
    extract_json_candidates,
    extract_json_substring,
    strip_thinking,
)


def test_fenced_json_block():
    raw = 'Here you go:\n```json\n{"a": 1}\n```\nThanks'
    assert extract_json_substring(raw) == '{"a": 1}'


def test_plain_object():
    assert extract_json_substring('prefix {"x": true} suffix') == '{"x": true}'


def test_nested_braces():
    raw = '{"outer": {"inner": 2}}'
    assert extract_json_substring(raw) == raw


def test_strip_thinking_block():
    raw = '<think>{"scratch": true}</think>\n{"final": true}'
    assert strip_thinking(raw) == '{"final": true}'
    assert extract_json_substring(raw) == '{"final": true}'


def test_strip_thinking_block_case_insensitive():
    raw = '<THINK>private reasoning</THINK>\n[{"rank": 1}]'
    assert extract_json_substring(raw) == '[{"rank": 1}]'


def test_strip_orphan_thinking_end_tag_keeps_final_answer_only():
    raw = 'scratch {"wrong": true} </think>\n{"final": true}'
    assert strip_thinking(raw) == '{"final": true}'
    assert extract_json_substring(raw) == '{"final": true}'


def test_extract_json_candidates_preserves_coordinate_and_response_order():
    raw = 'Clicked at [384, 483].\n{"trajectory_id": "traj-1", "task_id": "task-1"}'
    assert extract_json_candidates(raw)[:2] == [
        "[384, 483]",
        '{"trajectory_id": "traj-1", "task_id": "task-1"}',
    ]
