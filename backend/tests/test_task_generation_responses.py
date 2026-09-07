from __future__ import annotations

import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from backend.task_generation.config import TaskGenerationConfig, load_model_config
from backend.task_generation.model_client import TaskGenerationModel, parse_json_value, parse_jsonl_tasks
from backend.task_generation.response_parser import valid_task_text
from backend.task_generation.prompts import system_prompt
from backend.task_generation.service import _dependency_tasks, _generate_tasks, run_initial_generation, run_augmentation
from backend.task_generation.jobs import TaskGenerationJobManager
from backend.task_generation.knowledge_base import merged_nodes
from backend.tests.test_task_generation import _write_knowledge_base, _submit_initial
from backend.tests.test_scene_tree_editing import ImmediateExecutor


def config(**kwargs):
    base = TaskGenerationConfig(api_key="synthetic-test-key", base_url="http://local.invalid/v1", model="mock-model", proxy="", verify=True, timeout=10, trust_env=False, max_retries=0, max_concurrent=1, validation_retries=0)
    return replace(base, **kwargs)


def response(content=None, reasoning=None, finish="stop", **message_fields):
    return SimpleNamespace(id="mock-response", model="mock-model", usage={"completion_tokens": 42}, choices=[SimpleNamespace(finish_reason=finish, message=SimpleNamespace(content=content, reasoning_content=reasoning, **message_fields))])


class SequenceModel:
    def __init__(self, responses, **kwargs):
        self.config = config(**kwargs)
        self.responses = iter(responses)
        self.calls = []

    def complete(self, prompt, **kwargs):
        self.calls.append((prompt, kwargs))
        value = next(self.responses)
        if isinstance(value, Exception):
            raise value
        return value


class ResponseParsingTests(unittest.TestCase):
    def test_plain_array_object_wrapper_jsonl_and_fence(self):
        for raw in ['{"task":"a"}', '[{"task":"a"}]', '{"tasks":[{"task":"a"}]}', '```json\n{"task":"a"}\n```', 'analysis </think> {"task":"a"}', '思考说明\nFinal answer: {"task":"a"}', '说明\n**最终答案：**\n{"task":"a"}']:
            with self.subTest(raw=raw):
                self.assertEqual(parse_jsonl_tasks(raw), [{"task": "a"}])
        self.assertEqual(parse_jsonl_tasks('{"task":"a"}\n{\n"task":"b"\n}'), [{"task": "a"}, {"task": "b"}])

    def test_never_mines_first_example_from_analysis(self):
        raw = 'Thinking example:\n```json\n{"task":"..."}\n```\nFinal answer:\n```json\n{"task":"实际操作任务"}\n```'
        self.assertEqual(parse_jsonl_tasks(raw), [{"task": "实际操作任务"}])
        for ambiguous in [raw.replace('Final answer:', 'Another example:'), 'Here is a thinking process: {"task":"a"}', '<think>{"task":"a"}', '```json\n{"task":"a"}\n```\n```json\n{"task":"b"}\n```', '{"task":"a"}\n{"task":']:
            with self.subTest(raw=ambiguous):
                with self.assertRaises(ValueError):
                    parse_jsonl_tasks(ambiguous)

    def test_placeholder_schema_and_type_validation(self):
        for value in ['...', '……', ' . . . ', '生成的任务描述', '变体任务描述', '<task>', '打开App搜索...', '', None, {}, 123, 'TASK DESCRIPTION']:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    valid_task_text(value)
        self.assertEqual(valid_task_text('打开应用商店搜索 Keep 并安装'), '打开应用商店搜索 Keep 并安装')
        with self.assertRaises(ValueError):
            parse_json_value('{"a":1}\n{"a":2}', dict)


class ModelClientTests(unittest.TestCase):
    def make_client(self, root, reply, **kwargs):
        model = TaskGenerationModel(config(**kwargs), call=lambda _: '', diagnostics_dir=root)
        model._call_override = None
        model._client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=Mock(return_value=reply))))
        return model

    def test_final_content_is_selected_and_reasoning_preserved_only_in_trace(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            model = self.make_client(root, response('{"tasks":[{"task":"正常任务"}]}', 'reasoning containing synthetic-test-key'))
            raw = model.complete('业务输入 synthetic-test-key', stage='generating', item_id='unit-1')
            self.assertEqual(parse_jsonl_tasks(raw), [{"task": "正常任务"}])
            record = json.loads((root / (raw.trace_id + '.json')).read_text(encoding='utf-8'))
            self.assertNotIn('synthetic-test-key', json.dumps(record))
            self.assertEqual(record['stage'], 'generating')
            self.assertEqual(record['item_id'], 'unit-1')
            self.assertEqual(record['response']['usage']['completion_tokens'], 42)
            self.assertIn('[REDACTED]', record['response']['choices'][0]['message']['reasoning_content'])
            request = model._client.chat.completions.create.call_args.kwargs
            self.assertNotIn('extra_body', request)
            self.assertNotIn('response_format', request)
            self.assertEqual(request['max_tokens'], 8192)

    def test_reasoning_only_and_truncated_content_never_become_tasks(self):
        for reply in [response(None, 'reasoning only'), response('{"task":"完整但被截断的答案"}', finish='length'), response('<think>analysis'), response('analysis </think>'), response(None), response('{}', refusal='cannot'), response('{}', finish='tool_calls')]:
            with self.subTest(reply=reply), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                model = self.make_client(root, reply)
                with self.assertRaisesRegex(RuntimeError, '诊断记录'):
                    model.complete('mock')
                record = json.loads(next(root.glob('*.json')).read_text(encoding='utf-8'))
                self.assertIn('response', record)
                self.assertIn('error', record)

    def test_final_text_parts_and_explicit_provider_options(self):
        with tempfile.TemporaryDirectory() as temp:
            model = self.make_client(Path(temp), response([{'type': 'reasoning', 'text': 'ignore'}, {'type': 'text', 'text': '{"task":"正常"}'}]), json_mode=True, extra_body={'enable_thinking': False})
            self.assertEqual(model.complete('mock'), '{"task":"正常"}')
            kwargs = model._client.chat.completions.create.call_args.kwargs
            self.assertEqual(kwargs['response_format'], {'type': 'json_object'})
            self.assertEqual(kwargs['extra_body'], {'enable_thinking': False})

    def test_http_error_redaction_and_retry_records(self):
        with tempfile.TemporaryDirectory() as temp, patch('backend.task_generation.model_client.time.sleep'):
            root = Path(temp)
            model = self.make_client(root, response('{}'), max_retries=1)
            model._client.chat.completions.create.side_effect = RuntimeError('Bearer exposed-token https://user:pass@local/?api_key=synthetic-test-key')
            with self.assertRaises(RuntimeError) as caught:
                model.complete('mock')
            self.assertNotIn('exposed-token', str(caught.exception))
            self.assertNotIn('user:pass', str(caught.exception))
            self.assertEqual(len(list(root.glob('*.json'))), 2)
            for file in root.glob('*.json'):
                self.assertNotIn('synthetic-test-key', file.read_text(encoding='utf-8'))

    def test_concurrent_traces_are_independent_and_can_be_disabled(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            model = self.make_client(root, response('{"task":"正常"}'))
            with ThreadPoolExecutor(max_workers=4) as executor:
                values = list(executor.map(lambda index: model.complete('mock', item_id=str(index)), range(8)))
            self.assertEqual(len({value.trace_id for value in values}), 8)
            self.assertEqual(len(list(root.glob('*.json'))), 8)
            self.assertEqual(list(root.glob('*.tmp')), [])
            disabled = self.make_client(root / 'disabled', response('{}'), trace_enabled=False)
            self.assertIsNone(disabled.complete('mock').trace_id)
            self.assertFalse((root / 'disabled').exists())


class OutputValidationTests(unittest.TestCase):
    def test_count_placeholder_and_duplicate_failures_keep_valid_tasks(self):
        model = SequenceModel(['{"tasks":[{"task":"..."},{"task":"生成的任务描述"},{"task":"正常任务甲"},{"task":"正常任务甲"}]}'])
        tasks, errors = _generate_tasks('mock', 5, model=model, item_id='unit')
        self.assertEqual(tasks, [{'task': '正常任务甲'}])
        self.assertIn('仅得到 1', errors[0]['error'])

    def test_bounded_top_up_preserves_order_and_deduplicates_previous_answers(self):
        model = SequenceModel(['[{"task":"正常任务甲"}]', '{"tasks":[{"task":"正常任务甲"},{"task":"正常任务乙"}]}'], validation_retries=1)
        tasks, errors = _generate_tasks('生成 2 条', 2, model=model, item_id='unit')
        self.assertEqual([task['task'] for task in tasks], ['正常任务甲', '正常任务乙'])
        self.assertEqual(errors, [])
        self.assertEqual(len(model.calls), 2)
        self.assertIn('还缺少的 1 条', model.calls[1][0])

    def test_reasoning_only_repair_is_bounded(self):
        model = SequenceModel([RuntimeError('没有最终答案'), 'Here is a thinking process'], validation_retries=1)
        tasks, errors = _generate_tasks('mock', 1, model=model, item_id='unit')
        self.assertEqual(tasks, [])
        self.assertTrue(errors)
        self.assertEqual(len(model.calls), 2)

    def test_bad_dependency_enums_and_weak_placeholders_never_fall_back_to_zero(self):
        invalid = [{'dependency_relationships': 'zero/weak/strong'}, {}, {'dependency_relationships': []}, {'dependency_relationships': 'weak', 'pre_task': '...'}, {'dependency_relationships': 'zero', 'pre_task': 'null'}]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError):
                _dependency_tasks({'app': 'AppA', 'task': '查看收藏'}, kb_root=Path('.'), model=SequenceModel([json.dumps(value)]))

    def test_zero_and_strong_still_have_compatible_fields(self):
        for relationship in ['zero', 'strong']:
            rows = _dependency_tasks({'app': 'AppA', 'task': '操作任务'}, kb_root=Path('.'), model=SequenceModel([json.dumps({'dependency_relationships': relationship, 'pre_task': None})]))
            self.assertEqual(rows[0]['pre_dependency'], relationship)
            self.assertIsNone(rows[0]['pre_task_uuid'])
            if relationship == 'strong':
                self.assertEqual(rows[0]['status'], '-2')

    def test_generation_job_partial_and_failed_status_are_not_false_success(self):
        for text, expected, count in [('正常的 AppA 操作任务', 'partial', 1), ('...', 'failed', 0)]:
            with self.subTest(text=text), tempfile.TemporaryDirectory() as temp:
                root = Path(temp); kb = root / 'kb'; _write_knowledge_base(kb)
                fake = SequenceModel([json.dumps({'tasks': [{'task': text}, {'task': '...'}]}), '{"dependency_relationships":"zero","pre_task":null}'])
                def runner(ids, n, *, kb_root, progress):
                    return run_initial_generation(ids, n + 1, kb_root=kb_root, progress=progress, model=fake)
                manager = TaskGenerationJobManager(root/'jobs', root/'runs', root/'exports', kb, root/'logs', executor=ImmediateExecutor(), initial_runner=runner)
                try:
                    job = _submit_initial(manager, kb)
                    self.assertEqual(manager.get(job['job_id'])['status'], expected)
                    self.assertEqual(len(manager.results(job['job_id'])), count)
                    self.assertTrue(manager.get(job['job_id'])['errors'])
                finally:
                    manager.shutdown()

    def test_failed_dependency_does_not_drop_other_valid_tasks_in_same_unit(self):
        with tempfile.TemporaryDirectory() as temp:
            kb = Path(temp); _write_knowledge_base(kb)
            identifier = merged_nodes(kb)[0]['node_id']
            fake = SequenceModel(['[{"task":"正常任务甲"},{"task":"正常任务乙"}]', '{"dependency_relationships":"zero/weak/strong"}', '{"dependency_relationships":"zero","pre_task":null}'])
            outcome = run_initial_generation([identifier], 2, kb_root=kb, progress=lambda _: None, model=fake)
            self.assertEqual([row['task'] for row in outcome['results']], ['正常任务乙'])
            self.assertEqual(outcome['errors'][0]['stage'], 'dependency')

    def test_empty_resource_prompt_explicitly_avoids_inventing_entities(self):
        prompt = system_prompt('长视频', '播放', '搜索', '描述', '爱奇艺', [], '', 5)
        self.assertIn('先验为空时', prompt)
        self.assertIn('不编造具体片名', prompt)
        self.assertIn('长度恰好 5', prompt)
        self.assertNotIn('"task":"生成的任务描述"', prompt)

    def test_augmentation_also_rejects_placeholder_and_retains_valid_variants(self):
        import pandas as pd
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); kb = root / 'kb'; _write_knowledge_base(kb)
            path = root / 'seeds.xlsx'
            pd.DataFrame([{'app': 'AppA', 'task': '失败种子', 'scene': '场景A', 'capability': '能力A', 'sub_capability': '子能力A'}]).to_excel(path, index=False)
            outcome = run_augmentation(path, 2, kb_root=kb, progress=lambda _: None, model=SequenceModel(['[{"task":"..."},{"task":"AppA 的有效变体"}]']))
            self.assertEqual([row['task'] for row in outcome['results']], ['AppA 的有效变体'])
            self.assertTrue(outcome['errors'])


class ConfigTests(unittest.TestCase):
    def test_defaults_and_legacy_key_fallback(self):
        with patch.dict('os.environ', {}, clear=True), patch('backend.task_generation.config.load_env_values', return_value={'YUNAI_API_KEY': 'legacy-test-key'}):
            loaded = load_model_config()
            self.assertEqual(loaded.api_key, 'legacy-test-key')
            self.assertEqual(loaded.generation_max_tokens, 8192)
            self.assertFalse(loaded.json_mode)
            self.assertEqual(loaded.extra_body, {})

    def test_rejects_invalid_extra_body_and_token_budget(self):
        for value in [{'TASK_GENERATION_EXTRA_BODY': '[]'}, {'TASK_GENERATION_EXTRA_BODY': '{"messages":[]}'}, {'TASK_GENERATION_GENERATION_MAX_TOKENS': '0'}]:
            with self.subTest(value=value), patch.dict('os.environ', {}, clear=True), patch('backend.task_generation.config.load_env_values', return_value=value):
                with self.assertRaises(ValueError):
                    load_model_config()


if __name__ == '__main__':
    unittest.main()
