"""Tests for the Jiawen sequential experiment runner."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = PROJECT_ROOT / "examples" / "run-jiawen-experiments.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("run_jiawen_experiments", SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["run_jiawen_experiments"] = module
    spec.loader.exec_module(module)
    return module


def test_allocate_experiment_dirs_skips_existing_indices(tmp_path):
    module = _load_module()
    (tmp_path / "2026081801").mkdir()
    (tmp_path / "2026081803").mkdir()
    (tmp_path / "other").mkdir()

    dirs = module.allocate_experiment_dirs(tmp_path, date_prefix="20260818", count=3)

    assert [path.name for path in dirs] == [
        "2026081802",
        "2026081804",
        "2026081805",
    ]


def test_load_experiment_names_from_plan_file(tmp_path):
    module = _load_module()
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(
        json.dumps(
            {
                "experiments": [
                    "eval-stepwise-no-thinking",
                    "eval-stepwise-thinking",
                    "eval-stepwise-observation-no-thinking",
                    "eval-stepwise-observation-thinking",
                ]
            }
        ),
        encoding="utf-8",
    )

    assert module.load_experiment_names(plan_path=plan_path, override=None) == [
        "eval-stepwise-no-thinking",
        "eval-stepwise-thinking",
        "eval-stepwise-observation-no-thinking",
        "eval-stepwise-observation-thinking",
    ]


def test_load_experiment_names_override_skips_plan(tmp_path):
    module = _load_module()
    missing_plan = tmp_path / "missing.json"

    assert module.load_experiment_names(
        plan_path=missing_plan,
        override="eval-stepwise-no-thinking,eval-stepwise-thinking",
    ) == ["eval-stepwise-no-thinking", "eval-stepwise-thinking"]


def test_build_stepwise_no_thinking_config_points_outputs_to_experiment_dir(tmp_path):
    module = _load_module()
    spec = module.EXPERIMENTS["eval-stepwise-no-thinking"]
    config = module.build_experiment_config(
        {
            "evaluation_resume_from_jsonl": True,
            "model": "qwen",
            "extra_body": {"chat_template_kwargs": {"enable_thinking": True}},
        },
        spec,
        PROJECT_ROOT / "docs" / "experiments" / "2026081801",
    )

    assert config["model"] == "qwen"
    assert config["evaluation_resume_from_jsonl"] is False
    assert config["evaluation_include_action"] is True
    assert config["evaluation_include_action_input"] is True
    assert config["evaluation_chunk_enabled"] is True
    assert config["evaluation_step_lookahead"] == 1
    assert config["extra_body"] == {"chat_template_kwargs": {"enable_thinking": False}}
    assert config["evaluation_report_path"] == (
        "docs/experiments/2026081801/jiawen_gui_eval_stepwise_no_thinking.md"
    )
    assert config["evaluation_jsonl_path"] == (
        "docs/experiments/2026081801/jiawen_gui_eval_stepwise_no_thinking.jsonl"
    )


def test_build_stepwise_thinking_config_enables_qwen_thinking():
    module = _load_module()
    spec = module.EXPERIMENTS["eval-stepwise-thinking"]
    config = module.build_experiment_config(
        {
            "evaluation_resume_from_jsonl": True,
            "extra_body": {"chat_template_kwargs": {"enable_thinking": False}},
        },
        spec,
        PROJECT_ROOT / "docs" / "experiments" / "2026081802",
    )

    assert config["evaluation_resume_from_jsonl"] is False
    assert config["evaluation_include_action"] is True
    assert config["evaluation_include_action_input"] is True
    assert config["evaluation_chunk_enabled"] is True
    assert config["evaluation_step_lookahead"] == 1
    assert config["extra_body"] == {"chat_template_kwargs": {"enable_thinking": True}}
    assert config["evaluation_report_path"] == (
        "docs/experiments/2026081802/jiawen_gui_eval_stepwise_thinking.md"
    )
    assert config["evaluation_jsonl_path"] == (
        "docs/experiments/2026081802/jiawen_gui_eval_stepwise_thinking.jsonl"
    )


def test_build_stepwise_observation_no_thinking_config_hides_action_fields():
    module = _load_module()
    spec = module.EXPERIMENTS["eval-stepwise-observation-no-thinking"]
    config = module.build_experiment_config(
        {"evaluation_include_action": True, "evaluation_include_action_input": True},
        spec,
        PROJECT_ROOT / "docs" / "experiments" / "2026081803",
    )

    assert config["evaluation_include_action"] is False
    assert config["evaluation_include_action_input"] is False
    assert config["evaluation_chunk_enabled"] is True
    assert config["evaluation_step_lookahead"] == 1
    assert config["extra_body"] == {"chat_template_kwargs": {"enable_thinking": False}}
    assert config["evaluation_report_path"] == (
        "docs/experiments/2026081803/jiawen_gui_eval_stepwise_observation_no_thinking.md"
    )


def test_build_stepwise_observation_thinking_config_hides_action_fields():
    module = _load_module()
    spec = module.EXPERIMENTS["eval-stepwise-observation-thinking"]
    config = module.build_experiment_config(
        {"extra_body": {"chat_template_kwargs": {"enable_thinking": False}}},
        spec,
        PROJECT_ROOT / "docs" / "experiments" / "2026081804",
    )

    assert config["evaluation_include_action"] is False
    assert config["evaluation_include_action_input"] is False
    assert config["evaluation_chunk_enabled"] is True
    assert config["evaluation_step_lookahead"] == 1
    assert config["extra_body"] == {"chat_template_kwargs": {"enable_thinking": True}}
    assert config["evaluation_jsonl_path"] == (
        "docs/experiments/2026081804/jiawen_gui_eval_stepwise_observation_thinking.jsonl"
    )


def test_build_experiment_record_summarizes_purpose_and_variables():
    module = _load_module()
    spec = module.EXPERIMENTS["eval-stepwise-observation-no-thinking"]
    config = module.build_experiment_config(
        {"evaluation_max_concurrent": 2, "evaluation_max_tokens": 8042},
        spec,
        PROJECT_ROOT / "docs" / "experiments" / "2026081803",
    )
    manifest = {
        "generated_config": "docs/experiments/2026081803/experiment_config.json",
        "log_path": "docs/experiments/2026081803/run.log",
        "status": "completed",
        "return_code": 0,
    }

    record = module.build_experiment_record(
        experiment_id="2026081803",
        spec=spec,
        config=config,
        manifest=manifest,
    )

    assert "# Experiment 2026081803: eval-stepwise-observation-no-thinking" in record
    assert "Measure whether observation-only evidence is sufficient" in record
    assert "- input_evidence: observation only" in record
    assert "- thinking: disabled" in record
    assert "- status: completed" in record
    assert "- return_code: 0" in record
