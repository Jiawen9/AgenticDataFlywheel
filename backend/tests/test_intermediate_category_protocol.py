"""Single-category model protocol and cache migrations, without network or data writes."""
from __future__ import annotations

from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from PIL import Image

from backend.pipeline_retry_errors import ModelOutputError, failure_from_exception
from backend.trajectories_tree.intermediate_state_classifier import (
    CATEGORIES,
    LEGACY_PROMPT_VERSION,
    PREVIOUS_PROMPT_VERSION,
    PROMPT_VERSION,
    QwenIntermediateStateClassifier,
    _image_digest,
    parse_cached_classification,
    parse_classification_response,
)


def answer(category="none", *, legacy=False, observation="订单列表从空白变为显示三条订单。", **values):
    payload = {"category": category, "confidence": 0.93, "reason": "明确的页面状态", "observation": observation}
    if legacy:
        payload["is_intermediate"] = category != "none"
    payload.update(values)
    return json.dumps(payload, ensure_ascii=False)


class SingleCategoryProtocolTests(unittest.TestCase):
    def test_all_categories_derive_boolean_without_requesting_duplicate_field(self):
        self.assertEqual(CATEGORIES, {"none", "advertisement", "loading", "update_popup",
                                     "permission_privacy_popup", "transient_error", "system_overlay"})
        for category in sorted(CATEGORIES):
            for extra in ({}, {"is_intermediate": category == "none"}, {"is_intermediate": "ignored"}):
                with self.subTest(category=category, extra=extra):
                    result = parse_classification_response(answer(category, **extra), require_observation=True)
                    self.assertEqual(result.category, category)
                    self.assertIs(result.is_intermediate, category != "none")
                    self.assertIs(result.to_dict(0.8)["effective_intermediate"], category != "none")

    def test_missing_invalid_or_null_fields_remain_retryable_model_output_errors(self):
        cases = [("category", None), ("category", "missing"), ("category", "business"), ("category", False),
                 ("category", []), ("confidence", None), ("confidence", "missing"), ("confidence", True),
                 ("confidence", -0.1), ("confidence", 1.1), ("confidence", float("nan")),
                 ("reason", None), ("reason", ""), ("observation", None), ("observation", ""),
                 ("observation", "missing")]
        for field, value in cases:
            with self.subTest(field=field, value=value):
                payload = json.loads(answer())
                if value == "missing":
                    payload.pop(field)
                else:
                    payload[field] = value
                with self.assertRaises(ModelOutputError) as caught:
                    parse_classification_response(json.dumps(payload), require_observation=True)
                failure = failure_from_exception(caught.exception)
                self.assertEqual(failure["category"], "model_output")
                self.assertTrue(failure["retryable"])

    def test_legacy_protocols_require_both_fields_to_agree(self):
        for version in (LEGACY_PROMPT_VERSION, PREVIOUS_PROMPT_VERSION):
            for category in sorted(CATEGORIES):
                with self.subTest(version=version, category=category):
                    valid = parse_classification_response(answer(category, legacy=True), protocol_version=version)
                    self.assertIs(valid.is_intermediate, category != "none")
                    with self.assertRaises(ModelOutputError) as caught:
                        parse_classification_response(answer(category, legacy=True, is_intermediate=category == "none"),
                                                      protocol_version=version)
                    self.assertEqual(caught.exception.code, "inconsistent_fields")
                    with self.assertRaises(ModelOutputError):
                        parse_classification_response(answer(category), protocol_version=version)


class SingleCategoryCacheTests(unittest.TestCase):
    def fixture(self, root: Path, *, content: str | None = None):
        instance = object.__new__(QwenIntermediateStateClassifier)
        instance.model = "mock-category-model"
        instance.cache_path = root / "classification-cache.json"
        instance.cache = {"unrelated-entry": "keep this byte-equivalent value"}
        instance._cache_lock = threading.RLock()
        image = root / "before.png"
        Image.new("RGB", (10, 10), "white").save(image)
        after = root / "after.png"
        Image.new("RGB", (10, 10), "blue").save(after)
        create = Mock(return_value=SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content=content or answer()), finish_reason="stop")]))
        instance.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        inputs = {"trajectory": "trajectory-1", "step_index": 15, "current_image_path": image,
                  "next_image_path": after, "after_image_path": after, "action": {"action": "wait"},
                  "summary": "订单列表", "previous_summary": "进入订单", "next_summary": "打开订单",
                  "task": "查看订单"}
        self.save(instance)
        return instance, create, inputs

    def save(self, instance):
        instance.cache_path.write_text(json.dumps(instance.cache, ensure_ascii=False), encoding="utf-8")

    def key(self, instance, inputs, version):
        common = {"model": instance.model, "trajectory": inputs["trajectory"], "step_index": inputs["step_index"],
                  "current_image_digest": _image_digest(inputs["current_image_path"]),
                  "next_image_digest": _image_digest(inputs["next_image_path"]) if inputs["next_image_path"] else "",
                  "action": inputs["action"], "summary": inputs["summary"],
                  "previous_summary": inputs["previous_summary"], "next_summary": inputs["next_summary"]}
        if version != LEGACY_PROMPT_VERSION:
            after = inputs["after_image_path"] or inputs["next_image_path"] or inputs["current_image_path"]
            common.update(task=inputs["task"], after_image_digest=_image_digest(after))
        return instance._key({"version": version, **common})

    def seed(self, instance, inputs, version, raw):
        key = self.key(instance, inputs, version)
        instance.cache[key] = raw if version == LEGACY_PROMPT_VERSION else {"version": version, "raw_response": raw}
        self.save(instance)
        return key

    def assert_old_entries_retained(self, instance, old):
        for key, value in old.items():
            self.assertEqual(instance.cache[key], value)
            self.assertEqual(json.loads(instance.cache_path.read_text(encoding="utf-8"))[key], value)

    def assert_source(self, entry, name, version, key):
        self.assertEqual(entry[name], {"version": version, "cache_key": key})

    def test_valid_v4_is_promoted_without_a_model_request_and_raw_answer_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            instance, create, inputs = self.fixture(Path(directory))
            raw = answer("loading", legacy=True)
            previous_key = self.seed(instance, inputs, PREVIOUS_PROMPT_VERSION, raw)
            old = deepcopy(instance.cache)
            result = instance.classify(**inputs)
            self.assertTrue(result.cached)
            self.assertTrue(result.is_intermediate)
            self.assertEqual(result.raw_response, raw)
            create.assert_not_called()
            entry = instance.cache[self.key(instance, inputs, PROMPT_VERSION)]
            self.assertEqual(entry["version"], PROMPT_VERSION)
            self.assertEqual(entry["normalized_result"]["category"], "loading")
            self.assertNotIn("is_intermediate", entry["normalized_result"])
            self.assertEqual(entry["raw_response"], raw)
            for name in ("classification_source", "observation_source"):
                self.assert_source(entry, name, PREVIOUS_PROMPT_VERSION, previous_key)
            self.assert_old_entries_retained(instance, old)
            self.assertEqual(parse_cached_classification(entry).category, "loading")

    def test_v2_classification_and_v4_observation_are_preserved_as_separate_sources(self):
        with tempfile.TemporaryDirectory() as directory:
            instance, create, inputs = self.fixture(Path(directory))
            legacy_raw = answer("advertisement", legacy=True, observation="", confidence=0.97, reason="旧分类理由")
            previous_raw = answer("none", legacy=True, observation="页面出现订单明细。", reason="新分类理由")
            legacy_key = self.seed(instance, inputs, LEGACY_PROMPT_VERSION, legacy_raw)
            previous_key = self.seed(instance, inputs, PREVIOUS_PROMPT_VERSION, previous_raw)
            old = deepcopy(instance.cache)
            result = instance.classify(**inputs)
            create.assert_not_called()
            self.assertEqual((result.category, result.confidence, result.reason), ("advertisement", 0.97, "旧分类理由"))
            self.assertEqual(result.observation, "页面出现订单明细。")
            self.assertEqual(result.raw_response, previous_raw)
            self.assertTrue(result.classification_changed)
            entry = instance.cache[self.key(instance, inputs, PROMPT_VERSION)]
            self.assert_source(entry, "classification_source", LEGACY_PROMPT_VERSION, legacy_key)
            self.assert_source(entry, "observation_source", PREVIOUS_PROMPT_VERSION, previous_key)
            self.assertEqual(entry["normalized_result"]["category"], "advertisement")
            self.assertEqual(json.loads(entry["raw_response"])["category"], "none")
            self.assertEqual(parse_cached_classification(entry).category, "advertisement")
            self.assert_old_entries_retained(instance, old)

    def test_v2_only_requires_one_new_request_for_observation_then_reuses_v5(self):
        with tempfile.TemporaryDirectory() as directory:
            raw = answer("none", observation="广告关闭后显示商品列表。")
            instance, create, inputs = self.fixture(Path(directory), content=raw)
            legacy_key = self.seed(instance, inputs, LEGACY_PROMPT_VERSION,
                                   answer("advertisement", legacy=True, observation="", reason="旧广告分类"))
            old = deepcopy(instance.cache)
            result = instance.classify(**inputs)
            self.assertFalse(result.cached)
            self.assertEqual(result.category, "advertisement")
            self.assertEqual(result.observation, "广告关闭后显示商品列表。")
            self.assertEqual(result.raw_response, raw)
            create.assert_called_once()
            key = self.key(instance, inputs, PROMPT_VERSION)
            self.assert_source(instance.cache[key], "classification_source", LEGACY_PROMPT_VERSION, legacy_key)
            self.assert_source(instance.cache[key], "observation_source", PROMPT_VERSION, key)
            self.assert_old_entries_retained(instance, old)
            self.assertTrue(instance.classify(**inputs).cached)
            create.assert_called_once()

    def test_contradictory_old_caches_are_never_reinterpreted_as_valid_v5(self):
        for invalid_version in (LEGACY_PROMPT_VERSION, PREVIOUS_PROMPT_VERSION, "both"):
            with self.subTest(version=invalid_version), tempfile.TemporaryDirectory() as directory:
                instance, create, inputs = self.fixture(Path(directory), content=answer("none"))
                for version in (LEGACY_PROMPT_VERSION, PREVIOUS_PROMPT_VERSION):
                    if invalid_version in (version, "both"):
                        self.seed(instance, inputs, version, answer("loading", legacy=True, is_intermediate=False))
                old = deepcopy(instance.cache)
                result = instance.classify(**inputs)
                self.assertEqual(result.category, "none")
                self.assertFalse(result.is_intermediate)
                create.assert_called_once()
                key = self.key(instance, inputs, PROMPT_VERSION)
                self.assert_source(instance.cache[key], "classification_source", PROMPT_VERSION, key)
                self.assert_old_entries_retained(instance, old)

    def test_valid_v5_wins_over_later_valid_legacy_cache_without_writes_or_requests(self):
        with tempfile.TemporaryDirectory() as directory:
            instance, create, inputs = self.fixture(Path(directory), content=answer("none"))
            instance.classify(**inputs)
            self.seed(instance, inputs, LEGACY_PROMPT_VERSION, answer("loading", legacy=True))
            self.seed(instance, inputs, PREVIOUS_PROMPT_VERSION, answer("advertisement", legacy=True))
            before = instance.cache_path.read_bytes()
            create.reset_mock()
            result = instance.classify(**inputs)
            self.assertTrue(result.cached)
            self.assertEqual(result.category, "none")
            self.assertFalse(result.is_intermediate)
            create.assert_not_called()
            self.assertEqual(instance.cache_path.read_bytes(), before)

    def test_invalid_v5_source_versions_are_refreshed_without_dropping_other_entries(self):
        for source_name in ("classification_source", "observation_source"):
            for version in ([], {}, None, "unsupported-version"):
                with self.subTest(source=source_name, version=version), tempfile.TemporaryDirectory() as directory:
                    instance, create, inputs = self.fixture(Path(directory), content=answer("none"))
                    instance.classify(**inputs)
                    key = self.key(instance, inputs, PROMPT_VERSION)
                    instance.cache[key][source_name]["version"] = version
                    self.save(instance)
                    other_entries = {entry_key: deepcopy(value) for entry_key, value in instance.cache.items()
                                     if entry_key != key}
                    create.reset_mock()
                    create.return_value.choices[0].message.content = answer("loading")
                    result = instance.classify(**inputs)
                    self.assertFalse(result.cached)
                    self.assertEqual(result.category, "loading")
                    create.assert_called_once()
                    entry = instance.cache[key]
                    for name in ("classification_source", "observation_source"):
                        self.assert_source(entry, name, PROMPT_VERSION, key)
                    self.assertEqual(parse_cached_classification(entry).category, "loading")
                    self.assertEqual(json.loads(instance.cache_path.read_text(encoding="utf-8"))[key], entry)
                    self.assert_old_entries_retained(instance, other_entries)
                    self.assertTrue(instance.classify(**inputs).cached)
                    create.assert_called_once()

    def test_v4_cache_is_not_reused_when_task_action_or_image_changes(self):
        for changed in ("task", "action", "image"):
            with self.subTest(changed=changed), tempfile.TemporaryDirectory() as directory:
                instance, create, inputs = self.fixture(Path(directory), content=answer("none"))
                self.seed(instance, inputs, PREVIOUS_PROMPT_VERSION, answer("loading", legacy=True))
                old = deepcopy(instance.cache)
                if changed == "task":
                    inputs["task"] = "查询另一个订单"
                elif changed == "action":
                    inputs["action"] = {"action": "tap", "x": 5, "y": 6}
                else:
                    Image.new("RGB", (10, 10), "red").save(inputs["current_image_path"])
                self.assertEqual(instance.classify(**inputs).category, "none")
                create.assert_called_once()
                self.assert_old_entries_retained(instance, old)

    def test_failed_cache_promotion_rolls_back_in_memory_and_can_be_retried(self):
        with tempfile.TemporaryDirectory() as directory:
            instance, create, inputs = self.fixture(Path(directory))
            self.seed(instance, inputs, PREVIOUS_PROMPT_VERSION, answer("loading", legacy=True))
            old = deepcopy(instance.cache)
            before = instance.cache_path.read_bytes()
            with patch.object(instance, "_save_cache_unlocked", side_effect=OSError("simulated disk write failure")):
                with self.assertRaises(OSError):
                    instance.classify(**inputs)
            self.assertEqual(instance.cache, old)
            self.assertEqual(instance.cache_path.read_bytes(), before)
            create.assert_not_called()
            result = instance.classify(**inputs)
            self.assertTrue(result.cached)
            self.assertEqual(result.category, "loading")
            create.assert_not_called()
            self.assert_old_entries_retained(instance, old)

    def test_failed_fresh_cache_write_cannot_become_an_in_memory_success_on_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            instance, create, inputs = self.fixture(Path(directory))
            old = deepcopy(instance.cache)
            before = instance.cache_path.read_bytes()
            with patch.object(instance, "_save_cache_unlocked", side_effect=OSError("simulated disk write failure")):
                with self.assertRaises(OSError):
                    instance.classify(**inputs)
            self.assertEqual(instance.cache, old)
            self.assertEqual(instance.cache_path.read_bytes(), before)
            result = instance.classify(**inputs)
            self.assertFalse(result.cached)
            self.assertEqual(create.call_count, 2)
            self.assertTrue(instance.classify(**inputs).cached)
            self.assertEqual(create.call_count, 2)
            self.assert_old_entries_retained(instance, old)

    def test_interrupted_atomic_replace_leaves_old_disk_cache_and_recovers_after_reload(self):
        with tempfile.TemporaryDirectory() as directory:
            instance, create, inputs = self.fixture(Path(directory))
            self.seed(instance, inputs, PREVIOUS_PROMPT_VERSION, answer("loading", legacy=True))
            old = deepcopy(instance.cache)
            before = instance.cache_path.read_bytes()
            key = self.key(instance, inputs, PROMPT_VERSION)
            temporary = instance.cache_path.with_name(f".{instance.cache_path.name}.tmp")
            with patch.object(Path, "replace", side_effect=OSError("simulated replacement interruption")):
                with self.assertRaises(OSError):
                    instance.classify(**inputs)
            self.assertTrue(temporary.exists())
            self.assertIn(key, json.loads(temporary.read_text(encoding="utf-8")))
            self.assertEqual(instance.cache_path.read_bytes(), before)
            self.assertEqual(instance.cache, old)
            create.assert_not_called()
            self.assertEqual(instance.classify(**inputs).category, "loading")
            self.assertFalse(temporary.exists())
            reloaded = object.__new__(QwenIntermediateStateClassifier)
            reloaded.model, reloaded.cache_path, reloaded.client = instance.model, instance.cache_path, instance.client
            reloaded.cache = json.loads(instance.cache_path.read_text(encoding="utf-8"))
            self.assertTrue(reloaded.classify(**inputs).cached)
            create.assert_not_called()
            self.assert_old_entries_retained(instance, old)

    def test_concurrent_steps_keep_both_results_and_original_legacy_entries(self):
        with tempfile.TemporaryDirectory() as directory:
            instance, create, first = self.fixture(Path(directory))
            second = {**first, "step_index": 16}
            unused = {**first, "step_index": 17}
            legacy_first = self.seed(instance, first, LEGACY_PROMPT_VERSION,
                                     answer("loading", legacy=True, observation=""))
            legacy_second = self.seed(instance, second, LEGACY_PROMPT_VERSION,
                                      answer("advertisement", legacy=True, observation=""))
            self.seed(instance, unused, PREVIOUS_PROMPT_VERSION, answer("update_popup", legacy=True))
            old = deepcopy(instance.cache)
            gate = threading.Barrier(2, timeout=10)

            def model_response(**kwargs):
                prompt = kwargs["messages"][1]["content"][0]["text"]
                observation = "第十五步的页面变化。" if "步骤：15\n" in prompt else "第十六步的页面变化。"
                gate.wait()
                return SimpleNamespace(choices=[SimpleNamespace(
                    message=SimpleNamespace(content=answer("none", observation=observation)), finish_reason="stop")])

            create.side_effect = model_response
            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = [executor.submit(instance.classify, **inputs) for inputs in (first, second)]
                results = [future.result(timeout=15) for future in futures]
            self.assertEqual(create.call_count, 2)
            self.assertEqual([result.category for result in results], ["loading", "advertisement"])
            self.assertEqual([result.observation for result in results], ["第十五步的页面变化。", "第十六步的页面变化。"])
            disk = json.loads(instance.cache_path.read_text(encoding="utf-8"))
            for inputs, legacy_key in ((first, legacy_first), (second, legacy_second)):
                key = self.key(instance, inputs, PROMPT_VERSION)
                self.assertEqual(disk[key], instance.cache[key])
                self.assert_source(disk[key], "classification_source", LEGACY_PROMPT_VERSION, legacy_key)
                self.assert_source(disk[key], "observation_source", PROMPT_VERSION, key)
                self.assertEqual(parse_cached_classification(disk[key]).observation, instance.classify(**inputs).observation)
            self.assertEqual(create.call_count, 2)
            self.assert_old_entries_retained(instance, old)

    def test_same_key_competing_responses_return_one_canonical_result_with_matching_sources(self):
        with tempfile.TemporaryDirectory() as directory:
            instance, create, inputs = self.fixture(Path(directory))
            legacy_key = self.seed(instance, inputs, LEGACY_PROMPT_VERSION,
                                   answer("advertisement", legacy=True, observation="", reason="保留的旧分类"))
            old = deepcopy(instance.cache)
            gate = threading.Barrier(2, timeout=10)
            counter_lock, response_numbers = threading.Lock(), []

            def model_response(**kwargs):
                with counter_lock:
                    response_numbers.append(len(response_numbers) + 1)
                    number = response_numbers[-1]
                raw = answer("none", observation=f"候选回答 {number} 的页面变化。")
                gate.wait()
                return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=raw), finish_reason="stop")])

            create.side_effect = model_response
            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = [executor.submit(instance.classify, **inputs) for _ in range(2)]
                results = [future.result(timeout=15) for future in futures]
            self.assertEqual(create.call_count, 2)
            self.assertEqual(sum(result.cached for result in results), 1)
            self.assertEqual(results[0].raw_response, results[1].raw_response)
            self.assertEqual(results[0].observation, results[1].observation)
            self.assertEqual(results[0].classification_source, results[1].classification_source)
            self.assertEqual(results[0].observation_source, results[1].observation_source)
            key = self.key(instance, inputs, PROMPT_VERSION)
            entry = json.loads(instance.cache_path.read_text(encoding="utf-8"))[key]
            self.assertEqual(entry["normalized_result"]["category"], "advertisement")
            self.assertEqual(entry["normalized_result"]["observation"], json.loads(entry["raw_response"])["observation"])
            self.assertEqual(entry["normalized_result"]["observation"], results[0].observation)
            self.assert_source(entry, "classification_source", LEGACY_PROMPT_VERSION, legacy_key)
            self.assert_source(entry, "observation_source", PROMPT_VERSION, key)
            self.assert_old_entries_retained(instance, old)


if __name__ == "__main__":
    unittest.main()
