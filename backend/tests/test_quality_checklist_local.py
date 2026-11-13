"""Real native subprocess contract: only the provider is replaced, never the worker."""
from copy import deepcopy
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from PIL import Image
from backend import quality_checklist_local as local
from backend.ChecklistEvaluator.evaluation_profiles import freeze_profile
from backend.ChecklistEvaluator.runtime_config import normalize_config
from backend.data_store import RecordStore
from backend.file_io import io_path


PROVIDER = r'''
import json, os, socket, time
from pathlib import Path
from types import SimpleNamespace as NS
import openai
socket.socket.connect = lambda *a, **k: (_ for _ in ()).throw(AssertionError('network forbidden'))
class Client:
    def __init__(self, **kwargs):
        self.n = 0
        self.chat = NS(completions=NS(create=self.create))
    def create(self, **kwargs):
        time.sleep(float(os.environ.get('TEST_PROVIDER_SLEEP','0')))
        with open(os.environ['TEST_REQUEST_LOG'], 'a', encoding='utf-8') as log:
            log.write(json.dumps({'pid': os.getpid(), 'model': kwargs['model']})+'\n')
        self.n += 1
        if kwargs['model'] == 'vision-judge':
            value = {'before_description': '白色页面', 'after_description': '白色页面', 'observations': '目标未显示'}
        elif self.n == 1:
            value = {'tool': 'generate_verification_checklist', 'args': {}}
        elif self.n == 2:
            value = {'verdicts': [{'item_id':'chk_01','result':'failed','evidence':'目标页面未打开'}]}
        else:
            raise AssertionError('quality-only must not request report enrichment')
        return NS(choices=[NS(message=NS(content=json.dumps(value,ensure_ascii=False),tool_calls=None),finish_reason='stop')],usage=None)
openai.OpenAI = Client
'''


class LocalChecklistTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name); self.root = self.base / 'platform'
        self.fixture = self.base / 'provider'; self.fixture.mkdir()
        (self.fixture / 'sitecustomize.py').write_text(PROVIDER, encoding='utf-8')
        self.log = self.base / 'requests.jsonl'
        self.env = patch.dict(os.environ, {
            'PYTHONPATH': str(self.fixture) + os.pathsep + os.environ.get('PYTHONPATH', ''),
            'TEST_REQUEST_LOG': str(self.log),
            'PHONE_FACTORY_EVALUATOR_TEXT_API_KEY': 'offline-text-key-only',
            'PHONE_FACTORY_EVALUATOR_VISION_API_KEY': 'offline-vision-key-only',
        }); self.env.start(); self.addCleanup(self.env.stop)
        catalog = self.base / 'profiles' / 'v1'; catalog.mkdir(parents=True)
        self.write(catalog / 'profile.json', {'id': 'v1', 'name': '离线规则', 'gold_path': 'gold.json', 'rules_dir': 'rules'})
        self.write(catalog / 'gold.json', {'case-1': {'task': '打开目标页面', 'checklist': [
            {'id': 'chk_01', 'description': '目标页面已打开', 'check_method': 'screenshot'}]}})
        self.write(catalog / 'rules/evidence.json', {'rules': [{'id':'r1','prompt_text':'Judge observed evidence only.'}]})
        profile = freeze_profile(catalog.parent, 'v1', self.base / 'frozen', runtime_root=local.RUNTIME)
        options = {'version': 2, 'execution_backend': 'platform', 'output_policy':'quality_only_v1',
            'evaluator_config': normalize_config({role: {'base_url':'http://offline.invalid/v1', 'model':role+'-judge'} for role in ('text','vision')}),
            'evaluation_profile_id': 'v1', 'evaluation_profile_digest':profile['profile_digest'],
            'runtime_digest':profile['runtime_digest'], 'profile_snapshot':profile, 'python_executable':sys.executable}
        self.job = {'job_id':'quality-test', 'checklist_options': options}
        self.source = self.base / '中文原件'; self.source.mkdir()
        Image.new('RGB', (40,40), 'white').save(self.source / 'final.png')
        self.write(self.source / '_trajectory_for_evaluate.json', {'task':'打开目标页面', 'actions_flat':[], 'final_orch_screenshot':'final.png'})
        self.task = {'task_id':'t001','case_id':'case-1','task':'打开目标页面','key_elements':'',
                     'trajectories':[{'trajectory_id':'tr001','source_directory':str(self.source)}]}
        self.events=[]

    def write(self, path, value):
        io_path(path.parent).mkdir(parents=True, exist_ok=True)
        io_path(path).write_text(json.dumps(value, ensure_ascii=False),encoding='utf-8')

    def run_task(self):
        return local.evaluate_task(self.root,self.job,self.task,'input-1',progress=lambda **event:self.events.append(event))

    def test_real_native_process_report_reuse_and_readonly_original(self):
        before={p.name:p.read_bytes() for p in self.source.iterdir()}
        results=self.run_task()
        self.assertEqual(results['tr001']['status'],'complete',results)
        self.assertEqual(results['tr001']['verdict'],'fail')
        calls=self.log.read_bytes()
        self.assertEqual(len(calls.splitlines()),2)
        self.assertEqual({p.name:p.read_bytes() for p in self.source.iterdir()},before)
        self.assertEqual(self.run_task(),results)
        self.assertEqual(self.log.read_bytes(),calls)
        self.assertTrue(any(e.get('checklist_event',{}).get('kind')=='request_start' for e in self.events))
        self.assertEqual(local.get_task_results(self.root,self.job,self.task,'input-1'),results)
        definition=local.get_task_definition(self.root,self.job,self.task,'input-1')
        self.assertEqual(definition['definition_digest'],results['tr001']['definition_digest'])
        report=next(self.root.rglob('_report_*.json'))
        self.assertEqual(local._read(report)['step_visual_summary_metadata']['report_enrichment'],'not_requested')
        self.assertFalse(list(self.root.rglob('*.png')))
        self.assertTrue(all(row['state']=='released' for row in RecordStore(self.root).list(local.SLOTS)))

    def test_failed_source_only_is_retried_and_successful_report_is_reused(self):
        missing=self.base/'missing'
        self.task['trajectories'].append({'trajectory_id':'tr002','source_directory':str(missing)})
        first=self.run_task()
        self.assertEqual(first['tr001']['status'],'complete',first)
        self.assertEqual(first['tr002']['status'],'error')
        self.assertEqual(first['tr002']['definition_digest'],first['tr001']['definition_digest'])
        missing.mkdir()
        for p in self.source.iterdir(): (missing/p.name).write_bytes(p.read_bytes())
        second=self.run_task()
        self.assertEqual(second['tr001'],first['tr001'])
        self.assertEqual(second['tr002']['status'],'complete',second)
        self.assertEqual(len(self.log.read_text().splitlines()),4)

    def test_source_error_is_transient_and_all_invalid_skips_definition(self):
        key=local.task_key(self.job,self.task,'input-1')
        self.task['trajectories'][0]['source_error']='原件暂不可读'
        self.assertEqual(local.task_key(self.job,self.task,'input-1'),key)
        first=self.run_task()
        self.assertEqual(first['tr001']['status'],'error')
        self.assertIsNone(local.get_task_definition(self.root,self.job,self.task,'input-1'))
        self.assertFalse(self.log.exists())
        del self.task['trajectories'][0]['source_error']
        self.assertEqual(self.run_task()['tr001']['status'],'complete')

    def test_source_change_and_report_tamper_do_not_reuse_or_call_model(self):
        self.assertEqual(self.run_task()['tr001']['status'],'complete')
        calls=self.log.read_bytes()
        (self.source/'final.png').write_bytes(b'changed')
        result=self.run_task()['tr001']
        self.assertEqual(result['status'],'error');self.assertIn('source_changed',result['error'])
        self.assertEqual(self.log.read_bytes(),calls)

    def test_invalid_completed_report_repaired_same_retry_in_new_attempt(self):
        self.assertEqual(self.run_task()['tr001']['status'],'complete')
        report=next(self.root.rglob('_report_*.json'))
        broken=local._read(report);broken['checklist_summary']['passed']=99
        self.write(report,broken)
        result=self.run_task()
        self.assertEqual(result['tr001']['status'],'complete',result)
        self.assertEqual(local._read(report)['checklist_summary']['passed'],99)
        self.assertEqual(len(list(self.root.rglob('_report_*.json'))),2)
        self.assertEqual(len(self.log.read_text().splitlines()),4)

    def test_native_visual_checks_retained_but_terminal_summary_is_not_called(self):
        source=self.source/'_trajectory_for_evaluate.json'
        value=local._read(source)
        value['actions_flat']=[{'global_step':1,'action':{'action':'click','coordinate':[10,10]},
            'action_desc':'点击','screenshot_before':'final.png','screenshot_after':'final.png'}]
        self.write(source,value)
        result=self.run_task()
        self.assertEqual(result['tr001']['status'],'complete',result)
        requests=[json.loads(x) for x in self.log.read_text().splitlines()]
        self.assertTrue(any(x['model']=='vision-judge' for x in requests))
        self.assertEqual(sum(x['model']=='text-judge' for x in requests),2)

    def test_cancellation_stops_actual_child_and_releases_durable_slot(self):
        requested=[]
        def progress(**event):
            if event.get('checklist_event',{}).get('kind')=='request_start':requested.append(True)
        def check():
            if requested:raise local.LocalChecklistRevoked('cancelled')
        with patch.dict(os.environ,{'TEST_PROVIDER_SLEEP':'20'}),self.assertRaises(local.LocalChecklistRevoked):
            local.evaluate_task(self.root,self.job,self.task,'input-1',progress=progress,check=check)
        self.assertTrue(requested)
        slots=RecordStore(self.root).list(local.SLOTS)
        self.assertTrue(all(x['state']=='released' and x.get('child',{}).get('exited') for x in slots))

    def test_lease_enter_failure_releases_claim(self):
        original=local._Lease._acquire
        def broken(lease):
            original(lease)
            raise RuntimeError('after claim')
        with patch.object(local._Lease,'_acquire',broken),self.assertRaisesRegex(RuntimeError,'after claim'):
            with local._Lease(self.root,'first',self.job,None,None):pass
        self.assertTrue(all(x['state']=='released' for x in RecordStore(self.root).list(local.SLOTS)))

    def test_global_slots_are_shared_by_real_parent_processes(self):
        ready=self.base/'ready';release=self.base/'release'
        script='''
import sys,time
from pathlib import Path
from backend.quality_checklist_local import _Lease
with _Lease(Path(sys.argv[1]), 'other-process', {'job_id':'other'}, None, None):
    Path(sys.argv[2]).write_text('ready')
    end=time.monotonic()+15
    while not Path(sys.argv[3]).exists() and time.monotonic()<end:time.sleep(.05)
'''
        with local._Lease(self.root,'first',self.job,None,None):
            child=subprocess.Popen([sys.executable,'-c',script,str(self.root),str(ready),str(release)],
                                   stdout=subprocess.PIPE,stderr=subprocess.PIPE)
            try:
                end=time.monotonic()+8
                while not ready.exists() and child.poll() is None and time.monotonic()<end:time.sleep(.05)
                self.assertTrue(ready.exists())
                deadline=time.monotonic()+.3
                def bound():
                    if time.monotonic()>deadline:raise local.LocalChecklistRevoked('cancel wait')
                with self.assertRaises(local.LocalChecklistRevoked):
                    with local._Lease(self.root,'third',self.job,bound,None):pass
                self.assertEqual(sum(x['state']=='active' for x in RecordStore(self.root).list(local.SLOTS)),2)
            finally:
                release.write_text('done')
                out,err=child.communicate(timeout=10)
                self.assertEqual(child.returncode,0,err.decode(errors='replace'))

    def test_actual_parent_crash_ends_native_child_then_retry_recovers(self):
        marker=self.base/'request-started';input_file=self.base/'parent.json'
        self.write(input_file,{'job':self.job,'task':self.task})
        script='''
import sys,json
from pathlib import Path
from backend.quality_checklist_local import evaluate_task
value=json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
def progress(**event):
    if event.get('checklist_event',{}).get('kind')=='request_start':Path(sys.argv[3]).write_text('started')
evaluate_task(Path(sys.argv[2]),value['job'],value['task'],'input-1',progress=progress)
'''
        parent=subprocess.Popen([sys.executable,'-c',script,str(input_file),str(self.root),str(marker)],
            env={**os.environ,'TEST_PROVIDER_SLEEP':'30'},stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        native=None
        try:
            end=time.monotonic()+12
            while not marker.exists() and parent.poll() is None and time.monotonic()<end:time.sleep(.05)
            self.assertTrue(marker.exists())
            active=next(x for x in RecordStore(self.root).list(local.SLOTS) if x['state']=='active')
            native=active['child'];self.assertFalse(native['exited'])
        finally:
            local.stop_owned_process(parent);parent.communicate(timeout=5)
        end=time.monotonic()+6
        while native and local._process_identity(native['pid'])==native['process_identity'] and time.monotonic()<end:
            time.sleep(.05)
        self.assertNotEqual(local._process_identity(native['pid']),native['process_identity'])
        self.assertEqual(self.run_task()['tr001']['status'],'complete')

    def test_response_loss_reuses_valid_native_report_without_model(self):
        result=self.run_task();calls=self.log.read_bytes()
        key=local.task_key(self.job,self.task,'input-1')+':'+local.digest('tr001')[:24]
        RecordStore(self.root).update(local.ENTRIES,key,lambda row:row.update(report_path=None,result=None))
        self.assertEqual(self.run_task(),result)
        self.assertEqual(self.log.read_bytes(),calls)

    def test_split_secret_log_output_is_redacted_and_write_failure_drains(self):
        fake_runtime=self.base/'log-runtime';fake_runtime.mkdir()
        (fake_runtime/'run_quality_checklist.py').write_text('''
import sys,json,time
from pathlib import Path
spec=Path(sys.argv[sys.argv.index('--spec')+1])
if sys.stdin.readline().strip()!='GO':raise SystemExit(75)
sys.stdout.write('offline-text-');sys.stdout.flush();time.sleep(.15)
sys.stdout.write('key-only\\n');sys.stdout.flush()
sys.stdout.write('x'*100000+'\\n');sys.stdout.flush()
(spec.parent/'result.json').write_text(json.dumps({'ok':True}))
''',encoding='utf-8')
        work=self.base/'log-work'
        with patch.object(local,'RUNTIME',fake_runtime):
            with local._Lease(self.root,'logging',self.job,None,None) as lease:
                result=local._child(self.root,self.job['checklist_options'],{},work,lease,None)
            self.assertTrue(result['ok'])
            log=(work/'worker.log').read_text()
            self.assertNotIn('offline-text-key-only',log);self.assertIn('[REDACTED]',log)
            original_io=local.io_path
            class BrokenSink:
                def write(self,data):raise OSError('disk write failed')
                def flush(self):pass
                def close(self):pass
            class SinkPath:
                def open(self,*a,**k):return BrokenSink()
            def path(value):
                if Path(value).name=='worker.log':return SinkPath()
                return original_io(value)
            with local._Lease(self.root,'broken-writing',self.job,None,None) as lease:
                with patch.object(local,'io_path',side_effect=path),self.assertRaisesRegex(RuntimeError,'日志写入失败'):
                    local._child(self.root,self.job['checklist_options'],{},self.base/'broken-work',lease,None)
            self.assertTrue(all(x['state']=='released' for x in RecordStore(self.root).list(local.SLOTS)))

    def test_short_real_child_events_delivered_after_first_poll_already_exited(self):
        runtime=self.base/'short-events';runtime.mkdir()
        (runtime/'run_quality_checklist.py').write_text('''
import sys,json
from pathlib import Path
spec=Path(sys.argv[sys.argv.index('--spec')+1])
if sys.stdin.readline().strip()!='GO':raise SystemExit(75)
for kind in ('request_start','request_end'):
    print('@ADF_CHECKLIST_EVENT '+json.dumps({'kind':kind,'call_id':'short',
        'stage':'text','role':'evaluator','model':'short-model','attempt':1}),flush=True)
(spec.parent/'result.json').write_text(json.dumps({'ok':True}))
''',encoding='utf-8')
        original=subprocess.Popen;first_poll=[];observed=[]
        def start(*args,**kwargs):
            child=original(*args,**kwargs)
            if any('run_quality_checklist.py' in str(part) for part in args[0]):
                poll=child.poll
                def exited_on_first_poll():
                    if not first_poll:
                        child.wait(timeout=10)
                        first_poll.append(poll())
                    return poll()
                child.poll=exited_on_first_poll
            return child
        with patch.object(local,'RUNTIME',runtime),patch.object(local.subprocess,'Popen',side_effect=start):
            with local._Lease(self.root,'short-events',self.job,None,None) as lease:
                result=local._child(self.root,self.job['checklist_options'],{},self.base/'short-work',
                                    lease,lambda **event:observed.append(event))
        self.assertTrue(result['ok']);self.assertEqual(first_poll,[0])
        self.assertEqual([x['checklist_event']['kind'] for x in observed],['request_start','request_end'])
        self.assertEqual([x['current_model'] for x in observed],['short-model','short-model'])

    @unittest.skipUnless(os.name=='nt','Windows extended path contract')
    def test_chinese_long_source_path_real_native_process(self):
        long=self.base/('长目录'*25)/('原始证据'*20)
        while len(str(long/'_trajectory_for_evaluate.json')) <= 280:
            long=long/('追加中文目录'*8)
        io_path(long).mkdir(parents=True)
        for p in self.source.iterdir(): io_path(long/p.name).write_bytes(p.read_bytes())
        self.assertGreater(len(str(long/'_trajectory_for_evaluate.json')),260)
        self.task['trajectories'][0]['source_directory']=str(long)
        result=self.run_task()
        self.assertEqual(result['tr001']['status'],'complete',result)

    @unittest.skipUnless(os.name=='nt','Windows profile filesystem contract')
    def test_chinese_long_profile_import_freeze_verify_preserves_logical_paths(self):
        from backend.ChecklistEvaluator.evaluation_profiles import list_profiles,verify_snapshot
        from backend.ChecklistEvaluator.import_profile import import_profile
        from backend.ChecklistEvaluator.agent.evaluator.gold_checklist import validate_gold_file,load_gold_checklist
        long=self.base/('规则目录'*26)/('中文冻结输入'*18)
        source_root=long/'来源';destination_root=long/'平台规则'
        for path in (self.base/'profiles/v1').rglob('*'):
            if path.is_file():
                target=source_root/'v1'/path.relative_to(self.base/'profiles/v1')
                io_path(target.parent).mkdir(parents=True,exist_ok=True)
                io_path(target).write_bytes(path.read_bytes())
        self.assertGreater(len(str(source_root/'v1/gold.json')),260)
        catalogue=list_profiles(source_root)
        self.assertEqual(catalogue['errors'],[])
        expected=catalogue['profiles'][0]['digest']
        imported=import_profile(source_root,destination_root,'v1',expected)
        self.assertTrue(imported['ok'])
        frozen=freeze_profile(destination_root,'v1',long/'冻结',runtime_root=local.RUNTIME,expected_digest=expected)
        self.assertEqual(verify_snapshot(long/'冻结/profile_snapshot.json',local.RUNTIME),frozen)
        self.assertEqual(freeze_profile(destination_root,'v1',long/'冻结',runtime_root=local.RUNTIME),frozen)
        self.assertIn('case-1',validate_gold_file(frozen['gold_path']))
        matched=load_gold_checklist('打开目标页面',{'case_id':'case-1'},strict=True,path=frozen['gold_path'])
        self.assertEqual(matched['source_path'],frozen['gold_path'])
        self.assertEqual(frozen['profile_digest'],expected)
        for field in ('snapshot_root','gold_path','rules_dir'):
            self.assertFalse(frozen[field].startswith('\\\\?\\'))
        self.job['checklist_options'].update(profile_snapshot=frozen,runtime_digest=frozen['runtime_digest'])
        result=self.run_task()
        self.assertEqual(result['tr001']['status'],'complete',result)
        report=local._read(next(self.root.rglob('_report_*.json')))
        self.assertEqual(report['checklist_sha256'],local._sha(frozen['gold_path']))
        self.assertFalse(report['checklist_metadata']['source_path'].startswith('\\\\?\\'))

    def test_cancel_waiter_never_consumes_slot_and_lease_releases_after_failure(self):
        records=RecordStore(self.root)
        with local._Lease(self.root,'first',self.job,None,None):
            with local._Lease(self.root,'second',self.job,None,None):
                def cancelled():raise local.LocalChecklistRevoked('cancelled')
                with self.assertRaises(local.LocalChecklistRevoked):
                    with local._Lease(self.root,'third',self.job,cancelled,None):pass
                self.assertEqual(sum(x['state']=='active' for x in records.list(local.SLOTS)),2)
        self.assertTrue(all(x['state']=='released' for x in records.list(local.SLOTS)))

    def test_broken_log_sink_fails_before_child_and_does_not_leak_slot(self):
        key=local.task_key(self.job,self.task,'input-1')
        log=self.root/'system/qc'/key[:24]/'d/worker.log';log.mkdir(parents=True)
        result=self.run_task()
        self.assertEqual(result['tr001']['status'],'error')
        self.assertFalse(self.log.exists())
        self.assertTrue(all(x['state']=='released' for x in RecordStore(self.root).list(local.SLOTS)))

    def test_unconfirmed_cleanup_does_not_dispatch_next_trajectory_or_replace_child(self):
        self.task['trajectories'].append({'trajectory_id':'tr002','source_directory':str(self.source)})
        stop=local.stop_owned_process;calls=[]
        def unconfirmed(process):
            calls.append(process.pid)
            confirmed=stop(process)
            return confirmed if len(calls)==1 else False
        with patch.object(local,'stop_owned_process',side_effect=unconfirmed):
            with self.assertRaises(local.LocalChecklistCleanupUnconfirmed):self.run_task()
        self.assertEqual(len(calls),2)  # definition + first trajectory, never the second
        slot=next(x for x in RecordStore(self.root).list(local.SLOTS) if x['state']=='active')
        self.assertEqual(slot['child']['pid'],calls[-1]);self.assertTrue(slot['child']['cleanup_failed'])
        self.assertEqual(len(self.log.read_text().splitlines()),2)
        self.assertEqual(local.get_task_results(self.root,self.job,self.task,'input-1')['tr002']['status'],'error')
        with patch.object(local,'stop_recorded_process',return_value=False):
            with local._Lease(self.root,'unrelated',self.job,None,None):
                self.assertEqual(sum(x['state']=='active' for x in RecordStore(self.root).list(local.SLOTS)),2)
        # Once exit is confirmed, this same task can recover without orphan overlap.
        self.assertEqual(self.run_task()['tr002']['status'],'complete')

    def test_unconfirmed_live_child_does_not_block_stdout_close(self):
        fake_runtime=self.base/'waiting-runtime';fake_runtime.mkdir()
        (fake_runtime/'run_quality_checklist.py').write_text('''
import sys,time
if sys.stdin.readline().strip()!='GO':raise SystemExit(75)
print('started',flush=True)
time.sleep(30)
''',encoding='utf-8')
        processes=[];popen=subprocess.Popen;real_stop=local.stop_owned_process
        def start(*args,**kwargs):
            child=popen(*args,**kwargs)
            if any('run_quality_checklist.py' in str(part) for part in args[0]):processes.append(child)
            return child
        worker_log=self.base/'wait-work/worker.log'
        def cancel():
            if worker_log.exists() and worker_log.stat().st_size:
                raise local.LocalChecklistRevoked('stop')
        started=time.monotonic()
        try:
            with patch.object(local,'RUNTIME',fake_runtime),patch.object(local.subprocess,'Popen',side_effect=start),\
                 patch.object(local,'stop_owned_process',return_value=False):
                with self.assertRaises(local.LocalChecklistCleanupUnconfirmed):
                    with local._Lease(self.root,'unconfirmed',self.job,cancel,None) as lease:
                        local._child(self.root,self.job['checklist_options'],{},self.base/'wait-work',lease,None)
            self.assertLess(time.monotonic()-started,10)
            slot=next(x for x in RecordStore(self.root).list(local.SLOTS) if x['state']=='active')
            self.assertEqual(slot['child']['pid'],processes[0].pid)
        finally:
            for process in processes:real_stop(process)
            for thread in threading.enumerate():
                if thread.name=='quality-checklist-log':thread.join(timeout=2)


if __name__=='__main__':unittest.main()
