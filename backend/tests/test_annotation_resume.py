import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from openpyxl import load_workbook

from backend.bounding_box.qwen_reviewer import BoxReviewOutputError, QwenBoxReviewer
from backend.collection_runs import CollectionRunStore
from backend.pipeline_access import internal_pipeline
from backend.pipelines import PipelineRuntime
from backend.preprocessing_jobs import PreprocessingJobManager
from backend.preprocessing_service import SHEET, PreprocessingError, annotate_input
from backend.stage_artifacts import fingerprint
from backend.tests.test_collection_runs import seed_batch
from backend.tests.test_preprocessing_jobs import CONFIG, ManualQueue
from backend.tests.test_trajectories_preprocessing import FakeReviewResult, create_step
from backend.trajectories_preprocessing import parse_action_cell


def workbook_rows(path):
    workbook = load_workbook(path, read_only=True)
    try:
        return list(workbook.active.values)
    finally:
        workbook.close()


class AnnotationResumeTests(unittest.TestCase):
    batch = "batch-one"
    owner = "p-test"

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name) / "data"
        self.queue = ManualQueue()
        self.config = dict(CONFIG)
        self.reviewer = Mock()
        self.reviewer.review.side_effect = self.review
        self.factory = lambda **unused: self.reviewer
        self.manager = self.new_manager()
        self.sources = CollectionRunStore(self.root)
        payload, workbook = seed_batch(self.root)
        self.store = self.manager.artifacts
        self.store.publish(self.batch, "00_collection", payload, workbooks={"result.xlsx": workbook})
        self.pipeline = {"pipeline_id": self.owner, "batch_id": self.batch, "status": "running",
                         "steps": [{"id": "preprocessing", "job_ids": []}], "retry_policy": {"version": 1}}
        self.manager.records.put("pipelines", self.owner, self.pipeline)
        self.calls = []
        self.fail_at = None

    def new_manager(self):
        return PreprocessingJobManager(self.root, executor=self.queue, config_loader=lambda: dict(self.config),
                                       reviewer_factory=self.factory)

    def make_trajectory(self, count):
        run, _ = self.sources.create(self.batch)
        output = Path(run["output_dir"])
        directory = output / "task-one" / "resume-sample"
        directory.mkdir(parents=True)
        actions = []
        for n in range(1, count + 1):
            action = {"action": "wait"} if n == 2 else {"action": "click", "coordinate": [n, 40]}
            create_step(directory, n, action, summary=f"step {n}")
            actions.append({"global_step": n, "action": action})
        (directory / "_trajectory_for_evaluate.json").write_text(json.dumps({"actions_flat": actions}), encoding="utf-8")
        entry = {"collection_case_id": "task-one", "source_trajectory_id": directory.name,
                 "relative_dir": directory.relative_to(output).as_posix(),
                 "files": [{"path": p.relative_to(output).as_posix(), "sha256": fingerprint(p)}
                           for p in sorted(directory.iterdir())]}
        self.sources.complete(run["collection_run_id"], {"batch_id": self.batch, "trajectories": [entry]})

    def review(self, **request):
        step = request["action"]["coordinate"][0]
        self.calls.append(step)
        if step == self.fail_at:
            raise TimeoutError("mock model timeout")
        if step == 3:
            raise BoxReviewOutputError("invalid_json")
        return FakeReviewResult("replace", (step, 1, step + 10, 10))

    def submit(self, failed=None):
        runtime = PipelineRuntime(self.root, preprocessing=self.manager, tree=None, quality=None, cot=None,
                                  factory=None, overview=None)
        self.pipeline["steps"][0]["job_ids"] = [failed] if failed else []
        with internal_pipeline(self.owner):
            return runtime.submit("preprocessing", self.pipeline, retry=bool(failed))

    def finish(self, job):
        self.queue.drain()
        return self.manager.get(job["job_id"])

    def test_pipeline_retries_at_82_after_restart_and_outputs_equal_uninterrupted(self):
        self.make_trajectory(84)
        self.fail_at = 82
        first = self.finish(self.submit())
        self.assertEqual(first["status"], "failed", first)
        self.assertEqual(first["completed_steps"], 81)
        self.assertEqual([a["stage"] for a in first["artifacts"]], ["01_conversion"])
        self.assertIsNone(self.store.get(self.batch, "02_annotation"))
        self.calls.clear()
        self.fail_at = None
        self.manager = self.new_manager()
        retry = self.submit(first["job_id"])
        self.assertEqual((retry["status"], retry["completed_steps"], retry["total_steps"]), ("queued", 81, 84))
        with internal_pipeline(self.owner):
            self.assertEqual(self.manager.retry(first["job_id"])["job_id"], retry["job_id"])
        self.assertEqual(len(self.queue.calls), 1)
        with patch.object(self.manager, "_update", wraps=self.manager._update) as updates:
            done = self.finish(retry)
        self.assertEqual(done["status"], "succeeded", done.get("error"))
        self.assertEqual(self.calls, [82, 83, 84])
        for call in updates.call_args_list:
            if "completed_steps" in call.kwargs:
                self.assertGreaterEqual(call.kwargs["completed_steps"], 81)

        conversion, annotation = done["artifacts"]
        converted = self.store.read_payload(conversion)
        frozen = self.manager._snapshot(self.manager._get(done["job_id"]))
        reference_path = self.root / "reference.xlsx"
        reference, counts = annotate_input(self.store.resolve_file(conversion, "result.json"), converted,
            reference_path, snapshot=frozen, config=self.config, reviewer_factory=self.factory,
            env_file=self.root / "unused.env", cache_path=self.root / "unused.json",
            data_root=self.root, progress=lambda event: None)
        self.assertEqual(self.store.read_payload(annotation), reference)
        self.assertEqual(workbook_rows(self.store.resolve_file(annotation, "annotated_trajectories.xlsx")),
                         workbook_rows(reference_path))
        for key in ("annotated", "blank", "rows"):
            self.assertEqual(annotation["metadata"][key], counts[key])
        self.assertEqual(counts, {"annotated": 82, "blank": 2, "rows": 84})
        self.assertIsNone(reference["sheets"][SHEET][1]["actions_box"])
        self.assertIsNone(reference["sheets"][SHEET][2]["actions_box"])

    def use_cached_reviewer(self):
        response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{"decision":"accept"}'),
                                                            finish_reason="stop")])
        client = Mock()
        client.chat.completions.create.return_value = response
        self.enterContext(patch("backend.bounding_box.qwen_reviewer.OpenAI", return_value=client))
        self.enterContext(patch("backend.bounding_box.qwen_reviewer.httpx.Client"))
        self.manager.reviewer_factory = lambda config, cache_path: QwenBoxReviewer(config["model"], cache_path,
            client_settings={"api_key": "test", "base_url": CONFIG["base_url"], "proxy": None,
                             "timeout": 1, "verify": True, "trust_env": False})
        return client.chat.completions.create

    def test_cached_model_response_survives_failure_before_row_commit(self):
        self.make_trajectory(1)
        model = self.use_cached_reviewer()
        with patch("backend.trajectories_preprocessing.format_actions_box", side_effect=OSError("interrupted")):
            first = self.finish(self.submit())
        self.assertEqual((first["status"], first["completed_steps"]), ("failed", 0))
        done = self.finish(self.submit(first["job_id"]))
        self.assertEqual(done["status"], "succeeded", done.get("error"))
        self.assertEqual(model.call_count, 1)
        self.assertIn("<bbox>", self.store.read_payload(done["artifacts"][1])["sheets"][SHEET][0]["actions_box"])

    def test_independent_annotation_retries_only_uncommitted_rows(self):
        self.make_trajectory(4)
        self.fail_at = 3
        first = self.finish(self.manager.submit(self.batch))
        self.assertEqual((first["status"], first["completed_steps"], self.calls), ("failed", 2, [1, 3]))
        self.fail_at = None
        retry = self.manager.retry(first["job_id"])
        self.assertEqual((retry["completed_steps"], retry["total_steps"]), (2, 4))
        done = self.finish(retry)
        self.assertEqual(done["status"], "succeeded", done.get("error"))
        self.assertEqual(self.calls, [1, 3, 3, 4])

    def test_termination_while_waiting_for_model_rejects_late_row_and_publication(self):
        self.make_trajectory(4)
        def stop_before_reply(**req):
            if req["action"]["coordinate"][0] == 4:
                self.manager.records.update("pipelines", self.owner, lambda p: p.update(status="terminating"))
            return self.review(**req)
        self.reviewer.review.side_effect = stop_before_reply
        stopped = self.finish(self.submit())
        self.assertEqual(stopped["status"], "cancelled", stopped)
        self.assertEqual(stopped["completed_steps"], 3)
        self.assertIsNone(self.store.get(self.batch, "02_annotation"))

    def test_restart_after_row_commit_before_progress_acknowledgement(self):
        self.make_trajectory(4)
        queued = self.submit()
        update = self.manager._update

        def interrupt_before_progress_save(job_id, **changes):
            if changes.get("stage") == "annotating" and changes.get("completed_steps") == 1:
                # The worker disappears after its SQLite commit but before the job's
                # progress write or any normal Exception handling can run.
                raise SystemExit("simulated worker exit")
            return update(job_id, **changes)

        with patch.object(self.manager, "_update", side_effect=interrupt_before_progress_save):
            with self.assertRaisesRegex(SystemExit, "simulated worker exit"):
                self.queue.drain()
        job_id = queued["job_id"]
        stale = self.manager.get(job_id)
        self.assertEqual((stale["status"], stale["completed_steps"], self.calls), ("running", 0, [1]))
        self.manager = self.new_manager()
        self.assertEqual(self.manager.get(job_id)["status"], "interrupted")
        retry = self.submit(job_id)
        self.assertEqual((retry["status"], retry["completed_steps"], retry["total_steps"]), ("queued", 1, 4))
        self.calls.clear()
        done = self.finish(retry)
        self.assertEqual(done["status"], "succeeded", done.get("error"))
        self.assertEqual(self.calls, [3, 4])
        self.assertIn("<bbox>", self.store.read_payload(done["artifacts"][1])["sheets"][SHEET][0]["actions_box"])

    def test_different_frozen_model_cannot_resume_partial_rows(self):
        self.make_trajectory(4)
        self.fail_at = 4
        first = self.finish(self.submit())
        self.assertEqual(first["completed_steps"], 3)
        self.config["model"] = "changed-model"
        with self.assertRaises(PreprocessingError):
            self.submit(first["job_id"])
        self.calls.clear()
        self.fail_at = None
        done = self.finish(self.submit())
        self.assertEqual(done["status"], "succeeded", done.get("error"))
        self.assertEqual(self.calls, [1, 3, 4])
