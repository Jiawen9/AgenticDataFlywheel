"""Mock-only model transport tests; no credentials, network or production data."""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from PIL import Image

from backend.trajectories_tree.intermediate_state_classifier import QwenIntermediateStateClassifier
from backend.trajectories_tree.state_alignment_reviewer import QwenStateAlignmentReviewer
from backend.pipeline_retry_errors import ModelOutputError, failure_from_exception


CLASSIFICATION = json.dumps({"is_intermediate": False, "category": "none", "confidence": 0.95,
                             "reason": "正常业务页面", "observation": "页面显示订单列表。"}, ensure_ascii=False)
ALIGNMENT = json.dumps({"same_task_state": True, "confidence": 0.95, "reason": "截图展示相同的订单列表"}, ensure_ascii=False)


class TreeModelTransportTests(unittest.TestCase):
    def fixture(self, root, kind, model, *, content, finish="stop", reasoning=None):
        cls = QwenIntermediateStateClassifier if kind == "classification" else QwenStateAlignmentReviewer
        instance = object.__new__(cls)
        instance.model = model
        instance.cache_path = root / f"{kind}.json"
        instance.cache = {"unrelated-entry": "keep"}
        instance.cache_path.write_text(json.dumps(instance.cache), encoding="utf-8")
        message = SimpleNamespace(content=content, reasoning_content=reasoning, reasoning=reasoning)
        create = Mock(return_value=SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason=finish)]))
        instance.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        image = root / "screenshot.png"
        Image.new("RGB", (10, 10), "white").save(image)
        if kind == "classification":
            invoke = lambda: instance.classify(trajectory="task-1", step_index=1, current_image_path=image,
                                               next_image_path=None, action={"action": "wait"}, summary="等待",
                                               previous_summary="", next_summary="", task="查看订单")
        else:
            invoke = lambda: instance.review(trajectory="task-1", skipped_steps=[],
                                             candidate_image_paths=[image], reference_image_paths=[image],
                                             candidate_steps=[{"action": "wait"}], reference_steps=[{"action": "wait"}])
        return instance, create, invoke

    def test_max_model_json_mode_budget_and_successful_cache_reuse(self):
        for kind, content in (("classification", CLASSIFICATION), ("alignment", ALIGNMENT)):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                instance, create, invoke = self.fixture(Path(directory), kind, "qwen3.8-max", content=content,
                                                        reasoning="This must not become the result")
                result = invoke()
                kwargs = create.call_args.kwargs
                self.assertEqual(kwargs["model"], "qwen3.8-max")
                self.assertEqual(kwargs["max_tokens"], 2048)
                self.assertEqual(kwargs["response_format"], {"type": "json_object"})
                self.assertEqual(kwargs["temperature"], 0.0)
                self.assertEqual(kwargs["extra_body"], {"enable_thinking": False, "chat_template_kwargs": {"enable_thinking": False}})
                self.assertEqual(result.raw_response, content)
                self.assertFalse(result.cached)
                cached = invoke()
                self.assertTrue(cached.cached)
                self.assertEqual(cached.raw_response, content)
                create.assert_called_once()

    def test_other_models_retain_request_parameters(self):
        for kind, content, limit in (("classification", CLASSIFICATION, 700), ("alignment", ALIGNMENT, 250)):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                _, create, invoke = self.fixture(Path(directory), kind, "qwen-existing-model", content=content)
                invoke()
                kwargs = create.call_args.kwargs
                self.assertEqual(kwargs["max_tokens"], limit)
                self.assertNotIn("response_format", kwargs)
                self.assertEqual(kwargs["temperature"], 0.0)
                self.assertEqual(kwargs["extra_body"], {"enable_thinking": False, "chat_template_kwargs": {"enable_thinking": False}})

    def test_truncated_response_fails_without_caching_even_if_json_is_complete(self):
        for kind, content in (("classification", CLASSIFICATION), ("alignment", ALIGNMENT)):
            for model in ("qwen3.8-max", "qwen-existing-model"):
                with self.subTest(kind=kind, model=model), tempfile.TemporaryDirectory() as directory:
                    instance, create, invoke = self.fixture(Path(directory), kind, model, content=content, finish="length")
                    before = instance.cache_path.read_bytes()
                    with self.assertRaisesRegex(ValueError, "finish_reason=length"):
                        invoke()
                    self.assertEqual(instance.cache, {"unrelated-entry": "keep"})
                    self.assertEqual(instance.cache_path.read_bytes(), before)
                    create.assert_called_once()

    def test_reasoning_only_response_is_not_used_or_cached(self):
        for kind, content in (("classification", CLASSIFICATION), ("alignment", ALIGNMENT)):
            for body in (None, "", "  "):
                with self.subTest(kind=kind, body=body), tempfile.TemporaryDirectory() as directory:
                    instance, _, invoke = self.fixture(Path(directory), kind, "qwen3.8-max", content=body, reasoning=content)
                    before = instance.cache_path.read_bytes()
                    with self.assertRaisesRegex(ValueError, "empty"):
                        invoke()
                    self.assertEqual(instance.cache_path.read_bytes(), before)
                    self.assertEqual(instance.cache, {"unrelated-entry": "keep"})

    def test_invalid_final_content_fails_before_cache_write(self):
        for kind, reasoning in (("classification", CLASSIFICATION), ("alignment", ALIGNMENT)):
            for content in ("Explanation without JSON", '{"confidence":0.95}'):
                with self.subTest(kind=kind, content=content), tempfile.TemporaryDirectory() as directory:
                    instance, _, invoke = self.fixture(Path(directory), kind, "qwen3.8-max", content=content, reasoning=reasoning)
                    before = instance.cache_path.read_bytes()
                    with self.assertRaises(ValueError):
                        invoke()
                    self.assertEqual(instance.cache_path.read_bytes(), before)
                    self.assertEqual(instance.cache, {"unrelated-entry": "keep"})

    def test_conflicting_classification_is_retryable_without_accepting_or_caching_it(self):
        bad = json.dumps({"is_intermediate": False, "category": "loading", "confidence": 0.9,
                          "reason": "secret-response", "observation": "secret-observation"})
        with tempfile.TemporaryDirectory() as directory:
            instance, create, invoke = self.fixture(Path(directory), "classification", "mock", content=bad)
            before = instance.cache_path.read_bytes()
            with self.assertRaises(ModelOutputError) as caught:
                invoke()
            self.assertEqual(caught.exception.code, "inconsistent_fields")
            failure = failure_from_exception(caught.exception)
            self.assertEqual(failure["category"], "model_output")
            self.assertTrue(failure["retryable"])
            self.assertNotIn("secret", json.dumps(failure))
            self.assertEqual(instance.cache_path.read_bytes(), before)
            create.return_value.choices[0].message.content = CLASSIFICATION
            result = invoke()
            self.assertFalse(result.is_intermediate)
            self.assertEqual(result.category, "none")
            self.assertEqual(instance.cache["unrelated-entry"], "keep")
            self.assertEqual(create.call_count, 2)
            self.assertTrue(invoke().cached)
            self.assertEqual(create.call_count, 2)

    def test_bad_cached_output_is_refreshed_without_losing_other_entries(self):
        for kind, content in (("classification", CLASSIFICATION), ("alignment", ALIGNMENT)):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                instance, create, invoke = self.fixture(Path(directory), kind, "mock", content=content)
                invoke()
                cache_key = next(key for key in instance.cache if key != "unrelated-entry")
                invalid = '{"confidence":0.9}'
                instance.cache[cache_key] = ({"raw_response": invalid} if kind == "classification" else invalid)
                instance.cache_path.write_text(json.dumps(instance.cache), encoding="utf-8")
                create.reset_mock()
                refreshed = invoke()
                self.assertFalse(refreshed.cached)
                self.assertEqual(refreshed.raw_response, content)
                create.assert_called_once()
                self.assertEqual(instance.cache["unrelated-entry"], "keep")
                self.assertTrue(invoke().cached)
                create.assert_called_once()

    def test_invalid_legacy_entry_does_not_block_valid_current_cache(self):
        from backend.trajectories_tree.intermediate_state_classifier import LEGACY_PROMPT_VERSION, _image_digest
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            instance, create, invoke = self.fixture(root, "classification", "mock", content=CLASSIFICATION)
            invoke()
            legacy_key = instance._key({"version": LEGACY_PROMPT_VERSION, "model": "mock",
                "trajectory": "task-1", "step_index": 1, "current_image_digest": _image_digest(root / "screenshot.png"),
                "next_image_digest": "", "action": {"action": "wait"}, "summary": "等待",
                "previous_summary": "", "next_summary": ""})
            instance.cache[legacy_key] = "invalid-private-response"
            instance.cache_path.write_text(json.dumps(instance.cache), encoding="utf-8")
            before = instance.cache_path.read_bytes()
            create.reset_mock()
            self.assertTrue(invoke().cached)
            create.assert_not_called()
            self.assertEqual(instance.cache_path.read_bytes(), before)

    def test_response_envelope_errors_are_typed_and_do_not_enter_cache(self):
        for kind in ("classification", "alignment"):
            for choices, content, code in (([], "", "empty_choices"), (None, "", "empty_choices"),
                                           (True, {"secret": "body"}, "invalid_content"),
                                           (True, None, "empty_content")):
                with self.subTest(kind=kind, code=code), tempfile.TemporaryDirectory() as directory:
                    instance, create, invoke = self.fixture(Path(directory), kind, "mock", content=content)
                    if choices is not True:
                        create.return_value.choices = choices
                    before = instance.cache_path.read_bytes()
                    with self.assertRaises(ModelOutputError) as caught:
                        invoke()
                    self.assertEqual(caught.exception.code, code)
                    self.assertTrue(failure_from_exception(caught.exception)["retryable"])
                    self.assertEqual(instance.cache_path.read_bytes(), before)
                    create.assert_called_once()

    def test_null_text_and_boolean_or_missing_confidence_are_not_valid_results(self):
        for kind, valid in (("classification", CLASSIFICATION), ("alignment", ALIGNMENT)):
            cases = [("confidence", True), ("confidence", None), ("confidence", "missing"),
                     ("reason", None), ("reason", False), ("reason", {})]
            if kind == "classification":
                cases += [("observation", None), ("observation", False), ("observation", {})]
            for field, value in cases:
                with self.subTest(kind=kind, field=field, value=value), tempfile.TemporaryDirectory() as directory:
                    payload = json.loads(valid)
                    if value == "missing":
                        payload.pop(field)
                    else:
                        payload[field] = value
                    instance, create, invoke = self.fixture(Path(directory), kind, "mock", content=json.dumps(payload))
                    before = instance.cache_path.read_bytes()
                    with self.assertRaises(ModelOutputError) as caught:
                        invoke()
                    self.assertEqual(caught.exception.code, "invalid_schema")
                    self.assertTrue(failure_from_exception(caught.exception)["retryable"])
                    self.assertEqual(instance.cache_path.read_bytes(), before)
                    create.assert_called_once()

