"""Execution model overrides preserve engine/mode isolation and frozen requests."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.phone_factory import DEFAULT_CONFIG, PhoneFactoryError, PhoneFactoryStore, create_router
from backend.phonefactory_client import build_parser, cmd_start_run
from backend.pipelines import PipelineError
from backend.tests.test_pipeline_model_config import MODELS, ModelRunTests
from backend.tests import test_pipeline_result_batches as pipeline_fixture


class NewAgentDefaultsTests(unittest.TestCase):
    def test_engines_modes_and_older_clients_keep_separate_defaults(self):
        with TemporaryDirectory() as directory:
            store = PhoneFactoryStore(Path(directory))
            app = FastAPI()
            app.include_router(create_router(store))
            app.include_router(create_router(store, prefix='/api/model-iter', mode='modeliter'))
            with TestClient(app) as client:
                collection, evaluation = '/api/phone-factory/config', '/api/model-iter/config'
                self.assertEqual(client.get(collection).json()['newagent_model_defaults'], {'execution_model': ''})
                for endpoint, name in [(collection, 'collect-model'), (evaluation, 'evaluate-model')]:
                    response = client.post(endpoint, json={**DEFAULT_CONFIG, 'model_defaults': MODELS,
                        'newagent_model_defaults': {'execution_model': ' ' + name + ' '}})
                    self.assertEqual(response.status_code, 200, response.text)
                # Old clients send only their historical Pipeline defaults.
                changed_pipeline = {**MODELS, 'execution_model': 'changed-pipeline'}
                self.assertEqual(client.post(collection, json={**DEFAULT_CONFIG,
                    'model_defaults': changed_pipeline}).status_code, 200)
                for endpoint, name in [(collection, 'collect-model'), (evaluation, 'evaluate-model')]:
                    self.assertEqual(client.get(endpoint).json()['newagent_model_defaults'], {'execution_model': name})
                self.assertEqual(client.get(evaluation).json()['model_defaults'], MODELS)
                # Explicitly clearing NewAgent does not clear Pipeline or evaluation.
                client.post(collection, json={**DEFAULT_CONFIG, 'newagent_model_defaults': {}})
                current = client.get(collection).json()
                self.assertEqual(current['newagent_model_defaults'], {'execution_model': ''})
                self.assertEqual(current['model_defaults'], changed_pipeline)
                for invalid in [None, {'execution_model': 1}, {'execution_model': 'bad\n'},
                                {'planner_config': {}}, {'api_key': 'not-accepted'}]:
                    before = client.get(collection).json()
                    response = client.post(collection, json={**DEFAULT_CONFIG, 'newagent_model_defaults': invalid})
                    self.assertEqual(response.status_code, 400, response.text)
                    self.assertEqual(client.get(collection).json(), before)


class NewAgentDispatchTests(unittest.TestCase):
    setUp = ModelRunTests.setUp
    request = ModelRunTests.request
    client = ModelRunTests.client

    def test_collection_and_collect_evaluate_freeze_model_and_do_not_read_new_defaults(self):
        for mode in ('generate', 'modeliter'):
            self.store.add_task(self.upload, mode=mode)
            data = self.request(request_id='newagent-model-' + mode, agent_type='newagent',
                                execution_model='served-model-id')
            result = self.store.remote_start(data, mode=mode)
            args = [call for call in self.calls if call[0] == 'start-run'][-1]
            self.assertEqual(args[args.index('--execution-model') + 1], 'served-model-id')
            self.assertNotIn('--planner-config', args)
            self.store.dispatch('config', 'POST', {**DEFAULT_CONFIG,
                'newagent_model_defaults': {'execution_model': 'new-default'}, 'model_defaults': MODELS}, mode=mode)
            starts = len([call for call in self.calls if call[0] == 'start-run'])
            replay = self.store.remote_start(data, mode=mode)
            self.assertTrue(replay['reused'])
            self.assertEqual(replay['request'], result['request'])
            self.assertEqual(len([call for call in self.calls if call[0] == 'start-run']), starts)
            with self.assertRaises((ValueError, PhoneFactoryError)):
                self.store.remote_start({**data, 'execution_model': 'different-model'}, mode=mode)
            legacy = self.request(request_id='no-model-' + mode, agent_type='newagent')
            old = self.store.remote_start(legacy, mode=mode)
            frozen = old['request'].get('metadata', old['request'])
            self.assertNotIn('execution_model', frozen)
            self.assertNotIn('planner_config', frozen)

    def test_missing_or_unreachable_capability_cannot_silently_drop_newagent_model(self):
        self.store.add_task(self.upload)
        remote = self.store.run_client
        def old_collector(args):
            result = remote(args)
            if args[0] == 'capabilities':
                value = json.loads(result['output'])
                value['agent_types']['newagent'].pop('model_overrides', None)
                result['output'] = json.dumps(value)
            return result
        self.store.run_client = old_collector
        with self.assertRaisesRegex(PhoneFactoryError, 'newagent.*模型覆盖'):
            self.store.remote_start(self.request(execution_model='served-model-id'))
        self.assertFalse(any(call[0] == 'start-run' for call in self.calls))
        options = self.store.runtime._options(self.request(execution_model='served-model-id'), 'generate')
        with patch.object(self.store, '_remote_result', side_effect=PhoneFactoryError('unreachable', 502)):
            with self.assertRaisesRegex(PhoneFactoryError, 'newagent'):
                self.store.runtime._require_agent_capability(options)
            self.store.runtime._require_agent_capability({k: v for k, v in options.items() if k != 'execution_model'})

    def test_actual_cli_newagent_multipart_contains_execution_model_only(self):
        workbook, apps = self.root / 'task.xlsx', self.root / 'apps.json'
        workbook.write_bytes(b'workbook')
        apps.write_text('[]')
        command = ['start-run', str(workbook), str(apps), '--agent-type', 'newagent',
                   '--execution-model', 'served-model-id']
        with patch('backend.phonefactory_client.post_multipart', return_value={}) as post:
            cmd_start_run(build_parser().parse_args(command))
        fields = post.call_args.args[1]
        self.assertEqual(fields['agent_type'], 'newagent')
        self.assertEqual(fields['execution_model'], 'served-model-id')
        self.assertNotIn('planner_config', fields)


class NewAgentAutomaticPipelineTests(unittest.TestCase):
    setUp = pipeline_fixture.PipelineResultBatchTests.setUp
    request = pipeline_fixture.PipelineResultBatchTests.request

    def client(self, args):
        result = pipeline_fixture.PipelineResultBatchTests.client(self, args)
        if args[0] == 'capabilities':
            value = json.loads(result['output'])
            value['agent_types']['newagent']['model_overrides'] = True
            result['output'] = json.dumps(value)
        return result

    def test_pipeline_newagent_model_is_frozen_before_dispatch(self):
        request = self.request('newagent-auto')
        request['collection_config'].update(agent_type='newagent', execution_model='frozen-served-id')
        pipeline = self.manager.create(request)
        self.factory.dispatch('config', 'POST', {**DEFAULT_CONFIG,
            'newagent_model_defaults': {'execution_model': 'changed-default'}})
        self.manager.tick(pipeline['pipeline_id'])
        current = self.manager.get(pipeline['pipeline_id'])
        self.assertEqual(current['status'], 'running', current.get('error'))
        run = self.factory.collection_runs.get(current['collection_run_ids'][0])
        self.assertEqual(run['request']['metadata']['execution_model'], 'frozen-served-id')
        self.assertNotIn('planner_config', run['request']['metadata'])
        with self.assertRaises(PipelineError):
            self.manager.create({**request, 'collection_config': {**request['collection_config'], 'execution_model': 'changed'}})
