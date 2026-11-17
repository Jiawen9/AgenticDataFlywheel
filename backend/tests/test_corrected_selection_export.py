"""Complete multi-trajectory export and OPD use temporary frozen fixtures only."""
from copy import deepcopy
import hashlib
from unittest import TestCase
from unittest.mock import patch

from openpyxl import load_workbook

from backend.tests import test_pipeline_release, test_pipelines
from backend import pipeline_release
from backend.trajectory_correction import service, draft_store
from backend.trajectory_correction.session_state import fingerprint, reconcile, reviews_for_group
from backend.trajectory_correction.selected_export import project_selected


class CompleteSelectionTests(TestCase):
    def setUp(self):
        self.fixture = test_pipeline_release.PipelineReleaseTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        f = self.fixture
        self.pipeline = f.pipeline("manual")
        created = pipeline_release.prepare_manual(f.batch, self.pipeline["selection"], root=f.root)
        self.session = f.records.get("correction_sessions", created["session_id"])
        for group in self.session["snapshot_payload"]["groups"]:
            self.session["group_exports"][group["group_id"]] = True
        self.session = f.records.put("correction_sessions", self.session["session_id"], self.session)

    def project(self, policy="skip"):
        f, s = self.fixture, self.session
        confirmation = pipeline_release.confirm_manual(s["session_id"], root=f.root)
        tree, quality, table, snapshot, _ = pipeline_release._validate_source(confirmation["selection"], f.root)
        return project_selected(session=s, snapshot=snapshot, table=table, selection=confirmation["selection"],
                                cot_policy=policy, tree_payload=tree, quality_payload=quality)

    def test_full_steps_skip_thought_flags_and_source_immutability(self):
        s = self.session
        s["row_edits"]["2"] = {"actions": '{"action":"click","coordinate":[1080,238]}',
                               "original_actions": '{"action":"wait"}', "thought": "expert thought"}
        s["row_edits"]["3"] = {"deleted": True}
        s["row_edits"]["4"] = {"sop": "expert SOP"}
        self.fixture.records.put("correction_sessions", s["session_id"], s)
        before = deepcopy(s)
        result = self.project()
        rows = [row for group in result["groups"] for row in group["rows"]]
        self.assertEqual(len(rows), 11)
        self.assertEqual(len(result["groups"]), 4)
        self.assertEqual(sum(row["is_refined"] for row in rows), 2)
        self.assertTrue(all(row["thought"] == "" for row in rows))
        self.assertTrue(all("is_key_step" in row for row in rows))
        self.assertEqual(rows[0]["action"]["coordinate"], [1080, 238])
        self.assertEqual([row["excel_row"] for row in rows], list(range(2, 13)))
        self.assertNotIn(3, [row["source_excel_row"] for row in rows])
        self.assertEqual(s, before)

    def test_multiple_choices_survive_reconcile_and_reviews_do_not_cross_trajectories(self):
        s = self.session
        groups = s["snapshot_payload"]["groups"]
        first, second = groups[0], groups[1]
        s["group_exports"][first["group_id"]] = True
        s["group_exports"][second["group_id"]] = False
        for group in (first, second):
            row = group["rows"][0]
            s["row_edits"][str(row["excel_row"])] = {"sop": "manual " + group["meta_task"]}
        changed = deepcopy(s["snapshot_payload"])
        for group in changed["groups"][:2]:
            group["rows"][0]["summary"] = "changed upstream"
        reconcile(s, s["snapshot_payload"], changed, s["task_fingerprints"])
        self.assertTrue(s["group_exports"][first["group_id"]])
        self.assertFalse(s["group_exports"][second["group_id"]])
        reviews = reviews_for_group(s, s["snapshot_payload"], first)
        self.assertEqual(len(reviews), 1)
        self.assertEqual(reviews[0]["trajectory_id"], first["meta_task"])
        self.fixture.records.put("correction_sessions", s["session_id"], s)
        with patch.object(draft_store, "CORRECTION_SESSIONS_DIR", self.fixture.root / "sessions"), \
             patch.object(service, "CORRECTION_INPUTS_DIR", self.fixture.root / "system" / "trajectory_correction" / "inputs"):
            service.review_group(s["session_id"], first["group_id"], "discard")
        remaining = self.fixture.records.get("correction_sessions", s["session_id"])
        self.assertEqual(len(remaining["pending_review"]), 1)
        self.assertEqual(next(iter(remaining["pending_review"].values()))["trajectory_id"], second["meta_task"])

    def test_original_sop_and_explicit_empty_manual_sop_survive_projection(self):
        self.session["row_edits"]["3"] = {"sop": ""}
        self.session["row_edits"]["4"] = {"sop": "current manual SOP"}
        self.fixture.records.put("correction_sessions", self.session["session_id"], self.session)
        payload = self.project()
        rows = payload["groups"][0]["rows"]
        self.assertEqual([(row["sop"], row["sop_source"]) for row in rows],
                         [("original SOP", "original"), ("", "manual"), ("current manual SOP", "manual")])
        sheet = next(iter(payload["sheets"].values()))
        self.assertEqual([row["sop"] for row in sheet[:3]], ["original SOP", "", "current manual SOP"])

    def test_generated_summary_requires_current_step_task_and_action(self):
        s = self.session
        bases = s["snapshot_payload"]["groups"][0]["rows"]
        changed = {"action": "click", "coordinate": [1080, 238]}
        for base in bases:
            number = str(base["excel_row"])
            s["row_edits"][number] = {"actions": '{"action":"click","coordinate":[1080,238]}',
                                      "original_actions": base["actions"]}
            s["cot"][number] = {"content_tag": "thought_summary", "summary": "current generated summary",
                "thought": "current thought", "action_hash": fingerprint(changed),
                "bbox_hash": hashlib.sha256(base["actions_box"].encode()).hexdigest(),
                "source_fingerprint": base["source_fingerprint"], "task_fingerprint": s["task_fingerprints"][base["task_id"]]}
        s["cot"]["2"]["task_fingerprint"] = "old-task-version"
        s["cot"]["3"]["action_hash"] = "old-action-version"
        self.fixture.records.put("correction_sessions", s["session_id"], s)
        rows = self.project("generate")["groups"][0]["rows"]
        self.assertEqual([row["summary_source"] for row in rows], ["original", "original", "generated"])
        self.assertEqual(rows[2]["summary"], "current generated summary")
        self.assertEqual(rows[0]["sop_source"], "original")

    def test_new_profile_publishes_multi_selection_and_repeat_reuses_release(self):
        f, p, s = self.fixture, self.pipeline, self.session
        p.update(export_profile="corrected_selection_v1", cot_policy="skip", session_id=s["session_id"],
                 confirmation=pipeline_release.confirm_manual(s["session_id"], root=f.root))
        release = f.publish(p)
        self.assertEqual((release["task_count"], release["trajectory_count"], release["step_count"]), (2, 4, 12))
        self.assertEqual(f.publish(p)["release_id"], release["release_id"])
        payload = f.store.read_payload(release["source_refs"][0]["artifact"])
        self.assertEqual(payload["cot_policy"], "skip")
        self.assertEqual(len(payload["selection"]["selected_trajectory_ids"]["A"]), 2)

    def test_service_export_is_short_and_policy_specific_without_changing_draft(self):
        f, s = self.fixture, self.session
        with patch.object(draft_store, "CORRECTION_SESSIONS_DIR", f.root / "sessions"), \
             patch.object(service, "CORRECTION_EXPORTS_DIR", f.root / "exports"):
            before = f.records.get("correction_sessions", s["session_id"])
            result = service.export_selected_session(s["session_id"], before["storage_revision"], cot_policy="skip")
            saved = f.records.get("correction_sessions", s["session_id"])
            for field in ("snapshot_payload", "row_edits", "cot", "group_exports", "workbook_payload"):
                self.assertEqual(saved[field], before[field])
            again = service.export_selected_session(s["session_id"], saved["storage_revision"], cot_policy="skip")
            self.assertEqual(again["export_id"], result["export_id"])
            self.assertLess(len(result["filename"]), 40)
            self.assertEqual(result["summary"]["groups"], 4)
            book = load_workbook(f.root / "exports" / s["session_id"] / result["filename"], read_only=True)
            try:
                self.assertEqual(book.active.max_row, 13)
                headers = [cell.value for cell in book.active[1]]
                self.assertIn("is_refined", headers)
                self.assertIn("is_key_step", headers)
            finally:
                book.close()


class OpdSchedulingTests(TestCase):
    def test_new_automatic_keeps_original_policy_by_default(self):
        f = test_pipelines.PipelineTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        p = f.create("automatic", export_profile="corrected_selection_v1")
        self.assertEqual(p["cot_policy"], "generate")

    def test_skip_policy_never_submits_cot_and_is_frozen(self):
        f = test_pipelines.PipelineTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        f.runtime.cot_needed = True
        f.runtime.ready_stages.update({"preprocessing", "tree", "quality"})
        p = f.create("manual", export_profile="corrected_selection_v1", cot_policy="skip")
        p = f.tick(p, 5)
        p = f.control(p, "confirm-correction", session_revision=4)
        p = f.tick(p, 3)
        self.assertFalse(any(step == "cot" for step, _ in f.runtime.calls))
        self.assertEqual(p["steps"][5]["status"], "skipped")
        self.assertEqual(p["cot_policy"], "skip")


class OrdinarySelectionFlowTests(TestCase):
    def setUp(self):
        self.f = test_pipeline_release.PipelineReleaseTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        # Real tree inputs contain both the annotation and Rubric table. The
        # publication-only fixture intentionally omits the latter.
        tree = self.f.store.read_payload(self.f.store.get(self.f.batch, "04_tree"))
        tree["quality_input"] = deepcopy(tree["source_annotation"])
        quality = self.f.store.read_payload(self.f.store.get(self.f.batch, "05_quality"))
        ref = self.f.store.publish(self.f.batch, "04_tree", tree)
        self.f.store.publish(self.f.batch, "05_quality", quality, source_refs=[ref])
        for patcher in (patch.object(draft_store, "CORRECTION_SESSIONS_DIR", self.f.root / "sessions"),
                        patch.object(service, "CORRECTION_INPUTS_DIR", self.f.root / "inputs"),
                        patch.object(service, "CORRECTION_EXPORTS_DIR", self.f.root / "exports"),
                        patch.object(service, "ensure_correction_dirs")):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_real_quality_candidates_keep_top1_default_and_manual_multiple_on_refresh(self):
        created = service.create_session(batch_id=self.f.batch)
        self.assertEqual(len(created["groups"]), 4)
        self.assertEqual(sum(group["export"] for group in created["groups"]), 2)
        other = next(group for group in created["groups"] if not group["export"])
        service.patch_group_export(created["session_id"], other["group_id"], True, created["storage_revision"])
        refreshed = service.create_session(batch_id=self.f.batch)
        self.assertEqual(sum(group["export"] for group in refreshed["groups"]), 3)
        self.assertEqual(refreshed["session_id"], created["session_id"])

    def test_http_profile_exports_selected_steps_and_legacy_request_stays_legacy(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from backend.trajectory_correction import router
        created = service.create_session(batch_id=self.f.batch)
        app = FastAPI()
        app.include_router(router.router)
        with TestClient(app) as client:
            url = f"/api/correction/sessions/{created['session_id']}/dataset-export"
            result = client.post(url, json={"expected_revision": created["storage_revision"],
                "export_profile": "corrected_selection_v1", "cot_policy": "skip"})
            self.assertEqual(result.status_code, 200, result.text)
            data = result.json()
            self.assertEqual(data["kind"], "corrected_selection")
            self.assertEqual(data["summary"]["rows"], 6)
            payload = self.f.store.read_payload(data["artifact"])
            self.assertTrue(all(row["thought"] == "" for group in payload["groups"] for row in group["rows"]))
            old = client.post(url, json={"expected_revision": data["storage_revision"]})
            self.assertEqual(old.status_code, 200, old.text)
            self.assertEqual(old.json()["kind"], "full_dataset")
