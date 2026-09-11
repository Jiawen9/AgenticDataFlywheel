from __future__ import annotations

import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.task_generation.jobs import TaskGenerationJobManager
from backend.task_generation.router import router


TASK_FIELDS = {
    "task_id", "source_result_id", "task", "app", "scene", "capability", "sub_capability",
    "pre_dependency", "pre_task_id", "source_status", "source_seed_id", "source_row",
    "source_task", "case_id", "dependency_error",
}
RESPONSE_FIELDS = {
    "schema_version", "job_id", "kind", "job_status", "knowledge_base_version",
    "task_count", "tasks", "errors", "warnings",
}


class NoExecution:
    def submit(self, *args, **kwargs):
        raise AssertionError("读取采集输入不能启动任何后台任务")


class CollectionInputTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.manager = TaskGenerationJobManager(
            self.base / "jobs", self.base / "runs", self.base / "exports", self.base / "kb", self.base / "logs",
            executor=NoExecution(),
        )
        self.addCleanup(self.manager.shutdown)
        self.job_sequence = 0

    @staticmethod
    def row(result_id="result-1", **changes):
        return {
            "result_id": result_id, "task_uuid": f"uuid-{result_id}", "task": "请在 AppA 搜索一个视频",
            "app": "AppA", "scene": "场景A", "capability": "能力A", "sub_capability": "子能力A",
            "pre_dependency": "zero", "pre_task_uuid": None, "deleted": False, **changes,
        }

    def job(self, rows=None, *, kind="task_generation", status="succeeded", **changes):
        self.job_sequence += 1
        job_id = f"job-{self.job_sequence}"
        self.manager._write_job({
            "job_id": job_id, "kind": kind, "status": status, "stage": status,
            "knowledge_base_version": "kb-version-1", "errors": [], "warnings": [], **changes,
        })
        self.manager._write_json(self.manager._results_path(job_id), [self.row()] if rows is None else rows)
        return job_id

    def replace_rows(self, job_id, rows):
        self.manager._write_json(self.manager._results_path(job_id), rows)

    def file_snapshot(self):
        return {str(path.relative_to(self.base)): (path.read_bytes(), path.stat().st_mtime_ns)
                for path in self.base.rglob("*") if path.is_file()}

    def test_initial_and_augmentation_share_one_stable_response_contract(self):
        initial = self.job()
        augmented = self.job([{
            "result_id": "variant-id", "task": "人工编辑后的变体", "生成的变体任务": "旧值不得作为统一任务来源",
            "app": "AppB", "scene": "Unclassified", "capability": "Unclassified", "sub_capability": "Unclassified",
            "seed_id": "seed-id", "source_row": 8, "source_task": "原失败任务", "用例编号": "CASE-001",
            "审核状态": "待人工Review", "deleted": False,
        }], kind="augmentation")
        for job_id in (initial, augmented):
            with self.subTest(job_id=job_id):
                value = self.manager.collection_input(job_id)
                self.assertEqual(set(value), RESPONSE_FIELDS)
                self.assertEqual(set(value["tasks"][0]), TASK_FIELDS)
                self.assertEqual(value["schema_version"], 1)
                self.assertEqual(value["job_id"], job_id)
                self.assertEqual(value["job_status"], "succeeded")
                self.assertEqual(value["knowledge_base_version"], "kb-version-1")
                self.assertEqual(value["task_count"], 1)
                self.assertEqual(value, self.manager.collection_input(job_id))
        first = self.manager.collection_input(initial)["tasks"][0]
        self.assertEqual(first["task_id"], "uuid-result-1")
        self.assertEqual(first["source_result_id"], "result-1")
        second = self.manager.collection_input(augmented)["tasks"][0]
        self.assertEqual(second["task_id"], "variant-id")
        self.assertEqual(second["source_result_id"], "variant-id")
        self.assertEqual(second["task"], "人工编辑后的变体")
        self.assertEqual(second["source_seed_id"], "seed-id")
        self.assertEqual(second["source_row"], 8)
        self.assertEqual(second["source_task"], "原失败任务")
        self.assertEqual(second["case_id"], "CASE-001")
        self.assertEqual(second["pre_dependency"], "unknown")
        self.assertEqual(second["scene"], "Unclassified")
        self.assertIsNone(second["pre_task_id"])
        self.assertIsNone(second["source_status"])

    def test_latest_manual_edit_delete_and_restore_are_reflected_without_export(self):
        job_id = self.job([self.row("one"), self.row("two")], kind="augmentation")
        self.manager.patch_result(job_id, "one", {"task": "改成最新人工任务"})
        self.assertEqual(self.manager.collection_input(job_id)["tasks"][0]["task"], "改成最新人工任务")
        self.manager.patch_result(job_id, "one", {"deleted": True})
        after_delete = self.manager.collection_input(job_id)
        self.assertEqual(after_delete["task_count"], 1)
        self.assertEqual(after_delete["tasks"][0]["source_result_id"], "two")
        self.manager.patch_result(job_id, "one", {"deleted": False})
        restored = self.manager.collection_input(job_id)
        self.assertEqual(restored["task_count"], 2)
        self.assertEqual(restored["tasks"][0]["task"], "改成最新人工任务")
        self.assertEqual(list(self.manager.exports_dir.iterdir()), [])

    def test_get_is_read_only_and_returns_detached_data(self):
        job_id = self.job()
        before = self.file_snapshot()
        with patch.object(self.manager, "_write_json", side_effect=AssertionError("GET 不得写文件")), \
             patch.object(self.manager, "export", side_effect=AssertionError("GET 不得生成 Excel")), \
             patch("uuid.uuid4", side_effect=AssertionError("GET 不得生成新编号")):
            value = self.manager.collection_input(job_id)
            value["tasks"][0]["task"] = "不应影响原始文件"
            value["errors"].append({"error": "本地修改"})
            again = self.manager.collection_input(job_id)
            self.assertEqual(again["tasks"][0]["task"], self.row()["task"])
            self.assertEqual(again["errors"], [])
        self.assertEqual(self.file_snapshot(), before)

    def test_weak_dependency_returns_complete_pre_node_and_preserves_strong_status(self):
        job_id = self.job([
            self.row("pre", task_uuid="pre-uuid", pre_dependency="pre_node", dependency_group_id="group"),
            self.row("main", task_uuid="main-uuid", pre_dependency="weak", pre_task_uuid="pre-uuid", dependency_group_id="group"),
            self.row("strong", pre_dependency="strong", status="-2"),
        ])
        rows = self.manager.collection_input(job_id)["tasks"]
        self.assertEqual([row["pre_dependency"] for row in rows], ["pre_node", "weak", "strong"])
        self.assertEqual(rows[1]["pre_task_id"], rows[0]["task_id"])
        self.assertEqual(rows[2]["source_status"], "-2")
        self.assertIsNone(rows[2]["pre_task_id"])
        self.manager.patch_result(job_id, "main", {"deleted": True})
        self.assertEqual([row["source_result_id"] for row in self.manager.collection_input(job_id)["tasks"]], ["strong"])
        self.manager.patch_result(job_id, "main", {"deleted": False})
        self.assertEqual(self.manager.collection_input(job_id)["task_count"], 3)

    def test_optional_historical_fields_are_null_without_inventing_dependency_or_identity(self):
        row = {"result_id": "old-id", "task": "历史任务", "app": "旧App"}
        job_id = self.job([row], kind="augmentation")
        job = self.manager.get(job_id)
        job.pop("knowledge_base_version")
        self.manager._write_job(job)
        value = self.manager.collection_input(job_id)
        self.assertIsNone(value["knowledge_base_version"])
        output = value["tasks"][0]
        self.assertEqual(output["task_id"], "old-id")
        self.assertEqual(output["pre_dependency"], "unknown")
        for name in ("scene", "capability", "sub_capability", "pre_task_id", "source_status", "source_seed_id",
                     "source_row", "source_task", "case_id", "dependency_error"):
            self.assertIsNone(output[name], name)

    def test_uuid_falls_back_only_when_absent_or_empty_and_original_valid_strings_are_preserved(self):
        for uuid_value in (None, "", "  "):
            with self.subTest(uuid_value=uuid_value):
                job_id = self.job([self.row(task_uuid=uuid_value)])
                self.assertEqual(self.manager.collection_input(job_id)["tasks"][0]["task_id"], "result-1")
        row = self.row(" source-id ", task_uuid=" uuid-id ", task="  保留原始有效文本\n", app=" AppA ")
        value = self.manager.collection_input(self.job([row]))["tasks"][0]
        self.assertEqual(value["task_id"], row["task_uuid"])
        self.assertEqual(value["source_result_id"], row["result_id"])
        self.assertEqual(value["task"], row["task"])
        self.assertEqual(value["app"], row["app"])

    def test_partial_job_preserves_errors_warnings_and_source_status_types(self):
        errors = [{"stage": "generating", "item_id": "failed-row", "error": "部分生成失败"}]
        warnings = ["只返回已生成且未删除的任务"]
        job_id = self.job([self.row(status=-2, dependency_error="历史依赖错误")], status="partial", errors=errors, warnings=warnings)
        value = self.manager.collection_input(job_id)
        self.assertEqual(value["job_status"], "partial")
        self.assertEqual(value["errors"], errors)
        self.assertEqual(value["warnings"], warnings)
        self.assertEqual(value["tasks"][0]["source_status"], -2)
        self.assertIsInstance(value["tasks"][0]["source_status"], int)
        self.assertEqual(value["tasks"][0]["dependency_error"], "历史依赖错误")

    def test_structured_dependency_error_and_numeric_source_status_survive_http(self):
        dependency_error = {"stage": "dependency", "reason": "历史检查未完成", "attempts": 2}
        job_id = self.job([self.row(status=-2, dependency_error=dependency_error)])
        app = FastAPI()
        app.include_router(router)
        with patch("backend.task_generation.router.manager", self.manager), TestClient(app) as client:
            response = client.get(f"/api/task-generation/jobs/{job_id}/collection-input")
        self.assertEqual(response.status_code, 200)
        task = response.json()["tasks"][0]
        self.assertEqual(task["dependency_error"], dependency_error)
        self.assertEqual(task["source_status"], -2)
        self.assertIsInstance(task["source_status"], int)

    def test_empty_and_all_deleted_results_return_an_empty_list(self):
        for rows in ([], [self.row(deleted=True)], [{"deleted": True}]):
            with self.subTest(rows=rows):
                value = self.manager.collection_input(self.job(rows))
                self.assertEqual(value["task_count"], 0)
                self.assertEqual(value["tasks"], [])

    def test_missing_or_invalid_required_identifiers_app_and_task_are_rejected(self):
        for field in ("result_id", "app", "task"):
            for value in (None, "", "  ", 42, [], {}):
                with self.subTest(field=field, value=value):
                    row = self.row(**{field: value})
                    with self.assertRaises(ValueError):
                        self.manager.collection_input(self.job([row]))
            row = self.row()
            row.pop(field)
            with self.subTest(field=field, missing=True), self.assertRaises(ValueError):
                self.manager.collection_input(self.job([row]))
        with self.assertRaises(ValueError):
            self.manager.collection_input(self.job([self.row(task_uuid=123)]))

    def test_duplicate_task_or_source_identifiers_are_rejected(self):
        cases = [
            [self.row("one", task_uuid="same"), self.row("two", task_uuid="same")],
            [self.row("same", task_uuid="one"), self.row("same", task_uuid="two")],
            [self.row("one", task_uuid="two"), self.row("two", task_uuid=None)],
        ]
        for rows in cases:
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                self.manager.collection_input(self.job(rows))

    def test_deleted_rows_do_not_cause_false_duplicate_or_missing_field_errors(self):
        rows = [self.row("same"), self.row("same", deleted=True), {"deleted": True, "task": None}]
        value = self.manager.collection_input(self.job(rows))
        self.assertEqual(value["task_count"], 1)

    def test_deleted_flag_requires_boolean_and_null_means_not_deleted(self):
        for deleted in ("", "false", "0", 0, 1, [], {}):
            with self.subTest(deleted=deleted), self.assertRaises(ValueError):
                self.manager.collection_input(self.job([self.row(deleted=deleted)]))
        output = self.manager.collection_input(self.job([self.row(deleted=None)]))
        self.assertEqual(output["task_count"], 1)
        self.assertEqual(output["tasks"][0]["source_result_id"], "result-1")
        app = FastAPI()
        app.include_router(router)
        invalid = self.job([self.row(deleted="false")])
        with patch("backend.task_generation.router.manager", self.manager), TestClient(app) as client:
            response = client.get(f"/api/task-generation/jobs/{invalid}/collection-input")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.headers.get("Cache-Control"), "no-store")

    def test_missing_self_deleted_and_wrong_type_prerequisites_are_rejected(self):
        cases = [
            [self.row("main", pre_dependency="weak", pre_task_uuid=None)],
            [self.row("main", pre_dependency="weak", pre_task_uuid="")],
            [self.row("main", pre_dependency="weak", pre_task_uuid="absent")],
            [self.row("main", pre_dependency="weak", pre_task_uuid="uuid-main")],
            [self.row("pre", pre_dependency="pre_node", deleted=True), self.row("main", pre_dependency="weak", pre_task_uuid="uuid-pre")],
            [self.row("pre", pre_dependency="zero"), self.row("main", pre_dependency="weak", pre_task_uuid="uuid-pre")],
            [self.row("main", pre_dependency="zero", pre_task_uuid="absent")],
        ]
        for rows in cases:
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                self.manager.collection_input(self.job(rows))

    def test_missing_dependency_is_unknown_and_invalid_dependency_values_are_rejected(self):
        for value in (None, "", "unknown"):
            with self.subTest(value=value):
                row = self.row(pre_dependency=value, pre_task_uuid="")
                output = self.manager.collection_input(self.job([row]))["tasks"][0]
                self.assertEqual(output["pre_dependency"], "unknown")
                self.assertIsNone(output["pre_task_id"])
        for value in ("unexpected", 1, ["zero"], {"kind": "zero"}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.manager.collection_input(self.job([self.row(pre_dependency=value)]))
        with self.assertRaises(ValueError):
            self.manager.collection_input(self.job([self.row(pre_task_uuid=123)]))

    def test_pending_running_confirmation_failed_interrupted_and_unknown_kind_are_rejected(self):
        for status in ("queued", "running", "awaiting_confirmation", "failed", "interrupted"):
            with self.subTest(status=status), self.assertRaises(ValueError):
                self.manager.collection_input(self.job(status=status))
        with self.assertRaises(ValueError):
            self.manager.collection_input(self.job(kind="unrelated"))
        with self.assertRaises(FileNotFoundError):
            self.manager.collection_input("missing-job")

    def test_missing_malformed_and_non_array_results_are_rejected(self):
        missing = self.job()
        self.manager._results_path(missing).unlink()
        with self.assertRaises(ValueError):
            self.manager.collection_input(missing)
        malformed = self.job()
        self.manager._results_path(malformed).write_text("not valid JSON", encoding="utf-8")
        with self.assertRaises(ValueError):
            self.manager.collection_input(malformed)
        for invalid in ({"results": []}, None, "text", [42], [None]):
            with self.subTest(invalid=invalid):
                job_id = self.job([])
                self.replace_rows(job_id, invalid)
                with self.assertRaises(ValueError):
                    self.manager.collection_input(job_id)

    def test_metadata_and_rows_are_read_under_one_lock(self):
        job_id = self.job([self.row(task="旧版本任务")])
        entered_read = threading.Event()
        release_read = threading.Event()
        writer_attempted = threading.Event()
        writer_completed = threading.Event()
        original_get = self.manager.get

        def gated_get(identifier):
            value = original_get(identifier)
            if not entered_read.is_set():
                entered_read.set()
                if not release_read.wait(3):
                    raise AssertionError("读取同步点超时")
            return value

        def write_new_version():
            writer_attempted.set()
            with self.manager._lock:
                self.manager._progress(job_id, {"knowledge_base_version": "kb-version-2"})
                self.replace_rows(job_id, [self.row(task="新版本任务")])
            writer_completed.set()

        with patch.object(self.manager, "get", side_effect=gated_get), ThreadPoolExecutor(max_workers=2) as executor:
            reader = executor.submit(self.manager.collection_input, job_id)
            try:
                self.assertTrue(entered_read.wait(3))
                writer = executor.submit(write_new_version)
                self.assertTrue(writer_attempted.wait(3))
                self.assertFalse(writer_completed.wait(0.1), "读取期间写入不应穿过同一把作业锁")
            finally:
                release_read.set()
            before = reader.result(timeout=3)
            writer.result(timeout=3)
        self.assertEqual(before["knowledge_base_version"], "kb-version-1")
        self.assertEqual(before["tasks"][0]["task"], "旧版本任务")
        after = self.manager.collection_input(job_id)
        self.assertEqual(after["knowledge_base_version"], "kb-version-2")
        self.assertEqual(after["tasks"][0]["task"], "新版本任务")

    def test_http_success_errors_and_no_store_do_not_write(self):
        app = FastAPI()
        app.include_router(router)
        ready = self.job()
        running = self.job(status="running")
        invalid = self.job([self.row(task="")])
        before = self.file_snapshot()
        with patch("backend.task_generation.router.manager", self.manager), TestClient(app) as client:
            for identifier, expected in ((ready, 200), ("missing", 404), (running, 409), (invalid, 409)):
                with self.subTest(identifier=identifier):
                    response = client.get(f"/api/task-generation/jobs/{identifier}/collection-input")
                    self.assertEqual(response.status_code, expected)
                    self.assertEqual(response.headers.get("Cache-Control"), "no-store")
                    self.assertIn("application/json", response.headers.get("Content-Type", ""))
                    if expected == 200:
                        self.assertEqual(set(response.json()), RESPONSE_FIELDS)
                    else:
                        self.assertIsInstance(response.json()["detail"], str)
                        self.assertTrue(response.json()["detail"])
            response = client.post(f"/api/task-generation/jobs/{ready}/collection-input")
            self.assertEqual(response.status_code, 405)
        self.assertEqual(self.file_snapshot(), before)

    def test_openapi_documents_typed_success_and_conflict_contract(self):
        app = FastAPI()
        app.include_router(router)
        schema = app.openapi()
        path = schema["paths"]["/api/task-generation/jobs/{job_id}/collection-input"]
        self.assertEqual(set(path), {"get"})
        operation = path["get"]
        self.assertIn("404", operation["responses"])
        self.assertIn("409", operation["responses"])

        def resolve(value):
            while "$ref" in value:
                pieces = value["$ref"].removeprefix("#/").split("/")
                value = schema
                for piece in pieces:
                    value = value[piece]
            return value

        response = resolve(operation["responses"]["200"]["content"]["application/json"]["schema"])
        self.assertEqual(set(response["properties"]), RESPONSE_FIELDS)
        task = resolve(response["properties"]["tasks"]["items"])
        self.assertEqual(set(task["properties"]), TASK_FIELDS)
        dependency = resolve(task["properties"]["pre_dependency"])
        self.assertEqual(set(dependency["enum"]), {"zero", "weak", "strong", "pre_node", "unknown"})


if __name__ == "__main__":
    unittest.main()
