"""Short publication identities retain retry recovery and legacy reads."""
import hashlib
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from backend import pipeline_release as publishing
from backend.data_store import RecordStore, RevisionConflict
from backend.identifiers import ID_SCHEME, batch_uses_short_ids
from backend.pipelines import PipelineManager
from backend.tests.test_pipelines import OfflineRuntime
from backend.tests import test_pipeline_release as release_fixtures
from backend.tests import test_external_dataset_imports as external_fixtures
from backend.tests.test_trajectory_correction import write_correction_workbook
from backend.trajectory_correction import draft_store
from backend.trajectory_correction.exporter import export_full_dataset_workbook
from backend.trajectory_correction.workbook import load_snapshot


class ShortPipelineIdentityTests(unittest.TestCase):
    def test_create_restart_and_old_request_replay_keep_identity(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            data = {"request_id": "create", "name": "short", "batch_id": "old-batch", "mode": "automatic",
                    "start_mode": "existing", "threshold": 3}
            first = PipelineManager(OfflineRuntime(root), root).create(data)
            self.assertRegex(first["pipeline_id"], r"^p[0-9]{6}-[0-9]{3,}$")
            self.assertEqual(first["id_scheme"], ID_SCHEME)
            second = PipelineManager(OfflineRuntime(root), root).create(data)
            self.assertEqual(first["pipeline_id"], second["pipeline_id"])
            self.assertEqual(len(RecordStore(root).list("pipelines")), 1)

    def test_short_sessions_and_legacy_sessions_are_both_readable(self):
        with TemporaryDirectory() as directory, patch.object(draft_store, "CORRECTION_SESSIONS_DIR", Path(directory) / "sessions"):
            short = draft_store.new_session_id()
            self.assertRegex(short, r"^s[0-9]{6}-[0-9]{3,}$")
            for sid in (short, "a" * 16):
                draft_store.save_session({"session_id": sid, "batch_id": "legacy-batch"})
                self.assertEqual(draft_store.load_session(sid)["session_id"], sid)
            for invalid in ("../s261015-001", "s261015-001/extra", "s261015-0"):
                with self.assertRaises(ValueError):
                    draft_store.load_session(invalid)

    def test_same_day_exports_keep_full_identity_and_do_not_overwrite(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.xlsx"
            write_correction_workbook(source)
            snapshot = load_snapshot(source, allow_excel_import=True)
            exports = [export_full_dataset_workbook(workbook_path=source, snapshot=snapshot, session={},
                output_dir=root / "exports", export_id=identifier) for identifier in ("e261015-001", "e261015-002")]
            self.assertNotEqual(exports[0]["filename"], exports[1]["filename"])
            for result, identifier in zip(exports, ("e261015-001", "e261015-002")):
                self.assertIn(identifier, result["filename"])
                self.assertTrue((root / "exports" / result["filename"]).is_file())
            self.assertEqual(list((root / "exports").glob("*.tmp")), [])


class ShortPipelinePublicationTests(unittest.TestCase):
    setUp = release_fixtures.PipelineReleaseTests.setUp
    seed = release_fixtures.PipelineReleaseTests.seed
    pipeline = release_fixtures.PipelineReleaseTests.pipeline
    publish = release_fixtures.PipelineReleaseTests.publish

    def test_legacy_pipeline_keeps_deterministic_release_id(self):
        pipeline = self.pipeline()
        expected = "rel_" + hashlib.sha256(("pipeline:" + pipeline["pipeline_id"]).encode()).hexdigest()[:24]
        self.assertEqual(self.publish(pipeline)["release_id"], expected)

    def test_failed_commit_recovers_same_short_release_and_full_frozen_files(self):
        pipeline = {**self.pipeline(), "id_scheme": ID_SCHEME}
        original = RecordStore.put_many
        def fail_before_commit(records, entries, **kwargs):
            if any(item["namespace"] == "dataset_releases" for item in entries):
                raise OSError("offline commit interruption")
            return original(records, entries, **kwargs)
        with patch.object(RecordStore, "put_many", fail_before_commit):
            with self.assertRaises(OSError):
                self.publish(pipeline)
        intent = self.records.get("release_intents", "pipeline:" + pipeline["pipeline_id"])
        self.assertRegex(intent["release_id"], r"^rel[0-9]{6}-[0-9]{3,}$")
        self.assertEqual(self.registry.list_releases(), [])
        with self.assertRaises(RevisionConflict):
            self.publish({**pipeline, "name": "changed selection request"})
        # A retry on a later day/restarted process must use the durable intent,
        # without even asking the daily allocator for another release number.
        with patch.object(publishing, "allocate_id", side_effect=AssertionError("retry allocated a new ID")):
            release = self.publish(pipeline)
        self.assertEqual(release["release_id"], intent["release_id"])
        self.assertEqual(self.publish(pipeline)["release_id"], release["release_id"])
        self.assertTrue(self.registry.excel_file(release["release_id"], 0)[0].is_file())
        self.assertEqual(len(self.records.list("dataset_releases")), 1)

    def test_preupgrade_intent_is_authoritative(self):
        pipeline = {**self.pipeline(), "id_scheme": ID_SCHEME}
        self.records.put("release_intents", "pipeline:" + pipeline["pipeline_id"], {"release_id": "rel_0123456789abcdef"})
        self.assertEqual(publishing._pipeline_release_id(pipeline, self.root), "rel_0123456789abcdef")


class ShortExternalPublicationTests(unittest.TestCase):
    setUp = external_fixtures.ExternalDatasetImportTests.setUp
    tearDown = external_fixtures.ExternalDatasetImportTests.tearDown
    workbook = external_fixtures.ExternalDatasetImportTests.workbook
    preview = external_fixtures.ExternalDatasetImportTests.preview
    publish = external_fixtures.ExternalDatasetImportTests.publish

    def test_new_ids_and_duplicate_upload_keep_one_publication(self):
        preview = self.preview()
        self.assertRegex(preview["import_id"], r"^i[0-9]{6}-[0-9]{3,}$")
        first = self.publish(preview, request_id="publish-short")
        self.assertRegex(first["release_id"], r"^rel[0-9]{6}-[0-9]{3,}$")
        self.assertTrue(batch_uses_short_ids(first["batch_ids"][0], self.data))
        self.assertEqual(self.records.get("batch_identities", first["batch_ids"][0])["source_kind"], "external_manual")
        self.assertEqual(first["source_kind"], "external_manual")
        self.assertEqual(self.publish(preview, request_id="publish-short")["release_id"], first["release_id"])
        self.assertEqual(self.publish(self.preview())["release_id"], first["release_id"])
        self.assertEqual(len(self.records.list("dataset_releases")), 1)

    def test_old_import_and_prepared_identifiers_continue_unchanged(self):
        preview = self.preview()
        original = self.records.get("dataset_release_imports", preview["import_id"])
        legacy = "imp_" + "a" * 32
        self.importer._directory(preview["import_id"]).rename(self.importer._directory(legacy))
        record = {**original, "import_id": legacy, "target_release_id": "rel_" + "b" * 16,
                  "target_batch_id": "external_" + "c" * 32}
        self.records.put("dataset_release_imports", legacy, record)
        release = self.publish({"import_id": legacy}, request_id="legacy-import")
        self.assertEqual(release["release_id"], record["target_release_id"])
        self.assertEqual(release["batch_ids"], [record["target_batch_id"]])

    def test_external_retry_uses_saved_targets_without_new_day_allocation(self):
        preview = self.preview()
        with patch.object(self.importer.records, "put_many", side_effect=OSError("interrupted")):
            with self.assertRaises(OSError):
                self.publish(preview, request_id="same-request")
        record = self.records.get("dataset_release_imports", preview["import_id"])
        with patch("backend.data_publishing.external_imports.allocate_id", side_effect=AssertionError("retry allocated a new ID")):
            release = self.publish(preview, request_id="same-request")
        self.assertEqual(release["release_id"], record["target_release_id"])
        self.assertEqual(release["batch_ids"], [record["target_batch_id"]])


if __name__ == "__main__":
    unittest.main()
