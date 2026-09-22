from __future__ import annotations
import base64
import io
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from fastapi import FastAPI
from fastapi.testclient import TestClient
from openpyxl import Workbook
from backend.phone_factory import PhoneFactoryStore, PhoneFactoryError, create_router
from backend.batch_lifecycle import BatchPublishedError
from backend.task_generation.collection_batches import COLLECTION_COLUMNS


def workbook_bytes():
    book = Workbook()
    book.active.append(COLLECTION_COLUMNS)
    book.active.append(['case-a', '单APP', 'App', '购物', '搜索', None, '搜索商品', None, None, 2, '人工', None, None, None, None, None, None])
    output = io.BytesIO()
    book.save(output)
    book.close()
    return output.getvalue()

class FactoryRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / 'platform'
        self.calls, self.remote = [], {}
        self.store = PhoneFactoryStore(self.root, run_client_fn=self.client)
        self.store.initialize_settings({'phones': ['phone-1'], 'apps': ['App'], 'phoneApps': [{'phone_id': 'phone-1', 'app': 'App'}], 'vla': ['http://mock.invalid/vla']})
        self.upload = {'filename': 'tasks.xlsx', 'description': 'test', 'content_base64': base64.b64encode(workbook_bytes()).decode()}

    def client(self, args):
        self.calls.append(args)
        value = {'ok': True}
        if args[0] == 'capabilities':
            value.update(protocol_version=1, batch_results=True)
        if args[0] == 'start-run':
            run_id = args[args.index('--collection-run-id') + 1]
            mode = args[args.index('--runmode') + 1]
            value.update(run_id=run_id, collection_run_id=run_id, run_mode=mode)
            self.remote[run_id] = {**value, 'status': 'succeeded', 'errors': []}
        if args[0] == 'run':
            if args[1] not in self.remote:
                return {'ok': False, 'output': 'not found'}
            value = self.remote[args[1]]
        if args[0] == 'report-download':
            Path(args[args.index('--out') + 1]).write_bytes(b'mock report')
        return {'ok': True, 'output': json.dumps(value)}

    def request(self, **extra):
        return {'filename': 'tasks.xlsx', 'request_id': 'request-1', 'vla': 'http://mock.invalid/vla', **extra}

    def test_manual_retry_batch_list_and_frozen_classification(self):
        first = self.store.add_task(self.upload)
        second = self.store.add_task(self.upload)
        self.assertEqual(first, second)
        self.assertEqual(len(self.store.runtime.batches()), 1)
        batch = self.store.runtime.batches()[0]
        self.assertEqual(batch['kind'], 'manual_collection')
        self.assertIsNone(batch['source_job_id'])
        self.assertEqual(batch['snapshot']['tasks'][0]['scene'], '购物')
        result = self.store.remote_start(self.request())
        self.assertEqual(result['batch_id'], batch['batch_id'])
        self.assertEqual(result['request']['metadata']['phone_ids'], ['phone-1'])
        self.assertEqual(self.store.state()['tasks'][0]['status'], '运行中')
        self.assertEqual(self.store.records.list('model_iter_runs'), [])

    def test_parallel_same_request_executes_once_and_config_conflicts(self):
        self.store.add_task(self.upload)
        with ThreadPoolExecutor(max_workers=4) as pool:
            runs = list(pool.map(lambda _: self.store.remote_start(self.request()), range(4)))
        self.assertEqual(len({run['collection_run_id'] for run in runs}), 1)
        self.assertEqual(len([args for args in self.calls if args[0] == 'start-run']), 1)
        for change in ({'vla': 'http://other.invalid'}, {'config': {'temperature': 0.5}}, {'config': {'sampling_enabled': True}}):
            with self.subTest(change=change):
                with self.assertRaises(ValueError):
                    self.store.remote_start(self.request(**change))

    def test_model_iteration_run_persistence_reports_and_no_production_artifacts(self):
        self.store.add_task(self.upload, mode='modeliter')
        result = self.store.remote_start(self.request(run_mode='modeliter'), mode='modeliter')
        self.assertTrue(result['run_id'].startswith('eval_'))
        self.assertEqual(self.store.records.list('collection_runs'), [])
        self.assertEqual(self.store.runtime.batches(), [])
        self.assertFalse((self.root / 'batches').exists())
        self.assertEqual(self.store.state('generate')['tasks'][0]['status'], '未运行')
        self.assertEqual(self.store.state('modeliter')['tasks'][0]['status'], '运行中')
        rebuilt = PhoneFactoryStore(self.root, run_client_fn=self.client)
        self.assertEqual(rebuilt.runtime.sync_evaluation(result['run_id'])['status'], 'succeeded')
        app = FastAPI()
        app.include_router(create_router(rebuilt, prefix='/api/model-iter', mode='modeliter'))
        with TestClient(app) as client:
            response = client.get('/api/model-iter/report-download', params={'run_id': result['run_id'], 'file_id': 'file-1'})
            self.assertEqual(response.content, b'mock report')
            self.assertEqual(client.get('/api/model-iter/runs').json()['runs'][0]['status'], 'succeeded')
        self.assertEqual(list((self.root / 'tmp' / 'phone_factory_reports').iterdir()), [])
        self.assertIn('modeliter', self.calls[-1])

    def test_old_service_rejected_and_same_run_recovers_after_lost_response(self):
        self.store.add_task(self.upload)
        real = self.store.run_client
        self.store.run_client = lambda args: {'ok': True, 'output': '{"ok":true}'}
        with self.assertRaisesRegex(PhoneFactoryError, '升级'):
            self.store.remote_start(self.request())
        run_id = self.store.collection_runs.list_runs()[0]['collection_run_id']
        failed_once = [False]
        def losing_client(args):
            result = real(args)
            if args[0] == 'start-run' and not failed_once[0]:
                failed_once[0] = True
                return {'ok': False, 'output': 'response lost'}
            return result
        self.store.run_client = losing_client
        with self.assertRaisesRegex(PhoneFactoryError, 'response lost'):
            self.store.remote_start(self.request())
        self.assertEqual(self.store.remote_start(self.request())['collection_run_id'], run_id)
        self.assertEqual(len(self.store.collection_runs.list_runs()), 1)

    def test_delete_failure_preserves_local_state_success_interrupts_run(self):
        self.store.add_task(self.upload)
        run = self.store.remote_start(self.request())
        real = self.store.run_client
        self.store.run_client = lambda args: {'ok': True, 'output': '{"ok":false,"error":"busy"}'}
        with self.assertRaisesRegex(PhoneFactoryError, 'busy'):
            self.store.dispatch('remote/del-phone', 'POST', {'phone_id': 'phone-1'})
        self.assertEqual(self.store.state()['phones'], ['phone-1'])
        self.store.run_client = real
        self.store.dispatch('remote/del-phone', 'POST', {'phone_id': 'phone-1'})
        self.assertEqual(self.store.state()['phones'], [])
        self.assertEqual(self.store.collection_runs.get(run['collection_run_id'])['status'], 'interrupted')
        self.assertTrue(Path(run['output_dir']).exists())

    def test_model_iteration_late_dispatch_never_overwrites_interrupted(self):
        self.store.add_task(self.upload, mode='modeliter')
        real = self.store.run_client
        for fail in (False, True):
            def delayed(args):
                result = real(args)
                if args[0] == 'start-run':
                    run_id = args[args.index('--collection-run-id') + 1]
                    self.store.records.update('model_iter_runs', run_id, lambda item: item.update(status='interrupted'))
                    if fail:
                        return {'ok': False, 'output': 'late timeout'}
                return result
            self.store.run_client = delayed
            request = self.request(request_id='late-' + str(fail), run_mode='modeliter')
            if fail:
                with self.assertRaises(PhoneFactoryError):
                    self.store.remote_start(request, mode='modeliter')
            else:
                self.assertEqual(self.store.remote_start(request, mode='modeliter')['status'], 'interrupted')
        self.assertTrue(all(run['status'] == 'interrupted' for run in self.store.runtime.run_views('modeliter')))

    def test_new_upload_id_starts_new_batch_after_previous_publication(self):
        first = self.store.add_task({**self.upload, 'request_id': 'upload-one'})['tasks'][0]
        self.store.records.put('batch_lifecycle', first['source_batch_id'], {'batch_id': first['source_batch_id'], 'status': 'published', 'release_id': 'release-1'})
        second = self.store.add_task({**self.upload, 'request_id': 'upload-two'})['tasks'][0]
        self.assertNotEqual(first['source_batch_id'], second['source_batch_id'])
        self.assertEqual(self.store.add_task({**self.upload, 'request_id': 'upload-two'})['tasks'], [second])
        self.assertEqual(self.store.remote_start(self.request())['batch_id'], second['source_batch_id'])

    def test_old_evaluation_completion_does_not_hide_new_round(self):
        self.store.add_task(self.upload, mode='modeliter')
        first = self.store.remote_start(self.request(request_id='first'), mode='modeliter')
        second = self.store.remote_start(self.request(request_id='second'), mode='modeliter')
        self.store.runtime.sync_evaluation(first['run_id'])
        self.assertEqual(self.store.state('modeliter')['tasks'][0]['status'], '运行中')
        self.assertEqual(self.store.runtime.run_views('modeliter')[-1]['run_id'], second['run_id'])
        self.store.runtime.sync_evaluation(second['run_id'])
        self.remote[second['run_id']]['status'] = 'running'  # Delayed poll captured before completion.
        self.assertEqual(self.store.runtime.sync_evaluation(second['run_id'])['status'], 'succeeded')

    def test_closed_batch_cannot_run_or_restore_to_work_list(self):
        row = self.store.add_task(self.upload)['tasks'][0]
        batch_id = row['source_batch_id']
        self.store.records.put('batch_lifecycle', batch_id, {'batch_id': batch_id, 'status': 'published', 'release_id': 'release-1', 'published_at': '2026-09-21'})
        with self.assertRaises(BatchPublishedError):
            self.store.remote_start(self.request())
        self.assertEqual(self.store.state()['tasks'], [])
        self.assertEqual(self.store.runtime.batches(), [])
        self.assertEqual(self.calls, [])

    def test_newer_completion_does_not_hide_older_active_run_in_either_mode(self):
        self.store.dispatch('phone-apps', 'POST', {'phone_id': 'phone-2', 'app': 'App'})
        task = self.store.add_task(self.upload)['imported_task']
        for mode, namespace, final, label in (
            ('generate', 'collection_runs', 'completed', '已回传'),
            ('modeliter', 'model_iter_runs', 'succeeded', '已完成'),
        ):
            with self.subTest(mode=mode):
                first = self.store.remote_start(self.request(filename=task['filename'], request_id=mode + '-first', phone_id='phone-1'), mode=mode)
                second = self.store.remote_start(self.request(filename=task['filename'], request_id=mode + '-second', phone_id='phone-2'), mode=mode)
                first_id, second_id = first['collection_run_id'], second['collection_run_id']
                self.store.records.update(namespace, first_id, lambda item: item.update(created_at='2026-09-22T00:00:01Z'))
                self.store.records.update(namespace, second_id, lambda item: item.update(created_at='2026-09-22T00:00:02Z', status=final))
                for status, active_label in (('dispatching', '下发中'), ('queued', '排队中'), ('running', '运行中')):
                    with self.subTest(status=status):
                        self.store.records.update(namespace, first_id, lambda item: item.update(status=status))
                        rebuilt = PhoneFactoryStore(self.root, run_client_fn=self.client)
                        self.assertEqual(rebuilt.state(mode)['tasks'][0]['status'], active_label)
                self.store.records.update(namespace, first_id, lambda item: item.update(status=final))
                self.assertEqual(self.store.state(mode)['tasks'][0]['status'], label)

    def test_failed_transfer_does_not_hide_other_live_run_and_remains_actionable(self):
        self.store.add_task(self.upload)
        first = self.store.remote_start(self.request(request_id='first'))
        second = self.store.remote_start(self.request(request_id='second'))
        first_id, second_id = first['collection_run_id'], second['collection_run_id']
        self.store.records.update('collection_runs', first_id, lambda item: item.update(created_at='2026-09-22T00:00:01Z'))
        self.store.records.update('collection_runs', second_id, lambda item: item.update(created_at='2026-09-22T00:00:02Z'))
        self.store.records.put('collection_transfers', second_id, {'transfer_error': 'mock download failure'})
        self.assertEqual(self.store.state()['tasks'][0]['status'], '运行中')
        self.store.records.update('collection_runs', first_id, lambda item: item.update(status='completed'))
        self.assertEqual(self.store.state()['tasks'][0]['status'], '回传失败')

if __name__ == '__main__':
    unittest.main()
