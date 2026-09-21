"""HTTP protocol integration using only local simulated devices/execution."""
import contextlib
import io
import json
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch
from fastapi.testclient import TestClient
from backend import phonefactory_client
from backend.phone_factory import PhoneFactoryStore
from backend.phonefactory_manager import create_app
from backend.collector_service.core import CollectorConfig
from backend.preprocessing_jobs import PreprocessingJobManager
from backend.preprocessing_service import convert_input
from backend.tests.test_collector_service import Devices, Execution, ImmediateExecutor
from backend.tests.test_phone_factory_runtime import workbook_bytes
from backend.tests.test_collection_runs import seed_batch
import base64


class CollectorHttpIntegrationTests(unittest.TestCase):
    def test_manual_and_generated_batches_roundtrip_then_evaluation_isolation(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            execution = Execution()
            app = create_app(CollectorConfig(base / 'collector'), device_adapter=Devices(), execution_adapter=execution, executor=ImmediateExecutor())
            with TestClient(app) as remote:
                def urlopen(request, timeout=None):
                    from urllib.parse import urlsplit
                    url = urlsplit(request.full_url)
                    response = remote.request(request.get_method(), url.path + ('?' + url.query if url.query else ''), content=request.data, headers=dict(request.header_items()))
                    if response.status_code >= 400:
                        raise urllib.error.HTTPError(request.full_url, response.status_code, response.text, response.headers, io.BytesIO(response.content))
                    stream = io.BytesIO(response.content)
                    stream.headers = response.headers
                    return stream
                def client(args):
                    output = io.StringIO()
                    with patch.object(sys, 'argv', ['phonefactory_client', *args]), patch.object(phonefactory_client.urllib.request, 'urlopen', urlopen), contextlib.redirect_stdout(output):
                        try:
                            phonefactory_client.main()
                        except SystemExit as exc:
                            return {'ok': False, 'output': str(exc)}
                    return {'ok': True, 'output': output.getvalue()}
                platform = PhoneFactoryStore(base / 'platform', run_client_fn=client)
                platform.initialize_settings({'phones': ['phone-a', 'phone-b'], 'apps': ['App'], 'phoneApps': [{'phone_id': 'phone-a', 'app': 'App'}, {'phone_id': 'phone-b', 'app': 'App'}], 'vla': ['http://mock.invalid/vla']})
                original = workbook_bytes()
                state = platform.add_task({'filename': 'manual.xlsx', 'description': 'manual tasks', 'content_base64': base64.b64encode(original).decode()})
                manual = state['tasks'][0]
                params = {'filename': manual['filename'], 'request_id': 'manual-run', 'vla': 'http://mock.invalid/vla', 'config': {'sampling_enabled': True, 'temperature': 0.3, 'top_p': 0.8, 'use_experience_lib': True}}
                run = platform.remote_start(params)
                self.assertEqual(execution.calls, 1)
                self.assertEqual(platform.collection_runs.get(run['run_id'])['status'], 'running')
                with self.assertRaisesRegex(ValueError, '尚无'):
                    platform.collection_runs.ready_input(run['batch_id'])
                done = platform.runtime.transfer.sync(run['run_id'])
                self.assertEqual(done['status'], 'completed')
                self.assertEqual(done['transfer_status'], 'completed')
                self.assertEqual(len(done['trajectories']), 2)
                self.assertEqual(len({entry['relative_dir'] for entry in done['trajectories']}), 2)
                self.assertEqual(platform.remote_start(params)['run_id'], run['run_id'])
                self.assertEqual(platform.runtime.transfer.sync(run['run_id'])['status'], 'completed')
                self.assertEqual(execution.calls, 1)
                inputs = platform.collection_runs.ready_input(run['batch_id'])
                payload, _ = convert_input(inputs, lambda _: None, data_root=platform.root)
                rows = payload['sheets']['VLA trajectories']
                self.assertEqual(len(rows), 2)
                self.assertEqual({row['task_id'] for row in rows}, {next(iter(run['batch_tasks'].values()))['task_id']})
                self.assertTrue(all(row['source_row_id'] for row in rows))
                manager = PreprocessingJobManager(root=platform.root, executor=ImmediateExecutor())
                summary = next(batch for batch in manager.batches() if batch['batch_id'] == run['batch_id'])
                self.assertEqual(summary['collection_status'], 'ready')
                self.assertEqual(summary['ready_trajectory_count'], 2)
                self.assertEqual(next(iter(run['batch_tasks'].values()))['scene'], '购物')
                generated, content = seed_batch(platform.root)
                platform.add_task({'source_batch_id': generated['batch_id'], 'filename': generated['filename'], 'description': 'generated', 'content_base64': base64.b64encode(content.read_bytes()).decode()})
                # The generation fixture names its App differently; bind exactly that frozen App.
                generated_app = generated['snapshot']['tasks'][0]['app']
                platform.dispatch('phone-apps', 'POST', {'phone_id': 'phone-a', 'app': generated_app})
                generated_run = platform.remote_start({'filename': generated['filename'], 'request_id': 'generated-run', 'phone_id': 'phone-a', 'app': generated_app})
                self.assertEqual(platform.runtime.transfer.sync(generated_run['run_id'])['status'], 'completed')
                count_before = len(platform.collection_runs.list_runs())
                evaluation = platform.remote_start({**params, 'request_id': 'evaluation-run', 'run_mode': 'modeliter'}, mode='modeliter')
                self.assertEqual(platform.runtime.sync_evaluation(evaluation['run_id'])['status'], 'succeeded')
                self.assertEqual(len(platform.collection_runs.list_runs()), count_before)
                reports = platform.dispatch('reports', 'GET', {}, mode='modeliter')
                self.assertEqual(len(reports['folders']), 1)
                self.assertEqual(reports['folders'][0]['run_id'], evaluation['run_id'])
                file = reports['folders'][0]['files'][0]
                response = platform.dispatch('report-download', 'GET', {'run_id': evaluation['run_id'], 'file_id': file['file_id']}, mode='modeliter')
                self.assertEqual(Path(response.path).read_bytes(), b'report')
                Path(response.path).unlink()
                self.assertEqual(execution.calls, 3)

if __name__ == '__main__':
    unittest.main()
