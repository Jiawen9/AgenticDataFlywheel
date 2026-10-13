"""The category protocol must not change tree filtering or quality input rows."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from openpyxl import load_workbook

from backend.quality_input_builder import build_quality_workbook
from backend.tests.test_trajectory_tree import step
from backend.trajectories_tree.intermediate_state_classifier import (
    PREVIOUS_PROMPT_VERSION, PROMPT_VERSION, parse_classification_response,
)
from backend.trajectories_tree.tree_builder import apply_bounded_skip_policy, build_tree, count_nodes


class SummaryStub:
    model = "mock-model"

    def summarize_trajectory(self, task, trajectory, steps):
        return f"{task} / {trajectory} / {len(steps)} steps"


class IntermediatePipelineParityTests(unittest.TestCase):
    def test_matching_v4_and_v5_classifications_produce_same_tree_and_quality_rows(self):
        # category, confidence, action; expected rows are independent of parser output.
        normal = ("none", 0.99, "wait")
        cases = [
            ("short-ad", [("advertisement", 0.9, "wait"), normal, normal], [2, 3]),
            ("threshold-inclusive", [("loading", 0.8, "wait"), normal, normal], [2, 3]),
            ("low-confidence", [("loading", 0.79, "wait"), normal, normal], [1, 2, 3]),
            ("two-step-gap", [("loading", 0.9, "wait")] * 2 + [normal, normal], [3, 4]),
            ("long-gap-kept", [("loading", 0.9, "wait")] * 3 + [normal, normal], [1, 2, 3, 4, 5]),
            ("no-recovery", [("loading", 0.9, "wait"), normal], [1, 2]),
            ("protected-open", [("advertisement", 0.9, "open"), normal, normal], [1, 2, 3]),
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for label, values, expected in cases:
                outcomes = []
                for version in (PREVIOUS_PROMPT_VERSION, PROMPT_VERSION):
                    with self.subTest(case=label, version=version):
                        steps = []
                        for index, (category, confidence, kind) in enumerate(values, 1):
                            value = {"category": category, "confidence": confidence, "reason": "可见页面状态",
                                     "observation": f"动作 {index} 后的可见变化"}
                            if version == PREVIOUS_PROMPT_VERSION:
                                value["is_intermediate"] = category != "none"
                            item = step("trajectory", index, {"action": kind})
                            item.task_id = "task"
                            item.apply_classification(parse_classification_response(
                                json.dumps(value), require_observation=True, protocol_version=version), 0.8)
                            steps.append(item)
                        apply_bounded_skip_policy(steps)
                        tree, decisions, _ = build_tree([("trajectory", steps)], confidence_threshold=0.8,
                                                        trajectory_root=root, alignment_reviewer=None)
                        self.assertEqual([s.step_index for s in steps if s.counted_in_tree], expected)
                        output = root / f"{label}-{version}.xlsx"
                        counts = build_quality_workbook(grouped={"task": [("trajectory", steps)]},
                            task_goals={"task": "查看列表"}, trajectory_root=root, output=output,
                            env_path=root / "unused.env", summarizer=SummaryStub())
                        self.assertEqual(counts, (1, 1, len(expected)))
                        workbook = load_workbook(output, read_only=True, data_only=True)
                        try:
                            cells = {sheet.title: list(sheet.values) for sheet in workbook}
                        finally:
                            workbook.close()
                        payload = json.loads(output.with_suffix(".json").read_text(encoding="utf-8"))
                        # XLSX ZIP timestamps can differ although every cell is
                        # identical. Check each file's checksum, compare content.
                        self.assertEqual(payload.pop("workbook_sha256"), hashlib.sha256(output.read_bytes()).hexdigest())
                        outcomes.append((count_nodes(tree),
                            [(s.step_index, s.effective_intermediate, s.counted_in_tree) for s in steps],
                            [{key: d.get(key) for key in ("step", "decision", "decision_source", "node_id", "skip_count")}
                             for d in decisions], cells, payload))
                self.assertEqual(outcomes[0], outcomes[1], label)


if __name__ == "__main__":
    unittest.main()
