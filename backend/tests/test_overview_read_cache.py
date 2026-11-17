"""Parity and bounded reads over a 100k-row catalog; no workbook access."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from backend.data_store import RecordStore
from backend.training_data_overview.service import CATALOG, CONVERSIONS, summarize, TrainingOverviewManager
from backend.training_data_overview.read_cache import OverviewReadCache
from backend.tests.test_training_overview_service import row, Registry


class ReadCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.records = RecordStore(Path(self.temp.name))
        self.catalog = {'version': 'v1', 'rows': [row(), row(identity='second', app='App B', known=False)]}
        self.records.put(CATALOG, 'current', self.catalog)
        self.cache = OverviewReadCache(self.records, CATALOG, CONVERSIONS, summarize, limit=3)

    def test_versions_invalidate_without_redecoding_catalog_for_status_only_changes(self):
        with patch.object(self.cache, '_decode_catalog', wraps=self.cache._decode_catalog) as read:
            first = self.cache.query({})
            self.assertEqual(first, summarize(self.catalog, [], {}))
            first['overview']['total_steps'] = -1
            self.assertEqual(self.cache.query({'source': 'all', 'app': ''})['overview']['total_steps'], 6)
            self.records.put(CONVERSIONS, 'r1', {'release_id': 'r1', 'status': 'failed', 'warnings': ['check']})
            read.reset_mock()
            self.assertIn('check', self.cache.query({})['warnings'])
            self.assertEqual(read.call_count, 0)
            self.catalog = {**self.catalog, 'version': 'v2', 'rows': [row()]}
            self.records.put(CATALOG, 'current', self.catalog)
            read.reset_mock()
            self.assertEqual(self.cache.query({})['overview']['total_steps'], 3)
            self.assertEqual(read.call_count, 1)

    def test_filters_are_exact_and_lru_is_bounded(self):
        for filters in ({'app': 'App A'}, {'source': '数据飞轮'}, {'level1': '场景一'}, {'app': 'missing'}, {'start_date': '2026-09-17', 'end_date': '2026-09-17'}):
            self.assertEqual(self.cache.query(filters), summarize(self.catalog, [], filters))
        self.assertEqual(len(self.cache._queries), 3)
        with self.assertRaises(ValueError):
            self.cache.query({'start_date': 'not-a-date'})

    def test_concurrent_first_reads_decode_once(self):
        with patch.object(self.cache, '_decode_catalog', wraps=self.cache._decode_catalog) as read:
            with ThreadPoolExecutor(max_workers=8) as executor:
                values = list(executor.map(lambda _: self.cache.query({}), range(12)))
        self.assertTrue(all(value == values[0] for value in values))
        self.assertEqual(read.call_count, 1)

    def test_100k_rows_match_reference_and_warm_reads_are_small(self):
        rows = [row(identity=str(i // 2), app=f'App {i % 7}', scene=f'场景{i % 3}', steps=i % 11, known=i % 3 != 0) for i in range(100000)]
        for index, value in enumerate(rows):
            value.update({'子任务轨迹': str(index), '生产方式': 'DEV' if index % 2 else '数据飞轮', '时间': f'2026-09-{index % 28 + 1:02d} 10:00:00'})
        catalog = {'version': 'large', 'rows': rows}
        self.records.put(CATALOG, 'current', catalog)
        for filters in ({}, {'source': 'DEV', 'app': 'App 2', 'start_date': '2026-09-05'}, {'level1': '场景1', 'end_date': '2026-09-12'}):
            self.assertEqual(self.cache.query(filters), summarize(catalog, [], filters))
        self.cache.query({})
        durations = []
        with patch.object(self.cache, '_decode_catalog', side_effect=AssertionError('warm query decoded catalog')):
            for _ in range(20):
                start = time.perf_counter()
                self.cache.query({})
                durations.append(time.perf_counter() - start)
        p95 = sorted(durations)[18]
        print(f'overview 100000 rows warmed P95={p95:.4f}s')
        self.assertLess(p95, 2.0)

    def test_warmup_failure_cannot_remove_committed_export(self):
        manager = TrainingOverviewManager(Registry(Path(self.temp.name) / 'publication'))
        self.addCleanup(manager.close)
        manager.records.put(CONVERSIONS, 'rel_one', {'release_id': 'rel_one', 'status': 'queued'})
        manager._started = True
        with patch.object(manager._read_executor, 'submit', side_effect=RuntimeError('closed executor')), self.assertLogs('backend.training_data_overview.service', level='WARNING'):
            manager._publish('rel_one', {'rows': [row()]})
        self.assertTrue(manager.workbook().is_file())
        self.assertEqual(manager.records.get(CONVERSIONS, 'rel_one')['status'], 'succeeded')

