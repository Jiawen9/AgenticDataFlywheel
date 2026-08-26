"""Tests for Jiawen evaluation script helpers."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from adarubric import Trajectory, TrajectoryStep

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = PROJECT_ROOT / "examples" / "evaluate-jiawen-rubrics.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("evaluate_jiawen_rubrics", SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["evaluate_jiawen_rubrics"] = module
    spec.loader.exec_module(module)
    return module


def test_trajectory_for_evaluation_can_hide_action_and_action_input():
    module = _load_module()
    trajectory = Trajectory(
        trajectory_id="traj-1",
        task_id="task-1",
        steps=[
            TrajectoryStep(
                step_id=1,
                action='{"action":"click","coordinate":[384,483]}',
                action_input={"history": "Empty"},
                observation="The app opened.",
            )
        ],
    )

    prepared = module._trajectory_for_evaluation(
        trajectory,
        include_action=False,
        include_action_input=False,
    )

    assert prepared.steps[0].action == "Action hidden for this ablation."
    assert prepared.steps[0].action_input == {}
    assert prepared.steps[0].observation == "The app opened."
    assert "[384,483]" not in prepared.steps[0].action
    assert prepared.metadata["evaluation_include_action"] is False
    assert prepared.metadata["evaluation_include_action_input"] is False


def test_trajectory_chunks_use_history_plus_lookahead_context():
    module = _load_module()
    trajectory = Trajectory(
        trajectory_id="traj-1",
        task_id="task-1",
        steps=[
            TrajectoryStep(
                step_id=step_id,
                action="click",
                action_input={"summary": f"summary {step_id}", "screenshot": f"step{step_id}.jpg"},
                observation=f"observation {step_id}",
            )
            for step_id in range(1, 8)
        ],
    )

    chunks = module._trajectory_chunks(trajectory, lookahead_steps=1)

    assert len(chunks) == 7
    assert [step.step_id for step in chunks[0].trajectory.steps] == [1, 2]
    assert [step.step_id for step in chunks[4].trajectory.steps] == [1, 2, 3, 4, 5, 6]
    assert [step.step_id for step in chunks[6].trajectory.steps] == [1, 2, 3, 4, 5, 6, 7]
    assert chunks[0].core_step_ids == {1}
    assert chunks[4].core_step_ids == {5}
    assert chunks[6].core_step_ids == {7}
    assert chunks[0].trajectory.metadata["chunk_input_mode"] == "summary_history_plus_lookahead_v2"
    assert chunks[0].trajectory.metadata["chunk_lookahead_steps"] == 1
