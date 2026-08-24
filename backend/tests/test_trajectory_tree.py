from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from backend.trajectories_tree.intermediate_state_classifier import (
    IntermediateStateResult,
    parse_classification_response,
)
from backend.trajectories_tree.state_alignment_reviewer import (
    StateAlignmentResult,
    parse_alignment_response,
)
from backend.trajectories_tree.tree_builder import (
    FULL_SCORE,
    BuildStatistics,
    Step,
    apply_bounded_skip_policy,
    build_tree,
    classify_trajectories,
    count_nodes,
    direction,
    score_step,
    write_output,
)


def classification(
    is_intermediate: bool = False,
    confidence: float = 0.99,
    category: str | None = None,
) -> IntermediateStateResult:
    return IntermediateStateResult(
        is_intermediate=is_intermediate,
        category=category or ("advertisement" if is_intermediate else "none"),
        confidence=confidence,
        reason="unit test",
        raw_response="{}",
    )


def alignment(
    same_task_state: bool,
    confidence: float = 0.99,
) -> StateAlignmentResult:
    return StateAlignmentResult(
        same_task_state=same_task_state,
        confidence=confidence,
        reason="unit test",
        raw_response="{}",
    )


def step(
    trajectory: str,
    index: int,
    action: dict,
    box: str = "",
) -> Step:
    return Step(
        trajectory=trajectory,
        step_index=index,
        excel_row=index + 1,
        image=f"{trajectory}/step{index:03d}_vla_input.jpg",
        xml=f"{trajectory}/step{index:03d}_vla_input_ui.xml",
        action_text=json.dumps(action),
        action=action,
        summary=f"summary {index}",
        actions_box=box,
    )


def click_box(x0: int, y0: int, x1: int, y1: int) -> str:
    return f"click(bbox=<bbox>[{x0},{y0},{x1},{y1}]</bbox>)"


def swipe_box(x0: int, y0: int, x1: int, y1: int, swipe_direction: str) -> str:
    return (
        f"swipe_screen(bbox=<bbox>[{x0},{y0},{x1},{y1}]</bbox>, "
        f"direction={swipe_direction})"
    )


def action_step(trajectory: str, index: int, name: str, offset: int) -> Step:
    return step(
        trajectory,
        index,
        {"action": "click", "coordinate": [900 + offset, 900 + offset], "name": name},
        click_box(offset, offset, offset + 30, offset + 30),
    )


class FakeClassifier:
    model = "qwen3.6-27b:floor"

    def __init__(
        self,
        responses: dict[int | tuple[str, int], IntermediateStateResult],
    ) -> None:
        self.responses = responses
        self.calls: list[dict] = []

    def classify(self, **kwargs):
        self.calls.append(kwargs)
        key = (kwargs["trajectory"], kwargs["step_index"])
        if key in self.responses:
            return self.responses[key]
        return self.responses[kwargs["step_index"]]


class FakeAlignmentReviewer:
    model = "qwen3.6-27b:floor"

    def __init__(self, response: StateAlignmentResult) -> None:
        self.response = response
        self.calls: list[dict] = []

    def review(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


class FailingAlignmentReviewer:
    model = "qwen3.6-27b:floor"

    def review(self, **kwargs):
        raise RuntimeError("review failed")


class ResponseParsingTests(unittest.TestCase):
    def test_intermediate_classification_json_is_strict(self):
        parsed = parse_classification_response(
            '{"is_intermediate":true,"category":"loading",'
            '"confidence":0.91,"reason":"加载遮罩"}'
        )
        self.assertTrue(parsed.is_intermediate)
        self.assertEqual(parsed.category, "loading")
        with self.assertRaisesRegex(ValueError, "category='none'"):
            parse_classification_response(
                '{"is_intermediate":false,"category":"advertisement",'
                '"confidence":0.9,"reason":"bad"}'
            )

    def test_alignment_json_is_strict(self):
        parsed = parse_alignment_response(
            '{"same_task_state":true,"confidence":0.93,"reason":"页面一致"}'
        )
        self.assertTrue(parsed.same_task_state)
        with self.assertRaisesRegex(ValueError, "same_task_state must be boolean"):
            parse_alignment_response(
                '{"same_task_state":"yes","confidence":0.9,"reason":"bad"}'
            )


class AllTrajectoryClassificationTests(unittest.TestCase):
    def test_every_step_in_every_trajectory_reaches_classifier(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            first_steps = [
                step("first", 1, {"action": "wait"}),
                step("first", 2, {"action": "wait"}),
            ]
            second_steps = [step("second", 1, {"action": "wait"})]
            for current in first_steps + second_steps:
                image_path = root / current.image
                image_path.parent.mkdir(parents=True, exist_ok=True)
                Image.new("RGB", (20, 20), "white").save(image_path)
            classifier = FakeClassifier(
                {
                    ("first", 1): classification(),
                    ("first", 2): classification(True, 0.9, "loading"),
                    ("second", 1): classification(),
                }
            )

            classify_trajectories(
                [("first", first_steps), ("second", second_steps)],
                classifier,
                root,
                0.8,
            )

            self.assertEqual(len(classifier.calls), 3)
            self.assertIsNotNone(classifier.calls[0]["next_image_path"])
            self.assertIsNone(classifier.calls[1]["next_image_path"])
            self.assertIsNone(classifier.calls[2]["next_image_path"])
            self.assertTrue(first_steps[1].classification_candidate)

    def test_policy_ignores_only_short_runs_with_two_stable_followups(self):
        steps = [step("seed", index, {"action": "wait"}) for index in range(1, 14)]
        # Short prefix run followed by two stable states: ignore step 1.
        steps[0].apply_classification(classification(True), 0.8)
        steps[1].apply_classification(classification(), 0.8)
        steps[2].apply_classification(classification(), 0.8)
        # Two-step run followed by two stable states: ignore steps 4-5.
        steps[3].apply_classification(classification(True), 0.8)
        steps[4].apply_classification(classification(True), 0.8)
        steps[5].apply_classification(classification(), 0.8)
        steps[6].apply_classification(classification(), 0.8)
        # Three-step run is retained even though it recovers.
        for item in steps[7:10]:
            item.apply_classification(classification(True), 0.8)
        steps[10].apply_classification(classification(), 0.8)
        steps[11].apply_classification(classification(), 0.8)
        # Terminal intermediate is retained.
        steps[12].apply_classification(classification(True), 0.8)

        apply_bounded_skip_policy(steps)

        ignored = [item.step_index for item in steps if item.effective_intermediate]
        self.assertEqual(ignored, [1, 4, 5])
        self.assertTrue(all(steps[index - 1].counted_in_tree for index in [8, 9, 10, 13]))
        self.assertEqual(steps[3].skip_count, 2)
        self.assertEqual(steps[3].confirmation_step_indices, [6, 7])

    def test_low_confidence_result_is_retained_for_structural_fallback(self):
        steps = [step("seed", index, {"action": "wait"}) for index in range(1, 4)]
        steps[0].apply_classification(classification(True, 0.79, "loading"), 0.8)
        steps[1].apply_classification(classification(), 0.8)
        steps[2].apply_classification(classification(), 0.8)

        apply_bounded_skip_policy(steps)

        self.assertFalse(steps[0].effective_intermediate)
        self.assertTrue(steps[0].uncertain)
        self.assertTrue(steps[0].counted_in_tree)

    def test_short_ad_is_ignored_before_a_new_unseen_branch(self):
        steps = [
            step("new-branch", 1, {"action": "wait"}),
            action_step("new-branch", 2, "NEW-A", 200),
            action_step("new-branch", 3, "NEW-B", 250),
        ]
        steps[0].apply_classification(
            classification(True, 0.99, "advertisement"), 0.8
        )
        steps[1].apply_classification(classification(), 0.8)
        steps[2].apply_classification(classification(), 0.8)
        apply_bounded_skip_policy(steps)

        root, decisions, _ = build_tree(
            [("new-branch", steps)],
            confidence_threshold=0.8,
            trajectory_root=Path("unused"),
            alignment_reviewer=None,
        )

        self.assertTrue(steps[0].effective_intermediate)
        self.assertEqual(decisions[0]["decision_source"], "bounded_classification")
        self.assertEqual(root.children[0].reference.step_index, 2)
        self.assertEqual(count_nodes(root) - 1, 2)
        occurrence = root.children[0].occurrences[0]
        self.assertEqual(occurrence["action"], steps[1].action)
        self.assertEqual(occurrence["summary"], steps[1].summary)
        self.assertEqual(occurrence["actions_box"], steps[1].actions_box)


class ActionMatchingTests(unittest.TestCase):
    def test_click_and_long_press_prefer_box_overlap_over_raw_coordinates(self):
        reference = step(
            "a", 1, {"action": "click", "coordinate": [900, 100]}, click_box(10, 20, 110, 120)
        )
        candidate = step(
            "b", 1, {"action": "click", "coordinate": [1, 1]}, click_box(20, 30, 100, 110)
        )
        self.assertEqual(score_step(candidate, reference), FULL_SCORE)
        reference.action = {"action": "long_press", "coordinate": [900, 100]}
        candidate.action = {"action": "long_press", "coordinate": [1, 1]}
        reference.actions_box = "long_press(bbox=<bbox>[10,20,110,120]</bbox>)"
        candidate.actions_box = "long_press(bbox=<bbox>[20,30,100,110]</bbox>)"
        self.assertEqual(score_step(candidate, reference), FULL_SCORE)

    def test_disjoint_boxes_do_not_merge(self):
        reference = step("a", 1, {"action": "click"}, click_box(0, 0, 20, 20))
        candidate = step("b", 1, {"action": "click"}, click_box(30, 30, 50, 50))
        self.assertEqual(score_step(candidate, reference), 0.5)

    def test_all_swipe_directions(self):
        coordinates = {
            "left": ([10, 5], [0, 5]),
            "right": ([0, 5], [10, 5]),
            "up": ([5, 10], [5, 0]),
            "down": ([5, 0], [5, 10]),
        }
        for expected, (start, end) in coordinates.items():
            action = {"action": "swipe", "start_coordinate": start, "end_coordinate": end}
            self.assertEqual(direction(action), expected)
            reference = step("a", 1, action, swipe_box(0, 0, 50, 50, expected))
            candidate = step("b", 1, action, swipe_box(5, 5, 45, 45, expected))
            self.assertEqual(score_step(candidate, reference), FULL_SCORE)


class ShortGapTreeTests(unittest.TestCase):
    def _seed(self):
        return [
            action_step("seed", 1, "A", 0),
            action_step("seed", 2, "B", 50),
            action_step("seed", 3, "C", 100),
        ]

    def _build(self, later, reviewer):
        trajectories = [("seed", self._seed()), ("later", later)]
        return build_tree(
            trajectories,
            confidence_threshold=0.8,
            trajectory_root=Path("unused"),
            alignment_reviewer=reviewer,
        )

    def test_one_step_gap_is_ignored_after_qwen_accepts_two_pairs(self):
        later = [
            action_step("later", 1, "A", 0),
            step("later", 2, {"action": "wait"}),
            action_step("later", 3, "B", 50),
            action_step("later", 4, "C", 100),
        ]
        later[1].apply_classification(classification(True, 0.7, "loading"), 0.8)
        reviewer = FakeAlignmentReviewer(alignment(True))

        root, decisions, stats = self._build(later, reviewer)

        self.assertEqual(count_nodes(root) - 1, 3)
        self.assertEqual(len(reviewer.calls), 1)
        self.assertEqual(len(reviewer.calls[0]["candidate_image_paths"]), 2)
        ignored = [item for item in decisions if item["decision"] == "ignore_intermediate"]
        self.assertEqual(len(ignored), 1)
        self.assertEqual(ignored[0]["decision_source"], "uncertain_structural_resync")
        self.assertEqual(ignored[0]["skip_count"], 1)
        self.assertEqual(stats.resync_accepted_count, 1)
        self.assertEqual(stats.resync_ignored_step_count, 1)

    def test_two_step_gap_is_ignored(self):
        later = [
            action_step("later", 1, "A", 0),
            step("later", 2, {"action": "wait"}),
            step("later", 3, {"action": "open", "text": "ad"}),
            action_step("later", 4, "B", 50),
            action_step("later", 5, "C", 100),
        ]
        later[1].apply_classification(classification(True, 0.7, "loading"), 0.8)
        later[2].apply_classification(classification(True, 0.7, "loading"), 0.8)
        reviewer = FakeAlignmentReviewer(alignment(True))

        root, decisions, stats = self._build(later, reviewer)

        self.assertEqual(count_nodes(root) - 1, 3)
        ignored = [item for item in decisions if item["decision"] == "ignore_intermediate"]
        self.assertEqual([item["step"] for item in ignored], [2, 3])
        self.assertTrue(all(item["skip_count"] == 2 for item in ignored))
        self.assertEqual(stats.resync_ignored_step_count, 2)

    def test_qwen_rejection_keeps_original_branch(self):
        later = [
            action_step("later", 1, "A", 0),
            step("later", 2, {"action": "wait"}),
            action_step("later", 3, "B", 50),
            action_step("later", 4, "C", 100),
        ]
        later[1].apply_classification(classification(True, 0.7, "loading"), 0.8)
        reviewer = FakeAlignmentReviewer(alignment(False))

        root, decisions, stats = self._build(later, reviewer)

        self.assertEqual(count_nodes(root) - 1, 6)
        self.assertFalse(any(item["decision"] == "ignore_intermediate" for item in decisions))
        self.assertEqual(stats.resync_rejected_count, 1)
        self.assertIsNotNone(later[1].alignment_review)

    def test_high_confidence_normal_gap_never_calls_alignment_qwen(self):
        later = [
            action_step("later", 1, "A", 0),
            step("later", 2, {"action": "wait"}),
            action_step("later", 3, "B", 50),
            action_step("later", 4, "C", 100),
        ]
        later[1].apply_classification(classification(False, 0.99, "none"), 0.8)
        reviewer = FakeAlignmentReviewer(alignment(True))

        _, decisions, stats = self._build(later, reviewer)

        self.assertEqual(len(reviewer.calls), 0)
        self.assertEqual(stats.resync_candidate_count, 0)
        self.assertFalse(any(item["decision"] == "ignore_intermediate" for item in decisions))

    def test_bbox_mismatch_does_not_call_qwen(self):
        later = [
            action_step("later", 1, "A", 0),
            step("later", 2, {"action": "wait"}),
            action_step("later", 3, "B", 500),
            action_step("later", 4, "C", 100),
        ]
        later[1].apply_classification(classification(True, 0.7, "loading"), 0.8)
        reviewer = FakeAlignmentReviewer(alignment(True))

        _, decisions, stats = self._build(later, reviewer)

        self.assertEqual(len(reviewer.calls), 0)
        self.assertEqual(stats.resync_candidate_count, 0)
        self.assertFalse(any(item["decision"] == "ignore_intermediate" for item in decisions))

    def test_long_and_terminal_anomalies_are_preserved_without_review(self):
        later = [
            action_step("later", 1, "A", 0),
            step("later", 2, {"action": "wait"}),
            step("later", 3, {"action": "open", "text": "x"}),
            step("later", 4, {"action": "system_button", "button": "back"}),
            action_step("later", 5, "B", 50),
            action_step("later", 6, "C", 100),
            step("later", 7, {"action": "wait"}),
        ]
        for item in later:
            item.apply_classification(classification(), 0.8)
        for item in later[1:4]:
            item.apply_classification(classification(True, 0.99, "loading"), 0.8)
        later[6].apply_classification(classification(True, 0.99, "loading"), 0.8)
        apply_bounded_skip_policy(later)
        reviewer = FakeAlignmentReviewer(alignment(True))

        _, decisions, stats = self._build(later, reviewer)

        self.assertEqual(len(reviewer.calls), 0)
        self.assertEqual(stats.resync_review_count, 0)
        later_decisions = [item for item in decisions if item["trajectory"] == "later"]
        self.assertTrue(all(item["decision"] != "ignore_intermediate" for item in later_decisions))

    def test_review_failure_leaves_existing_output_untouched(self):
        later = [
            action_step("later", 1, "A", 0),
            step("later", 2, {"action": "wait"}),
            action_step("later", 3, "B", 50),
            action_step("later", 4, "C", 100),
        ]
        later[1].apply_classification(classification(True, 0.7, "loading"), 0.8)
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "tree.json"
            output.write_text("old complete tree", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "截图一致性复核失败"):
                self._build(later, FailingAlignmentReviewer())
            self.assertEqual(output.read_text(encoding="utf-8"), "old complete tree")


class OutputAuditTests(unittest.TestCase):
    def test_json_contains_full_audit_and_no_page_fields(self):
        seed_steps = [
            action_step("seed", 1, "A", 0),
            step("seed", 2, {"action": "wait"}),
            action_step("seed", 3, "B", 50),
            action_step("seed", 4, "C", 100),
        ]
        for index, item in enumerate(seed_steps):
            item.apply_classification(
                classification(index == 1, category="advertisement" if index == 1 else "none"),
                0.8,
            )
        apply_bounded_skip_policy(seed_steps)
        trajectories = [("seed", seed_steps)]
        root, decisions, stats = build_tree(
            trajectories,
            confidence_threshold=0.8,
            trajectory_root=Path("unused"),
            alignment_reviewer=None,
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "tree.json"
            write_output(
                root,
                decisions,
                stats,
                trajectories,
                model_name="qwen3.6-27b:floor",
                confidence_threshold=0.8,
                max_incidental_skip=2,
                json_path=output,
            )
            payload = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(payload["original_step_count"], 4)
        self.assertEqual(payload["tree_step_count"], 3)
        self.assertEqual(payload["ignored_incidental_step_count"], 1)
        self.assertEqual(payload["classification_count"], 4)
        self.assertEqual(payload["bounded_ignored_step_count"], 1)
        self.assertEqual(payload["retained_long_or_terminal_intermediate_count"], 0)
        self.assertEqual(len(payload["decisions"]), 4)
        self.assertEqual(
            sum(len(item["steps"]) for item in payload["source_trajectories"]), 4
        )

        def keys(value):
            if isinstance(value, dict):
                yield from value.keys()
                for child in value.values():
                    yield from keys(child)
            elif isinstance(value, list):
                for child in value:
                    yield from keys(child)

        forbidden = {"page_id", "page_similarity", "page_catalog"}
        self.assertTrue(forbidden.isdisjoint(set(keys(payload))))


if __name__ == "__main__":
    unittest.main()
