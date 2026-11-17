"""Read projections must preserve eligibility without reading evidence."""
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from backend.data_store import ArtifactStore, RecordStore
from backend.batch_results import annotation_task_fingerprints
from backend.stage_page_reads import batch_page, task_page, quality_candidate_page, projection_entries


class StagePageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.records = RecordStore(self.root)

    def stage(self, batch, stage, payload, version='v1', project=True):
        ref = dict(batch_id=batch, stage=stage, version=version, content_hash=stage + version,
                   created_at='2026-11-17T10:00:00Z', files={'data.xlsx': {'path': 'file.xlsx', 'sha256': 'digest'}})
        entries = [dict(namespace='artifacts', key=ArtifactStore._key(batch, stage), payload=ref)]
        if project: entries += projection_entries(ref, payload)
        self.records.put_many(entries)
        return ref

    def fixture(self, batch='b1', count=2, quality=True):
        rows = [{'task_id': f't{i:04}', 'trajectory_id': f'tr{i}', 'task': f'任务{i}', 'image': f'x/step{i}.jpg', 'action': 'click(1,2)'} for i in range(count)]
        annotation = {'sheets': {'rows': rows}}
        self.stage(batch, '02_annotation', annotation)
        fingerprints = annotation_task_fingerprints(annotation)
        tasks = [dict(task_id=row['task_id'], trajectory_count=1, original_step_count=1, tree_step_count=1) for row in rows]
        hashes = {row['task_id']: 'tree-' + row['task_id'] for row in rows}
        tree = dict(tasks=tasks, trees={row['task_id']: {} for row in rows}, source_task_fingerprints=fingerprints, tree_hashes=hashes)
        self.stage(batch, '04_tree', tree)
        if quality:
            scored = [dict(task_id=row['task_id'], trajectory_count=1, evaluations={f'tr{i}': {'global_score': 3.0}}, fusion_complete=True, quality_job_id='committed', status='succeeded') for i, row in enumerate(rows)]
            self.stage(batch, '05_quality', dict(tasks=scored, source_tree_hashes=hashes, job_id='committed'))
        return annotation, tree

    def test_page_preserves_current_versions_downloads_and_candidate_eligibility(self):
        annotation, tree = self.fixture()
        with patch.object(ArtifactStore, 'read_payload', side_effect=AssertionError('evidence read')):
            page = task_page(self.root, 'b1', limit=1)
            self.assertEqual(page['total'], 2)
            self.assertEqual(len(page['items']), 1)
            self.assertEqual(page['quality_counts']['completed'], 2)
            batches = batch_page(self.root)
            self.assertEqual(batches['items'][0]['artifacts'][0]['stage'], '02_annotation')
            candidates = quality_candidate_page(self.root)
            self.assertEqual(candidates['total'], 1)
            self.assertEqual(candidates['items'][0]['reviewed_task_count'], 2)
        annotation['sheets']['rows'][0]['action'] = 'click(5,6)'
        self.stage('b1', '02_annotation', annotation, version='v2')
        page = task_page(self.root, 'b1')
        self.assertEqual(page['quality_counts']['completed'], 1)
        self.assertEqual(quality_candidate_page(self.root)['items'][0]['reviewed_task_count'], 1)

    def test_missing_projection_reports_preparing_not_zero_or_stale_success(self):
        annotation, _ = self.fixture()
        self.stage('b1', '02_annotation', annotation, version='v2', project=False)
        page = task_page(self.root, 'b1')
        self.assertFalse(page['counts_complete'])
        self.assertIsNone(page['task_count'])
        self.assertIsNone(page['total'])
        self.assertEqual(page['items'], [])
        batch = batch_page(self.root)['items'][0]
        self.assertTrue(batch['summary_preparing'])
        self.assertIsNone(batch['ready_trajectory_count'])
        self.assertFalse(batch['can_start'])

    def test_filter_before_pagination_and_published_batch_exclusion(self):
        for batch, status in [('b1', 'failed'), ('b2', 'succeeded'), ('b3', 'failed')]:
            self.fixture(batch)
            self.records.put('preprocessing_jobs', batch, dict(job_id=batch, batch_id=batch, status=status, created_at=batch))
        result = batch_page(self.root, status='failed', limit=1, offset=1)
        self.assertEqual(result['total'], 2)
        self.assertEqual(result['items'][0]['batch_id'], 'b1')
        self.records.put('batch_lifecycle', 'b3', {'batch_id': 'b3', 'status': 'published'})
        self.assertEqual([row['batch_id'] for row in batch_page(self.root, status='failed')['items']], ['b1'])
        self.assertEqual(quality_candidate_page(self.root, status='pending')['total'], 0)
        self.assertEqual(quality_candidate_page(self.root, q='b1')['total'], 1)

    def partial(self, batch='b1', count=2):
        ids = [f't{i:04}' for i in range(count)]
        self.records.put('quality_jobs', 'job2', dict(job_id='job2', batch_id=batch, created_at='later', status='running', task_ids=ids, task_fingerprints={task: 'f-' + task for task in ids}))
        self.records.put('quality_executions', 'job2', dict(token='token', state='running'))
        self.records.put_many([dict(namespace='quality_partial_results', key=task, payload=dict(batch_id=batch, task_id=task, job_id='job2', execution_token='token', source_tree_hash='tree-' + task, input_fingerprint='f-' + task, result=dict(task_id=task, trajectory_count=1, evaluations={'tr': {'global_score': 4}}, fusion_complete=True))) for task in ids])

    def test_partials_are_fenced_and_never_counted_as_complete(self):
        self.fixture(quality=False)
        self.partial()
        page = task_page(self.root, 'b1', view='quality')
        self.assertEqual(page['quality_counts']['completed'], 0)
        self.assertTrue(page['items'][0]['partial_results_available'])
        self.assertFalse(page['items'][0]['fusion_complete'])
        self.assertEqual(page['items'][0]['status'], 'running')
        self.assertEqual(quality_candidate_page(self.root)['total'], 0)
        self.records.put('quality_executions', 'job2', dict(token='new-token', state='running'))
        self.assertFalse(task_page(self.root, 'b1')['items'][0].get('results_available', False))
        self.records.put('quality_executions', 'job2', dict(token='token', state='committed'))
        self.assertFalse(task_page(self.root, 'b1')['items'][0].get('results_available', False))

    def test_1000_tasks_have_bounded_metadata_queries(self):
        self.fixture(count=1000, quality=False)
        self.partial(count=1000)
        statements = []
        original = RecordStore._connection
        @contextmanager
        def traced(store, **options):
            with original(store, **options) as connection:
                connection.set_trace_callback(statements.append)
                yield connection
        with patch.object(RecordStore, '_connection', traced), patch.object(ArtifactStore, 'read_payload', side_effect=AssertionError('evidence scan')):
            start = time.perf_counter()
            page = task_page(self.root, 'b1', limit=10)
            elapsed = time.perf_counter() - start
        self.assertEqual(page['total'], 1000)
        self.assertEqual(len(page['items']), 10)
        self.assertLess(len(statements), 20)
        print(f'stage 1000 tasks metadata queries={len(statements)} elapsed={elapsed:.4f}s')
        self.assertLess(elapsed, 2.0)

    def test_544_times_10_and_1000_historical_batches_against_legacy_reader(self):
        from backend.preprocessing_jobs import PreprocessingJobManager
        rows = [dict(task_id=f't{task:04}', trajectory_id=f'tr{task:04}-{repeat}', task=f'任务 {task}', image=f't{task:04}/tr{repeat}/step001.jpg', action='click(1,2)') for task in range(544) for repeat in range(10)]
        store = ArtifactStore(self.root)
        store.publish('b-scale', '02_annotation', {'sheets': {'rows': rows}})
        self.records.put_many([dict(namespace='preprocessing_jobs', key=f'j{i:04}', payload=dict(job_id=f'j{i:04}', batch_id=f'b-old-{i:04}', status='failed' if i % 2 else 'succeeded', created_at='2026-01-01T00:00:00Z')) for i in range(1000)])
        manager = PreprocessingJobManager(self.root)
        self.addCleanup(manager.shutdown)
        with patch.object(manager.artifacts, 'read_payload', wraps=manager.artifacts.read_payload) as read:
            start = time.perf_counter()
            baseline = manager.batches()
            legacy = time.perf_counter() - start
        self.assertEqual(read.call_count, 1)
        expected = next(row for row in baseline if row['batch_id'] == 'b-scale')
        timings = []
        with patch.object(ArtifactStore, 'read_payload', side_effect=AssertionError('summary scanned artifact')):
            for _ in range(20):
                start = time.perf_counter()
                page = batch_page(self.root, limit=10)
                timings.append(time.perf_counter() - start)
            target = batch_page(self.root, batch_id='b-scale')['items'][0]
            self.assertEqual(task_page(self.root, 'b-scale', limit=10)['total'], 544)
        self.assertEqual(page['total'], len(baseline))
        self.assertEqual(target['ready_trajectory_count'], 5440)
        for key in ('task_count', 'ready_trajectory_count', 'ready_step_count', 'collection_status', 'preprocessing_status', 'can_start'):
            self.assertEqual(target[key], expected[key], key)
        p95 = sorted(timings)[18]
        print(f'1000 history + 544x10: legacy full list={legacy:.4f}s, summary cold={timings[0]:.4f}s hotP95={p95:.4f}s; evidence reads old=1 new=0')
        self.assertLess(p95, 2.0)

    def test_missing_store_contract_and_selected_off_page_task(self):
        empty = task_page(self.root, 'absent')
        self.assertIsNone(empty['task_count'])
        self.assertEqual(empty['task_ids'], [])
        self.assertEqual(empty['ready_task_ids'], [])
        self.assertFalse(empty['counts_complete'])
        self.assertEqual(empty['tree_counts'], {'completed': None, 'pending': None, 'stale': None})
        self.fixture()
        page = task_page(self.root, 'b1', limit=1, selected_task_id='t0001')
        self.assertEqual(page['items'][0]['task_id'], 't0000')
        self.assertEqual(page['selected_task']['task_id'], 't0001')
        self.assertEqual(page['tree_counts'], {'completed': 2, 'pending': 0, 'stale': 0})
        filtered = task_page(self.root, 'b1', limit=1, q='t0001')
        self.assertEqual(filtered['total'], 1)
        self.assertEqual(filtered['tree_counts'], page['tree_counts'])
        self.assertEqual(filtered['tree_counts_scope'], 'batch')

    def test_quality_list_payload_and_short_display_search(self):
        from backend.stage_page_reads import quality_summary
        self.assertFalse(quality_summary({'evaluations': []})['rubric_scores_valid'])
        self.assertFalse(quality_summary(None)['rubric_scores_valid'])
        self.fixture(batch='hash-long-batch')
        self.records.put('display_identifiers', 'batch-alias', dict(kind='batch', id='hash-long-batch', display_id='b261117-001'))
        self.assertEqual(batch_page(self.root, q='b261117-001')['total'], 1)
        self.stage('hash-long-batch', '00_collection', {'kind': 'manual_collection', 'task_count': 1})
        self.assertEqual(batch_page(self.root, q='手工采集')['total'], 1)

    def test_legacy_version_pointers_and_invalid_projection_never_fake_empty_success(self):
        annotation, _ = self.fixture()
        ref = self.records.get('artifacts', 'b1/02_annotation')
        self.records.delete('artifacts', 'b1/02_annotation')
        self.records.put('artifacts', 'b1/02_annotation/v1', ref)
        self.assertEqual(task_page(self.root, 'b1')['total'], 2)
        self.stage('b1', '02_annotation', annotation, version='v2')
        self.records.put('artifacts', 'b1/02_annotation/v9', {**ref, 'version': 'v9', 'created_at': '2099'})
        self.assertEqual(task_page(self.root, 'b1')['annotation_version'], 'v2')
        self.stage('b1', '05_quality', {'tasks': [{}]}, version='v3')
        page = task_page(self.root, 'b1')
        self.assertFalse(page['counts_complete'])
        self.assertIsNone(page['total'])
        self.assertIn('KeyError', batch_page(self.root)['items'][0]['summary_error'])

