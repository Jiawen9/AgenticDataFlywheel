import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.trajectory_correction import quality_selection as selection


class FusionSelectionTests(unittest.TestCase):
    def select(self, evaluations, version="checklist_fusion_v1"):
        run = {"run_id": "quality1", "tree_run_id": "tree1", "completed_at": "now", "current": True,
               "tasks": [{"task_id": "task1"}], "results": {"task1": {"scoring_version": version, "evaluations": evaluations}}}
        snapshot = {"groups": [{"task_id": "task1", "meta_task": key, "rows": [{}]} for key in evaluations]}
        with patch.object(selection, "_source_sha256", return_value="frozen"):
            return selection._selection_for_run(run, snapshot, source_path=Path("source.xlsx"))

    def test_f_not_g_controls_new_recommendation_and_tie_is_stable(self):
        scores = {"z": {"global_score": 2, "fusion": {"valid": True, "score": .95}},
                  "a": {"global_score": 5, "fusion": {"valid": True, "score": .8}}}
        self.assertEqual(self.select(scores)["selected_trajectories"], {"task1": "z"})
        scores["a"]["fusion"]["score"] = .95
        self.assertEqual(self.select(scores)["selected_trajectories"], {"task1": "z"})

    def test_incomplete_new_scores_are_not_silently_ranked_by_g(self):
        with self.assertRaises(selection.QualitySelectionUnavailable):
            self.select({"a": {"global_score": 5, "fusion": {"valid": False, "score": None}}})
        historical = self.select({"a": {"global_score": 5}, "b": {"global_score": 4}}, version="")
        self.assertEqual(historical["tasks"][0]["ranking_basis"], "rubric")
        self.assertEqual(historical["selected_trajectories"], {"task1": "a"})


if __name__ == "__main__":
    unittest.main()
