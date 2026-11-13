"""Small structured events on stdout; no prompts, credentials, or image content."""
from __future__ import annotations
from contextlib import contextmanager
import json
import os
import sys
import threading
import time

PREFIX = '@ADF_CHECKLIST_EVENT '
_lock = threading.Lock()


def emit(kind, **fields):
    if os.environ.get('ADF_CHECKLIST_EVENTS') != '1':
        return
    allowed = {'stage', 'role', 'model', 'call_id', 'attempt', 'timeout_seconds',
               'elapsed_seconds', 'completed', 'total', 'error_type', 'output_policy'}
    value = {'kind': kind, **{k: v for k, v in fields.items() if k in allowed}}
    with _lock:
        sys.stdout.write(PREFIX + json.dumps(value, ensure_ascii=False) + '\n')
        sys.stdout.flush()


@contextmanager
def request(*, role, model, call_id, attempt, stage=''):
    started = time.monotonic()
    fields = dict(role=role, model=model, call_id=call_id, attempt=attempt,
                  stage=stage, timeout_seconds=120)
    emit('request_start', **fields)
    try:
        yield
    except BaseException as exc:
        emit('request_error', **fields, error_type=type(exc).__name__,
             elapsed_seconds=round(time.monotonic() - started, 3))
        raise
    else:
        emit('request_end', **fields, elapsed_seconds=round(time.monotonic() - started, 3))
