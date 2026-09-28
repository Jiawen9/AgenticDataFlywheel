"""A new managed Pipeline, rather than the client, owns model retry attempts."""
from __future__ import annotations

import importlib
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from openai import APIConnectionError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "DevelopRubrics"))


class ModelSingleAttemptTests(unittest.IsolatedAsyncioTestCase):
    async def test_pipeline_disables_wrapper_and_sdk_retries(self):
        for module_name, class_name in (
            ("adarubric.llm.openai_client", "OpenAIClient"),
            ("adarubric.llm.vllm_client", "VLLMClient"),
        ):
            with self.subTest(class_name=class_name):
                module = importlib.import_module(module_name)
                calls, settings = [], []

                class Completions:
                    async def create(self, **kwargs):
                        calls.append(kwargs)
                        raise APIConnectionError(request=httpx.Request("POST", "https://example.invalid/v1"))

                class FakeSDK:
                    def __init__(self, **kwargs):
                        settings.append(kwargs)
                        self.chat = type("Chat", (), {"completions": Completions()})()

                with patch.dict(os.environ, {"PIPELINE_MODEL_SINGLE_ATTEMPT": "1"}), patch.object(module, "AsyncOpenAI", FakeSDK):
                    client = getattr(module, class_name)(model="mock", api_key="mock", base_url="https://example.invalid/v1",
                                                         max_retries=9)
                    with self.assertRaises(APIConnectionError):
                        await client._chat([], temperature=0.0, max_tokens=16)
                self.assertEqual(len(calls), 1)
                self.assertEqual(client._max_retries, 1)
                self.assertEqual(settings[0]["max_retries"], 0)

    def test_manual_client_keeps_previous_configuration(self):
        for module_name, class_name in (
            ("adarubric.llm.openai_client", "OpenAIClient"),
            ("adarubric.llm.vllm_client", "VLLMClient"),
        ):
            with self.subTest(class_name=class_name):
                module = importlib.import_module(module_name)
                settings = []

                class FakeSDK:
                    def __init__(self, **kwargs):
                        settings.append(kwargs)

                with patch.dict(os.environ, {"PIPELINE_MODEL_SINGLE_ATTEMPT": "0"}), patch.object(module, "AsyncOpenAI", FakeSDK):
                    client = getattr(module, class_name)(model="mock", api_key="mock", base_url="https://example.invalid/v1",
                                                         max_retries=3)
                self.assertEqual(client._max_retries, 3)
                self.assertNotIn("max_retries", settings[0])


class PreprocessingSingleAttemptTests(unittest.TestCase):
    def test_new_pipeline_uses_one_shot_box_reviewer_without_changing_config(self):
        import copy

        from backend.data_store import RecordStore
        from backend.pipeline_access import internal_pipeline
        from backend.preprocessing_jobs import PreprocessingJobManager
        from backend.tests.test_preprocessing_jobs import CONFIG, PreprocessingJobTests
        from backend.bounding_box import qwen_reviewer
        from backend.tests.test_trajectories_preprocessing import AcceptingReviewer

        # Use the existing collection fixture; all images and results stay in temp.
        fixture = PreprocessingJobTests("test_pipeline_keeps_business_columns_identity_and_idempotent_reads")
        fixture.setUp()
        try:
            fixture.complete()
            calls = []

            class FakeClient:
                def with_options(self, **kwargs):
                    calls.append(("options", kwargs))
                    return self

                def close(self):
                    calls.append(("close", None))

            class FakeReviewer(AcceptingReviewer):
                def __init__(self, **kwargs):
                    self.client = FakeClient()
                    calls.append(("created", kwargs["model"]))

            RecordStore(fixture.root).put("pipelines", "pl_one_shot", {
                "pipeline_id": "pl_one_shot", "retry_policy": {"version": 1,
                "max_attempts": 3, "delays_seconds": [30, 120]},
            })
            manager = PreprocessingJobManager(fixture.root, source_store=fixture.sources,
                executor=fixture.queue, config_loader=lambda: copy.deepcopy(CONFIG))
            values = {"YUNAI_API_KEY": "test-only", "MODEL_NAME": CONFIG["model"],
                      "MODEL_URL": CONFIG["base_url"]}
            with patch("backend.preprocessing_jobs.read_env_file", return_value=values), \
                 patch.object(qwen_reviewer, "qwen_settings", return_value={"api_key": "test-only"}), \
                 patch.object(qwen_reviewer, "QwenBoxReviewer", FakeReviewer):
                with internal_pipeline("pl_one_shot"):
                    job = manager.submit("batch-one")
                fixture.queue.drain()
            self.assertEqual(manager.get(job["job_id"])["status"], "succeeded")
            self.assertIn(("options", {"max_retries": 0}), calls)
            self.assertIn(("close", None), calls)
            self.assertEqual(manager.get(job["job_id"])["pipeline_id"], "pl_one_shot")
        finally:
            fixture.doCleanups()


class CotRestartRetryEvidenceTests(unittest.TestCase):
    def test_interrupted_cot_job_records_restart_evidence(self):
        import tempfile

        from backend.data_store import RecordStore
        from backend.trajectory_correction.cot_jobs import CotJobManager

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_id = "a" * 32
            RecordStore(root).put("correction_cot_jobs", job_id, {
                "job_id": job_id, "session_id": "test-session", "batch_id": "test-batch",
                "status": "running", "pipeline_id": "pl_test",
            })
            manager = CotJobManager(jobs_dir=root / "jobs", executor=object())
            recovered = manager.get(job_id)
            self.assertEqual(recovered["status"], "interrupted")
            self.assertEqual(recovered["failure"]["category"], "service_restart")
            self.assertTrue(recovered["failure"]["retryable"])
