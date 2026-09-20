from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.data_store import ArtifactStore, RecordStore, RevisionConflict


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def pair(value):
    return [
        {"stage": "03_observation", "payload": {"generation": value},
         "tables": {"Observation": [{"generation": value}]}},
        {"stage": "04_tree", "payload": {"generation": value},
         "source_stages": ["03_observation"]},
    ]


class SingleStageArtifactTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "data"
        self.store = ArtifactStore(self.root)

    def assert_no_transactions(self):
        self.assertEqual(list(self.root.glob("tmp/artifact_transactions/*/*")), [])

    def assert_pair(self, value, revision):
        restarted = ArtifactStore(self.root)
        manifests = [restarted.get("batch", stage) for stage in ("03_observation", "04_tree")]
        for manifest in manifests:
            self.assertEqual(restarted.read_payload(manifest), {"generation": value})
            self.assertEqual(manifest["revision"], revision)
            self.assertEqual(Path(manifest["files"][0]["path"]).parent,
                             Path("batches") / "batch" / manifest["stage"])
            self.assertFalse(any(path.is_dir() for path in (self.root / "batches" / "batch" / manifest["stage"]).iterdir()))
        self.assertEqual(manifests[1]["source_refs"][0]["version"], manifests[0]["version"])
        self.assertEqual(len(restarted.list(batch_id="batch")), 2)
        self.assert_no_transactions()

    def test_identical_payload_tables_and_sources_are_idempotent_without_rewriting_files(self):
        first = self.store.publish_many("batch", pair("first"))
        before = {path.relative_to(self.root): (path.read_bytes(), path.stat().st_mtime_ns)
                  for path in self.root.rglob("*") if path.is_file()}
        repeated = self.store.publish_many("batch", pair("first"))
        after = {path.relative_to(self.root): (path.read_bytes(), path.stat().st_mtime_ns)
                 for path in self.root.rglob("*") if path.is_file()}
        self.assertEqual(repeated, first)
        self.assertEqual(after, before)
        self.assert_pair("first", 1)

    def test_job_metadata_does_not_create_another_artifact_but_meaningful_config_does(self):
        first = self.store.publish("batch", "stage", {"A": 1}, metadata={"job_id": "first", "model": "a"})
        repeated = self.store.publish("batch", "stage", {"A": 1}, metadata={"job_id": "retry", "model": "a"})
        self.assertEqual(repeated, first)
        changed = self.store.publish("batch", "stage", {"A": 1}, metadata={"job_id": "next", "model": "b"})
        self.assertEqual(changed["revision"], 2)
        self.assertEqual(len(self.store.list()), 1)

    def test_stale_compare_and_swap_cannot_replace_or_reuse_newer_current_data(self):
        first = self.store.publish("batch", "stage", {"A": 1})
        second = self.store.publish("batch", "stage", {"A": 2}, expected_version=first["version"])
        for payload in ({"A": 1}, {"A": 2}):
            with self.assertRaises(RevisionConflict):
                self.store.publish("batch", "stage", payload, expected_version=first["version"])
        self.assertEqual(self.store.get("batch", "stage"), second)
        self.assertEqual(self.store.read_payload(second), {"A": 2})
        self.assert_no_transactions()

    def test_pair_rolls_back_files_and_registry_when_database_fails_mid_commit(self):
        before = self.store.publish_many("batch", pair("before"))
        previous_bytes = {file["path"]: (self.root / file["path"]).read_bytes()
                          for manifest in before for file in manifest["files"]}
        real_save = RecordStore._save

        def fail_tree(connection, namespace, key, payload, revision):
            if namespace == "artifacts" and key == "batch/04_tree":
                raise RuntimeError("simulated failure after observation record write")
            return real_save(connection, namespace, key, payload, revision)

        with patch.object(RecordStore, "_save", side_effect=fail_tree):
            with self.assertRaisesRegex(RuntimeError, "observation record"):
                self.store.publish_many("batch", pair("after"))
        self.assertEqual([self.store.get("batch", item["stage"]) for item in before], before)
        for relative, content in previous_bytes.items():
            self.assertEqual((self.root / relative).read_bytes(), content)
        self.assert_pair("before", 1)
        self.store.publish_many("batch", pair("after"))
        self.assert_pair("after", 2)

    def _processes(self, code, arguments):
        processes = [subprocess.Popen([sys.executable, "-c", textwrap.dedent(code), str(self.root), *args],
                                      cwd=PROJECT_ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                      text=True, encoding="utf-8") for args in arguments]
        try:
            values = []
            for process in processes:
                output, error = process.communicate(timeout=30)
                self.assertEqual(process.returncode, 0, error)
                values.append(json.loads(output))
            return values
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                    process.communicate(timeout=5)

    def test_separate_process_publishers_serialize_replacements_without_lost_revisions(self):
        self.store.publish("batch", "stage", {"writer": "initial"})
        results = self._processes('''
            import json, sys
            from pathlib import Path
            from backend.data_store import ArtifactStore
            saved = ArtifactStore(Path(sys.argv[1])).publish("batch", "stage", {"writer": sys.argv[2]})
            print(json.dumps({"revision": saved["revision"], "writer": sys.argv[2]}))
        ''', [[str(index)] for index in range(4)])
        self.assertEqual(sorted(item["revision"] for item in results), [2, 3, 4, 5])
        current = self.store.get("batch", "stage")
        winner = next(item["writer"] for item in results if item["revision"] == 5)
        self.assertEqual(self.store.read_payload(current), {"writer": winner})
        self.assertEqual(len(self.store.list()), 1)
        self.assert_no_transactions()

    def test_separate_process_cas_has_one_winner_and_keeps_its_payload(self):
        first = self.store.publish("batch", "stage", {"writer": "initial"})
        results = self._processes('''
            import json, sys
            from pathlib import Path
            from backend.data_store import ArtifactStore, RevisionConflict
            try:
                saved = ArtifactStore(Path(sys.argv[1])).publish("batch", "stage", {"writer": sys.argv[2]}, expected_version=sys.argv[3])
                print(json.dumps({"ok": True, "writer": sys.argv[2], "revision": saved["revision"]}))
            except RevisionConflict:
                print(json.dumps({"ok": False}))
        ''', [[str(index), first["version"]] for index in range(4)])
        winners = [item for item in results if item["ok"]]
        self.assertEqual(len(winners), 1)
        self.assertEqual(winners[0]["revision"], 2)
        self.assertEqual(self.store.read_payload(self.store.get("batch", "stage")), {"writer": winners[0]["writer"]})
        self.assert_no_transactions()

    def _crash_publication(self, phase):
        code = '''
            import os, sys
            from pathlib import Path
            from backend.data_store import ArtifactStore
            store = ArtifactStore(Path(sys.argv[1]))
            phase = sys.argv[2]
            if phase == "after_replace":
                original = Path.rename
                def interrupted(source, target):
                    result = original(source, target)
                    if source.parent.name == "prepared" and source.name == "03_observation":
                        os._exit(73)
                    return result
                Path.rename = interrupted
            else:
                original = store.records.put_many
                def interrupted(*args, **kwargs):
                    original(*args, **kwargs)
                    os._exit(74)
                store.records.put_many = interrupted
            store.publish_many("batch", [
                {"stage": "03_observation", "payload": {"generation": "after"}, "tables": {"Observation": [{"generation": "after"}]}},
                {"stage": "04_tree", "payload": {"generation": "after"}, "source_stages": ["03_observation"]},
            ])
            raise AssertionError("fault hook did not execute")
        '''
        process = subprocess.run([sys.executable, "-c", textwrap.dedent(code), str(self.root), phase],
                                 cwd=PROJECT_ROOT, capture_output=True, text=True, encoding="utf-8", timeout=30)
        self.assertEqual(process.returncode, 73 if phase == "after_replace" else 74, process.stderr)
        self.assertEqual(len(list(self.root.glob("tmp/artifact_transactions/batch/*/commit.json"))), 1)

    def test_process_exit_after_first_file_replacement_restores_both_old_stages_on_read(self):
        self.store.publish_many("batch", pair("before"))
        self._crash_publication("after_replace")
        self.assert_pair("before", 1)
        self.store.publish_many("batch", pair("retry"))
        self.assert_pair("retry", 2)

    def test_process_exit_after_database_commit_keeps_both_new_stages_on_read(self):
        self.store.publish_many("batch", pair("before"))
        self._crash_publication("after_commit")
        self.assert_pair("after", 2)
        self.store.publish_many("batch", pair("next"))
        self.assert_pair("next", 3)

    def test_process_exit_during_first_publication_recovers_to_no_artifacts(self):
        self._crash_publication("after_replace")
        restarted = ArtifactStore(self.root)
        restarted.recover()
        self.assertEqual(restarted.list(), [])
        self.assertFalse((self.root / "batches" / "batch" / "03_observation").exists())
        self.assertFalse((self.root / "batches" / "batch" / "04_tree").exists())
        self.assert_no_transactions()
        restarted.publish_many("batch", pair("retry"))
        self.assert_pair("retry", 1)


if __name__ == "__main__":
    unittest.main()
