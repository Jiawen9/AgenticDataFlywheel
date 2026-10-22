"""Logprob capture tests never run a device, model, or evaluator."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from backend.collector_service.core import Collector, CollectorConfig
from backend.collector_service.logprobs import extract_logprob_sidecars
from backend.collector_service.worker import run as run_worker
from backend.tests.test_collector_service import Devices, Execution, ImmediateExecutor, make_trajectory, workbook

TRACE = {"status": "ok", "backend": "vllm", "response_token_ids": [101, 102],
         "behavior_logprobs": [-0.2, -0.4], "response_loss_mask": [1, 1],
         "sampling": {"temperature": 0.7, "top_p": 0.85}, "extension": {"preserved": True}}


def with_trace(directory, trace=TRACE):
    make_trajectory(directory)
    response = directory / "step001_vla_model_response.json"
    payload = json.loads(response.read_text(encoding="utf-8"))
    payload["behavior_trace"] = copy.deepcopy(trace)
    response.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return response


class LogprobSidecarTests(unittest.TestCase):
    def test_extracts_only_formal_response_and_preserves_source_bytes(self):
        with tempfile.TemporaryDirectory() as temp:
            trajectory = Path(temp)
            response = with_trace(trajectory)
            before = response.read_bytes()
            for hidden in ("_prefetch_staging/candidate", "_stability_rejected/attempt"):
                with_trace(trajectory / hidden)
            self.assertEqual(extract_logprob_sidecars(trajectory, required=True), [])
            sidecar = trajectory / "step001_logprobs.json"
            self.assertEqual(json.loads(sidecar.read_text(encoding="utf-8")), TRACE)
            self.assertEqual(response.read_bytes(), before)
            self.assertEqual(list(trajectory.rglob("*_logprobs.json")), [sidecar])
            sidecar_bytes = sidecar.read_bytes()
            self.assertEqual(extract_logprob_sidecars(trajectory, required=True), [])
            self.assertEqual(sidecar.read_bytes(), sidecar_bytes)

    def test_missing_and_invalid_traces_keep_ordinary_trajectory(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            make_trajectory(directory)
            self.assertEqual(extract_logprob_sidecars(directory), [])
            self.assertIn("missing_behavior_trace", extract_logprob_sidecars(directory, required=True)[0])
            self.assertFalse((directory / "step001_logprobs.json").exists())
            invalid = {**TRACE, "status": "invalid", "error": "length_mismatch"}
            with_trace(directory, invalid)
            self.assertIn("length_mismatch", extract_logprob_sidecars(directory, required=True)[0])
            self.assertEqual(json.loads((directory / "step001_logprobs.json").read_text(encoding="utf-8")), invalid)
            with_trace(directory, {**TRACE, "behavior_logprobs": [-0.2]})
            self.assertIn("invalid_behavior_logprobs_length", extract_logprob_sidecars(directory, required=True)[0])

    def test_non_finite_trace_write_failure_leaves_no_sidecar_or_temporary_file(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            invalid = {**TRACE, "status": "invalid", "error": "non_finite_logprob",
                       "behavior_logprobs": [float("nan"), -0.4]}
            response = with_trace(directory, invalid)
            before = response.read_bytes()
            errors = extract_logprob_sidecars(directory, required=True)
            self.assertEqual(len(errors), 1)
            self.assertIn("step001_vla_model_response.json", errors[0])
            self.assertIn("logprobs JSON 写入失败", errors[0])
            self.assertEqual(response.read_bytes(), before)
            self.assertFalse((directory / "step001_logprobs.json").exists())
            self.assertEqual(list(directory.glob("*.tmp")), [])

    def test_worker_scopes_capture_to_collection_and_separate_runs(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {"GUI_AGENT_VLA_CAPTURE_LOGPROBS": "true"}):
            root = Path(temp)
            work = root / "device"
            work.mkdir()
            for name in ("config_trajectory.py", "config_report.py"):
                (work / name).write_text(name)
            task = root / "tasks.xlsx"
            task.write_bytes(workbook())
            observed = []

            def execute(args, **kwargs):
                if "get-state" in args:
                    return SimpleNamespace(returncode=0, stdout="device\n")
                if "-c" in args:
                    observed.append(("collection", kwargs["env"]["GUI_AGENT_VLA_CAPTURE_LOGPROBS"]))
                    directory = work / ".runs" / "timestamp" / "case-1" / "trajectory"
                    if trace_status is not None:
                        trace = TRACE if trace_status == "ok" else {**TRACE, "status": "invalid", "error": "length_mismatch"}
                        with_trace(directory, trace)
                    else:
                        make_trajectory(directory)
                elif any("run_evaluator_batch.py" in str(value) for value in args):
                    observed.append(("evaluation", kwargs["env"]["GUI_AGENT_VLA_CAPTURE_LOGPROBS"]))
                return SimpleNamespace(returncode=0, stdout="")

            cases = ((True, "ok"), (True, None), (True, "invalid"), (False, None), (None, None))
            for index, (capture, trace_status) in enumerate(cases):
                output = root / f"run-{index}"
                output.mkdir()
                params = {"sampling_enabled": True, "temperature": .7, "top_p": .85,
                          "use_experience_lib": False, "vla": "http://mock/v1/"}
                if capture is not None:
                    params["capture_logprobs"] = capture
                config = {"phone_id": "phone", "workdir": str(work), "output": str(output),
                          "task_workbook": str(task), "cases": ["case-1"], "params": params,
                          "adb_path": "mock-adb", "execution_timeout": 30, "evaluator_timeout": 10}
                with patch("backend.collector_service.worker.subprocess.run", side_effect=execute):
                    run_worker(config)
                completion = json.loads((output / "completion.json").read_text(encoding="utf-8"))
                if capture is True and trace_status != "ok":
                    self.assertEqual(len(completion["errors"]), 1)
                    error = completion["errors"][0]["error"]
                    self.assertIn("手机 phone", error)
                    self.assertIn("轨迹 trajectory", error)
                    self.assertIn("step001_vla_model_response.json", error)
                    self.assertIn("missing_behavior_trace" if trace_status is None else "length_mismatch", error)
                else:
                    self.assertEqual(completion["errors"], [])
                trajectory = Path(completion["trajectories"][0]["source_dir"])
                self.assertEqual((trajectory / "step001_logprobs.json").exists(), trace_status is not None)
            self.assertEqual(observed, [("collection", "true"), ("evaluation", "false"),
                                        ("collection", "true"), ("evaluation", "false"),
                                        ("collection", "true"), ("evaluation", "false"),
                                        ("collection", "false"), ("evaluation", "false"),
                                        ("collection", "false"), ("evaluation", "false")])
            self.assertEqual(os.environ["GUI_AGENT_VLA_CAPTURE_LOGPROBS"], "true")

    def test_sidecar_is_frozen_in_manifest_and_zip_and_missing_capture_is_partial(self):
        class CaptureExecution(Execution):
            def execute(self, run, workdirs, output, cancel):
                result = super().execute(run, workdirs, output, cancel)
                for item in result["trajectories"]:
                    directory = Path(item["source_dir"])
                    if run["run_id"] == "capture-ok":
                        with_trace(directory)
                    for error in extract_logprob_sidecars(directory, required=run["params"]["capture_logprobs"]):
                        result["errors"].append({"phone_id": item["phone_id"], "error": error})
                return result

        with tempfile.TemporaryDirectory() as temp:
            collector = Collector(CollectorConfig(Path(temp)), device_adapter=Devices(),
                                  execution_adapter=CaptureExecution(), executor=ImmediateExecutor())
            try:
                form = {"batch_id": "batch-1", "vla": "http://mock/v1", "capture_logprobs": "true",
                        "task_manifest": json.dumps({"tasks": [{"collection_case_id": "case-1", "task_id": "task-1",
                                                                 "task": "打开App", "app": "App"}]})}
                apps = json.dumps([{"phone_id": "phone-a", "app": "App"}]).encode()
                ok = collector.submit(task_bytes=workbook(), apps_bytes=apps,
                                      form={**form, "collection_run_id": "capture-ok"})
                self.assertEqual(ok["status"], "succeeded", ok["errors"])
                self.assertIs(collector.get("capture-ok")["params"]["capture_logprobs"], True)
                files = ok["manifest"]["trajectories"][0]["files"]
                sidecar = next(item for item in files if item["path"].endswith("/step001_logprobs.json"))
                with zipfile.ZipFile(collector.archive("capture-ok")) as archive:
                    content = archive.read(sidecar["path"])
                self.assertEqual(json.loads(content), TRACE)
                self.assertEqual(hashlib.sha256(content).hexdigest(), sidecar["sha256"])
                missing = collector.submit(task_bytes=workbook(), apps_bytes=apps,
                                           form={**form, "collection_run_id": "capture-missing"})
                self.assertEqual(missing["status"], "partial")
                self.assertEqual(len(missing["manifest"]["trajectories"]), 1)
                self.assertTrue(any("missing_behavior_trace" in error["error"] for error in missing["errors"]))
            finally:
                collector.close()
