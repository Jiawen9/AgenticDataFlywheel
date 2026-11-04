import copy
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.collection_artifact_transfer import ArtifactTransfer
from backend.collection_artifact_transfer import digest
from backend.collection_receipts import ReceiptStore
from backend.collection_resume_projection import PlanProjection
from backend.collection_resume_snapshots import current_input
from backend.collection_resume_snapshots import evaluation_snapshot_status
from backend.phone_factory import PhoneFactoryStore, PhoneFactoryError
from backend.tests.test_collection_runs import seed_batch
from backend.tests.test_collection_receipts import V2Remote


class ResumeTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        seed_batch(self.root)
        self.calls = []
        self.responses = {}
        self.store = PhoneFactoryStore(self.root, run_client_fn=self.client)
        self.addCleanup(self.store.runtime.close)
        self.run, _ = self.store.collection_runs.create('batch-one', dispatch_key='resume-test',metadata={
            'run_mode':'generate','result_transport':'http','repeat_times':10,
            'collection_artifacts_version':2,'phone_ids':['phone-one'],
            'phone_apps':[{'phone_id':'phone-one','app':'Demo'}]})
        self.id = self.run['collection_run_id']
        self.service = self.store.runtime.resume_service
        self.preview = {'preview_id':'pv_demo','root_run_id':self.id,'batch_id':'batch-one',
            'selection_digest':'d'*64,'expected_revision':0,
            'instances':[{'phone_id':'phone-one','collection_case_id':'task-one','repeat_index':5,
                          'repeat_times':10,'expanded_case_id':'task-one-phone-one-005'}],
            'requires_unknown_phone_ids':[],'blocked_phones':[],'counts':{'selected_instances':1}}
        self.responses['capabilities']={'collection_resume':{'version':1}}
        self.responses['resume-preview']=self.preview
        self.responses['run-diagnostics']={'root_run_id':self.id,'batch_id':'batch-one','phones':[]}
        self.responses['resume-collection']={'root_run_id':self.id,'operation_id':'req_one','status':'accepted','execution_id':'cx_demo'}
        self.body={'request_id':'req_one','preview_id':'pv_demo','selection_digest':'d'*64,'expected_revision':0}

    def client(self,args):
        self.calls.append(args)
        result = self.responses.get(args[0],{})
        if isinstance(result,Exception):
            raise result
        return {'ok':True,'output':json.dumps(result)}

    def submit(self):
        self.service.preview(self.id,{})
        with patch.object(self.service.pool,'submit',return_value=False):
            return self.service.submit(self.id,self.body)

    def test_preview_and_atomic_idempotent_intent_do_not_mutate_original(self):
        before=self.store.records.get('collection_runs',self.id)
        result=self.submit()
        again=self.service.submit(self.id,self.body)
        self.assertEqual(result,again)
        self.assertEqual(self.store.records.get('collection_runs',self.id),before)
        self.assertEqual(self.store.records.get('collection_resume_heads',self.id)['revision'],1)
        self.assertNotIn('resume-collection',[c[0] for c in self.calls])
        with self.assertRaises(PhoneFactoryError):
            self.service.submit(self.id,{**self.body,'expected_revision':1})

    def test_disk_full_before_intent_never_calls_collector_resume(self):
        self.service.preview(self.id,{})
        with patch.object(self.store.records,'put_many',side_effect=sqlite3.OperationalError('database or disk is full')):
            with self.assertRaises(sqlite3.Error):self.service.submit(self.id,self.body)
        self.assertNotIn('resume-collection',[c[0] for c in self.calls])

    def test_duplicate_click_with_another_request_id_does_not_create_another_intent(self):
        self.submit()
        with self.assertRaisesRegex(PhoneFactoryError,'已有续采请求'):
            self.service.submit(self.id,{**self.body,'request_id':'req_duplicate'})
        self.assertIsNone(self.store.records.get('collection_resume_operations','req_duplicate'))
        self.assertEqual(self.store.records.get('collection_resume_heads',self.id)['revision'],1)

    def test_uncertain_send_recovers_same_frozen_request(self):
        self.submit()
        self.responses['resume-collection']=TimeoutError('lost response')
        self.service._send_operation(self.run,'req_one')
        self.assertEqual(self.service.operation(self.id,'req_one')['status'],'uncertain')
        self.responses['resume-collection']={'root_run_id':self.id,'operation_id':'req_one','status':'accepted','execution_id':'cx_demo'}
        self.service._recover_operations(self.run)
        posts=[c for c in self.calls if c[0]=='resume-collection']
        self.assertEqual(len(posts),2)
        self.assertEqual(posts[0],posts[1])
        self.assertEqual(self.service.operation(self.id,'req_one')['status'],'accepted')

    def test_unknown_confirmation_required(self):
        self.preview['requires_unknown_phone_ids']=['phone-one']
        self.service.preview(self.id,{})
        with self.assertRaises(PhoneFactoryError):self.service.submit(self.id,self.body)
        with patch.object(self.service.pool,'submit',return_value=False):
            self.service.submit(self.id,{**self.body,'confirm_unknown_phone_ids':['phone-one']})

    def test_wrong_preview_identity_and_unsupported_are_rejected(self):
        self.preview['instances'][0]['phone_id']='wrong-phone'
        with self.assertRaises(PhoneFactoryError):self.service.preview(self.id,{})
        self.responses['capabilities']={}
        with self.assertRaisesRegex(PhoneFactoryError,'resume_unsupported'):self.service.preview(self.id,{})

    def test_read_diagnostics_survives_persistence_failure_and_does_no_work(self):
        self.service.run_cache[self.id]=self.run
        self.service.cache[self.id]={'supported':True,'phones':[{'phone_id':'phone-one','phase':'model_request'}]}
        self.store.runtime.transfer._receipt_storage_states[self.id]={'paused':True,'reason':'platform_storage_unavailable'}
        with patch.object(self.service,'_read',side_effect=sqlite3.OperationalError('full')):
            result=self.service.diagnostics(self.id,'generate')
        self.assertTrue(result['receipt_storage']['paused'])
        self.assertEqual(self.calls,[])

    def test_projection_keeps_first_four_and_separate_attempt_five(self):
        p=PlanProjection(self.root,self.id)
        plan=ArtifactTransfer._expected(self.run)
        p.initialize(self.run,plan)
        receipts=[]
        for index in range(1,11):
            receipts.append({'artifact_id':'old'+str(index),'status':'received','entry':{
                'phone_id':'phone-one','collection_case_id':'task-one','repeat_index':index,
                'execution_status':'completed' if index<5 else 'interrupted'}})
        p.observe(self.id,0,plan,receipts)
        p.observe('cx_demo',1,[('phone-one','task-one',5)],[{'artifact_id':'new5','status':'received','entry':{
            'phone_id':'phone-one','collection_case_id':'task-one','repeat_index':5,'execution_status':'completed'}}])
        # Late original receipt cannot steal the current result.
        p.observe(self.id,0,plan,receipts)
        result=p.instances('task-one','phone-one')
        self.assertEqual([x['artifact_id'] for x in result['items'][:5]],['old1','old2','old3','old4','new5'])
        self.assertEqual(result['items'][4]['attempts'][0]['artifact_id'],'old5')
        groups=p.cases(phone_id='phone-one',q='task-one')
        self.assertEqual(groups['items'][0]['planned_count'],10)
        self.assertEqual(groups['items'][0]['completed_count'],5)
        self.assertEqual(groups['items'][0]['attempt_count'],11)

    def test_projection_5440_slots_paged_and_read_does_not_change_bytes(self):
        run=copy.deepcopy(self.run)
        run['batch_tasks']={f'case-{n:04}':{'app':'A' if n%2 else 'B','task':f'task{n}'} for n in range(544)}
        plan={('phone-one',case,index) for case in run['batch_tasks'] for index in range(1,11)}
        p=PlanProjection(self.root,self.id)
        p.initialize(run,plan)
        before=p.path.read_bytes()
        page=p.cases(limit=20,app='A')
        self.assertEqual(page['total'],272)
        self.assertEqual(page['scope_counts']['planned_count'],2720)
        self.assertEqual(len(page['items']),20)
        self.assertEqual(before,p.path.read_bytes())

    def test_segment_registration_is_hidden_and_frozen(self):
        child=self.service._register_execution(self.run,{'execution_id':'cx_demo','root_run_id':self.id,
            'batch_id':'batch-one','instances':self.preview['instances']},1)
        self.assertEqual(ArtifactTransfer._expected(child),{('phone-one','task-one',5)})
        self.assertEqual(len(self.store.collection_runs.list_runs()),1)
        self.assertEqual(self.service.executions.runs.get('cx_demo')['root_run_id'],self.id)
        changed=copy.deepcopy(self.preview['instances']);changed[0]['repeat_index']=6
        with self.assertRaises(ValueError):self.service._register_execution(self.run,{
            'execution_id':'cx_demo','root_run_id':self.id,'instances':changed},1)

    def test_segment_v2_real_receipts_do_not_change_parent_manifest(self):
        self.submit()
        self.service._send_operation(self.run,'req_one')
        child=self.service._register_execution(self.run,{'execution_id':'cx_demo','root_run_id':self.id,
            'instances':self.preview['instances']},1)
        remote=V2Remote(child,self.root/'remote')
        remote.planned=lambda:ArtifactTransfer._expected(child)
        remote.add_outcome('task-one',5,status='interrupted')
        remote.finish('partial')
        self.service.executions.remote=remote
        before=self.store.records.get('collection_runs',self.id)
        saved=self.service.executions.sync('cx_demo')
        self.assertTrue(saved['receipt_closed'])
        self.assertEqual(self.store.records.get('collection_runs',self.id),before)
        self.assertEqual(ReceiptStore(self.store.records,'cx_demo').counts()['received'],1)

    def test_active_snapshot_blocks_new_input(self):
        self.submit()
        with self.assertRaisesRegex(Exception,'正在续采'):
            current_input(self.store.records,self.id)

    def test_complete_snapshot_selects_new_attempt_without_reopening_old_receipts(self):
        original=V2Remote(self.run,self.root/'oldremote')
        original.finish('partial')
        self.store.runtime.transfer.remote=original
        self.store.runtime.transfer.sync(self.id)
        self.run=self.store.collection_runs.get(self.id)
        before=copy.deepcopy(self.run)
        final=copy.deepcopy(self.store.records.get('collection_source_final_manifests',self.id))
        self.submit();self.service._send_operation(self.run,'req_one')
        child=self.service._register_execution(self.run,{'execution_id':'cx_demo','root_run_id':self.id,
            'instances':self.preview['instances']},1)
        remote=V2Remote(child,self.root/'newremote');remote.planned=lambda:ArtifactTransfer._expected(child)
        artifact=remote.add_outcome('task-one',5,status='interrupted')
        remote.manifests[artifact]['outcome']['source_trajectory_id']='cx_demo_source'
        remote.items[0]['manifest_sha256']=digest(remote.manifests[artifact])
        remote.finish('partial')
        self.service.executions.remote=remote
        self.service.executions.sync('cx_demo')
        self.store.records.update('collection_resume_operations','req_one',lambda v:v.update(status='completed'))
        selected=[]
        for row in ReceiptStore(self.store.records,self.id).rows(limit=20):
            entry=row['entry'];execution=self.id;chosen=row
            if (entry['collection_case_id'],entry['repeat_index'])==('task-one',5):
                execution='cx_demo';chosen=ReceiptStore(self.store.records,'cx_demo').get(artifact)
            selected.append({**{k:entry[k] for k in ('collection_case_id','phone_id','repeat_index')},
                'execution_id':execution,'artifact_id':chosen['artifact_id'],'manifest_sha256':chosen['item']['manifest_sha256']})
        source={'root_run_id':self.id,'batch_id':'batch-one','snapshot_id':'cs_demo','revision':1,'stable':True,'instances':selected}
        self.responses['collection-snapshot']=source
        self.service._commit_snapshot(self.run,source)
        head=self.store.records.get('collection_resume_heads',self.id)
        self.assertFalse(head['active'])
        self.assertEqual(head['collection_summary']['planned_trajectory_count'],20)
        self.assertEqual(head['collection_summary']['collection_failed_count'],1)
        self.assertEqual(head['collection_summary']['collection_not_run_count'],19)
        self.assertEqual(self.store.collection_runs.get(self.id),before)
        self.assertEqual(self.store.records.get('collection_source_final_manifests',self.id),final)
        self.assertEqual(current_input(self.store.records,self.id,require_stable=False)['collection_snapshot_id'],'cs_demo')
        # A later rejected continuation creates no physical segment. Re-adopting
        # the same immutable snapshot must release only that failed intent's barrier.
        snapshot_before=self.store.records.get('collection_resume_snapshots','cs_demo')
        self.store.records.put('collection_resume_operations','req_failed',{'operation_id':'req_failed',
            'run_id':self.id,'status':'failed','request':{'expected_revision':1}})
        self.store.records.update('collection_resume_heads',self.id,lambda v:v.update(revision=2,active=True))
        self.service._commit_snapshot(self.run,source)
        self.assertFalse(self.store.records.get('collection_resume_heads',self.id)['active'])
        self.assertEqual(self.store.records.get('collection_resume_snapshots','cs_demo'),snapshot_before)

    def test_preparing_failure_is_visible_in_operation_status(self):
        self.submit();self.service._send_operation(self.run,'req_one')
        self.responses['resume-operation']={'root_run_id':self.id,'operation_id':'req_one','execution_id':'cx_demo',
            'status':'failed','error':{'code':'phone_process_exit_unconfirmed'}}
        self.service._recover_operations(self.run)
        op=self.service.operation(self.id,'req_one')
        self.assertEqual(op['status'],'failed')
        self.assertEqual(op['error'],'phone_process_exit_unconfirmed')

    def test_unknown_diagnostics_are_never_counted_as_healthy(self):
        self.service.run_cache[self.id]=self.run
        self.service.cache[self.id]={'supported':True,'phones':[{'phone_id':'phone-one','status':'unknown'}]}
        result=self.service.diagnostics(self.id,'generate')
        self.assertEqual(result['summary'],{'total':1,'healthy':0,'warning':1,'error':0})

    def test_projection_activity_filter_does_not_mark_a_finished_slot_running(self):
        projection=PlanProjection(self.root,self.id);plan=ArtifactTransfer._expected(self.run)
        projection.initialize(self.run,plan)
        projection.observe(self.id,0,plan,[{'artifact_id':'old1','status':'received','entry':{
            'phone_id':'phone-one','collection_case_id':'task-one','repeat_index':1,'execution_status':'completed'}}])
        projection.observe_activity([{'phone_id':'phone-one','case_id':'task-one','repeat_index':1,'execution_id':self.id,'status':'running'},
            {'phone_id':'phone-one','case_id':'task-two','repeat_index':1,'execution_id':self.id,'status':'running'}])
        self.assertEqual(projection.cases(status='running')['total'],1)
        self.assertEqual(projection.instances('task-one')['items'][0]['status'],'completed')

    def test_durable_stop_dispatch_continues_when_attempt_write_fails(self):
        self.store.records.update('collection_runs',self.id,lambda v:v.update(stop_requested_at='2026-11-03T01:00:00Z'))
        self.responses['run']={'collection_run_id':self.id,'run_mode':'generate','agent_type':'newagent','status':'running'}
        self.responses['cancel-run']={'collection_run_id':self.id,'ok':True,'status':'stopping'}
        with patch.object(self.store.records,'update',side_effect=sqlite3.OperationalError('database or disk is full')):
            self.store.runtime._cancel_collection_remote(self.id)
        self.assertIn('cancel-run',[c[0] for c in self.calls])
        self.assertIn('error',self.store.runtime._control_memory[self.id])

    def test_legacy_stop_retry_is_scoped_to_initial_physical_run(self):
        self.submit()
        self.store.records.update('collection_runs',self.id,lambda v:v.update(stop_requested_at='2026-11-03T01:00:00Z'))
        with patch.object(self.store.runtime._control_pool,'submit') as submit:
            self.store.runtime._schedule_controls()
        self.assertEqual(submit.call_args.args[0],self.id)
        self.responses['run']={'collection_run_id':self.id,'run_mode':'generate','agent_type':'newagent','status':'running'}
        self.responses['cancel-run']={'collection_run_id':self.id,'ok':True,'status':'stopping'}
        self.store.runtime._cancel_collection_remote(self.id)
        self.assertIn(['cancel-run',self.id],self.calls)
        self.assertNotIn('executions-cancel',[c[0] for c in self.calls])

    def test_late_stop_acknowledgement_does_not_acknowledge_a_newer_stop(self):
        self.submit()
        old={'request_id':'stop_old','expected_revision':0,'epoch':1,'acknowledged':False}
        new={'request_id':'stop_new','expected_revision':1,'epoch':2,'acknowledged':False}
        self.store.records.update('collection_resume_heads',self.id,lambda h:h.update(cancel_intent=old))
        def delayed_response(command,run_id,body):
            self.assertEqual(body,{'request_id':'stop_old','expected_revision':0})
            self.store.records.update('collection_resume_heads',self.id,lambda h:h.update(revision=2,cancel_intent=new))
            return {'ok':True,'root_run_id':self.id}
        with patch.object(self.service,'_remote',side_effect=delayed_response):
            self.service._send_cancel(self.id)
        self.assertEqual(self.store.records.get('collection_resume_heads',self.id)['cancel_intent'],new)

    def test_pending_replacement_keeps_adopted_result_until_verified(self):
        p=PlanProjection(self.root,self.id);plan=ArtifactTransfer._expected(self.run)
        p.initialize(self.run,plan)
        original={'artifact_id':'old5','status':'received','entry':{'phone_id':'phone-one',
            'collection_case_id':'task-one','repeat_index':5,'execution_status':'failed','error':'model timeout'}}
        p.observe(self.id,0,plan,[original])
        p.observe('cx_demo',1,[('phone-one','task-one',5)],[])
        slot=p.instances('task-one','phone-one')['items'][4]
        self.assertEqual(slot['artifact_id'],'old5')
        self.assertEqual(slot['execution_id'],self.id)
        self.assertEqual(slot['active_attempt']['execution_id'],'cx_demo')
        self.assertEqual(slot['status'],'pending')
        self.assertEqual(p.cases(q='task-one')['items'][0]['planned_count'],10)

    def test_cold_diagnostics_do_not_invent_an_observation_timestamp(self):
        self.responses['run-diagnostics']={'root_run_id':self.id,'phones':[],'observed_at':None,'stale':True}
        self.service.refresh(self.run)
        result=self.service.diagnostics(self.id,'generate')
        self.assertIsNone(result['observed_at'])
        self.assertTrue(result['stale'])
        self.assertIn('received_at',result)

    def test_snapshot_labels_never_rewrite_frozen_evaluation(self):
        source={'source_run_id':self.id,'snapshot_id':'cs_old'}
        frozen={'run_id':'eval_frozen','request':{'source_request':source},'status':'completed'}
        self.store.records.put('model_iter_runs','eval_frozen',frozen)
        self.store.records.put('collection_resume_heads',self.id,{'batch_id':'batch-one','revision':2,
            'active':False,'current_snapshot_id':'cs_current'})
        before=self.store.records.get('model_iter_runs','eval_frozen')
        value=self.store.runtime._evaluation_snapshot_label(before)
        self.assertTrue(value['stale_collection_snapshot'])
        self.assertEqual(value['source_collection_snapshot_id'],'cs_old')
        self.assertEqual(value['current_collection_snapshot_id'],'cs_current')
        self.assertFalse(value['collection_snapshot_pending'])
        self.assertEqual(self.store.records.get('model_iter_runs','eval_frozen'),before)
        self.assertFalse(evaluation_snapshot_status(self.store.records,{'source_run_id':self.id,
            'snapshot_id':'cs_current'})['stale_collection_snapshot'])

    def test_default_evaluation_selects_stable_snapshot_and_active_requires_explicit_history(self):
        from backend.model_iteration import preview_source
        self.store.records.put('collection_resume_heads',self.id,{'batch_id':'batch-one','revision':1,
            'active':False,'current_snapshot_id':'cs_current'})
        self.store.records.put('collection_resume_snapshots','cs_current',{'logical_run_id':self.id})
        preview={'preview_id':'pv_eval','valid':False,'status':'ready','trajectory_count':0,'cases':[],
            'errors':['No eligible input']}
        self.responses['evaluation-preview']=preview
        with patch('backend.evaluation_previews.schedule'):
            result=preview_source(self.store,{'source_run_id':self.id})
            self.assertFalse(result['stale_collection_snapshot'])
            self.assertEqual(self.store.records.get('model_iter_previews',result['preview_id'])['request'],
                             {'source_run_id':self.id,'snapshot_id':'cs_current'})
            self.store.records.update('collection_resume_heads',self.id,lambda v:v.update(active=True))
            with self.assertRaisesRegex(PhoneFactoryError,'等待稳定快照'):
                preview_source(self.store,{'source_run_id':self.id})
            result=preview_source(self.store,{'source_run_id':self.id,'snapshot_id':'cs_current'})
            self.assertTrue(result['stale_collection_snapshot'])
            self.assertTrue(result['collection_snapshot_pending'])

    def test_evidence_execution_must_belong_to_logical_run(self):
        from backend.collection_evidence_api import dispatch
        self.store.records.put('collection_executions','cx_other',{'root_run_id':'cr_other','batch_id':'batch-one'})
        with patch.object(self.service.executions,'artifact_source') as source:
            with self.assertRaisesRegex(PhoneFactoryError,'不属于'):
                dispatch(self.store.runtime,['collection-runs',self.id,'artifacts','ca_one'],
                         {'execution_id':'cx_other'},'generate')
            source.assert_not_called()

    def test_resume_cli_uses_exact_scoped_routes_and_frozen_payload(self):
        from backend import phonefactory_client as cli
        cases=[('run-diagnostics',[], '/diagnostics',None),
               ('collection-executions',[], '/executions',None),
               ('resume-operation',['request_one'], '/resume-operations/request_one',None),
               ('collection-snapshot',['cs_one'], '/collection-snapshots/cs_one',None),
               ('resume-preview',['--payload','{}'], '/resume-preview',{}),
               ('resume-collection',['--payload',json.dumps(self.body)], '/resume',self.body),
               ('executions-cancel',['--payload','{"request_id":"stop_one","expected_revision":1}'],
                '/executions/cancel',{'request_id':'stop_one','expected_revision':1})]
        for command,args,suffix,payload in cases:
            with self.subTest(command=command),patch('sys.argv',['client',command,self.id,*args]),\
                 patch('sys.stdout',new=io.StringIO()),patch.object(cli,'get_json',return_value={}) as get,\
                 patch.object(cli,'post_json',return_value={}) as post:
                cli.main()
                if payload is None:
                    get.assert_called_once_with('/runs/'+self.id+suffix);post.assert_not_called()
                else:
                    post.assert_called_once_with('/runs/'+self.id+suffix,payload);get.assert_not_called()


if __name__=='__main__':unittest.main()
