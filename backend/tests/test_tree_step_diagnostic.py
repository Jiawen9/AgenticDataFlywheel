"""The optional one-step model probe must remain bounded and non-mutating."""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import logging
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from PIL import Image

from backend.trajectories_tree import intermediate_state_classifier as classifier_module
from scripts import diagnose_tree_step as diagnostic


class TreeStepDiagnosticTests(unittest.TestCase):
    KEY = "sk-diagnostic-fixture-secret"
    BODY = "private-provider-body-do-not-print"
    GOAL = "private-task-goal-do-not-print"
    SUMMARY = "private-step-summary-do-not-print"

    def test_local_inspection_recognizes_extracted_truncation_guard(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            diagnostic.local_storage_probe(self.root, self.root / "system" / "app.sqlite", self.cache, "qwen3-vl-plus")
        self.assertIn("当前部署有 length 截断检查： True", output.getvalue())
        self.assertIn("当前模型输出 token 上限： 700", output.getvalue())
        self.assertIn("当前部署的新响应先校验再缓存： True", output.getvalue())

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.raw = self.root / "raw" / "test-batch"
        self.raw.mkdir(parents=True)
        self.current = self.raw / "step015_input.jpg"
        self.after = self.raw / "step015_done.jpg"
        self.following = self.raw / "step016_input.jpg"
        for path in (self.current, self.after, self.following):
            Image.new("RGB", (12, 20), "teal").save(path)
        self.cache = self.root / "cache" / "trajectories_tree" / "qwen_intermediate_state_cache.json"
        self.cache.parent.mkdir(parents=True)
        self.cache.write_text('{"unrelated":{"raw_response":"keep original bytes"}}', encoding="utf-8")
        self.env = self.root / "backend.env"
        self.env.write_text(
            f"YUNAI_API_KEY={self.KEY}\nMODEL_NAME=qwen3-vl-plus\nMODEL_URL=https://model.invalid/v1\n",
            encoding="utf-8",
        )
        self.job = {
            "job_id": "job-diagnostic", "batch_id": "test-batch", "status": "failed",
            "raw_root": str(self.raw), "annotation_version": "revision-1",
            "failure": {"trajectory_id": "tr-fixture", "step": 15},
            "build_config": {"model": "qwen3-vl-plus", "model_url": "https://model.invalid/v1",
                             "prompt_version": classifier_module.PREVIOUS_PROMPT_VERSION},
        }
        self.inputs = {
            "trajectory": "tr-fixture", "step_index": 15,
            "current_image_path": self.current, "next_image_path": self.following,
            "after_image_path": self.after, "action": {"action": "click", "coordinate": [1, 2]},
            "summary": self.SUMMARY, "previous_summary": "", "next_summary": "next-private",
            "task": self.GOAL,
        }
        self.annotation = self.root / "batches" / "test-batch" / "02_annotation"
        self.annotation.mkdir(parents=True)
        rows = [
            {"trajectory_id": "tr-fixture", "task_id": "task-fixture", "image": self.current.name,
             "action": json.dumps(self.inputs["action"]), "summary": self.SUMMARY},
            {"trajectory_id": "tr-fixture", "task_id": "task-fixture", "image": self.following.name,
             "action": '{"action":"terminate","status":"success"}', "summary": "next-private"},
        ]
        data = json.dumps({"sheets": {"Trajectories": rows}}, ensure_ascii=False).encode()
        (self.annotation / "result.json").write_bytes(data)
        (self.annotation / "manifest.json").write_text('{"version":"revision-1"}', encoding="utf-8")
        self.job["annotation_ref"] = {"files": [{"name": "result.json", "sha256": hashlib.sha256(data).hexdigest()}]}
        collection = self.annotation.parent / "00_collection"
        collection.mkdir()
        (collection / "result.json").write_text(json.dumps({"snapshot": {"tasks": [
            {"task_id": "task-fixture", "collection_case_id": "001", "task": self.GOAL}
        ]}}), encoding="utf-8")
        database = self.root / "system" / "app.sqlite"
        database.parent.mkdir()
        with contextlib.closing(sqlite3.connect(database)) as db:
            db.execute("CREATE TABLE records (namespace TEXT, record_key TEXT, payload TEXT)")
            db.execute("INSERT INTO records VALUES (?,?,?)", ("tree_jobs", self.job["job_id"], json.dumps(self.job)))
            db.commit()

        env = {k: os.environ[k] for k in ("SYSTEMROOT", "WINDIR") if k in os.environ}
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, env, clear=True).start()
        patch("socket.create_connection", side_effect=AssertionError("real network is forbidden")).start()
        patch("socket.socket.connect", side_effect=AssertionError("real network is forbidden")).start()
        self.requests = []
        self.clients = []

    def response(self, content=None, *, finish="stop", choices=True):
        if content is None:
            content = json.dumps({"category": "none", "confidence": 0.9,
                                  "reason": self.BODY, "observation": self.BODY})
        return {"id": "chatcmpl-fixture", "object": "chat.completion", "created": 0,
                "model": self.job["build_config"]["model"],
                "choices": [{"index": 0, "finish_reason": finish,
                             "message": {"role": "assistant", "content": content}}] if choices else [],
                "usage": {"prompt_tokens": 1, "completion_tokens": 7, "total_tokens": 8}}

    @contextlib.contextmanager
    def transport(self, body, *, status=200, request_id="req-safe-fixture"):
        def handle(request):
            self.requests.append(request)
            return httpx.Response(status, json=body, headers={"x-request-id": request_id})

        def client_factory(**kwargs):
            client = httpx.Client(transport=httpx.MockTransport(handle), trust_env=False)
            self.clients.append(client)
            return client

        with patch.object(classifier_module, "httpx", SimpleNamespace(Client=client_factory)):
            yield

    def run_probe(self, body, *, full=False, status=200, request_id="req-safe-fixture"):
        before = self.cache.read_bytes()
        inventory = {str(p.relative_to(self.root)) for p in self.root.rglob("*")}
        output = io.StringIO()
        with self.transport(body, status=status, request_id=request_id), contextlib.redirect_stdout(output):
            if full:
                code = diagnostic.probe(self.root, self.job["job_id"], probe_model=True, env_path=self.env)
            else:
                code = diagnostic.model_probe(self.job, self.cache, self.env, self.inputs)
        text = output.getvalue()
        self.assertEqual(hashlib.sha256(self.cache.read_bytes()).hexdigest(), hashlib.sha256(before).hexdigest())
        self.assertEqual({str(p.relative_to(self.root)) for p in self.root.rglob("*")}, inventory)
        self.assertTrue(all(client.is_closed for client in self.clients))
        for private in (self.KEY, self.BODY, self.GOAL, self.SUMMARY, "next-private"):
            self.assertNotIn(private, text)
        return code, text

    def test_success_uses_real_prompt_once_without_changing_cache(self):
        code, output = self.run_probe(self.response(), full=True)
        self.assertEqual(code, 0)
        self.assertEqual(len(self.requests), 1)
        body = json.loads(self.requests[0].content)
        self.assertEqual(body["model"], "qwen3-vl-plus")
        self.assertEqual(body["temperature"], 0)
        self.assertEqual(body["max_tokens"], 700)
        self.assertFalse(body["enable_thinking"])
        self.assertIn(self.GOAL, body["messages"][1]["content"][0]["text"])
        self.assertIn(self.SUMMARY, body["messages"][1]["content"][0]["text"])
        images = [item for item in body["messages"][1]["content"] if item["type"] == "image_url"]
        self.assertEqual(len(images), 2)
        self.assertIn('"choices_count": 1', output)
        self.assertIn('"completion_tokens": 7', output)
        self.assertIn("req-safe-fixture", output)
        self.assertIn(classifier_module.PROMPT_VERSION, output)
        self.assertIn("不是旧模型请求的原样复现", output)
        self.assertIn("不复现旧缓存分类组合", output)

    def test_current_job_does_not_claim_old_prompt_replay(self):
        self.job["build_config"]["prompt_version"] = classifier_module.PROMPT_VERSION
        code, output = self.run_probe(self.response())
        self.assertEqual(code, 0)
        self.assertEqual(len(self.requests), 1)
        self.assertNotIn("不是旧模型请求的原样复现", output)

    def test_unknown_job_protocol_is_sanitized_and_declares_new_prompt(self):
        self.job["build_config"]["prompt_version"] = self.KEY
        code, output = self.run_probe(self.response())
        self.assertEqual(code, 0)
        self.assertIn("原作业分类协议： 未记录或未知", output)
        self.assertIn("不是旧模型请求的原样复现", output)

    def cache_keys(self):
        common = {
            "model": self.job["build_config"]["model"],
            "trajectory": self.inputs["trajectory"], "step_index": self.inputs["step_index"],
            "current_image_digest": classifier_module._image_digest(self.current),
            "next_image_digest": classifier_module._image_digest(self.following),
            "action": self.inputs["action"], "summary": self.inputs["summary"],
            "previous_summary": self.inputs["previous_summary"], "next_summary": self.inputs["next_summary"],
        }
        keys = {classifier_module.LEGACY_PROMPT_VERSION: classifier_module.QwenIntermediateStateClassifier._key(
            {"version": classifier_module.LEGACY_PROMPT_VERSION, **common})}
        for version in (classifier_module.PREVIOUS_PROMPT_VERSION, classifier_module.PROMPT_VERSION):
            keys[version] = classifier_module.QwenIntermediateStateClassifier._key({
                "version": version, **common, "task": self.GOAL,
                "after_image_digest": classifier_module._image_digest(self.after)})
        return keys

    def test_offline_probe_uses_all_three_cache_contracts_and_normalized_sources(self):
        keys = self.cache_keys()
        v2, v4, v5 = (classifier_module.LEGACY_PROMPT_VERSION,
                      classifier_module.PREVIOUS_PROMPT_VERSION, classifier_module.PROMPT_VERSION)
        legacy = {"is_intermediate": True, "category": "loading", "confidence": 0.9, "reason": self.BODY}
        previous = {"is_intermediate": False, "category": "none", "confidence": 0.9,
                    "reason": self.BODY, "observation": self.SUMMARY}
        current = {
            "version": v5, "normalized_result": {"category": "loading", "confidence": 0.9,
                                                  "reason": self.BODY, "observation": self.SUMMARY},
            "raw_response": json.dumps(previous),
            "classification_source": {"version": v2, "cache_key": keys[v2]},
            "observation_source": {"version": v4, "cache_key": keys[v4]},
            "classification_changed": True,
        }
        self.cache.write_text(json.dumps({keys[v2]: json.dumps(legacy),
                                         keys[v4]: {"version": v4, "raw_response": json.dumps(previous)},
                                         keys[v5]: current}), encoding="utf-8")
        before = self.cache.read_bytes()
        output = io.StringIO()
        with patch.object(diagnostic, "parse_cached_classification", wraps=classifier_module.parse_cached_classification) as parser, \
             patch.object(diagnostic, "model_probe", side_effect=AssertionError("offline model forbidden")), \
             contextlib.redirect_stdout(output):
            code = diagnostic.probe(self.root, self.job["job_id"], env_path=self.env)
        text = output.getvalue()
        self.assertEqual(code, 0)
        self.assertEqual(self.cache.read_bytes(), before)
        self.assertEqual([call.kwargs["version"] for call in parser.call_args_list], [v2, v4, v5])
        for label in ("v2 旧缓存", "v4 旧缓存", "v5 当前缓存"):
            self.assertIn(label + "：解析通过", text)
        self.assertIn('v5 当前缓存 分类来源： {"version": "' + v2 + '"', text)
        self.assertIn('v5 当前缓存 Observation 来源： {"version": "' + v4 + '"', text)
        for value in keys.values():
            self.assertIn(value, text)
        for private in (self.KEY, self.BODY, self.GOAL, self.SUMMARY):
            self.assertNotIn(private, text)

    def test_old_cache_conflicts_are_rejected_without_exposing_content(self):
        for version in (classifier_module.LEGACY_PROMPT_VERSION, classifier_module.PREVIOUS_PROMPT_VERSION):
            with self.subTest(version=version):
                raw = json.dumps({"is_intermediate": False, "category": "loading", "confidence": 0.9,
                                  "reason": self.BODY, "observation": self.SUMMARY})
                item = raw if version == classifier_module.LEGACY_PROMPT_VERSION else {"version": version, "raw_response": raw}
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    diagnostic.parse_probe("旧缓存", item, version)
                self.assertIn("分类字段冲突", output.getvalue())
                self.assertNotIn(self.BODY, output.getvalue())
                self.assertNotIn(self.SUMMARY, output.getvalue())

    def test_untrusted_cache_source_is_not_printed(self):
        v5 = classifier_module.PROMPT_VERSION
        item = {"version": v5, "raw_response": self.BODY,
                "normalized_result": {"category": "none", "confidence": 0.9,
                                      "reason": self.BODY, "observation": self.SUMMARY},
                "classification_source": {"version": v5, "cache_key": self.KEY},
                "observation_source": {"version": v5, "cache_key": "a" * 64}}
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            diagnostic.parse_probe("v5 当前缓存", item, v5)
        self.assertIn("解析失败", output.getvalue())
        for private in (self.KEY, self.BODY, self.SUMMARY):
            self.assertNotIn(private, output.getvalue())

    def test_extra_boolean_does_not_break_current_single_category_probe(self):
        content = json.dumps({"is_intermediate": False, "category": "loading", "confidence": 0.9,
                              "reason": self.BODY, "observation": self.SUMMARY})
        code, output = self.run_probe(self.response(content=content))
        self.assertEqual(code, 0)
        self.assertEqual(len(self.requests), 1)
        self.assertIn("模型调用及内容校验：通过", output)

    def test_length_stops_after_one_request(self):
        code, output = self.run_probe(self.response(finish="length"))
        self.assertEqual(code, 1)
        self.assertEqual(len(self.requests), 1)
        self.assertIn("截断", output)

    def test_empty_choices_are_reported_without_response_body(self):
        code, output = self.run_probe(self.response(choices=False))
        self.assertEqual(code, 1)
        self.assertEqual(len(self.requests), 1)
        self.assertIn("choices 为空", output)

    def test_empty_content_is_reported(self):
        code, output = self.run_probe(self.response(content=""))
        self.assertEqual(code, 1)
        self.assertEqual(len(self.requests), 1)
        self.assertIn("模型返回空内容", output)

    def test_null_content_is_reported_as_type_without_body(self):
        body = self.response()
        body["choices"][0]["message"]["content"] = None
        code, output = self.run_probe(body)
        self.assertEqual(code, 1)
        self.assertEqual(len(self.requests), 1)
        self.assertIn("content 缺失或类型", output)

    def test_invalid_json_is_safely_classified(self):
        code, output = self.run_probe(self.response(content='{"private": "' + self.BODY))
        self.assertEqual(code, 1)
        self.assertEqual(len(self.requests), 1)
        self.assertIn("JSON 不完整或格式错误", output)

    def test_missing_observation_is_safely_classified(self):
        content = json.dumps({"is_intermediate": False, "category": "none", "confidence": 0.9, "reason": self.BODY})
        code, output = self.run_probe(self.response(content=content))
        self.assertEqual(code, 1)
        self.assertEqual(len(self.requests), 1)
        self.assertIn("observation 为空", output)

    def test_http_503_does_not_use_sdk_retries(self):
        code, output = self.run_probe({"error": {"message": self.BODY + " " + self.KEY,
                                               "type": "server_error", "code": "server_error"}}, status=503)
        self.assertEqual(code, 1)
        self.assertEqual(len(self.requests), 1)
        self.assertIn('"http_status": 503', output)

    def test_actual_base_url_override_stops_before_request_and_closes(self):
        with patch.dict(os.environ, {"TRAJECTORY_VLA_API_BASE_URL": "https://other.invalid/v1"}):
            code, output = self.run_probe(self.response())
        self.assertEqual(code, 1)
        self.assertEqual(len(self.requests), 0)
        self.assertIn("实际客户端地址", output)

    def test_changed_frozen_model_stops_before_client_creation(self):
        self.job["build_config"]["model"] = "old-model"
        with patch.object(diagnostic, "QwenIntermediateStateClassifier", side_effect=AssertionError("constructor forbidden")):
            code, output = self.run_probe(self.response())
        self.assertEqual(code, 1)
        self.assertEqual(self.requests, [])
        self.assertEqual(self.clients, [])

    def test_offline_probe_does_not_initialize_client_or_read_env(self):
        output = io.StringIO()
        before = self.cache.read_bytes()
        with patch.object(diagnostic.QwenIntermediateStateClassifier, "__init__", side_effect=AssertionError("offline constructor forbidden")), \
             patch("backend.trajectories_preprocessing.read_env_file", side_effect=AssertionError("offline env read forbidden")), \
             patch.object(diagnostic, "model_probe", side_effect=AssertionError("offline model probe forbidden")), \
             contextlib.redirect_stdout(output):
            code = diagnostic.probe(self.root, self.job["job_id"], env_path=self.env)
        self.assertEqual(code, 0)
        self.assertEqual(self.cache.read_bytes(), before)
        self.assertNotIn(self.SUMMARY, output.getvalue())

    def test_invalid_picture_stops_before_client_creation(self):
        self.current.write_bytes(b"broken-jpeg-fixture")
        with patch.object(diagnostic, "model_probe", side_effect=AssertionError("invalid image must block model")):
            code, output = self.run_probe(self.response(), full=True)
        self.assertEqual(code, 1)
        self.assertEqual(self.requests, [])
        self.assertIn("截图未通过", output)

    def test_changed_annotation_version_stops_before_client_creation(self):
        (self.annotation / "manifest.json").write_text('{"version":"revision-2"}', encoding="utf-8")
        with patch.object(diagnostic, "model_probe", side_effect=AssertionError("changed annotation must block model")):
            code, output = self.run_probe(self.response(), full=True)
        self.assertEqual(code, 1)
        self.assertEqual(self.requests, [])
        self.assertIn("标框修订已变化", output)

    def test_changed_annotation_bytes_stops_before_client_creation(self):
        with (self.annotation / "result.json").open("ab") as handle:
            handle.write(b" ")
        with patch.object(diagnostic, "model_probe", side_effect=AssertionError("changed bytes must block model")):
            code, output = self.run_probe(self.response(), full=True)
        self.assertEqual(code, 1)
        self.assertEqual(self.requests, [])
        self.assertIn("无法确认标框", output)

    def test_sdk_child_debug_logs_are_suppressed_and_logging_is_restored(self):
        stream = io.StringIO()
        logger = logging.getLogger("openai._base_client")
        handler = logging.StreamHandler(stream)
        old_level = logger.level
        old_disabled = logger.disabled
        old_global_disable = logging.root.manager.disable
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
        logger.disabled = False
        logging.disable(logging.NOTSET)
        try:
            code, output = self.run_probe(self.response())
            self.assertEqual(code, 0)
            self.assertEqual(stream.getvalue(), "")
            self.assertEqual(logging.root.manager.disable, logging.NOTSET)
        finally:
            logging.disable(old_global_disable)
            logger.setLevel(old_level)
            logger.disabled = old_disabled
            logger.removeHandler(handler)

    def test_untrusted_metadata_values_are_not_printed(self):
        body = self.response(finish=self.BODY)
        code, output = self.run_probe(body, request_id=self.KEY)
        self.assertEqual(code, 0)
        self.assertEqual(len(self.requests), 1)
        self.assertIn('"request_id": null', output)
        self.assertIn('"finish_reason": "其他或缺失"', output)


if __name__ == "__main__":
    unittest.main()
