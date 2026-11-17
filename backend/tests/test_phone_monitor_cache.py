from copy import deepcopy
import threading
import time
import unittest
from unittest.mock import Mock

from backend.phone_monitor_cache import PhoneMonitorCache


class PhoneMonitorCacheTests(unittest.TestCase):
    def setUp(self):
        self.store = Mock()
        self.store._current.return_value = {'phones': ['p1', 'p2'], 'phoneApps': [{'phone_id': 'p1', 'app': '中文 App'}], 'tasks': [1]}
        self.store._remote_result.side_effect = lambda command: {'devices': [{'serial': 'p1', 'model': 'Phone', 'battery': 80}]} if command == ['adb-devices'] else {'statuses': [{'phone_id': 'p1', 'status': '运行中'}]}
        self.now = 100.0
        self.cache = PhoneMonitorCache(self.store, clock=lambda: self.now)
        self.addCleanup(self.cache.close)

    def finish(self):
        self.cache._thread.join(timeout=2)
        self.assertFalse(self.cache._thread.is_alive())

    def test_first_read_and_concurrent_reads_never_wait_for_remote(self):
        entered, release = threading.Event(), threading.Event()
        def blocked(command):
            entered.set()
            release.wait(3)
            return {'devices': []} if command == ['adb-devices'] else {'statuses': []}
        self.store._remote_result.side_effect = blocked
        start = time.perf_counter()
        first = self.cache.read()
        self.assertLess(time.perf_counter() - start, .2)
        self.assertTrue(first['refreshing'])
        self.assertIsNone(first['observed_at'])
        self.assertTrue(entered.wait(1))
        for _ in range(20):
            snapshot = self.cache.read(force=True)
            self.assertIsNone(snapshot['devices'][0]['connected'])
        self.assertEqual(self.store._remote_result.call_count, 1)
        release.set()
        self.finish()
        self.assertFalse(self.cache.read()['refreshing'])

    def test_fields_staleness_failure_and_recovery_keep_real_observation(self):
        self.cache.read()
        self.finish()
        good = self.cache.read()
        self.assertEqual(good['task_count'], 1)
        self.assertEqual(good['devices'][0]['apps'], ['中文 App'])
        self.assertEqual([value['connected'] for value in good['devices']], [True, False])
        self.assertFalse(good['stale'])
        self.store._remote_result.side_effect = RuntimeError('timeout token=secret')
        self.now += 21
        self.cache.read()
        self.finish()
        failed = self.cache.read()
        self.assertTrue(failed['stale'])
        self.assertEqual(failed['devices'], good['devices'])
        self.assertEqual(failed['observed_at'], good['observed_at'])
        self.assertNotIn('secret', failed['error'])
        calls = self.store._remote_result.call_count
        self.cache.read()
        self.assertEqual(self.store._remote_result.call_count, calls)
        self.store._remote_result.side_effect = lambda command: {'devices': []} if command == ['adb-devices'] else {'statuses': []}
        self.cache.read(force=True)
        self.finish()
        self.assertFalse(self.cache.read()['stale'])

    def test_close_fences_late_probe_without_blocking(self):
        entered, release = threading.Event(), threading.Event()
        def block(command):
            entered.set()
            release.wait(2)
            return {'devices': []} if command == ['adb-devices'] else {'statuses': []}
        self.store._remote_result.side_effect = block
        self.cache.read()
        self.assertTrue(entered.wait(1))
        start = time.perf_counter()
        self.cache.close()
        self.assertLess(time.perf_counter() - start, .2)
        release.set()
        self.finish()
        self.assertIsNone(self.cache.read()['observed_at'])
        self.assertTrue(self.cache.read()['stale'])

