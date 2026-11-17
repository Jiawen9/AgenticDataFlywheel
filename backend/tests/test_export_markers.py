from copy import deepcopy
import unittest

from backend.export_markers import annotate_rows


class ExportMarkerTests(unittest.TestCase):
    def fixture(self):
        tree = {"id": 0, "children": [{"id": 1, "occurrences": [{"trajectory": "a", "step": 3, "image": "a/step003.jpg"}], "terminal_trajectories": ["a", "b"]},
                                       {"id": 2, "occurrences": [{"trajectory": "c", "step": 1, "image": "c/step001.jpg"}]}]}
        groups = [{"task_id": "t", "meta_task": "a", "rows": [{"excel_row": 2, "step": 3, "image": "a/step003.jpg", "actions": '{"action":"wait"}', "original_action": '{"action":"wait"}', "sop": ""},
                                                                         {"excel_row": 3, "step": 2, "image": "a/skipped.jpg"}]}]
        quality = {"source_tree_hashes": {"t": "same"}, "artifact": {"version": "qv1"}, "tasks": [{"task_id": "t", "scoring_version": "checklist_fusion_v1", "node_scores": {"1": {"q": .8000001, "scored_count": 1, "total_count": 2}, "2": {"q": .8}}}]}
        return groups, {"trees": {"t": tree}, "tree_hashes": {"t": "same"}, "artifact": {"version": "tv1"}}, quality

    def test_raw_q_fork_maps_occurrence_not_depth_and_preserves_inputs(self):
        groups, tree, quality = self.fixture()
        before = deepcopy((groups, tree, quality))
        result = annotate_rows(groups, tree_payload=tree, quality_payload=quality, row_edits={})
        self.assertTrue(result[0]["rows"][0]["is_key_step"])
        self.assertIsNone(result[0]["rows"][1]["is_key_step"])
        self.assertFalse(result[0]["rows"][0]["is_refined"])
        self.assertEqual(result[0]["rows"][0]["source_node_id"], 1)
        self.assertEqual(result[0]["rows"][0]["source_score_coverage"]["total_count"], 2)
        self.assertEqual(result[0]["rows"][0]["source_tree_version"], "tv1")
        self.assertEqual((groups, tree, quality), before)

    def test_wrong_or_unrecorded_tree_version_cannot_label_a_branch(self):
        groups, tree, quality = self.fixture()
        for expected in ({"t": "different"}, {}):
            quality["source_tree_hashes"] = expected
            row = annotate_rows(groups, tree_payload=tree, quality_payload=quality, row_edits={})[0]["rows"][0]
            self.assertIsNone(row["is_key_step"])

    def test_equal_and_missing_q_differ(self):
        groups, tree, quality = self.fixture()
        quality["tasks"][0]["node_scores"]["1"]["q"] = .8
        self.assertFalse(annotate_rows(groups, tree_payload=tree, quality_payload=quality, row_edits={})[0]["rows"][0]["is_key_step"])
        quality["tasks"][0]["node_scores"].pop("2")
        self.assertIsNone(annotate_rows(groups, tree_payload=tree, quality_payload=quality, row_edits={})[0]["rows"][0]["is_key_step"])

    def test_only_effective_action_or_sop_is_refined(self):
        groups, tree, quality = self.fixture()
        row = groups[0]["rows"][0]
        for edits, expected in [({"actions": ' { "action": "wait" } ', "actions_box": "box", "thought": "new"}, False),
                                ({"sop": ""}, False), ({"sop": "改过的说明"}, True),
                                ({"actions": '{"action":"open","text":"App"}'}, True)]:
            row.update(actions=edits.get("actions", '{"action":"wait"}'), sop=edits.get("sop", ""))
            result = annotate_rows(groups, tree_payload=tree, quality_payload=quality, row_edits={"2": edits})
            self.assertEqual(result[0]["rows"][0]["is_refined"], expected)


if __name__ == "__main__":
    unittest.main()
