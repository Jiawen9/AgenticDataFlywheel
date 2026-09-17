from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend import data_registry_api
from backend.data_store import ArtifactStore


class DataRegistryApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "data"
        self.store = ArtifactStore(self.root)
        self.patcher = patch.object(data_registry_api, "store", self.store)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        app = FastAPI()
        app.include_router(data_registry_api.router)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def snapshot(self):
        return {
            path.relative_to(self.root): (path.read_bytes(), path.stat().st_mtime_ns)
            for path in self.root.rglob("*") if path.is_file()
        }

    def artifact_url(self, manifest):
        return f'/api/data-batches/{manifest["batch_id"]}/artifacts/{manifest["stage"]}/{manifest["version"]}'

    def test_missing_reads_do_not_create_store(self):
        self.assertEqual(self.client.get("/api/data-batches").json(), {"batches": []})
        info = self.client.get("/api/data-storage").json()
        self.assertEqual(info["data_root"], str(self.root))
        self.assertEqual(info["database"], "system/app.sqlite")
        self.assertEqual(info["storage_mode"], "new_only")
        self.assertNotIn("legacy_root", info)
        self.assertEqual(self.client.get("/api/data-batches/missing/artifacts").status_code, 404)
        self.assertEqual(self.client.get("/api/data-batches/missing/artifacts/stage/version").status_code, 404)
        self.assertFalse(self.root.exists())

    def test_lists_versions_and_downloads_exact_json_excel_without_writing(self):
        payload = {"steps": [{"summary": "", "thought": "保留全文", "task": "=literal"}]}
        first = self.store.publish("batch-a", "01_conversion", payload, tables={"步骤": payload["steps"]})
        self.store.publish("batch-a", "02_annotation", {"marked": True})
        self.store.publish("batch-a", "02_annotation", {"marked": False})
        self.store.publish("batch-b", "04_tree", {"nodes": []})
        before = self.snapshot()
        batches = self.client.get("/api/data-batches").json()["batches"]
        self.assertEqual(len(batches), 2)
        batch = next(item for item in batches if item["batch_id"] == "batch-a")
        self.assertEqual(batch["version_count"], 3)
        self.assertEqual(batch["stages"], ["01_conversion", "02_annotation"])
        listed = self.client.get("/api/data-batches/batch-a/artifacts").json()["artifacts"]
        self.assertEqual(len(listed), 3)
        url = self.artifact_url(first)
        self.assertEqual(self.client.get(url).json(), first)
        for file in first["files"]:
            response = self.client.get(url + "/files/" + file["name"])
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers["cache-control"], "no-store")
            self.assertEqual(hashlib.sha256(response.content).hexdigest(), file["sha256"])
            self.assertEqual(response.content, (self.root / file["path"]).read_bytes())
            if file["kind"] == "json":
                self.assertEqual(response.json(), payload)
        self.assertEqual(self.snapshot(), before)

    def test_missing_file_and_checksum_mismatch_are_explicit(self):
        manifest = self.store.publish("batch", "stage", {"value": "original"})
        url = self.artifact_url(manifest)
        self.assertEqual(self.client.get(url + "/files/missing.xlsx").status_code, 404)
        path = self.store.resolve_file(manifest, "result.json")
        path.write_text('{"value":"tampered"}', encoding="utf-8")
        self.assertEqual(self.client.get(url + "/files/result.json").status_code, 409)
        path.unlink()
        self.assertEqual(self.client.get(url + "/files/result.json").status_code, 404)

    def test_traversal_and_manifest_escape_are_rejected(self):
        manifest = self.store.publish("batch", "stage", {"value": "safe"})
        url = self.artifact_url(manifest)
        self.assertEqual(self.client.get("/api/data-batches/bad%5Cid/artifacts").status_code, 422)
        self.assertEqual(self.client.get("/api/data-batches/bad%5Cid/artifacts/stage/version").status_code, 409)
        self.assertEqual(self.client.get(url + "/files/..%5Coutside.json").status_code, 409)
        outside = self.root.parent / "outside.json"
        outside.write_text("private", encoding="utf-8")
        manifest["files"][0]["path"] = "../outside.json"
        manifest["files"][0]["sha256"] = hashlib.sha256(outside.read_bytes()).hexdigest()
        manifest["files"][0]["size"] = outside.stat().st_size
        self.store.records.put("artifacts", f'batch/stage/{manifest["version"]}', manifest)
        before = outside.read_bytes()
        self.assertEqual(self.client.get(url + "/files/result.json").status_code, 409)
        self.assertEqual(outside.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
