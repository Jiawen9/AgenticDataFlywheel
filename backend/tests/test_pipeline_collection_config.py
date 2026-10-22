from __future__ import annotations

import base64
import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from backend.phone_factory import DEFAULT_CONFIG, PhoneFactoryStore
from backend.pipelines import PipelineError, PipelineRuntime
from backend.tests.test_phone_factory_runtime import workbook_bytes


class PipelineCollectionConfigTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.calls = []
        self.factory = PhoneFactoryStore(self.root, run_client_fn=self.client)
        self.factory.initialize_settings({'phones': ['phone-a'], 'apps': ['App'],
            'phoneApps': [{'phone_id': 'phone-a', 'app': 'App'}],
            'vla': ['http://mock.invalid/vla'],
            'config': {**DEFAULT_CONFIG, 'capture_logprobs': True}})
        registered = self.factory.add_task({'filename': 'tasks.xlsx', 'description': 'test',
            'content_base64': base64.b64encode(workbook_bytes()).decode()})
        self.batch_id = registered['tasks'][0]['source_batch_id']
        self.runtime = PipelineRuntime(self.root, preprocessing=None, tree=None, quality=None,
            cot=None, factory=self.factory, overview=None)

    def client(self, args):
        self.calls.append(args)
        response = {'ok': True}
        if args[0] == 'capabilities':
            response.update(protocol_version=1, batch_results=True)
        return {'ok': True, 'output': json.dumps(response)}

    def pipeline(self, config):
        return {'pipeline_id': 'pipeline-config', 'batch_id': self.batch_id,
                'name': 'frozen collection', 'collection_config': config}

    def test_legacy_frozen_config_defaults_off_even_when_current_factory_default_is_on(self):
        frozen = self.runtime.collection_options({'config': {'capture_logprobs': False}})
        del frozen['config']['capture_logprobs']
        pipeline = self.pipeline(frozen)
        before = deepcopy(pipeline)
        result = self.runtime.dispatch_collection(pipeline)
        self.assertFalse(result['request']['metadata']['config']['capture_logprobs'])
        start = next(args for args in self.calls if args[0] == 'start-run')
        self.assertNotIn('--capture-logprobs', start)
        self.assertTrue(self.factory.config()['capture_logprobs'])
        self.assertEqual(pipeline, before)

    def test_new_frozen_true_is_kept_when_factory_default_later_changes_to_off(self):
        frozen = self.runtime.collection_options({})
        self.assertTrue(frozen['config']['capture_logprobs'])
        self.factory.dispatch('config', 'POST', {**DEFAULT_CONFIG, 'capture_logprobs': False})
        pipeline = self.pipeline(frozen)
        before = deepcopy(pipeline)
        result = self.runtime.dispatch_collection(pipeline)
        self.assertTrue(result['request']['metadata']['config']['capture_logprobs'])
        start = next(args for args in self.calls if args[0] == 'start-run')
        self.assertIn('--capture-logprobs', start)
        self.assertFalse(self.factory.config()['capture_logprobs'])
        self.assertEqual(pipeline, before)

    def test_legacy_upgrade_still_rejects_changed_phone_associations(self):
        frozen = self.runtime.collection_options({})
        del frozen['config']['capture_logprobs']
        self.factory.dispatch('phone-apps', 'POST', {'phone_id': 'phone-b', 'app': 'App'})
        with self.assertRaisesRegex(PipelineError, '关联已变化'):
            self.runtime.dispatch_collection(self.pipeline(frozen))
        self.assertEqual(self.calls, [])


if __name__ == '__main__':
    unittest.main()
