"""Actual CLI -> collector Flask storage lifecycle; all files live in temp dirs."""
import json
import time
import unittest
from pathlib import Path

from backend.tests import test_model_iteration_flask as fixture


@unittest.skipUnless(fixture.SNAPSHOT, 'set PHONE_FACTORY_EVALUATION_ROOT to reviewed collector sources')
class StorageCollectorHttpTests(unittest.TestCase):
    setUp = fixture.ModelIterationFlaskTests.setUp
    client = fixture.ModelIterationFlaskTests.client
    urlopen = fixture.ModelIterationFlaskTests.urlopen

    def seed(self, rid='cr_cleanup', root=None):
        folder=Path(self.manager.HTTP_RUNS_DIR)/rid
        result=folder/'results'
        result.mkdir(parents=True)
        (result/'original.bin').write_bytes(b'real original evidence')
        record={'collection_run_id':rid,'run_id':rid,'batch_id':'batch_cleanup','run_mode':'generate',
                'agent_type':'pipelineagent','status':'failed','phone_ids':[],'workers':{},
                'created_at':'2026-11-05T00:00:00Z','result_dir':str(result),
                'archive_path':str(Path(self.manager.HTTP_ARCHIVES_DIR)/(rid+'.zip'))}
        if root:record['root_run_id']=root
        self.manager._save_http_run(record)
        self.factory.records.put('collection_executions' if root else 'collection_runs',rid,{
            **record,'request':{'metadata':{'run_mode':'generate','collector_server':'http://fixture:9011'}},
            'created_at':'2026-11-05T00:00:00Z'})
        return folder

    def wait(self, operation):
        deadline=time.monotonic()+15
        while time.monotonic()<deadline:
            current=self.factory.runtime.storage_service.get(operation['operation_id'])
            if current['status'] in {'completed','partial','failed'}:
                return current
            self.factory.runtime.storage_service.schedule()
            time.sleep(.03)
        self.fail(current)

    def test_real_cli_cleanup_and_restart_no_resurrection_reports_platform_preserved(self):
        first=self.seed();child=self.seed('cx_cleanup','cr_cleanup');other=self.seed('cr_other')
        platform_file=self.base/'platform'/'raw'/'keep.bin'
        platform_file.parent.mkdir(parents=True);platform_file.write_bytes(b'platform stays')
        value=self.factory.dispatch('storage/runs/cr_cleanup/cleanup','POST',{'request_id':'delete-confirmed'})
        completed=self.wait(value)
        self.assertEqual(completed['status'],'completed',completed)
        self.assertFalse(first.exists());self.assertFalse(child.exists());self.assertTrue(other.exists())
        self.assertEqual(platform_file.read_bytes(),b'platform stays')
        record=self.remote.get('/runs/cr_cleanup')
        self.assertEqual(record.status_code,200,record.text)
        self.assertEqual(record.json['collector_data_state'],'retired')
        self.assertEqual(self.remote.get('/runs/cr_cleanup/collection-artifacts').status_code,410)
        self.manager._storage_service().recover()
        self.assertFalse(first.exists())
        repeat=self.factory.dispatch('storage/runs/cr_cleanup/cleanup','POST',{'request_id':'delete-confirmed'})
        self.assertEqual(repeat['operation_id'],value['operation_id'])

    def test_scan_not_required_and_later_new_request_is_idempotent(self):
        folder=self.seed()
        first=self.wait(self.factory.dispatch('storage/runs/cr_cleanup/cleanup','POST',{'request_id':'first'}))
        self.assertEqual(first['status'],'completed',first)
        second=self.wait(self.factory.dispatch('storage/runs/cr_cleanup/cleanup','POST',{'request_id':'second'}))
        self.assertEqual(second['status'],'completed',second)
        self.assertEqual(second['request_id'],'second')
        self.assertFalse(folder.exists())

    def test_scan_reports_size_without_deleting_or_starting_collection(self):
        folder=self.seed()
        result=self.wait(self.factory.dispatch('storage/runs/cr_cleanup/scan','POST',{'request_id':'scan'}))
        self.assertEqual(result['status'],'completed',result)
        self.assertGreater(result['summary']['bytes'],0)
        self.assertTrue((folder/'results'/'original.bin').is_file())
        self.launch.assert_not_called()

