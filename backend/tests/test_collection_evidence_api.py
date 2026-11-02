from __future__ import annotations

import hashlib
from pathlib import Path
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.phone_factory import PhoneFactoryStore, create_router
from backend.tests import test_collection_receipts as fixtures


class CollectionEvidenceApiTests(unittest.TestCase):
    rewrite = fixtures.ReceiptTests.rewrite

    def setUp(self):
        fixtures.ReceiptTests.setUp(self)
        self.store = PhoneFactoryStore(self.root, run_client_fn=lambda args: self.fail("unexpected collector command"))
        self.store.runtime._transfer = self.manager
        self.addCleanup(self.store.runtime.close)
        app = FastAPI()
        app.include_router(create_router(self.store))
        app.include_router(create_router(self.store, prefix="/api/model-iter", mode="modeliter"))
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        self.base = f"/api/phone-factory/collection-runs/{self.run_id}/artifacts"

    def receive(self):
        key = self.remote.add()
        self.rewrite(key, missing_xml=True)
        self.manager.sync(self.run_id)
        return key

    def file(self, key):
        source = self.manager.artifact_source(self.run_id, key)
        name = next(iter(source["files"]))
        return name, Path(source["root"]) / name

    def records_snapshot(self):
        with self.manager.records._connection() as connection:
            return connection.execute("SELECT namespace,record_key,payload FROM records ORDER BY namespace,record_key").fetchall()

    def test_received_file_list_and_download_are_manifest_bound(self):
        key = self.receive()
        page = self.client.get(self.base, params={"limit": 10, "search": "task-one", "status": "completed"})
        self.assertEqual(page.status_code, 200)
        self.assertEqual((page.json()["total"], page.json()["items"][0]["artifact_id"]), (1, key))
        detail = self.client.get(self.base + "/" + key)
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.json()["receipt_status"], "received")
        self.assertTrue(detail.json()["eligibility"]["evaluation"])
        self.assertFalse(detail.json()["eligibility"]["preprocessing"])
        self.assertEqual(detail.json()["files"], sorted(self.remote.manifests[key]["trajectory"]["files"], key=lambda row: row["path"]))
        name, local = self.file(key)
        downloaded = self.client.get(self.base + "/" + key + "/file", params={"path": name})
        self.assertEqual(downloaded.status_code, 200)
        self.assertEqual(downloaded.content, local.read_bytes())
        self.assertEqual(downloaded.headers["cache-control"], "no-store")
        self.assertEqual(downloaded.headers["x-content-type-options"], "nosniff")
        self.assertIn("attachment", downloaded.headers["content-disposition"])

    def test_traversal_absolute_and_undeclared_files_are_not_downloadable(self):
        key = self.receive()
        name, local = self.file(key)
        undeclared = local.parent / "extra-secret.txt"
        undeclared.write_bytes(b"private local data")
        candidates = ["../secret.txt", "../../system/registry.sqlite3", str(local),
            "C:/Windows/win.ini", "..\\secret.txt", name + "/../secret.txt",
            undeclared.relative_to(Path(self.run["output_dir"])).as_posix()]
        for requested in candidates:
            with self.subTest(path=requested):
                response = self.client.get(self.base + "/" + key + "/file", params={"path": requested})
                self.assertEqual(response.status_code, 404)
                self.assertIn("不在", response.json()["error"])
                self.assertNotIn(b"private local data", response.content)

    def test_same_size_tampering_is_rejected_without_rewriting_receipt(self):
        key = self.receive()
        name, local = self.file(key)
        before = self.records_snapshot()
        original = local.read_bytes()
        modified = bytes([original[0] ^ 1]) + original[1:]
        self.assertEqual(len(original), len(modified))
        self.assertNotEqual(hashlib.sha256(original).digest(), hashlib.sha256(modified).digest())
        local.write_bytes(modified)
        response = self.client.get(self.base + "/" + key + "/file", params={"path": name})
        self.assertEqual(response.status_code, 409)
        self.assertIn("摘要", response.json()["error"])
        self.assertEqual(self.records_snapshot(), before)
        self.assertEqual(local.read_bytes(), modified)

    def test_missing_or_resized_file_is_rejected(self):
        key = self.receive()
        name, local = self.file(key)
        local.write_bytes(b"shortened")
        response = self.client.get(self.base + "/" + key + "/file", params={"path": name})
        self.assertEqual(response.status_code, 409)
        self.assertIn("大小", response.json()["error"])
        local.unlink()
        self.assertEqual(self.client.get(self.base + "/" + key + "/file", params={"path": name}).status_code, 409)

    def test_outcome_has_no_download_and_unreceived_artifacts_are_rejected(self):
        outcome = self.remote.add_outcome("task-one")
        pending = self.remote.add(case="task-two")
        self.remote.fail.add(pending)
        self.manager.sync(self.run_id)
        detail = self.client.get(self.base + "/" + outcome)
        self.assertEqual(detail.status_code, 200)
        self.assertEqual((detail.json()["kind"], detail.json()["files"]), ("outcome", []))
        self.assertEqual(self.client.get(self.base + "/" + outcome + "/file", params={"path": "archive.zip"}).status_code, 404)
        self.assertEqual(self.client.get(self.base + "/" + pending).status_code, 404)
        self.assertEqual(self.client.get(self.base + "/" + pending + "/file", params={"path": "anything"}).status_code, 404)
        self.assertEqual(self.client.get(self.base + "/missing-artifact").status_code, 404)

    def test_published_evidence_remains_read_only_and_independent_of_collector(self):
        key = self.receive()
        name, local = self.file(key)
        self.manager.records.put("batch_lifecycle", "batch-one", {"batch_id": "batch-one", "status": "published"})
        before = self.records_snapshot()
        with patch.object(self.remote, "get_collection_artifact", side_effect=AssertionError("remote read")), patch.object(
                self.remote, "download_collection_artifact", side_effect=AssertionError("remote download")):
            self.assertEqual(self.client.get(self.base).status_code, 200)
            self.assertEqual(self.client.get(self.base + "/" + key).status_code, 200)
            response = self.client.get(self.base + "/" + key + "/file", params={"path": name})
            self.assertEqual((response.status_code, response.content), (200, local.read_bytes()))
        self.assertEqual(self.records_snapshot(), before)

    def test_wrong_mode_and_missing_run_are_rejected(self):
        key = self.receive()
        self.assertEqual(self.client.get(self.base.replace("/phone-factory/", "/model-iter/") + "/" + key).status_code, 404)
        self.assertEqual(self.client.get("/api/phone-factory/collection-runs/missing-run/artifacts").status_code, 404)

    def test_late_stop_observation_cannot_modify_published_source_or_sync(self):
        self.receive()
        self.manager.records.put("batch_lifecycle", "batch-one", {"batch_id": "batch-one", "status": "published"})
        source = self.runs.get(self.run_id)
        before = self.records_snapshot()
        result = self.store.runtime._collection_stopped(self.run_id, {
            "run_id": self.run_id, "status": "cancelled", "collection_status": "cancelled",
            "state_revision": 99, "phone_states": {"phone-one": {"status": "cancelled", "stop_confirmed": True}}})
        self.assertEqual(result, source)
        self.assertEqual(self.runs.get(self.run_id), source)
        self.assertEqual(self.records_snapshot(), before)


if __name__ == "__main__":
    unittest.main()
