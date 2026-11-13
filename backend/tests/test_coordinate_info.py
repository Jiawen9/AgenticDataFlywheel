"""Small real-image fixtures; no device, model, or production data access."""
from copy import deepcopy
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from openpyxl import Workbook, load_workbook
from PIL import Image

from backend import coordinate_info as coordinates
from backend.data_store import ArtifactStore, RevisionConflict
from backend.trajectory_context import AnnotationVersionConflict
from backend.trajectory_correction import draft_store, service
from backend.trajectory_correction.exporter import export_full_dataset_workbook, export_session_workbook
from backend.trajectory_correction.router import router
from backend.trajectory_correction.workbook import load_snapshot


class CoordinateEvidenceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="coordinate-info-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.raw = self.root / "raw" / "source"
        self.folder = self.raw / "task" / "trajectory"
        self.folder.mkdir(parents=True)
        self.image = "task/trajectory/step001_vla_input.jpg"
        Image.new("RGB", (2160, 3840), "white").save(self.raw / self.image)
        self.action = {"action": "click", "coordinate": [1080, 2500]}
        self.response = self.folder / "step001_vla_model_response.json"
        self.response.write_text(json.dumps({"agent_type": "pipelineagent", "global_step": 1,
            "actual_action_coordinate_system": "pixel", "executed_action": self.action}), encoding="utf-8")
        self.row = {"文件夹名": "trajectory", "trajectory_id": "trajectory", "task_id": "task",
            "image": self.image, "action": json.dumps(self.action), "agent_type": "pipelineagent"}
        coordinates._INDEXES.clear()
        service._COORDINATE_ROWS.clear()

    def test_pipeline_uses_original_image_not_xml_bounds_or_1000(self):
        # A partial/mismatched UI tree is not a canvas-size source.
        (self.folder / "step001_vla_input_ui.xml").write_text('<node bounds="[0,0][200,300]"/>')
        info = coordinates.resolve_coordinate_info(self.row, self.raw)
        self.assertEqual((info["space"], info["width"], info["height"]), ("pixel", 2160, 3840))
        (self.folder / "step001_vla_input_ui.xml").unlink()
        self.assertEqual(coordinates.resolve_coordinate_info(self.row, self.raw), info)

    def test_missing_evidence_and_historical_edited_action_are_unknown(self):
        edited = {"action": "click", "coordinate": [100, 200]}
        self.assertEqual(coordinates.resolve_coordinate_info(self.row, self.raw, current_action=edited)["space"], "unknown")
        self.assertEqual(coordinates.resolve_coordinate_info({**self.row, "action": edited}, self.raw)["reason"], "executed_action_mismatch")
        self.response.unlink()
        self.assertEqual(coordinates.resolve_coordinate_info(self.row, self.raw)["space"], "unknown")

    def test_native_compatibility_requires_engine_format_or_native_evidence(self):
        self.assertEqual(coordinates.resolve_coordinate_info({**self.row, "agent_type": "newagent"}, self.raw)["space"], "normalized")
        self.response.unlink()
        unknown = {key: value for key, value in self.row.items() if key != "agent_type"}
        self.assertEqual(coordinates.resolve_coordinate_info(unknown, self.raw)["space"], "unknown")
        declared = {**unknown, "trajectory_format": "newagent-compatible-v1", "normalizer_version": "native-v1"}
        self.assertEqual(coordinates.resolve_coordinate_info(declared, self.raw)["space"], "normalized")
        self.assertEqual(coordinates.resolve_coordinate_info(unknown, self.raw, legacy_native=True)["reason"], "legacy_native_format")
        self.response.write_text(json.dumps({"content": "<tool_call>" + json.dumps(self.action) + "</tool_call>"}))
        self.assertEqual(coordinates.resolve_coordinate_info(unknown, self.raw)["reason"], "native_response")
        action = {"action": "click", "coordinate": [500, 900]}
        contract = coordinates.edit_contract(coordinates.resolve_coordinate_info(unknown, self.raw), action, self.image)
        reopened = {**unknown, "action": action, "coordinate_info": contract}
        self.assertEqual(coordinates.resolve_coordinate_info(reopened, self.raw)["space"], "normalized")

    def test_new_contract_bound_to_action_image_and_actual_canvas(self):
        info = coordinates.resolve_coordinate_info(self.row, self.raw)
        action = {"action": "swipe", "start_coordinate": [100, 200], "end_coordinate": [2000, 3500]}
        contract = coordinates.edit_contract(info, action, self.image)
        info = coordinates.resolve_coordinate_info(self.row, self.raw, current_action=action, saved_contract=contract)
        self.assertEqual(info["space"], "pixel")
        reopened = {**self.row, "action": action, "coordinate_info": json.dumps(contract)}
        self.assertEqual(coordinates.resolve_coordinate_info(reopened, self.raw)["space"], "pixel")
        for bad in ({**contract, "width": 999}, {**contract, "image": "other.jpg"}, {**contract, "source_action": {"action": "wait"}}):
            self.assertEqual(coordinates.resolve_coordinate_info(reopened, self.raw, saved_contract=bad)["space"], "unknown")
        with self.assertRaises(ValueError):
            coordinates.edit_contract(info, {"action": "click", "coordinate": [2160, 0]}, self.image)

    def publish(self):
        payload = {"sheets": {"VLA trajectories": [self.row]}}
        store = ArtifactStore(self.root)
        manifest = store.publish("batch", "02_annotation", payload, metadata={"raw_root": str(self.raw)})
        return store, manifest

    def test_batch_lookup_registered_identity_cached_and_versioned(self):
        store, manifest = self.publish()
        with patch.object(coordinates, "resolve_batch_context", wraps=coordinates.resolve_batch_context) as resolve:
            for _ in range(2):
                result = coordinates.batch_coordinate_info("batch", manifest["version"], "trajectory", self.image, root=self.root)
                self.assertEqual(result["coordinate_info"]["width"], 2160)
            self.assertEqual(resolve.call_count, 1)
            with self.assertRaises(FileNotFoundError):
                coordinates.batch_coordinate_info("batch", manifest["version"], "trajectory", "../../secret.jpg", root=self.root)
            store.publish("batch", "02_annotation", {"sheets": {"VLA trajectories": [{**self.row, "summary": "new"}]}}, metadata={"raw_root": str(self.raw)})
            with self.assertRaises(AnnotationVersionConflict):
                coordinates.batch_coordinate_info("batch", manifest["version"], "trajectory", self.image, root=self.root)

    def test_cold_batch_index_single_flight_for_concurrent_steps(self):
        _, manifest = self.publish()
        resolve = coordinates.resolve_batch_context
        ready = threading.Barrier(8)
        def slow_resolve(*args):
            time.sleep(.1)
            return resolve(*args)
        def request(_):
            ready.wait(timeout=5)
            return coordinates.batch_coordinate_info("batch", manifest["version"], "trajectory", self.image, root=self.root)
        with patch.object(coordinates, "resolve_batch_context", side_effect=slow_resolve) as call:
            with ThreadPoolExecutor(max_workers=8) as pool:
                results = list(pool.map(request, range(8)))
        self.assertEqual(call.call_count, 1)
        self.assertTrue(all(result["coordinate_info"]["width"] == 2160 for result in results))

    def create_session(self):
        self.source = self.root / "input.xlsx"
        book = Workbook()
        sheet = book.active
        sheet.append(["文件夹名", "image", "action", "agent_type", "summary", "actions_box"])
        sheet.append(["trajectory", self.image, json.dumps(self.action), "pipelineagent", "original", ""])
        book.save(self.source)
        book.close()
        self.snapshot = load_snapshot(self.source, asset_root=self.raw, allow_excel_import=True)
        self.sid = "b" * 16
        for patcher in (patch.object(draft_store, "CORRECTION_SESSIONS_DIR", self.root / "sessions"),
                        patch.object(service, "_source_for_session", return_value=(self.source, self.raw))):
            patcher.start()
            self.addCleanup(patcher.stop)
        session = {"session_id": self.sid, "snapshot_payload": self.snapshot,
                   "row_edits": {}, "cot": {}, "group_exports": {"group_0": True}}
        return draft_store.save_session(session)

    def test_revision_and_noop_preserve_original_pixel_action(self):
        session = self.create_session()
        revision = session["storage_revision"]
        info = service.get_row_coordinate_info(self.sid, 2, revision)
        self.assertEqual(info["storage_revision"], revision)
        self.assertEqual(info["coordinate_info"]["width"], 2160)
        original = self.snapshot["groups"][0]["rows"][0]["actions"]
        result = service.patch_row(self.sid, 2, {"actions": json.dumps(self.action, separators=(",", ":")), "sop": "edited", "expected_revision": revision})
        self.assertEqual(result["row"]["actions"], original)
        self.assertNotIn("actions", draft_store.load_session(self.sid)["row_edits"]["2"])
        self.assertEqual(result["row"]["coordinate_info"]["space"], "pixel")
        self.assertEqual(result["row"]["coordinate_info_revision"], result["storage_revision"])
        self.assertEqual(result["group"]["storage_revision"], result["storage_revision"])
        with self.assertRaises(RevisionConflict):
            service.get_row_coordinate_info(self.sid, 2, revision)

    def test_existing_group_route_embeds_only_selected_rows_outside_lock(self):
        session = self.create_session()
        group_id = self.snapshot["groups"][0]["group_id"]
        # A legacy workbook value must not be copied as if already verified.
        session["snapshot_payload"]["groups"][0]["rows"][0]["coordinate_info"] = '{"space":"normalized"}'
        other = deepcopy(session["snapshot_payload"]["groups"][0])
        other["group_id"] = "not-opened"
        other["meta_task"] = "other-trajectory"
        other["rows"][0]["excel_row"] = 3
        other["rows"][0]["meta_task"] = "other-trajectory"
        other["rows"][0]["image"] = "task/other-trajectory/step001_vla_input.jpg"
        session["snapshot_payload"]["groups"].append(other)
        session = draft_store.save_session(session)
        lock_depth = 0
        original_lock = service.active_session_lock
        @contextmanager
        def tracked_lock(sid):
            nonlocal lock_depth
            with original_lock(sid) as current:
                lock_depth += 1
                try:
                    yield current
                finally:
                    lock_depth -= 1
        original_resolve = service.resolve_coordinate_info
        def resolve(*args, **kwargs):
            self.assertEqual(lock_depth, 0, "image/evidence I/O held the batch lock")
            return original_resolve(*args, **kwargs)
        with patch.object(service, "active_session_lock", tracked_lock), patch.object(service, "resolve_coordinate_info", side_effect=resolve) as called:
            result = service.get_group(self.sid, group_id)
            self.assertEqual(called.call_count, 1)
        self.assertEqual(result["storage_revision"], session["storage_revision"])
        row = result["rows"][0]
        self.assertEqual(row["coordinate_info"]["space"], "pixel")
        self.assertEqual(row["coordinate_info_version"], 1)
        self.assertEqual(row["coordinate_info_revision"], result["storage_revision"])
        with patch.object(service, "resolve_coordinate_info") as called:
            service.get_groups(self.sid)
            service.get_cot(self.sid)
            called.assert_not_called()

    def test_group_rejects_revision_changed_while_evidence_is_read(self):
        self.create_session()
        group_id = self.snapshot["groups"][0]["group_id"]
        original = service.resolve_coordinate_info
        def changed(*args, **kwargs):
            current = draft_store.load_session(self.sid)
            draft_store.save_session(current)
            return original(*args, **kwargs)
        with patch.object(service, "resolve_coordinate_info", side_effect=changed):
            with self.assertRaises(RevisionConflict):
                service.get_group(self.sid, group_id)

    def test_group_missing_source_keeps_text_and_does_not_authorize_coordinates(self):
        self.create_session()
        group_id = self.snapshot["groups"][0]["group_id"]
        with patch.object(service, "_source_for_session", side_effect=FileNotFoundError("source removed")):
            result = service.get_group(self.sid, group_id)
            self.assertEqual(result["rows"][0]["coordinate_info"]["space"], "unknown")
            saved = service.patch_row(self.sid, 2, {"sop": "text still saved"})
            self.assertEqual(saved["row"]["sop"], "text still saved")
            self.assertEqual(saved["row"]["coordinate_info"]["space"], "unknown")
            with self.assertRaisesRegex(ValueError, "坐标来源"):
                service.patch_row(self.sid, 2, {"actions": '{"action":"click","coordinate":[20,30]}'})

    def test_patch_coordinate_reads_are_unlocked_and_late_edits_do_not_fail_save(self):
        self.create_session()
        original = service.resolve_coordinate_info
        def changed(*args, **kwargs):
            current = draft_store.load_session(self.sid)
            current["row_edits"]["2"]["sop"] = "newer text"
            draft_store.save_session(current)
            return original(*args, **kwargs)
        with patch.object(service, "resolve_coordinate_info", side_effect=changed):
            result = service.patch_row(self.sid, 2, {"sop": "saved text"})
        self.assertEqual(result["row"]["sop"], "saved text")
        self.assertEqual(result["row"]["coordinate_info"]["reason"], "session_changed_reload_required")
        self.assertEqual(draft_store.load_session(self.sid)["row_edits"]["2"]["sop"], "newer text")

    def test_unknown_blocks_only_coordinate_changes(self):
        self.create_session()
        self.response.unlink()
        service.patch_row(self.sid, 2, {"sop": "still editable", "actions": json.dumps(self.action)})
        with self.assertRaisesRegex(ValueError, "坐标来源"):
            service.patch_row(self.sid, 2, {"actions": '{"action":"click","coordinate":[20,30]}'})
        result = service.patch_row(self.sid, 2, {"actions": '{"action":"type","text":"still editable"}'})
        self.assertEqual(result["row"]["action"]["text"], "still editable")

    def test_noncoordinate_action_keeps_source_canvas_for_later_point_edit(self):
        self.create_session()
        service.patch_row(self.sid, 2, {"actions": '{"action":"type","text":"hello"}'})
        self.assertEqual(service.get_row_coordinate_info(self.sid, 2)["coordinate_info"]["space"], "pixel")
        service.patch_row(self.sid, 2, {"actions": '{"action":"click","coordinate":[1500,3000]}'})
        self.assertEqual(service.get_row_coordinate_info(self.sid, 2)["coordinate_info"]["width"], 2160)
        # A historical unbound edit is not promoted merely because its source
        # step was originally Pipeline. It still needs the original contract.
        session = draft_store.load_session(self.sid)
        session["row_edits"]["2"] = {"actions": '{"action":"type","text":"old"}'}
        draft_store.save_session(session)
        self.assertEqual(service.get_row_coordinate_info(self.sid, 2)["coordinate_info"]["space"], "unknown")
        with self.assertRaisesRegex(ValueError, "坐标来源"):
            service.patch_row(self.sid, 2, {"actions": '{"action":"click","coordinate":[1500,3000]}'})

    def test_pixel_edit_persist_export_and_reopen_both_workbooks(self):
        self.create_session()
        action = {"action": "long_press", "coordinate": [1900, 3600], "duration": 2}
        service.patch_row(self.sid, 2, {"actions": json.dumps(action)})
        saved = draft_store.load_session(self.sid)
        self.assertEqual(saved["row_edits"]["2"]["coordinate_info"]["width"], 2160)
        self.assertEqual(service.get_row_coordinate_info(self.sid, 2)["coordinate_info"]["space"], "pixel")
        contract = deepcopy(saved["row_edits"]["2"]["coordinate_info"])
        service.patch_row(self.sid, 2, {"sop": "only text", "actions": json.dumps(action)})
        saved = draft_store.load_session(self.sid)
        self.assertEqual(saved["row_edits"]["2"]["coordinate_info"], contract)
        self.assertEqual(service.get_cot(self.sid)["groups"][0]["rows"][0]["agent_type"], "pipelineagent")
        for function in (export_full_dataset_workbook, export_session_workbook):
            result = function(workbook_path=self.source, snapshot=self.snapshot, session=saved,
                              output_dir=self.root / "exports", export_id=function.__name__)
            snapshot = load_snapshot(self.root / "exports" / result["filename"], asset_root=self.raw, allow_excel_import=True)
            row = snapshot["groups"][0]["rows"][0]
            self.assertEqual(coordinates.resolve_coordinate_info(row, self.raw)["space"], "pixel")
            self.assertEqual(json.loads(row["actions"]), action)

    def test_human80_schema_normalized_but_unknown_external_not_guessed(self):
        session = self.create_session()
        self.response.unlink()
        row = session["snapshot_payload"]["groups"][0]["rows"][0]
        row.pop("agent_type", None)
        session["snapshot_payload"]["headers"] = ["task", "meta_task", "actions", "task_manual_result", "micro_manual", "macro_manual"]
        draft_store.save_session(session)
        self.assertEqual(service.get_row_coordinate_info(self.sid, 2)["coordinate_info"]["space"], "normalized")
        # The schema compatibility branch never overrides an explicit Pipeline.
        session = draft_store.load_session(self.sid)
        session["snapshot_payload"]["groups"][0]["rows"][0]["agent_type"] = "pipelineagent"
        draft_store.save_session(session)
        self.assertEqual(service.get_row_coordinate_info(self.sid, 2)["coordinate_info"]["space"], "unknown")


    def test_real_routes_and_stale_revision(self):
        from backend import api
        _, annotation = self.publish()
        session = self.create_session()
        app = FastAPI()
        app.add_api_route("/api/coordinate-info", api.coordinate_info, methods=["POST"])
        app.include_router(router)
        with patch.object(api, "DATA_ROOT", self.root), TestClient(app) as client:
            response = client.post("/api/coordinate-info", json={"batch_id": "batch", "annotation_version": annotation["version"], "trajectory_id": "trajectory", "image": self.image})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["coordinate_info"]["height"], 3840)
            url = f"/api/correction/sessions/{self.sid}/rows/2/coordinate-info"
            response = client.get(url, params={"expected_revision": session["storage_revision"]})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(client.get(url, params={"expected_revision": -1}).status_code, 409)

    def test_existing_task_http_route_works_when_new_coordinate_route_is_unavailable(self):
        from fastapi.responses import HTMLResponse
        session = self.create_session()
        app = FastAPI()
        app.include_router(router)
        @app.middleware("http")
        async def old_gateway(request, call_next):
            if request.url.path.endswith("/coordinate-info"):
                return HTMLResponse("<html>Not Found</html>", status_code=404)
            return await call_next(request)
        with TestClient(app) as client:
            self.assertEqual(client.get(f"/api/correction/sessions/{self.sid}/rows/2/coordinate-info").status_code, 404)
            group_id = self.snapshot["groups"][0]["group_id"]
            response = client.get(f"/api/correction/sessions/{self.sid}/tasks/{group_id}")
            self.assertEqual(response.status_code, 200, response.text)
            group = response.json()["group"]
            self.assertEqual(group["storage_revision"], session["storage_revision"])
            self.assertEqual(group["rows"][0]["coordinate_info"]["width"], 2160)
            saved = client.patch(f"/api/correction/sessions/{self.sid}/rows/2", json={"sop": "text", "expected_revision": group["storage_revision"]})
            self.assertEqual(saved.status_code, 200, saved.text)
            self.assertEqual(saved.json()["row"]["coordinate_info"]["space"], "pixel")


    def publish_tree(self):
        from backend.batch_results import annotation_task_fingerprints
        store, annotation = self.publish()
        payload = {'sheets': {'VLA trajectories': [self.row]}}
        tree_payload = {'trees': {'task': {'id': 0, 'children': [], 'task_id': 'task'}},
                        'source_task_fingerprints': annotation_task_fingerprints(payload)}
        tree = store.publish('batch', '04_tree', tree_payload, metadata={'task_ids': ['task', 'other-task']})
        return store, annotation, tree

    def test_tree_coordinate_existing_route_avoids_tree_rebuild_and_checks_scope(self):
        from backend import batch_api
        store, annotation, tree = self.publish_tree()
        app = FastAPI(); app.include_router(batch_api.router)
        url = '/api/data-batches/batch/tasks/task/tree'
        query = {'view': 'coordinate', 'annotation_version': annotation['version'], 'tree_version': tree['version'],
                 'trajectory_id': 'trajectory', 'image': self.image}
        with patch.object(batch_api, 'store', store), TestClient(app) as client:
            initial = client.get(url)
            self.assertEqual(initial.status_code, 200, initial.text)
            self.assertEqual(initial.json()['annotation_version'], annotation['version'])
            self.assertEqual(initial.json()['tree_version'], tree['version'])
            self.assertEqual(initial.json()['coordinate_lookup_version'], 1)
            with patch.object(batch_api, 'current_tree_payload', side_effect=AssertionError('single coordinate cannot rebuild full tree')):
                with patch.object(coordinates, 'resolve_batch_context', wraps=coordinates.resolve_batch_context) as lookup:
                    for _ in range(2):
                        response = client.get(url, params=query)
                        self.assertEqual(response.status_code, 200, response.text)
                        self.assertEqual(response.json()['coordinate_info']['width'], 2160)
                        self.assertEqual(response.json()['task_id'], 'task')
                        self.assertEqual(response.headers['cache-control'], 'no-store')
                    self.assertEqual(lookup.call_count, 1)
                self.assertEqual(client.get(url, params={**query, 'tree_version': 'old'}).status_code, 409)
                self.assertEqual(client.get(url, params={**query, 'annotation_version': 'old'}).status_code, 409)
                self.assertEqual(client.get(url, params={**query, 'image': '../../missing.jpg'}).status_code, 404)
                self.assertEqual(client.get(url, params={'view': 'coordinate'}).status_code, 422)
                self.assertEqual(client.get(url.replace('/task/', '/other-task/'), params=query).status_code, 404)
                self.response.unlink()
                response = client.get(url, params=query)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()['coordinate_info']['space'], 'unknown')

    def test_tree_coordinate_rejects_late_artifact_change(self):
        store, annotation, tree = self.publish_tree()
        original = coordinates.resolve_coordinate_info
        def changed(*args, **kwargs):
            store.publish('batch', '04_tree', {'trees': {}}, metadata={'task_ids': []})
            return original(*args, **kwargs)
        with patch.object(coordinates, 'resolve_coordinate_info', side_effect=changed):
            with self.assertRaises(AnnotationVersionConflict):
                coordinates.tree_coordinate_info('batch', 'task', annotation['version'], tree['version'], 'trajectory', self.image, root=self.root)

    def test_tree_route_survives_gateway_without_new_coordinate_post(self):
        from backend import batch_api
        from fastapi.responses import HTMLResponse
        store, annotation, tree = self.publish_tree()
        app = FastAPI(); app.include_router(batch_api.router)
        @app.middleware('http')
        async def gateway(request, call_next):
            if request.url.path == '/api/coordinate-info':
                return HTMLResponse('<html>Not Found</html>', status_code=404)
            return await call_next(request)
        with patch.object(batch_api, 'store', store), TestClient(app) as client:
            self.assertEqual(client.post('/api/coordinate-info').status_code, 404)
            response = client.get('/api/data-batches/batch/tasks/task/tree', params={'view': 'coordinate', 'annotation_version': annotation['version'], 'tree_version': tree['version'], 'trajectory_id': 'trajectory', 'image': self.image})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()['coordinate_info']['height'], 3840)
