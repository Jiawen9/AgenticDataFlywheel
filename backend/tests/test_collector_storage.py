"""Storage lifecycle contracts, no real collector or production deletion."""
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend import collector_storage as storage, phonefactory_client as cli
from backend.phone_factory import PhoneFactoryStore, PhoneFactoryError, create_router
from backend.collection_runs import CollectionRunError
from backend.tests.test_offline_evaluation_jobs import ManualPool


class StorageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.calls, self.ops = [], {}
        self.supported = True
        self.lose_ack = False
        self.store = PhoneFactoryStore(self.root, run_client_fn=self.client)
        self.runtime = self.store.runtime
        self.service = self.runtime.storage_service
        self.service.pool.close()
        self.service.pool = ManualPool()
        self.addCleanup(self.runtime.close)
        self.store.records.put('collection_runs', 'cr_one', {
            'collection_run_id':'cr_one', 'batch_id':'batch_one', 'status':'failed',
            'created_at':'2026-11-05T00:00:00Z', 'request':{'metadata':{'run_mode':'generate',
                'collector_server':'http://frozen-collector:9011', 'collection_artifacts_version':2}}})
        self.store.records.put('collection_executions', 'cx_two', {
            'collection_run_id':'cx_two', 'root_run_id':'cr_one', 'batch_id':'batch_one'})
        self.store.records.put('model_iter_runs', 'eval_independent', {
            'run_id':'eval_independent', 'status':'completed', 'request':{'execution_mode':'evaluate_only'}})

    def client(self, args):
        self.calls.append(args)
        command = args[0]
        if command == 'capabilities':
            value = {'storage_management':{'version':int(self.supported)}, 'archive_on_demand':{'version':int(self.supported)}}
        elif command in {'storage-cleanup','storage-scan','storage-archive'}:
            body = json.loads(args[3]); rid=args[1]; request_id=body['request_id']
            value = self.ops.setdefault(request_id, {'operation_id':'remote_'+request_id, 'request_id':request_id,
                'run_id':rid, 'kind':command.removeprefix('storage-'), 'status':'running',
                'execution_ids':['cr_one','cx_two'] if rid=='cr_one' else [rid], 'summary':{}, 'errors':[]})
            if value['status'] in {'partial','failed'}:
                value['status']='running'
            if self.lose_ack:
                self.lose_ack=False
                raise PhoneFactoryError('lost acknowledgement',502,remote_kind='timeout')
        elif command == 'storage-operation':
            value=next(v for v in self.ops.values() if v['operation_id']==args[1])
        else:
            raise AssertionError(args)
        return {'ok':True,'output':json.dumps(value)}

    def submit(self, kind='cleanup', rid='cr_one', request_id='clear_one', **extra):
        return self.service.accept(rid,kind,{'request_id':request_id,**extra})

    def drain(self):
        self.service.pool.drain()

    def test_failed_run_missing_platform_data_cleans_without_quality_or_receipt_gates(self):
        row=self.submit(); self.drain()
        self.assertTrue(storage.unavailable(self.store.records,'cr_one'))
        self.assertTrue(storage.unavailable(self.store.records,'cx_two'))
        self.ops['clear_one'].update(status='completed',summary={'deleted_bytes':1024,'deleted_files':5})
        self.service.work(row['operation_id'])
        self.assertEqual(self.service.get(row['operation_id'])['status'],'completed')
        self.assertEqual(storage.state(self.store.records,'cr_one')['collector_data_state'],'retired')
        self.assertEqual(self.store.records.get('collection_runs','cr_one')['status'],'failed')
        self.assertFalse(storage.unavailable(self.store.records,'eval_independent'))
        self.assertFalse((self.root/'raw').exists())

    def test_platform_files_are_untouched(self):
        path=self.root/'raw'/'collection_batches'/'batch_one'/'evidence.bin'
        path.parent.mkdir(parents=True);path.write_bytes(b'original')
        row=self.submit();self.drain();self.ops['clear_one']['status']='completed'
        self.service.work(row['operation_id'])
        self.assertEqual(path.read_bytes(),b'original')

    def test_replay_lost_ack_uses_frozen_server_and_same_request(self):
        self.lose_ack=True
        row=self.submit();self.drain()
        self.assertEqual(self.service.get(row['operation_id'])['status'],'queued')
        self.assertTrue(storage.unavailable(self.store.records,'cr_one'))
        with patch.dict('os.environ',{'PHONE_FACTORY_SERVER':'http://changed:9011'}):
            same=self.submit();self.drain()
        self.assertEqual(row['operation_id'],same['operation_id'])
        self.assertEqual(len(self.ops),1)
        self.assertTrue(all(call[-1]=='http://frozen-collector:9011' for call in self.calls))

    def test_identity_conflict_and_bad_payload(self):
        self.submit()
        with self.assertRaises(PhoneFactoryError) as error:
            self.submit(kind='archive')
        self.assertEqual(error.exception.status,409)
        for body in ({'request_id':'x','path':'D:/'},{'request_id':'x','mode':'all_disks'},{}):
            with self.assertRaises((PhoneFactoryError, CollectionRunError)):self.service.accept('cr_one','cleanup',body)

    def test_unsupported_does_not_mark_data_retired(self):
        self.supported=False
        row=self.submit();self.drain()
        self.assertEqual(self.service.get(row['operation_id'])['status'],'failed')
        self.assertFalse(storage.unavailable(self.store.records,'cr_one'))
        self.assertFalse(self.ops)
        self.assertEqual(self.service.get(row['operation_id'])['collector_data_state'],'available')

    def test_old_cleanup_response_cannot_roll_retired_state_back(self):
        first=self.submit(request_id='first');self.drain()
        second=self.submit(request_id='second');self.drain()
        self.ops['second']['status']='completed'
        self.service.work(second['operation_id'])
        self.ops['first']['status']='partial'
        self.service.work(first['operation_id'])
        self.assertEqual(storage.state(self.store.records,'cr_one')['collector_data_state'],'retired')

    def test_partial_cleanup_reposts_same_operation_and_preserves_history(self):
        row=self.submit();self.drain()
        self.ops['clear_one'].update(status='partial',errors=[{'path':'busy','error':'WinError32'}])
        self.service.work(row['operation_id'])
        self.assertEqual(storage.state(self.store.records,'cr_one')['collector_data_state'],'cleanup_failed')
        again=self.submit();self.drain()
        self.assertEqual(again['operation_id'],row['operation_id'])
        self.assertEqual(self.ops['clear_one']['status'],'running')

    def test_dependent_eval_is_stopped_without_deleting_its_data(self):
        self.store.records.update('model_iter_runs','eval_independent',lambda row:row.update(status='running'))
        row=self.submit();self.drain()
        self.ops['clear_one'].update(status='completed',dependent_run_ids=['eval_independent'])
        self.service.work(row['operation_id'])
        self.assertEqual(self.store.records.get('model_iter_runs','eval_independent')['status'],'cancelled')
        self.assertFalse(storage.unavailable(self.store.records,'eval_independent'))

    def test_read_status_does_not_call_remote_and_background_recovers(self):
        row=self.submit();self.service.pool.items.clear()
        self.assertFalse(self.calls)
        for _ in range(3):self.service.get(row['operation_id'])
        self.assertFalse(self.calls)
        self.service.schedule();self.drain()
        self.assertEqual(self.service.get(row['operation_id'])['status'],'running')

    def test_cleaned_data_blocks_resume_transfer_and_queued_read_callbacks(self):
        self.submit();self.drain()
        for callback in (lambda:self.runtime.get_run('cr_one'),
                         lambda:self.runtime.get_collection_artifacts('cx_two'),
                         lambda:self.runtime.retry_collection_transfer('cr_one'),
                         lambda:self.runtime.resume_service._run('cr_one')):
            with self.assertRaises(PhoneFactoryError) as error:callback()
            self.assertEqual(error.exception.status,410)

    def test_scan_and_cache_do_not_disable_data(self):
        for kind,extra in [('scan',{}),('cleanup',{'mode':'cache'})]:
            self.submit(kind=kind,request_id=kind,**extra);self.drain()
            self.assertFalse(storage.unavailable(self.store.records,'cr_one'))

    def test_http_accepts_before_remote_work_and_get_is_cached(self):
        app=FastAPI();app.include_router(create_router(self.store))
        with TestClient(app) as client:
            response=client.post('/api/phone-factory/storage/runs/cr_one/cleanup',json={'request_id':'http'})
            self.assertEqual(response.status_code,200,response.text)
            self.assertEqual(response.json()['status'],'queued')
            self.assertFalse(self.calls)
            value=client.get('/api/phone-factory/storage/operations/'+response.json()['operation_id'])
            self.assertEqual(value.status_code,200)
            self.assertFalse(self.calls)

    def test_archive_download_streams_without_full_platform_staging(self):
        op=self.submit(kind='archive',request_id='archive');self.drain()
        self.ops['archive'].update(status='completed',download_path='/runs/cr_one/collection-archive')
        self.service.work(op['operation_id'])
        class Stream(io.BytesIO):
            headers={'Content-Length':'12'}
            reads=0
            def read(self,*args):
                self.reads+=1
                return super().read(*args)
        stream=Stream(b'archive data')
        with patch('backend.collector_storage.urllib.request.urlopen',return_value=stream):
            response=self.service.download('cr_one')
            self.assertEqual(stream.reads,0)
            self.assertFalse((self.root/'tmp').exists())
            import asyncio
            async def consume():
                result=b''
                async for chunk in response.body_iterator:result+=chunk
                await response.background()
                return result
            self.assertEqual(asyncio.run(consume()),b'archive data')
        self.assertTrue(stream.closed)

    def test_historical_ready_archive_download_does_not_require_new_operation(self):
        self.store.records.update('collection_runs','cr_one',lambda row:row.update(archive_status='ready'))
        stream=io.BytesIO(b'old archive');stream.headers={}
        with patch('backend.collector_storage.urllib.request.urlopen',return_value=stream) as opened:
            response=self.service.download('cr_one')
            self.assertIn('/runs/cr_one/',opened.call_args.args[0].full_url)
            import asyncio
            asyncio.run(response.background())
        self.assertTrue(stream.closed)

    def test_cache_cleanup_resets_ready_status_only_after_actual_deletion(self):
        self.store.records.update('collection_runs','cr_one',lambda row:row.update(archive_status='ready'))
        first=self.submit(request_id='cache_noop',mode='cache');self.drain()
        self.ops['cache_noop'].update(status='completed',summary={'deleted_files':0})
        self.service.work(first['operation_id'])
        self.assertEqual(self.store.records.get('collection_runs','cr_one')['archive_status'],'ready')
        second=self.submit(request_id='cache_deleted',mode='cache');self.drain()
        self.ops['cache_deleted'].update(status='completed',summary={'deleted_files':1})
        self.service.work(second['operation_id'])
        self.assertEqual(self.store.records.get('collection_runs','cr_one')['archive_status'],'not_requested')
        self.assertEqual(self.service.get(second['operation_id'])['archive_status'],'not_requested')


class StorageCliTests(unittest.TestCase):
    def test_actual_cli_routes_and_json_request_ids(self):
        routes=[('storage-scan','/runs/cr_one/storage-scan'),('storage-cleanup','/runs/cr_one/cleanup'),
                ('storage-archive','/runs/cr_one/archive')]
        for command,path in routes:
            with patch('sys.argv',['client',command,'cr_one','--payload','{"request_id":"same"}',
                    '--server','http://target:9011']),patch.object(cli,'post_json',return_value={'ok':True}) as post,redirect_stdout(io.StringIO()),patch.object(cli,'SERVER',cli.SERVER):
                cli.main()
                post.assert_called_once_with(path,{'request_id':'same'})
        with patch('sys.argv',['client','storage-operation','op1']),patch.object(cli,'get_json',return_value={}) as get,redirect_stdout(io.StringIO()):
            cli.main();get.assert_called_once_with('/storage-operations/op1')

