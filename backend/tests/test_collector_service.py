"""The standalone service is exercised only with simulated devices and execution."""
from __future__ import annotations

import hashlib
import io
import json
import tempfile
import threading
import unittest
import zipfile
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace

from fastapi.testclient import TestClient
from openpyxl import Workbook

from backend.collector_service.core import Collector, CollectorConfig, CollectorError, atomic_json, sha
from backend.collector_service.adapters import TemplateExecutor
from backend.phonefactory_manager import create_app


class ImmediateExecutor:
    def submit(self, fn, *args):
        future = Future()
        try:
            future.set_result(fn(*args))
        except BaseException as exc:
            future.set_exception(exc)
        return future


class QueuedExecutor:
    def __init__(self):
        self.jobs = []
    def submit(self, fn, *args):
        future = Future()
        self.jobs.append((fn, args, future))
        return future
    def finish(self):
        for fn, args, future in self.jobs:
            if not future.done():
                future.set_result(fn(*args))


class Devices:
    def devices(self):
        return [{"serial": "phone-a", "model": "Mock Phone", "battery": 76}]
    def monitor(self, phone, workdir):
        return {"screenshot": None, "device_size": {"width": 1080, "height": 2400}, "log": phone + "模拟日志"}


def make_trajectory(path):
    path.mkdir(parents=True, exist_ok=True)
    (path / "_trajectory_for_evaluate.json").write_text(json.dumps({"actions_flat": [{"action": "click"}]}))
    (path / "step001_vla_model_response.json").write_text(json.dumps({"content": '<tool_call>{"action":"click","coordinate":[1,2]}</tool_call>'}))
    (path / "step001_vla_input.jpg").write_bytes(b"mock-image")
    (path / "step001_vla_input_ui.xml").write_text("<hierarchy/>")


class Execution:
    def __init__(self, *, empty=False, unknown=False, error=False):
        self.calls, self.cancels = 0, []
        self.empty, self.unknown, self.error = empty, unknown, error
    def preflight(self, params):
        pass
    def prepare_device(self, path):
        path.mkdir(parents=True)
    def execute(self, run, workdirs, output, cancel):
        self.calls += 1
        result = {"trajectories": [], "reports": [], "errors": []}
        if self.empty:
            return result
        for phone, cases in run["phone_tasks"].items():
            for case in cases:
                source = output / phone.replace(":", "_") / case / "same-name"
                make_trajectory(source)
                result["trajectories"].append({"phone_id": phone, "collection_case_id": "unknown" if self.unknown else case,
                                              "source_trajectory_id": "same-name", "source_dir": str(source)})
        report = output / "report.xlsx"
        report.write_bytes(b"report")
        result["reports"].append({"path": str(report)})
        if self.error:
            result["errors"].append({"error": "模拟评估警告"})
        return result
    def cancel(self, run, root):
        self.cancels.append(run["run_id"])


def workbook(app="App"):
    wb = Workbook()
    wb.active.append(["用例编号", "涉及APP", "任务", "一级场景", "二级场景"])
    wb.active.append(["case-1", app, "打开" + app, "一级", "二级"])
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


class CollectorServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.config = CollectorConfig(self.root)
        self.execution = Execution()
        self.executor = ImmediateExecutor()
        self.collector = self.new_collector()
        self.task = workbook()
        self.apps = json.dumps([{"phone_id": "phone-a", "app": "App"}, {"phone_id": "127.0.0.1:5555", "app": "App"}]).encode()
        self.form = {"collection_run_id": "cr_1", "batch_id": "batch-1", "runmode": "generate", "vla": "http://mock/v1",
                     "task_manifest": json.dumps({"tasks": [{"collection_case_id": "case-1", "task_id": "task-1",
                         "task": "打开App", "app": "App", "scene": "一级", "capability": "二级"}]})}
    def tearDown(self):
        self.collector.close()
        self.tmp.cleanup()
    def new_collector(self):
        return Collector(self.config, device_adapter=Devices(), execution_adapter=self.execution, executor=self.executor)
    def submit(self, **changes):
        return self.collector.submit(task_bytes=self.task, apps_bytes=self.apps, form={**self.form, **changes})

    def test_multi_phone_same_trajectory_immutable_complete_archive(self):
        run = self.submit()
        self.assertEqual(run["status"], "succeeded")
        manifest = run["manifest"]
        self.assertEqual(len(manifest["trajectories"]), 2)
        self.assertEqual(len({t["relative_dir"] for t in manifest["trajectories"]}), 2)
        self.assertTrue(all(len(t["relative_dir"].split("/")) == 2 for t in manifest["trajectories"]))
        archive = self.collector.archive("cr_1")
        self.assertEqual(sha(archive), manifest["archive"]["sha256"])
        with zipfile.ZipFile(archive) as z:
            declared = [f for t in manifest["trajectories"] for f in t["files"]] + manifest["reports"]
            self.assertEqual(set(z.namelist()), {f["path"] for f in declared})
            for item in declared:
                data = z.read(item["path"])
                self.assertEqual(hashlib.sha256(data).hexdigest(), item["sha256"])
                self.assertEqual(len(data), item["size"])
        restored = self.new_collector()
        self.assertEqual(restored.public(restored.get("cr_1")), run)
        self.assertEqual(self.execution.calls, 1)

    def test_replay_and_all_execution_parameters_conflict(self):
        first = self.submit()
        self.assertEqual(self.submit(), first)
        for field, value in {"vla": "http://other", "temperature": "1", "top_p": ".5", "sampling_enabled": "true",
                             "use_experience_lib": "true", "runmode": "modeliter", "phone_id": "phone-a"}.items():
            with self.subTest(field=field), self.assertRaises(CollectorError) as caught:
                self.submit(**{field: value})
            self.assertEqual(caught.exception.status, 409)
        self.assertEqual(self.execution.calls, 1)

    def test_same_batch_different_phones_run_concurrently_and_retry_reuses_run(self):
        self.executor = QueuedExecutor()
        self.collector.close()
        self.collector = self.new_collector()
        requests = [("cr_phone_a", "phone-a"), ("cr_phone_b", "127.0.0.1:5555")]
        with ThreadPoolExecutor(max_workers=2) as pool:
            runs = list(pool.map(lambda item: self.submit(collection_run_id=item[0], phone_id=item[1]), requests))
        self.assertEqual({run["status"] for run in runs}, {"queued"})
        self.assertEqual({run["batch_id"] for run in runs}, {"batch-1"})
        self.assertEqual(len(self.executor.jobs), 2)
        for run, (run_id, phone) in zip(runs, requests):
            self.assertEqual(run["phones"], [{"phone_id": phone}])
            self.assertEqual(self.submit(collection_run_id=run_id, phone_id=phone), run)
        self.assertEqual(len(self.executor.jobs), 2)
        self.executor.finish()
        self.assertEqual(self.execution.calls, 2)
        self.assertEqual({run["status"] for run in self.collector.list_runs()}, {"succeeded"})

    def test_physical_phone_is_exclusive_across_apps_and_run_modes(self):
        self.executor = QueuedExecutor()
        self.collector.close()
        self.collector = self.new_collector()
        self.submit(collection_run_id="cr_active", phone_id="phone-a", app="App")
        for mode, app in (("generate", "Other"), ("modeliter", "App"), ("modeliter", "Other")):
            with self.subTest(mode=mode, app=app):
                tasks = [{"collection_case_id": "case-1", "task_id": "task-1", "task": "打开" + app, "app": app}]
                with self.assertRaisesRegex(CollectorError, "手机已有运行中的任务") as caught:
                    self.collector.submit(task_bytes=workbook(app),
                        apps_bytes=json.dumps([{"phone_id": "phone-a", "app": app}]).encode(),
                        form={**self.form, "collection_run_id": "cr_" + mode + app, "phone_id": "phone-a",
                              "app": app, "runmode": mode, "task_manifest": json.dumps({"tasks": tasks})})
                self.assertEqual(caught.exception.status, 409)
        self.assertEqual([run["run_id"] for run in self.collector.list_runs()], ["cr_active"])
        self.assertEqual(len(self.executor.jobs), 1)

    def test_whole_batch_busy_phone_rejects_atomically_without_reserving_free_phone(self):
        self.executor = QueuedExecutor()
        self.collector.close()
        self.collector = self.new_collector()
        self.submit(collection_run_id="cr_active", phone_id="phone-a")
        with self.assertRaisesRegex(CollectorError, "phone-a") as caught:
            self.submit(collection_run_id="cr_whole_batch")
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual([run["run_id"] for run in self.collector.list_runs()], ["cr_active"])
        self.assertFalse(self.collector.run_dir("cr_whole_batch").exists())
        self.assertFalse(self.collector.phone_dir("127.0.0.1:5555").exists())
        self.assertEqual(len(self.executor.jobs), 1)
        other = self.submit(collection_run_id="cr_free_phone", phone_id="127.0.0.1:5555")
        self.assertEqual(other["status"], "queued")
        self.assertEqual(len(self.executor.jobs), 2)

    def test_device_busy_and_delete_cancels_keeps_archives(self):
        self.submit()
        archive = self.collector.archive("cr_1").read_bytes()
        self.executor = QueuedExecutor()
        self.collector = self.new_collector()
        queued = self.submit(collection_run_id="cr_2")
        self.assertEqual(queued["status"], "queued")
        with self.assertRaises(CollectorError):
            self.submit(collection_run_id="cr_3")
        deleted = self.collector.delete_phone("phone-a")
        self.assertTrue(deleted["ok"])
        self.executor.finish()
        self.assertEqual(self.collector.get("cr_2")["status"], "interrupted")
        self.assertEqual(self.collector.archive("cr_1").read_bytes(), archive)
        self.assertEqual(self.execution.cancels, ["cr_2"])

    def test_delete_failure_not_reported_success_and_run_stays_active(self):
        self.executor = QueuedExecutor()
        self.collector = self.new_collector()
        self.submit()
        with patch.object(self.execution, "cancel", side_effect=CollectorError("remote stop failed", 503)):
            with self.assertRaises(CollectorError):
                self.collector.delete_phone("phone-a")
        self.assertTrue(self.collector.phone_dir("phone-a").exists())
        self.assertEqual(self.collector.get("cr_1")["status"], "queued")
        self.executor.finish()
        self.assertEqual(self.collector.get("cr_1")["status"], "succeeded")

    def test_empty_exit_zero_not_success_and_unknown_case_not_guessed(self):
        self.execution.empty = True
        self.assertEqual(self.submit()["status"], "failed")
        self.execution.empty, self.execution.unknown = False, True
        invalid = self.submit(collection_run_id="cr_2")
        self.assertEqual(invalid["status"], "failed")
        self.assertEqual(invalid["manifest"]["trajectories"], [])
        with zipfile.ZipFile(self.collector.archive("cr_2")) as z:
            self.assertTrue(all(name.startswith("reports/") for name in z.namelist()))

    def test_partial_reports_modes_and_safe_download(self):
        self.execution.error = True
        result = self.submit()
        self.assertEqual(result["status"], "partial")
        self.submit(collection_run_id="eval_1", batch_id="", runmode="modeliter")
        reports = self.collector.reports("modeliter")["folders"]
        self.assertEqual([f["run_id"] for f in reports], ["eval_1"])
        file = reports[0]["files"][0]
        self.assertEqual(self.collector.report(mode="modeliter", run_id="eval_1", file_id=file["id"]).read_bytes(), b"report")
        with self.assertRaises(CollectorError):
            self.collector.report(mode="generate", run_id="eval_1", file_id=file["id"])
        with self.assertRaises(CollectorError):
            self.collector.report(mode="modeliter", folder="../", name="report.xlsx")

    def test_concurrent_duplicate_dispatch_once(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: self.submit(), range(4)))
        self.assertEqual({r["run_id"] for r in results}, {"cr_1"})
        self.assertEqual(self.execution.calls, 1)

    def test_restart_recovers_queued_and_rename_before_database_commit(self):
        self.executor = QueuedExecutor()
        self.collector = self.new_collector()
        self.submit()
        self.executor = ImmediateExecutor()
        self.collector = self.new_collector()
        saved = self.collector.get("cr_1")
        self.assertEqual(saved["status"], "succeeded")
        # Simulate crash after freeze but before the final DB commit.
        with self.collector.db() as conn:
            saved.update(status="running", manifest=None, completed_at=None)
            self.collector._save(conn, saved)
        calls = self.execution.calls
        self.collector = self.new_collector()
        self.assertEqual(self.collector.get("cr_1")["status"], "succeeded")
        self.assertEqual(self.execution.calls, calls)

    def test_archive_corruption_rejected(self):
        self.submit()
        self.collector.archive("cr_1").write_bytes(b"corrupt")
        with self.assertRaises(CollectorError):
            self.collector.archive("cr_1")

    def test_preflight_missing_template_is_explicit_and_creates_no_run(self):
        collector = Collector(self.config, device_adapter=Devices(), execution_adapter=TemplateExecutor(self.config),
                              executor=self.executor)
        with self.assertRaises(CollectorError) as caught:
            collector.submit(task_bytes=self.task, apps_bytes=self.apps, form=self.form)
        self.assertEqual(caught.exception.status, 503)
        self.assertEqual(collector.list_runs(), [])

    def test_invalid_parameters_and_manifest_fail_before_execution(self):
        for changes in ({"temperature": "nan"}, {"top_p": "-0.1"}, {"runmode": "bad"}, {"batch_id": ""},
                        {"collection_run_id": "../escape"}, {"task_manifest": '{"tasks":[]}'},
                        {"task_manifest": json.dumps({"tasks": [{"collection_case_id": "wrong", "task_id": "a"}]})}):
            with self.subTest(changes=changes), self.assertRaises(CollectorError):
                self.submit(**changes)
        self.assertEqual(self.execution.calls, 0)

    def test_manual_normalized_task_app_and_numeric_case_preserve_original_workbook(self):
        from backend.manual_collection import ManualCollectionStore
        from backend.task_generation.collection_batches import COLLECTION_COLUMNS
        for case in (1, 1.0, "case-with-space"):
            with self.subTest(case=case):
                book = Workbook()
                book.active.append(COLLECTION_COLUMNS)
                row = [None] * len(COLLECTION_COLUMNS)
                row[0], row[2], row[6] = case, " App ", " 打开App "
                book.active.append(row)
                output = io.BytesIO(); book.save(output); book.close()
                original = output.getvalue()
                run_id = "cr_normalized_" + str(len(self.collector.list_runs()))
                _, tasks, _ = ManualCollectionStore(self.root / "manual")._parse(original, "manual_test")
                result = self.collector.submit(task_bytes=original, apps_bytes=self.apps,
                    form={**self.form, "collection_run_id": run_id, "task_manifest": json.dumps({"tasks": tasks})})
                self.assertEqual(result["status"], "succeeded", result["errors"])
                self.assertEqual({t["collection_case_id"] for t in result["manifest"]["trajectories"]},
                                 {tasks[0]["collection_case_id"]})
                self.assertEqual((self.collector.run_dir(run_id) / "tasks.xlsx").read_bytes(), original)

    def test_failed_db_run_still_owned_blocks_new_run_and_delete_stops_it(self):
        from backend.collector_service.core import token
        self.executor = QueuedExecutor()
        self.collector = self.new_collector()
        self.submit()
        with self.collector.db() as conn:
            run = self.collector._get(conn, "cr_1")
            run.update(status="failed", errors=[{"error": "PID inspection timed out"}])
            self.collector._save(conn, run)
        marker_dir = self.collector.run_dir("cr_1") / "execution" / token("phone-a")
        atomic_json(marker_dir / "process.json", {"pid": 321, "config_path": str(marker_dir / "worker-config.json")})
        adapter = TemplateExecutor(self.config)
        self.collector.execution = adapter
        with patch.object(adapter, "preflight"), patch("backend.collector_service.adapters.owned_process", return_value=True), \
                patch("backend.collector_service.adapters.terminate_owned") as stop:
            self.assertEqual(self.collector.statuses(["phone-a"])[0]["status"], "运行中")
            with self.assertRaises(CollectorError) as conflict:
                self.submit(collection_run_id="cr_2")
            self.assertEqual(conflict.exception.status, 409)
            self.assertEqual(len(self.collector.list_runs()), 1)
            self.collector.delete_phone("phone-a")
            self.assertEqual(stop.call_args.args[0]["pid"], 321)
        self.assertEqual(self.collector.get("cr_1")["status"], "interrupted")
        self.assertFalse(self.collector.phone_dir("phone-a").exists())

    def test_process_identity_uncertain_fails_closed_before_dispatch_or_delete(self):
        from backend.collector_service.core import token
        marker_dir = self.collector.run_dir("cr_old") / "execution" / token("phone-a")
        atomic_json(marker_dir / "process.json", {"pid": 321, "config_path": str(marker_dir / "worker-config.json")})
        self.collector.phone_dir("phone-a").mkdir(parents=True)
        adapter = TemplateExecutor(self.config)
        self.collector.execution = adapter
        with patch.object(adapter, "preflight"), \
                patch("backend.collector_service.adapters.owned_process", side_effect=OSError("identity unavailable")):
            for call in (lambda: self.submit(), lambda: self.collector.statuses(["phone-a"]),
                         lambda: self.collector.delete_phone("phone-a")):
                with self.assertRaises(CollectorError) as unavailable:
                    call()
                self.assertEqual(unavailable.exception.status, 503)
        self.assertTrue(self.collector.phone_dir("phone-a").is_dir())
        self.assertEqual(self.collector.list_runs(), [])

    def test_http_devices_monitor_start_status_archive_reports(self):
        with TestClient(create_app(self.config, device_adapter=Devices(), execution_adapter=self.execution,
                                   executor=ImmediateExecutor())) as client:
            self.assertTrue(client.get("/capabilities").json()["batch_results"])
            self.assertEqual(client.post("/adb_devices").json()["devices"][0]["battery"], 76)
            self.assertEqual(client.post("/add_phone", json={"phoneid": "phone-a"}).status_code, 200)
            monitor = client.post("/monitor", json={"phone_id": "phone-a"}).json()
            self.assertIn("模拟日志", monitor["log"])
            self.assertFalse(monitor["running"])
            response = client.post("/start_run", data=self.form, files={"task_file": ("tasks.xlsx", self.task),
                                                                       "apps_file": ("apps.json", self.apps)})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["status"], "succeeded")
            self.assertEqual(client.get("/runs/cr_1").json()["batch_id"], "batch-1")
            self.assertEqual(client.get("/runs/cr_1/archive").status_code, 200)
            reports = client.get("/reports?runmode=generate").json()["folders"]
            self.assertEqual(client.get("/report_download", params={"runmode": "generate", "run_id": "cr_1",
                                                                   "file_id": reports[0]["files"][0]["id"]}).content, b"report")
            status = client.post("/status", json={"phones": ["phone-a"]}).json()
            self.assertEqual(status["statuses"][0]["status"], "空闲")
            self.assertEqual(client.post("/del_phone", json={"phoneid": "phone-a"}).status_code, 200)
            self.assertEqual(client.get("/runs/cr_1/archive").status_code, 200)


class TemplateAdapterTests(unittest.TestCase):
    def test_worker_preserves_task_columns_runs_main_then_evaluation_and_retains_old_output(self):
        from backend.collector_service.worker import run
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            work, output = root / "device", root / "output"
            work.mkdir(); output.mkdir()
            for name in ("config_trajectory.py", "config_report.py"):
                (work / name).write_text(name)
            (work / ".runs").mkdir()
            (work / ".runs" / "old.txt").write_text("keep")
            (work / "evaluator_results_old.xlsx").write_bytes(b"old report")
            task = root / "tasks.xlsx"
            task.write_bytes(workbook())
            params = {"sampling_enabled": True, "temperature": .4, "top_p": .8, "use_experience_lib": True,
                      "vla": "http://mock/v1/", "runmode": "modeliter"}
            config = {"phone_id": "phone", "workdir": str(work), "output": str(output), "task_workbook": str(task),
                      "cases": ["case-1"], "run_id": "eval_1", "params": params, "adb_path": "mock-adb",
                      "execution_timeout": 60, "evaluator_timeout": 30}
            calls = []
            def subprocess_run(args, **kwargs):
                calls.append((args, kwargs))
                if "get-state" in args:
                    return SimpleNamespace(returncode=0, stdout="device\n")
                if "-c" in args:
                    self.assertEqual((work / "config.py").read_text(), "config_trajectory.py")
                    self.assertEqual(kwargs["env"]["ADB_SERIAL"], "phone")
                    self.assertEqual(kwargs["env"]["GUI_AGENT_CORE_VLA_TEMPERATURE"], "0.4")
                    make_trajectory(work / ".runs" / "timestamp" / "case-1" / "trajectory")
                elif any("run_evaluator_batch.py" in value for value in args):
                    self.assertEqual((work / "config.py").read_text(), "config_report.py")
                    (work / "evaluator_results_new.xlsx").write_bytes(b"new report")
                return SimpleNamespace(returncode=0, stdout="")
            with patch("backend.collector_service.worker.subprocess.run", side_effect=subprocess_run):
                run(config)
            result = json.loads((output / "completion.json").read_text(encoding="utf-8"))
            self.assertEqual(result["errors"], [])
            self.assertEqual(len(result["trajectories"]), 1)
            self.assertEqual(result["trajectories"][0]["collection_case_id"], "case-1")
            self.assertEqual([Path(r["path"]).name for r in result["reports"]], ["evaluator_results_new.xlsx"])
            self.assertEqual((output / "previous-device-output" / "old.txt").read_text(), "keep")
            self.assertEqual((output / "previous-reports" / "evaluator_results_old.xlsx").read_bytes(), b"old report")
            self.assertEqual((work / "config.py").read_text(), "config_trajectory.py")
            self.assertLess(next(i for i, c in enumerate(calls) if "-c" in c[0]),
                            next(i for i, c in enumerate(calls) if any("run_evaluator_batch.py" in v for v in c[0])))

    def test_durable_process_disappeared_never_relaunched_or_treated_as_success(self):
        from backend.collector_service.core import token
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            output, work = root / "execution", root / "device"
            output.mkdir(); work.mkdir()
            device = output / token("phone")
            device.mkdir()
            atomic_json(device / "process.json", {"pid": 123, "config_path": "owned-worker"})
            adapter = TemplateExecutor(CollectorConfig(root))
            run = {"run_id": "cr_1", "params": {}, "phone_tasks": {"phone": ["case"]}}
            with patch("backend.collector_service.adapters.owned_process", return_value=False), \
                    patch("backend.collector_service.adapters.subprocess.Popen") as spawn:
                result = adapter.execute(run, {"phone": work}, output, threading.Event())
            spawn.assert_not_called()
            self.assertEqual(result["trajectories"], [])
            self.assertIn("中断", result["errors"][0]["error"])

    def test_delete_waits_for_spawn_registration_and_fences_later_workers(self):
        from backend.collector_service.core import token
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            output, work = root / "execution", root / "device"
            output.mkdir(); work.mkdir()
            adapter = TemplateExecutor(CollectorConfig(root))
            run = {"run_id": "cr_1", "params": {"vla": "http://mock/v1"}, "phone_tasks": {"phone": ["case"]}}
            entered, release_spawn, cancel_requested = threading.Event(), threading.Event(), threading.Event()
            killed = []
            def spawn(*args, **kwargs):
                entered.set()
                if not release_spawn.wait(5):
                    raise RuntimeError("test spawn barrier timed out")
                return SimpleNamespace(pid=321)
            def delete():
                cancel_requested.set()
                adapter.cancel(run, root)
            with patch("backend.collector_service.adapters.subprocess.Popen", side_effect=spawn) as process, \
                    patch("backend.collector_service.adapters.terminate_owned", side_effect=lambda marker: killed.append(marker["pid"])), \
                    patch("backend.collector_service.adapters.owned_process", return_value=True), \
                    ThreadPoolExecutor(max_workers=2) as pool:
                executing = pool.submit(adapter.execute, run, {"phone": work}, output, threading.Event())
                self.assertTrue(entered.wait(5))
                deleting = pool.submit(delete)
                self.assertTrue(cancel_requested.wait(5))
                self.assertFalse(deleting.done(), "delete cannot acknowledge before the launched PID is registered")
                release_spawn.set()
                deleting.result(timeout=5)
                result = executing.result(timeout=5)
                self.assertIn(321, killed)
                self.assertEqual(result["trajectories"], [])
                self.assertTrue((output / "cancelled.json").is_file())
                # A fresh adapter simulates a service restart; the durable fence still applies.
                restarted = TemplateExecutor(CollectorConfig(root))
                blocked = restarted.execute(run, {"phone": work}, output, threading.Event())
                self.assertIn("取消", blocked["errors"][0]["error"])
                self.assertEqual(process.call_count, 1)

    def test_late_worker_checks_durable_cancel_before_touching_device(self):
        from backend.collector_service.worker import run
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            output = root / "execution" / "phone"
            output.mkdir(parents=True)
            atomic_json(output.parent / "cancelled.json", {"run_id": "cr_1"})
            deleted_workdir = root / "deleted-device"
            with patch("backend.collector_service.worker.subprocess.run") as command:
                run({"phone_id": "phone", "workdir": str(deleted_workdir), "output": str(output)})
            command.assert_not_called()
            self.assertFalse(deleted_workdir.exists())
            result = json.loads((output / "completion.json").read_text(encoding="utf-8"))
            self.assertIn("取消", result["errors"][0]["error"])

    def test_adb_model_battery_and_png_dimensions_without_real_device(self):
        import struct
        from backend.collector_service.adapters import AdbAdapter
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            adapter = AdbAdapter(CollectorConfig(root))
            def command(args, **kwargs):
                if args == ["devices"]:
                    return "List of devices attached\n192.0.2.1:5555\tdevice\noffline\tunauthorized\n"
                if "getprop" in args:
                    return "Mock Model"
                if "battery" in args:
                    return "AC powered: false\nlevel: 73\nscale: 100\n"
                return b"\x89PNG\r\n\x1a\n" + b"\0" * 8 + struct.pack(">II", 100, 200)
            with patch.object(adapter, "command", side_effect=command):
                self.assertEqual(adapter.devices(), [{"serial": "192.0.2.1:5555", "model": "Mock Model", "battery": 73}])
                image = adapter.monitor("192.0.2.1:5555", root)
                self.assertEqual(image["device_size"], {"width": 100, "height": 200})
                self.assertTrue(image["screenshot"])


if __name__ == "__main__":
    unittest.main()
