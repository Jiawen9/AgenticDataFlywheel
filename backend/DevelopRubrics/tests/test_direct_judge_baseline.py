"""Tests for the Jiawen direct-judge baseline prompt switches."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from adarubric import TaskDescription, Trajectory, TrajectoryStep
from adarubric.core.models import TaskComplexity

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = PROJECT_ROOT / "examples" / "direct-judge-jiawen-baseline.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("direct_judge_jiawen_baseline", SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["direct_judge_jiawen_baseline"] = module
    spec.loader.exec_module(module)
    return module


def _task() -> TaskDescription:
    return TaskDescription(
        task_id="task-1",
        instruction="Delete completed playback records.",
        complexity=TaskComplexity.MODERATE,
    )


def _trajectory() -> Trajectory:
    return Trajectory(
        trajectory_id="traj-1",
        task_id="task-1",
        steps=[
            TrajectoryStep(
                step_id=1,
                action="tap",
                action_input={"target": "history"},
                observation="The playback history page is visible.",
            )
        ],
    )


def _prefixed_trajectory(source_session_id: str) -> Trajectory:
    return Trajectory(
        trajectory_id=f"task-folder__{source_session_id}",
        task_id="task-1",
        steps=[
            TrajectoryStep(
                step_id=1,
                action="tap",
                action_input={"target": "history"},
                observation="The playback history page is visible.",
            )
        ],
        metadata={"source_session_id": source_session_id},
    )


class _FakeClient:
    def __init__(self, outputs: list[str]) -> None:
        self.outputs = outputs

    async def generate_text(self, *_args, **_kwargs) -> str:
        return self.outputs.pop(0)


def test_completion_first_prompt_is_off_by_default():
    module = _load_module()

    messages = module.build_messages(_task(), [_trajectory()], config={})

    assert "Completion of the requested task is the most important criterion" not in messages[0][
        "content"
    ]


def test_completion_first_prompt_can_be_enabled():
    module = _load_module()

    messages = module.build_messages(
        _task(),
        [_trajectory()],
        config={"direct_judge_completion_first": True},
    )

    assert "Completion of the requested task is the most important criterion" in messages[0][
        "content"
    ]


def test_prompt_does_not_include_copyable_trajectory_id_placeholder():
    module = _load_module()

    messages = module.build_messages(_task(), [_trajectory()], config={})
    prompt = "\n".join(message["content"] for message in messages)

    assert "one exact trajectory_id or candidate label" not in prompt
    assert '"trajectory_id": "C1"' in prompt


def test_parse_direct_judge_response_accepts_ranking_array():
    module = _load_module()

    response = module._parse_direct_judge_response(
        '[{"trajectory_id": "traj-1", "reason": "better"}]',
        task=_task(),
    )

    assert response.task_id == "task-1"
    assert response.ranking[0].rank == 1
    assert response.ranking[0].trajectory_id == "traj-1"


def test_parse_direct_judge_response_accepts_common_aliases():
    module = _load_module()

    response = module._parse_direct_judge_response(
        '{"rankings": [{"trajectoryId": "traj-1", "rationale": "observable progress"}]}',
        task=_task(),
    )

    assert response.ranking[0].rank == 1
    assert response.ranking[0].trajectory_id == "traj-1"
    assert response.ranking[0].reason == "observable progress"


def test_parse_direct_judge_response_accepts_python_literal():
    module = _load_module()

    response = module._parse_direct_judge_response(
        "{'ranking': [{'trajectory_id': 'traj-1', 'reason': 'better'}]}",
        task=_task(),
    )

    assert response.ranking[0].rank == 1
    assert response.ranking[0].trajectory_id == "traj-1"


def test_normalize_response_maps_source_session_id_aliases():
    module = _load_module()
    trajectories = [
        _prefixed_trajectory("YYSP-TXSP-468faa-9-1"),
        _prefixed_trajectory("YYSP-TXSP-468faa-9-2"),
    ]
    response = module.DirectJudgeResponse(
        ranking=[
            module.DirectJudgeRank(
                rank=1,
                trajectory_id="YYSP-TXSP-468faa-9-2",
                reason="more complete",
            ),
            module.DirectJudgeRank(
                rank=2,
                trajectory_id="YYSP-TXSP-468faa-9-1",
                reason="less complete",
            ),
        ]
    )

    normalized = module._normalize_response(response, _task(), trajectories)

    assert [item.trajectory_id for item in normalized.ranking] == [
        "task-folder__YYSP-TXSP-468faa-9-2",
        "task-folder__YYSP-TXSP-468faa-9-1",
    ]


def test_normalize_response_maps_candidate_labels():
    module = _load_module()
    trajectories = [
        _prefixed_trajectory("YYSP-TXSP-468faa-9-1"),
        _prefixed_trajectory("YYSP-TXSP-468faa-9-2"),
    ]
    response = module.DirectJudgeResponse(
        ranking=[
            module.DirectJudgeRank(rank=1, trajectory_id="C2", reason="better"),
            module.DirectJudgeRank(rank=2, trajectory_id="C1", reason="weaker"),
        ]
    )

    normalized = module._normalize_response(response, _task(), trajectories)

    assert [item.trajectory_id for item in normalized.ranking] == [
        "task-folder__YYSP-TXSP-468faa-9-2",
        "task-folder__YYSP-TXSP-468faa-9-1",
    ]


def test_normalize_response_appends_missing_candidates_after_ranked_items():
    module = _load_module()
    trajectories = [
        _prefixed_trajectory("YYSP-TXSP-468faa-9-1"),
        _prefixed_trajectory("YYSP-TXSP-468faa-9-2"),
    ]
    response = module.DirectJudgeResponse(
        ranking=[module.DirectJudgeRank(rank=1, trajectory_id="C2", reason="better")]
    )

    normalized = module._normalize_response(response, _task(), trajectories)

    assert [item.trajectory_id for item in normalized.ranking] == [
        "task-folder__YYSP-TXSP-468faa-9-2",
        "task-folder__YYSP-TXSP-468faa-9-1",
    ]
    assert normalized.ranking[1].reason == (
        "Missing from model ranking; appended after ranked candidates."
    )


def test_response_from_candidate_mentions_recovers_text_ranking():
    module = _load_module()
    trajectories = [
        _prefixed_trajectory("YYSP-TXSP-468faa-9-1"),
        _prefixed_trajectory("YYSP-TXSP-468faa-9-2"),
        _prefixed_trajectory("YYSP-TXSP-468faa-9-5"),
    ]

    response = module._response_from_candidate_mentions(
        "1. C2 is best\n" "2. C3 is second\n" "3. C1 is weakest",
        task=_task(),
        trajectories=trajectories,
    )

    assert [item.trajectory_id for item in response.ranking] == [
        "task-folder__YYSP-TXSP-468faa-9-2",
        "task-folder__YYSP-TXSP-468faa-9-5",
        "task-folder__YYSP-TXSP-468faa-9-1",
    ]


async def test_judge_task_once_falls_back_when_json_contains_placeholder(tmp_path):
    module = _load_module()
    trajectories = [
        _prefixed_trajectory("YYSP-TXSP-468faa-9-1"),
        _prefixed_trajectory("YYSP-TXSP-468faa-9-2"),
    ]
    raw = (
        '{"ranking": [{"rank": 1, '
        '"trajectory_id": "one exact trajectory_id or candidate label", '
        '"reason": "bad placeholder"}]}\n'
        "Actual ranking: C2 > C1"
    )
    client = _FakeClient([raw, raw])

    result = await module.judge_task_once(
        client=client,
        task=_task(),
        trajectories=trajectories,
        config={"direct_judge_raw_response_path": str(tmp_path / "raw.jsonl")},
        run_number=1,
    )

    assert [item.trajectory_id for item in result.response.ranking] == [
        "task-folder__YYSP-TXSP-468faa-9-2",
        "task-folder__YYSP-TXSP-468faa-9-1",
    ]
