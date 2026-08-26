"""Tests for Jiawen rubric-generation evidence preparation."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from adarubric import Trajectory, TrajectoryStep

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = PROJECT_ROOT / "examples" / "generate-jiawen-rubrics.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("generate_jiawen_rubrics", SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["generate_jiawen_rubrics"] = module
    spec.loader.exec_module(module)
    return module


def test_all_text_excludes_thought_but_keeps_observable_evidence():
    module = _load_module()
    trajectory = Trajectory(
        trajectory_id="traj-1",
        task_id="task-1",
        final_answer="final answer evidence",
        steps=[
            TrajectoryStep(
                step_id=1,
                thought="private chain of thought",
                action="tap",
                action_input={"target": "history"},
                observation="observable screenshot difference",
            )
        ],
    )

    text = module._all_text(trajectory)

    assert "private chain of thought" not in text
    assert "final answer evidence" in text
    assert "tap" in text
    assert "observable screenshot difference" in text
