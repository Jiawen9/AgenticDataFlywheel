import copy
import errno
import io
import json
import unittest
from unittest.mock import patch

from backend import phonefactory_client as cli
from backend.collection_receipts import ReceiptStore
from backend.collection_receipt_storage import ReceiptStorage
from backend.collection_resume_projection import PlanProjection
from backend.collection_artifact_transfer import ArtifactTransfer
from backend.phone_factory import PhoneFactoryError
from backend.tests import test_collection_resume as fixtures


class PhoneControlTests(unittest.TestCase):
    client = fixtures.ResumeTests.client

    def setUp(self):
        fixtures.ResumeTests.setUp(self)
        self.control = self.service.phone_control
        self.pause_pool = patch.object(self.control.pool, 'submit', return_value=False)
        self.pause_pool.start(); self.addCleanup(self.pause_pool.stop)
        self.owner = {'execution_id':'old_owner', 'state_revision':10, 'ownership_token':'proof',
                      'availability':'free', 'supported_actions':['reconcile','stop']}
        self.responses['phone-control-capabilities'] = {'phone_control':{'version':1}}
        self.responses['phone-ownership'] = {'phone_id':'phone-one','availability':'free',
                                            'stale':False,'owners':[self.owner]}
        self.request = {'request_id':'control_one','execution_id':'old_owner',
                        'expected_state_revision':10,'ownership_token':'proof'}
        self.response = {'operation_id':'control_one','request_id':'control_one','execution_id':'old_owner',
                         'phone_id':'phone-one','action':'reconcile','status':'completed'}
        self.responses['phone-reconcile'] = self.response

    def submit_control(self):
        return self.control.submit(self.id,'phone-one','reconcile',self.request,'generate')

    def test_cached_read_and_foreign_phone_never_probe_or_mutate(self):
        self.assertEqual(self.control.ownership(self.id,'phone-one','generate')['availability'],'unknown')
        self.assertEqual(self.calls,[])
        with self.assertRaises(PhoneFactoryError):self.control.ownership(self.id,'other','generate')

    def test_durable_idempotent_control_and_exact_target(self):
        with patch.dict('os.environ',{'PHONE_FACTORY_SERVER':'http://collector-one:9011'}):
            first=self.submit_control()
        self.assertEqual(self.submit_control()['operation_id'],first['operation_id'])
        self.assertEqual(self.calls,[])
        with patch.dict('os.environ',{'PHONE_FACTORY_SERVER':'http://collector-two:9011'}):
            self.control._work('control_one')
        self.assertEqual(self.control.operation(self.id,'control_one','generate')['status'],'completed')
        post=next(c for c in self.calls if c[0]=='phone-reconcile')
        self.assertEqual(post[1:3],['old_owner','phone-one'])
        self.assertEqual(post[-2:],['--server','http://collector-one:9011'])
        self.assertEqual(json.loads(post[post.index('--payload')+1]),{k:v for k,v in self.request.items() if k!='execution_id'})
        self.assertFalse(any(c[0] in {'cancel-run','resume-collection'} for c in self.calls))
        with self.assertRaises(PhoneFactoryError):
            self.control.submit(self.id,'phone-one','stop',self.request,'generate')

    def test_stale_token_unsupported_and_foreign_operation_rejected(self):
        self.submit_control();self.owner['state_revision']=11
        self.control._work('control_one')
        self.assertEqual(self.control.operation(self.id,'control_one','generate')['status'],'conflict')
        self.assertNotIn('phone-reconcile',[c[0] for c in self.calls])
        self.responses['phone-control-capabilities']={}
        self.request['request_id']='control_two';self.submit_control();self.control._work('control_two')
        self.assertEqual(self.control.operation(self.id,'control_two','generate')['status'],'conflict')
        with self.assertRaises(PhoneFactoryError):self.control.operation(self.id,'other','generate')

    def test_token_refresh_does_not_replace_original_proof_or_reject_unchanged_owner(self):
        self.submit_control();self.owner['ownership_token']='rotated-proof'
        self.control._work('control_one')
        self.assertEqual(self.control.operation(self.id,'control_one','generate')['status'],'completed')
        post=next(c for c in self.calls if c[0]=='phone-reconcile')
        self.assertEqual(json.loads(post[post.index('--payload')+1])['ownership_token'],'proof')

    def test_lost_ack_queries_original_id_and_does_not_repeat_stop(self):
        self.responses['phone-reconcile']=TimeoutError('lost ack')
        self.submit_control();self.control._work('control_one')
        self.responses['phone-control-operation']=self.response
        self.control.due.clear();self.control._work('control_one')
        self.assertEqual([c[0] for c in self.calls].count('phone-reconcile'),1)
        self.assertEqual(self.control.operation(self.id,'control_one','generate')['status'],'completed')

    def test_unexpected_phone_response_never_claims_success(self):
        self.response['phone_id']='other'
        self.submit_control();self.control._work('control_one')
        self.assertEqual(self.control.operation(self.id,'control_one','generate')['status'],'uncertain')

    def test_sqlite_intent_failure_prevents_remote_mutation(self):
        with patch.object(self.store.records,'put',side_effect=OSError(errno.ENOSPC,'full')):
            with self.assertRaises(OSError):self.submit_control()
        self.assertEqual(self.calls,[])

    def test_cli_routes_quote_phone_and_execution_and_preserve_payload(self):
        routes=[(['phone-ownership','phone/a'],'/phones/phone%2Fa/ownership',None),
                (['phone-control-operation','request/a'],'/phone-control-operations/request%2Fa',None),
                (['phone-stop','run/a','phone/b','--payload','{"request_id":"x"}'],
                 '/runs/run%2Fa/phones/phone%2Fb/stop',{'request_id':'x'}),
                (['result-corrections','run/a','--cursor','3','--limit','10'],
                 '/runs/run%2Fa/result-corrections?cursor=3&limit=10',None)]
        for args,url,payload in routes:
            with self.subTest(args=args),patch('sys.argv',['client',*args]),patch('sys.stdout',new=io.StringIO()),\
                 patch.object(cli,'get_json',return_value={}) as get,patch.object(cli,'post_json',return_value={}) as post:
                cli.main()
                if payload is None:get.assert_called_once_with(url);post.assert_not_called()
                else:post.assert_called_once_with(url,payload);get.assert_not_called()

    def test_quarantined_old_storage_flag_does_not_block_recovery(self):
        ledger=ReceiptStore(self.store.records,self.id)
        ledger.register([{'artifact_id':'bad','sequence':1,'kind':'trajectory','manifest_sha256':'a'*64}])
        ledger.update('bad',status='quarantined',storage_error=True)
        self.assertEqual(ledger.storage_blocked_count(),0)
        ledger.update('bad',status='retry_wait')
        self.assertEqual(ledger.storage_blocked_count(),1)

    def test_pause_ack_preserves_original_failure_and_recovery_keeps_history(self):
        owner=self.store.runtime.transfer
        guard=ReceiptStorage(owner,self.run)
        with patch.object(owner.remote,'set_receipt_storage',return_value={'ok':True,'receipt_storage':{'paused':True}}):
            guard.on_error(PermissionError(errno.EACCES,'write denied'))
        state=owner._receipt_storage_states[self.id]
        self.assertIn('write denied',state['error']);self.assertIsNone(state['notification_error'])
        guard.failed.clear()
        ledger=ReceiptStore(self.store.records,self.id)
        with patch.object(owner.remote,'set_receipt_storage',return_value={'ok':True,'receipt_storage':{'paused':False}}):guard.recover(ledger)
        self.assertFalse(state['paused']);self.assertIsNone(state['error']);self.assertIn('write denied',state['last_error'])

    def test_correction_preserves_receipt_and_validates_identity(self):
        self.responses['phone-control-capabilities']={'collection_resume':{'version':1,'result_reconciliation_version':1}}
        row={'artifact_id':'artifact_one','status':'received','item':{'manifest_sha256':'a'*64},
             'entry':{'phone_id':'phone-one','collection_case_id':'task-one','repeat_index':1,'execution_status':'not_run'}}
        item={'sequence':1,'correction_id':'correction_one','execution_id':self.id,'artifact_id':'artifact_one',
              'phone_id':'phone-one','collection_case_id':'task-one','repeat_index':1,'manifest_sha256':'a'*64,
              'execution_status':'completed','classification':'completed','reason':'verified_worker_result',
              'evidence_sha256':{'worker-result.json':'b'*64}}
        self.responses['result-corrections']={'root_run_id':self.id,'items':[item],'next_cursor':1,'has_more':False}
        projection=PlanProjection(self.root,self.id);projection.initialize(self.run,ArtifactTransfer._expected(self.run))
        self.service.result_corrections.sync(self.run,projection)
        corrected=self.service.result_corrections.corrected(self.id,self.id,row)
        self.assertEqual(row['entry']['execution_status'],'not_run')
        self.assertEqual(corrected['entry']['execution_status'],'completed')
        self.assertTrue(self.store.records.get('collection_resume_heads',self.id)['active'])
        bad=copy.deepcopy(row);bad['item']['manifest_sha256']='c'*64
        self.assertIs(self.service.result_corrections.corrected(self.id,self.id,bad),bad)

    def test_corrected_stable_snapshot_changes_counts_without_mutating_original_receipts(self):
        original=fixtures.V2Remote(self.run,self.root/'oldremote')
        original.finish('partial');self.store.runtime.transfer.remote=original
        self.store.runtime.transfer.sync(self.id)
        self.run=self.store.collection_runs.get(self.id)
        ledger=ReceiptStore(self.store.records,self.id)
        rows=copy.deepcopy(ledger.rows(limit=100))
        final=copy.deepcopy(self.store.records.get('collection_source_final_manifests',self.id))
        row=rows[0];entry=row['entry']
        item={**{k:entry[k] for k in ('phone_id','collection_case_id','repeat_index')},
              'sequence':1,'correction_id':'correction_stable','execution_id':self.id,
              'artifact_id':row['artifact_id'],'manifest_sha256':row['item']['manifest_sha256'],
              'execution_status':'completed','classification':'completed','reason':'verified_worker_result',
              'evidence_sha256':{'worker-result.json':'b'*64}}
        self.responses['phone-control-capabilities']={'collection_resume':{'version':1,'result_reconciliation_version':1}}
        self.responses['result-corrections']={'root_run_id':self.id,'items':[item],'next_cursor':1,'has_more':False}
        projection=PlanProjection(self.root,self.id);plan=ArtifactTransfer._expected(self.run)
        projection.initialize(self.run,plan);self.service.result_corrections.sync(self.run,projection)
        self.service.result_corrections.observe(self.run,projection,self.id,0,plan,ledger)
        self.assertEqual(projection.cases()['scope_counts']['completed_count'],1)
        selected=[]
        for source in rows:
            selected.append({**{k:source['entry'][k] for k in ('phone_id','collection_case_id','repeat_index')},
                'execution_id':self.id,'artifact_id':source['artifact_id'],
                'manifest_sha256':source['item']['manifest_sha256'],
                **({'result_correction':{'correction_id':'correction_stable'}} if source['artifact_id']==row['artifact_id'] else {})})
        snapshot={'root_run_id':self.id,'batch_id':'batch-one','snapshot_id':'cs_corrected','revision':1,'stable':True,'instances':selected}
        self.responses['collection-snapshot']=snapshot
        self.service._commit_snapshot(self.run,snapshot)
        head=self.store.records.get('collection_resume_heads',self.id)
        self.assertFalse(head['active']);self.assertEqual(head['collection_summary']['collection_completed_count'],1)
        self.assertEqual(head['collection_summary']['collection_not_run_count'],19)
        self.assertEqual(ledger.rows(limit=100),rows)
        self.assertEqual(self.store.records.get('collection_source_final_manifests',self.id),final)
        # A correction of an old attempt cannot steal a newer adopted result.
        projection.observe('new_attempt',1,[(entry['phone_id'],entry['collection_case_id'],entry['repeat_index'])],
            [{'artifact_id':'new_result','status':'received','entry':{**entry,'execution_status':'failed'}}])
        self.service.result_corrections.observe(self.run,projection,self.id,0,plan,ledger)
        adopted=next(x for x in projection.instances(entry['collection_case_id'],entry['phone_id'])['items']
                     if x['repeat_index']==entry['repeat_index'])
        self.assertEqual(adopted['artifact_id'],'new_result')

    def test_same_batch_device_revision_is_allowlisted_capability_checked_and_frozen(self):
        self.store.records.update('collection_runs',self.id,lambda value:value['request']['metadata'].update(agent_type='pipelineagent'))
        self.responses['capabilities']={'collection_resume':{'version':1,'device_config_revision':1}}
        self.service.preview(self.id,{})
        body={**self.body,'device_config_revision':{'adb_path':'D:/adb/adb.exe','app_list':{'Demo':'com.example.demo'}}}
        with patch.object(self.service.pool,'submit',return_value=False):
            self.service.submit(self.id,body)
        stored=self.store.records.get('collection_resume_operations',body['request_id'])['request']
        self.assertEqual(stored['device_config_revision'],body['device_config_revision'])
        self.assertNotIn('planner_config',stored)
        with self.assertRaises(PhoneFactoryError):self.service.submit(self.id,{**body,'device_config_revision':{'adb_path':'E:/adb.exe'}})
        with self.assertRaises(PhoneFactoryError):self.service.submit(self.id,{**body,'device_config_revision':{'planner_config':{}}})
        with self.assertRaises(PhoneFactoryError):self.service.submit(self.id,{**body,'device_config_revision':{'adb_path':'relative.exe'}})

    def test_phone_control_http_accepts_without_waiting_for_remote_and_scopes_reads(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from backend.phone_factory import create_router
        app=FastAPI();app.include_router(create_router(self.store))
        with TestClient(app) as client:
            response=client.post(f'/api/phone-factory/collection-runs/{self.id}/phones/phone-one/reconcile',json=self.request)
            self.assertEqual(response.status_code,202)
            self.assertEqual(response.json()['status'],'accepted')
            self.assertEqual(self.calls,[])
            response=client.get(f'/api/phone-factory/collection-runs/{self.id}/phones/foreign/ownership')
            self.assertEqual(response.status_code,404)

    def test_revision_can_advance_without_new_execution_but_cannot_regress(self):
        self.store.records.put('collection_resume_heads',self.id,{'run_id':self.id,'batch_id':'batch-one','revision':1,'remote_revision':1,'active':True})
        p=PlanProjection(self.root,self.id);p.initialize(self.run,ArtifactTransfer._expected(self.run))
        self.responses['collection-executions']={'root_run_id':self.id,'revision':2,'executions':[{'execution_id':self.id}]}
        self.service._sync_executions(self.run,p)
        self.assertEqual(self.store.records.get('collection_resume_heads',self.id)['remote_revision'],2)
        self.responses['collection-executions']['revision']=1
        with self.assertRaisesRegex(ValueError,'回退'):self.service._sync_executions(self.run,p)


if __name__=='__main__':unittest.main()
