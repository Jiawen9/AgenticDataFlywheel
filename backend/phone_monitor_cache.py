"""Small device snapshot: remote probes only run in one background thread."""
from copy import deepcopy
from datetime import datetime, timezone
import json
import threading
import time
from .data_store.summary_index import _text


def _error_text(error):
    return _text(str(error), 500)


class PhoneMonitorCache:
    """The cache never claims a failed probe means a disconnected device.

    Memory-only observations intentionally start unknown after restart: a saved
    snapshot from a different configured collector must not look like live data.
    No source/history lists or evidence files are loaded by read().
    """
    def __init__(self, store, *, interval=10.0, stale_after=20.0, clock=time.monotonic):
        self.store = store
        self.interval, self.stale_after, self.clock = interval, stale_after, clock
        self._lock = threading.Lock()
        self._closed = False
        self._thread = None
        self._refreshing = False
        self._next = 0.0
        self._observed = None
        self._failures = 0
        self._snapshot = dict(devices=[], task_count=0, observed_at=None, error=None)

    def read(self, *, force=False):
        with self._lock:
            now = self.clock()
            if not self._closed and not self._refreshing and (force or now >= self._next):
                self._refreshing = True
                self._thread = threading.Thread(target=self._refresh, name='phone-monitor-snapshot', daemon=True)
                self._thread.start()
            result = deepcopy(self._snapshot)
            age = None if self._observed is None else max(0, now - self._observed)
            result.update(refreshing=self._refreshing, observation_age_seconds=age,
                          stale=age is None or age > self.stale_after or bool(result['error']))
            return result

    def _refresh(self):
        try:
            state = self.store._current()
            associations = state.get('phoneApps') or []
            ids = list(dict.fromkeys([*(state.get('phones') or []), *(row['phone_id'] for row in associations)]))
            apps = {phone: list(dict.fromkeys(row['app'] for row in associations if row['phone_id'] == phone)) for phone in ids}
            # Configured devices can be shown before the slow first probe finishes.
            with self._lock:
                if self._closed:
                    return
                if self._observed is None:
                    self._snapshot['devices'] = [dict(id=phone, apps=apps[phone], model='', battery=None, connected=None, status='未知') for phone in ids]
                    self._snapshot['task_count'] = len(state.get('tasks') or [])
            adb = self.store._remote_result(['adb-devices'])
            if not isinstance(adb, dict) or not isinstance(adb.get('devices'), list):
                raise ValueError('采集服务设备快照格式无效')
            by_id = {row['serial']: row for row in adb['devices']}
            ids = list(dict.fromkeys([*ids, *by_id]))
            status = self.store._remote_result(['status', json.dumps(ids)]) if ids else {'statuses': []}
            if not isinstance(status, dict) or not isinstance(status.get('statuses'), list):
                raise ValueError('采集服务运行状态快照格式无效')
            statuses = {row['phone_id']: row for row in status['statuses']}
            devices = []
            for phone in ids:
                device, running = by_id.get(phone, {}), statuses.get(phone, {})
                devices.append(dict(id=phone, apps=apps.get(phone, []), model=device.get('model', ''),
                                    battery=device.get('battery'), connected=phone in by_id,
                                    status=running.get('status') or '未知', agent_type=running.get('agent_type'), run_id=running.get('run_id')))
            with self._lock:
                if not self._closed:
                    self._snapshot = dict(devices=devices, task_count=len(state.get('tasks') or []),
                                          observed_at=datetime.now(timezone.utc).isoformat(), error=None)
                    self._observed, self._failures = self.clock(), 0
        except Exception as error:
            with self._lock:
                if not self._closed:
                    self._snapshot['error'] = _error_text(error)
                    self._failures += 1
        finally:
            with self._lock:
                self._refreshing = False
                self._next = self.clock() + min(120.0, self.interval * 2 ** min(self._failures, 4))

    def close(self):
        with self._lock:
            self._closed = True
        # A slow CLI may finish later; its observation is fenced above. Closing
        # the platform must not wait for the collector's full network timeout.

