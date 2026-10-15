"""A real request deadline propagates through quality jobs into durable retries."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from backend.pipelines import PipelineManager
from backend.quality_jobs import QualityJobManager
from backend.tests.test_data_publishing import ImmediateExecutor
from backend.tests.test_pipelines import OfflineRuntime

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "DevelopRubrics"))
from adarubric.llm.openai_client import OpenAIClient
from adarubric.llm.request_control import observe_requests


class RubricRepairDeadlineTests(unittest.IsolatedAsyncioTestCase):
    async def test_json_repair_has_its_own_request_and_timeout_without_extra_retry(self):
        from backend.DevelopRubrics import quality_job_runner
        gen = quality_job_runner.GEN
        events, calls = [], []

        async def response(**kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="{invalid JSON"))])
            await asyncio.Event().wait()

        with TemporaryDirectory() as directory, patch.dict(os.environ, {
            "PIPELINE_MODEL_SINGLE_ATTEMPT": "1", "ADARUBRIC_REQUEST_TIMEOUT_SECONDS": "0.02",
            "ADARUBRIC_HTTP_TRUST_ENV": "false", "ADARUBRIC_HTTP_PROXY_URL": "",
            "ADARUBRIC_REPAIR_INVALID_JSON": "true",
        }), observe_requests(events.append):
            client = OpenAIClient(model="offline-model", api_key="test-only",
                                  base_url="https://never-contact.invalid/v1")
            client._client.chat.completions.create = AsyncMock(side_effect=response)
            try:
                with self.assertRaises(TimeoutError):
                    await gen._generate_rubric_json(client, config={}, messages=[{"role": "user", "content": "fixture"}],
                        task=SimpleNamespace(task_id="task-A"), num_dimensions=5, temperature=0, max_tokens=4096,
                        raw_response_path=Path(directory) / "raw.txt")
            finally:
                await client.close()
        self.assertEqual(len(calls), 2)
        starts = [event for event in events if event["event"] == "started"
                  and event["operation"] in {"rubric_generate", "rubric_repair"}]
        self.assertEqual([event["operation"] for event in starts], ["rubric_generate", "rubric_repair"])
        self.assertNotEqual(starts[0]["local_request_id"], starts[1]["local_request_id"])
        self.assertTrue(all(event["task_id"] == "task-A" for event in starts))
        timeouts = [event for event in events if event["event"] == "timeout"
                    and event["operation"] == "rubric_repair"]
        self.assertEqual(len(timeouts), 1)
        self.assertEqual(timeouts[0]["local_request_id"], starts[1]["local_request_id"])


class QualityDeadlinePipelineTests(unittest.TestCase):
    def test_real_deadline_stops_at_three_attempts_and_keeps_manual_retry(self):
        calls = []

        async def request_that_never_returns(**kwargs):
            calls.append(1)
            await asyncio.Event().wait()

        def runner(*args, **kwargs):
            async def run():
                client = OpenAIClient(model="offline-model", api_key="test-only",
                                      base_url="https://never-contact.invalid/v1")
                client._client.chat.completions.create = AsyncMock(side_effect=request_that_never_returns)
                try:
                    await client.generate_text([{"role": "user", "content": "mock evidence"}])
                finally:
                    await client.close()
            return asyncio.run(run())

        with TemporaryDirectory() as directory, patch.dict(os.environ, {
            "PIPELINE_MODEL_SINGLE_ATTEMPT": "1", "ADARUBRIC_REQUEST_TIMEOUT_SECONDS": "0.01",
            "ADARUBRIC_HTTP_TRUST_ENV": "false", "ADARUBRIC_HTTP_PROXY_URL": "",
        }):
            root = Path(directory)
            quality = QualityJobManager(root / "quality-jobs", runner=runner,
                                        executor=ImmediateExecutor(), data_root=root)
            self.addCleanup(quality.shutdown)

            class Runtime(OfflineRuntime):
                def jobs(self, step, pipeline):
                    return quality.list_jobs() if step == "quality" else super().jobs(step, pipeline)

                def submit(self, step, pipeline, retry=False):
                    if step != "quality":
                        return super().submit(step, pipeline, retry)
                    self.calls.append((step, retry))
                    return quality._submit_job(pipeline["batch_id"], pipeline["task_ids"], {
                        "batch_id": pipeline["batch_id"], "pipeline_id": pipeline["pipeline_id"],
                    })

            runtime = Runtime(root)
            runtime.ready_stages.update({"preprocessing", "tree"})
            manager = PipelineManager(runtime, root)
            pipeline = manager.create({"request_id": "deadline-test", "name": "offline deadlines",
                "batch_id": "batch", "mode": "automatic", "start_mode": "existing", "threshold": 4})

            def advance(target):
                for _ in range(12):
                    manager.tick(pipeline["pipeline_id"])
                    current = manager.get(pipeline["pipeline_id"])
                    if current["status"] == target:
                        return current
                self.fail(f"Expected {target}, got {current['status']}")

            for attempt in (1, 2, 3):
                current = advance("failed" if attempt == 3 else "retry_waiting")
                step = next(item for item in current["steps"] if item["id"] == "quality")
                self.assertEqual(step["retry_info"]["attempts"], attempt)
                self.assertEqual(len(calls), attempt)
                job = quality.get(step["job_ids"][-1])
                self.assertEqual(job["failure"]["category"], "timeout")
                if attempt < 3:
                    # Repeated polls before the due time cannot submit anything.
                    manager.tick(pipeline["pipeline_id"])
                    self.assertEqual(len(calls), attempt)
                    with patch("backend.pipelines._retry_due", return_value=True):
                        manager.tick(pipeline["pipeline_id"])
            manager.tick(pipeline["pipeline_id"])
            self.assertEqual(len(calls), 3)
            current = manager.get(pipeline["pipeline_id"])
            manager.control(pipeline["pipeline_id"], "retry", current["storage_revision"])
            advance("retry_waiting")
            self.assertEqual(len(calls), 4)
            quality.shutdown()


if __name__ == "__main__":
    unittest.main()
