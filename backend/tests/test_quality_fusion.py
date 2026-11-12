import math
import unittest
from copy import deepcopy

from backend.quality_fusion import enrich_task, fuse, node_values
from backend.trajectories_tree.tree_builder import Node


def checklist(k=2, m=3, definition="same"):
    return {"status": "complete", "definition_digest": definition, "source": "llm", "total": m,
            "passed": k, "failed": m-k, "unchecked": 0, "verdict": "success" if k == m else "fail",
            "items": [{"id": str(i), "checked": True, "result": "passed" if i < k else "failed"} for i in range(m)]}


class QualityFusionTests(unittest.TestCase):
    def test_formula_and_original_scores_preserved(self):
        quality = {"global_score": 4, "dimension_global_scores": {"a": 4}, "step_evaluations": [{"step_id": 1}]}
        original = deepcopy(quality)
        result = fuse(quality, checklist())
        self.assertAlmostEqual(result["score"], 2.4/3.5)
        self.assertEqual(quality, original)

    def test_invalid_checklist_and_no_evidence_never_become_zero_score(self):
        for c in [dict(checklist(), status="error"), dict(checklist(), passed=5),
                  dict(checklist(), definition_digest=""), dict(checklist(), items=[]),
                  dict(checklist(), items=[{"id": "x", "checked": False, "result": None}])]:
            self.assertIsNone(fuse({"global_score": 4}, c)["score"])
        for g in [0, True, math.nan, 6]:
            self.assertFalse(fuse({"global_score": g}, checklist())["valid"])
        self.assertFalse(fuse({"global_score": 3, "dimension_global_scores": {"a": 0}}, checklist())["valid"])

    def test_true_tree_occurrence_shape_deduplicates_trajectories_and_has_root_mean(self):
        # Uses the production tree serializer and its actual `trajectory` field.
        left = Node(1, 1, occurrences=[{"trajectory": "a"}, {"trajectory": "a"}, {"trajectory": "b"}])
        right = Node(2, 1, occurrences=[{"trajectory": "c"}])
        root = Node(0, 0, children=[left, right]).to_dict()
        values = node_values(root, {key: {"fusion": {"valid": True, "score": score}}
                                  for key, score in {"a": .2, "b": .6, "c": 1.0}.items()}, ["a", "b", "c"])
        self.assertAlmostEqual(values["1"]["q"], .4)
        self.assertEqual(values["1"]["total_count"], 2)
        self.assertAlmostEqual(values["0"]["q"], .6)
        self.assertAlmostEqual(values["1"]["advantage"], -1, places=6)
        self.assertAlmostEqual(values["2"]["advantage"], 1, places=6)

    def test_missing_branch_makes_sibling_advantages_unavailable(self):
        tree = Node(0, 0, children=[Node(1, 1, occurrences=[{"trajectory": "a"}, {"trajectory": "b"}]),
                                        Node(2, 1, occurrences=[{"trajectory": "c"}])]).to_dict()
        values = node_values(tree, {"a": {"fusion": {"valid": True, "score": .8}}}, ["a", "b", "c"])
        self.assertEqual(values["1"]["status"], "partial")
        self.assertEqual(values["1"]["scored_count"], 1)
        self.assertIsNone(values["1"]["advantage"])
        self.assertIsNone(values["2"]["q"])

    def test_root_mean_weights_trajectories_not_branch_means(self):
        ids = [str(i) for i in range(10)]
        tree = Node(0, 0, children=[Node(1, 1, occurrences=[{"trajectory": i} for i in ids[:9]]),
                                    Node(2, 1, occurrences=[{"trajectory": ids[9]}])]).to_dict()
        evaluations = {i: {"fusion": {"valid": True, "score": .9 if i != ids[9] else .1}} for i in ids}
        self.assertAlmostEqual(node_values(tree, evaluations, ids)["0"]["q"], .82)

    def test_mixed_gold_and_llm_definition_metadata_invalidates_task(self):
        checklists = {"a": checklist(), "b": dict(checklist(), source="gold")}
        result = enrich_task({"evaluations": {"a": {"global_score": 4}, "b": {"global_score": 4}}},
                             Node(0, 0).to_dict(), checklists, ["a", "b"], "same")
        self.assertEqual(result["fusion_order"], [])
        self.assertIn("mixed_task_checklist_standards", result["evaluations"]["a"]["fusion"]["invalid_reasons"])

    def test_one_child_and_equal_siblings_have_zero_advantage(self):
        for children in [[Node(1, 1, occurrences=[{"trajectory": "a"}])],
                         [Node(1, 1, occurrences=[{"trajectory": "a"}]), Node(2, 1, occurrences=[{"trajectory": "a"}])]]:
            values = node_values(Node(0, 0, children=children).to_dict(), {"a": {"fusion": {"valid": True, "score": .8}}}, ["a"])
            self.assertTrue(all(values[str(c.id)]["advantage"] == 0 for c in children))

    def test_fusion_ranks_count_then_quality_then_workbook_order(self):
        evaluations = {"a": {"global_score": 5}, "b": {"global_score": 2}, "c": {"global_score": 2}}
        result = enrich_task({"evaluations": evaluations}, Node(0, 0).to_dict(),
                             {"a": checklist(1), "b": checklist(3), "c": checklist(3)}, ["a", "c", "b"], "same")
        self.assertEqual(result["fusion_order"], ["c", "b", "a"])
        self.assertTrue(result["fusion_complete"])
        self.assertNotIn("fusion", evaluations["a"])
        wrong = enrich_task({"evaluations": evaluations}, Node(0, 0).to_dict(),
                            {"a": checklist(definition="other")}, ["a"], "same")
        self.assertEqual(wrong["fusion_order"], [])


if __name__ == "__main__":
    unittest.main()
