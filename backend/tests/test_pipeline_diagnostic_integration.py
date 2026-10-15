"""Real manager integration with offline jobs and a local fake worker only."""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from backend import pipeline_diagnostics as diagnostics, quality_jobs
from backend.data_store import RecordStore
from backend.pipeline_access import internal_pipeline
from backend.pipelines import PipelineManager
from backend.tests.test_pipelines import OfflineRuntime
from backend.tests import test_collection_transfer as collection_fixture
from backend.tests.test_training_overview_service import Registry, row
from backend.training_data_overview.service import TrainingOverviewManager


class PipelineDiagnosticIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def events(self, pipeline_id):
        return [json.loads(line) for path in (self.root / "logs/pipelines" / pipeline_id / "events").glob("*.jsonl")
                for line in path.read_text(encoding="utf-8").splitlines() if line]

    def test_orchestration_keeps_failed_attempt_and_exact_bindings_after_retry(self):
        runtime = OfflineRuntime(self.root)
        manager = PipelineManager(runtime, self.root)
        p = manager.create({"request_id": "r", "name": "日志", "batch_id": "b", "mode": "automatic",
                            "start_mode": "existing", "threshold": 4})
        ident = p["pipeline_id"]
        manager.tick(ident); manager.tick(ident)
        first = runtime.children["preprocessing"][-1]["job_id"]
        runtime.complete("preprocessing", fail=True)
        manager.tick(ident)
        p = manager.get(ident)
        manager.control(ident, "retry", p["storage_revision"])
        manager.tick(ident)
        second = runtime.children["preprocessing"][-1]["job_id"]
        self.assertNotEqual(first, second)
        index = json.loads((self.root / "logs/pipelines" / ident / "index.json").read_text(encoding="utf-8"))
        self.assertEqual(index["pipeline"]["steps"][1]["previous_job_ids"], [first])
        self.assertEqual(index["pipeline"]["steps"][1]["job_ids"], [second])
        events = self.events(ident)
        self.assertTrue(any(e["event"] == "pipeline_stage_exception" for e in events))
        self.assertEqual({e["job_id"] for e in events if e["event"] == "job_bound"}, {first, second})
        self.assertEqual({e["pipeline_id"] for e in events}, {ident})

    def test_quality_executor_propagates_pipeline_and_keeps_full_exception(self):
        seen = []
        def failing_runner(*args, **kwargs):
            seen.append(diagnostics.current_context())
            raise ValueError("simulated scoring payload invalid")
        pool = ThreadPoolExecutor(max_workers=1)
        self.addCleanup(pool.shutdown)
        manager = quality_jobs.QualityJobManager(self.root / "quality-jobs", runner=failing_runner,
                                                executor=pool, data_root=self.root)
        with internal_pipeline("pl_test"):
            job = manager._submit_job("b", ["task"])
        manager._futures[job["job_id"]].result(timeout=10)
        self.assertEqual(manager.get(job["job_id"])["status"], "failed")
        self.assertEqual(seen[0]["pipeline_id"], "pl_test")
        self.assertEqual(seen[0]["job_id"], job["job_id"])
        self.assertEqual(seen[0]["step_id"], "quality")
        events = self.events("pl_test")
        text = json.dumps(events, ensure_ascii=False)
        self.assertIn("simulated scoring payload invalid", text)
        self.assertIn("failing_runner", text)
        self.assertTrue(any(e["event"] == "job_snapshot" and e.get("status") == "failed" for e in events))

    def test_logging_filesystem_failure_does_not_change_quality_outcome(self):
        pool = ThreadPoolExecutor(max_workers=1)
        self.addCleanup(pool.shutdown)
        manager = quality_jobs.QualityJobManager(self.root / "quality-jobs", runner=lambda *a, **k: {"warnings": []},
                                                executor=pool, data_root=self.root)
        with patch.object(diagnostics, "_path", side_effect=OSError("disk unavailable")):
            with internal_pipeline("pl_disk"):
                job = manager._submit_job("b", ["task"])
            manager._futures[job["job_id"]].result(timeout=10)
        self.assertEqual(manager.get(job["job_id"])["status"], "succeeded")

    def test_quality_output_tee_keeps_non_protocol_text_without_secrets(self):
        RecordStore(self.root).put("quality_jobs", "j", {"job_id": "j", "pipeline_id": "pl_worker", "batch_id": "b"})
        real_popen = subprocess.Popen
        spawned_commands = []
        def spawn(command, **options):
            spawned_commands.append(command)
            code = ("import sys; assert sys.stdin.readline() == 'QUALITY_GO\\n'; "
                    "print('ordinary traceback detail Authorization: Bearer test-secret',flush=True); "
                    "print('RESULT {\"warnings\":[]}',flush=True)")
            return real_popen([sys.executable, "-u", "-c", code], **options)
        with patch.object(quality_jobs.subprocess, "Popen", side_effect=spawn), \
                patch.object(quality_jobs, "_env_values", return_value={"MODEL_API_KEY": "test-secret"}):
            report = quality_jobs.run_quality_subprocess("b", ["task"], job_id="j", progress=lambda _: None,
                                                        data_root=self.root)
        self.assertEqual(report, {"warnings": []})
        self.assertIn("--diagnostic-execution-id", spawned_commands[0])
        logs = list((self.root / "logs/pipelines/pl_worker/jobs/j").glob("*/worker-output.log"))
        self.assertEqual(len(logs), 1)
        text = logs[0].read_text(encoding="utf-8")
        self.assertIn("ordinary traceback detail", text)
        self.assertNotIn("test-secret", text)
        self.assertIn("RESULT", text)

    def test_conversion_recovery_uses_registered_release_owner(self):
        registry = Registry(self.root)
        registry.releases["rel_one"].update(pipeline_id="pl_release", batch_ids=["b"])
        seen = []
        def convert(registry, release):
            seen.append(diagnostics.current_context())
            return {"release_id": release["release_id"], "rows": [row()], "warnings": []}
        manager = TrainingOverviewManager(registry, converter=convert)
        self.addCleanup(manager.close)
        manager.start(); manager.wait()
        self.assertEqual(seen[0]["pipeline_id"], "pl_release")
        self.assertEqual(seen[0]["job_id"], "rel_one")
        self.assertEqual(seen[0]["step_id"], "overview")
        self.assertTrue(any(e["event"] == "conversion_committed" for e in self.events("pl_release")))

    def test_collection_transfer_logs_verified_files_for_exact_dispatch_owner(self):
        fixture = collection_fixture.CollectionTransferTests("test_complete_two_devices_same_original_trajectory_and_replay_without_download")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        records = RecordStore(fixture.root)
        records.put("pipelines", "request", {"pipeline_id": "request", "batch_id": "batch-one"})
        result = fixture.manager.sync(fixture.run_id)
        self.assertEqual(result["status"], "completed")
        events = [json.loads(line) for p in (fixture.root / "logs/pipelines/request/events").glob("*.jsonl")
                  for line in p.read_text(encoding="utf-8").splitlines()]
        self.assertTrue(any(e["event"] == "collection_archive_verified" for e in events))
        self.assertTrue(any(e["event"] == "collection_files_verified" for e in events))
        self.assertEqual({e["job_id"] for e in events}, {fixture.run_id})
        self.assertNotIn("pipeline_id", result)

    def test_pipeline_context_does_not_leak_between_executor_jobs(self):
        seen = []
        pool = ThreadPoolExecutor(max_workers=1)
        self.addCleanup(pool.shutdown)
        def runner(*args, **kwargs):
            seen.append(diagnostics.current_context())
            return {"warnings": []}
        manager = quality_jobs.QualityJobManager(self.root / "quality-jobs", runner=runner,
                                                executor=pool, data_root=self.root)
        jobs = []
        for ident in ("pl_first", "pl_second"):
            with internal_pipeline(ident):
                jobs.append(manager._submit_job(ident, ["task"]))
        for job in jobs:
            manager._futures[job["job_id"]].result(timeout=10)
        self.assertEqual([value["pipeline_id"] for value in seen], ["pl_first", "pl_second"])
        self.assertNotEqual(seen[0]["execution_id"], seen[1]["execution_id"])
        for ident, job in zip(("pl_first", "pl_second"), jobs):
            self.assertEqual({e["job_id"] for e in self.events(ident)}, {job["job_id"]})


if __name__ == "__main__":
    unittest.main()
