"""Old work may finish, but cannot promote or publish a resumed input version."""
import copy
import unittest
from unittest.mock import patch

from backend.tests import test_preprocessing_jobs as preprocessing_fixtures
from backend.tests import test_pipelines as pipeline_fixtures
from backend.collection_resume_snapshots import is_current_collection_snapshot


class PreprocessingResumeFenceTests(unittest.TestCase):
    setUp = preprocessing_fixtures.PreprocessingJobTests.setUp
    new_manager = preprocessing_fixtures.PreprocessingJobTests.new_manager
    complete = preprocessing_fixtures.PreprocessingJobTests.complete

    def test_resume_during_annotation_keeps_old_outputs_without_promoting(self):
        self.complete()
        original = self.manager.annotator
        def annotate(*args, **kwargs):
            value = original(*args, **kwargs)
            self.manager.records.put('collection_resume_heads', 'root', {
                'run_id': 'root', 'batch_id': 'batch-one', 'revision': 1, 'active': True})
            return value
        self.manager.annotator = annotate
        job = self.manager.submit('batch-one')
        self.queue.drain()
        final = self.manager.get(job['job_id'])
        self.assertEqual(final['status'], 'succeeded', final.get('error'))
        self.assertTrue(final['stale_collection_input'])
        self.assertTrue(final['historical_outputs'])
        self.assertIsNone(self.store.get('batch-one', '02_annotation'))
        for output in final['historical_outputs']:
            from pathlib import Path
            self.assertTrue(Path(output['path']).is_file())

    def test_old_snapshot_cannot_match_new_head_even_with_same_tasks(self):
        self.manager.records.put('collection_resume_heads', 'root', {
            'run_id': 'root', 'batch_id': 'batch-one', 'revision': 2, 'active': False,
            'current_snapshot_id': 'new', 'current_snapshot_digest': 'new-digest'})
        self.assertFalse(is_current_collection_snapshot(self.manager.records, 'batch-one',
            {'collection_snapshot_id': 'old', 'input_digest': 'old-digest'}))
        self.assertTrue(is_current_collection_snapshot(self.manager.records, 'batch-one',
            {'collection_snapshot_id': 'new', 'input_digest': 'new-digest'}))


class PipelineResumeFenceTests(unittest.TestCase):
    setUp = pipeline_fixtures.PipelineTests.setUp
    create = pipeline_fixtures.PipelineTests.create
    tick = pipeline_fixtures.PipelineTests.tick
    control = pipeline_fixtures.PipelineTests.control

    def test_multiple_root_conflict_is_visible_and_does_not_reset_forever(self):
        p = self.create()
        for run in ('root-a', 'root-b'):
            self.manager.records.put('collection_resume_heads', run, {
                'run_id': run, 'batch_id': 'batch-test', 'revision': 1, 'active': False,
                'current_snapshot_id': run, 'current_snapshot_digest': run})
        p = self.tick(p, 3)
        self.assertEqual(p['status'], 'failed')
        self.assertIn('多个续采根运行', p['error'])
        self.assertEqual(sum('多个续采根运行' in item['message'] for item in p['logs']), 1)

    def test_explicit_resume_while_waiting_survives_snapshot_settlement(self):
        p = self.control(self.create(), 'pause')
        self.manager.records.put('collection_resume_heads', 'root', {
            'run_id': 'root', 'batch_id': 'batch-test', 'revision': 1, 'active': True})
        p = self.tick(p)
        self.assertEqual(p['status'], 'paused')
        p = self.control(p, 'resume')
        self.manager.records.update('collection_resume_heads', 'root', lambda head: head.update(
            active=False, current_snapshot_id='cs-new', current_snapshot_digest='digest-new'))
        p = self.tick(p)
        self.assertEqual(p['status'], 'running')

    def test_old_job_completion_waits_for_stable_snapshot_then_reschedules(self):
        p = self.tick(self.create(), 2)
        self.assertEqual(p['current_step'], 'preprocessing')
        old_job = self.runtime.children['preprocessing'][-1]['job_id']
        self.manager.records.put('collection_resume_heads', 'root', {
            'run_id': 'root', 'batch_id': 'batch-test', 'revision': 1, 'active': True})
        p = self.tick(p)
        self.assertEqual(p['status'], 'paused')
        self.runtime.complete('preprocessing')
        p = self.tick(p, 3)
        self.assertEqual(p['status'], 'paused')
        self.assertEqual(self.runtime.publish_count, 0)
        self.manager.records.update('collection_resume_heads', 'root', lambda head: head.update(
            active=False, current_snapshot_id='cs-new', current_snapshot_digest='digest-new'))
        p = self.tick(p)
        self.assertEqual(p['status'], 'running')
        self.assertEqual(p['current_step'], 'collection')
        step = next(step for step in p['steps'] if step['id'] == 'preprocessing')
        self.assertEqual(step['status'], 'pending')
        self.assertIn(old_job, step['previous_job_ids'])
        self.assertEqual(p['collection_input_version']['collection_snapshot_id'], 'cs-new')


if __name__ == '__main__':
    unittest.main()
