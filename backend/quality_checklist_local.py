"""Platform-owned, read-only native Checklist jobs with durable global slots.

SQLite holds short control transactions only. Models, hashing and child cleanup
never hold the transaction. Successful reports are reused only after source,
definition, configuration and native report contracts have been revalidated.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import queue
import secrets
import socket
import subprocess
import threading
import time
from uuid import uuid4

from .batch_operations import _process_identity
from .data_store import RecordStore
from .data_store.registry import utc_now
from .file_io import io_path, logical_path, resolve_path, is_link_or_junction
from .pipeline_diagnostics import sanitize
from .pipeline_retry_errors import failure_from_exception
from .quality_process import process_metadata, stop_owned_process, stop_recorded_process
from .ChecklistEvaluator.evaluation_profiles import verify_snapshot
from .ChecklistEvaluator.readonly_inputs import SKIP, IGNORED, verify_reference
from .ChecklistEvaluator.report_validation import validate_report
from .ChecklistEvaluator.runtime_config import config_digest

RUNTIME = Path(__file__).with_name('ChecklistEvaluator')
TASKS = 'quality_checklist_local_tasks'
ENTRIES = 'quality_checklist_local_entries'
SLOTS = 'quality_checklist_local_slots'
EVENT_PREFIX = '@ADF_CHECKLIST_EVENT '
GLOBAL_CONCURRENCY = 2
MAX_QUEUED = 16


class LocalChecklistRevoked(RuntimeError):
    pass


class LocalChecklistCleanupUnconfirmed(RuntimeError):
    """The old child still owns its slot; no later trajectory may be launched."""
    pass


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(',', ':')).encode('utf-8')).hexdigest()


def task_key(job, task_manifest, input_digest):
    return digest({'input': input_digest, 'options': job['checklist_options'], 'task': _stable_manifest(task_manifest)})


def _stable_manifest(task_manifest):
    return {**task_manifest, 'trajectories': [
        {k: v for k, v in row.items() if k != 'source_error'} for row in task_manifest['trajectories']]}


def get_task_definition(root, job, task_manifest, input_digest):
    return (RecordStore(root).get(TASKS, task_key(job, task_manifest, input_digest)) or {}).get('definition')


def get_task_results(root, job, task_manifest, input_digest):
    key = task_key(job, task_manifest, input_digest)
    ids = [entry['trajectory_id'] for entry in task_manifest['trajectories']]
    rows = RecordStore(root).get_many(ENTRIES, [key + ':' + digest(ident)[:24] for ident in ids])
    return {row['trajectory_id']: row['result'] for row in rows if row.get('result')}


def _read(path):
    return json.loads(io_path(path).read_text(encoding='utf-8-sig'))


def _atomic(path, value, *, immutable=False):
    path = io_path(path)
    data = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    if immutable and path.is_file():
        if path.read_bytes() != data:
            raise ValueError('frozen Checklist descriptor differs from existing content')
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + uuid4().hex[:8] + '.tmp')
    try:
        with temporary.open('xb') as stream:
            stream.write(data); stream.flush(); os.fsync(stream.fileno())
        # Readers are short lived; retry Windows sharing violations, not all I/O errors.
        for attempt in range(8):
            try:
                os.replace(temporary, path); break
            except OSError as exc:
                if getattr(exc, 'winerror', None) not in {5, 32, 33} or attempt == 7:
                    raise
                time.sleep(.025 * (attempt + 1))
    finally:
        temporary.unlink(missing_ok=True)


def _sha(path, check=None):
    value = hashlib.sha256()
    with io_path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            if check: check()
            value.update(block)
    return value.hexdigest()


def _owner_dead(row):
    if row.get('hostname') != socket.gethostname():
        return False
    pid, expected = row.get('owner_pid'), row.get('owner_identity')
    if not isinstance(pid, int) or not expected:
        return False
    observed = _process_identity(pid)
    return observed is False or (observed is not None and observed != expected)


class _Lease:
    def __init__(self, root, key, job, check, progress):
        self.records = RecordStore(root); self.key = key; self.job = job
        self.external_check = check or (lambda: None); self.progress = progress
        self.token = uuid4().hex; self.slot = None

    def check(self):
        self.external_check()
        if self.slot is not None:
            row = self.records.get(SLOTS, self.slot) or {}
            if row.get('token') != self.token or row.get('state') != 'active':
                raise LocalChecklistRevoked('Checklist 执行代次已失效')

    def _cleanup(self):
        # Check identities and kill outside SQLite. CAS below protects a newer owner.
        for slot in range(GLOBAL_CONCURRENCY):
            key = str(slot); row = self.records.get(SLOTS, key) or {}
            if row.get('state') != 'active' or not (
                    _owner_dead(row) or (row.get('child') or {}).get('cleanup_failed')):
                continue
            child = row.get('child') or {}
            if child and not stop_recorded_process(child):
                continue  # Retain this slot; a different free slot remains usable.
            with self.records._connection(write=True) as connection:
                connection.execute('BEGIN IMMEDIATE')
                current = self.records._read(connection, SLOTS, key) or {}
                if current.get('token') == row.get('token'):
                    self.records._save(connection, SLOTS, key,
                        {**current, 'state': 'released', 'child': {}, 'recovered_at': utc_now()},
                        current.get('storage_revision', 0) + 1)
                connection.commit()
        with self.records._connection(write=True) as connection:
            queued = connection.execute("SELECT record_key,payload FROM records WHERE namespace=? "
                "AND json_extract(payload,'$.queued')=1 LIMIT ?", (TASKS, MAX_QUEUED + 1)).fetchall()
        for key, payload in queued:
            row = json.loads(payload)
            if not _owner_dead(row.get('queue_owner') or {}): continue
            def release(current):
                if current.get('queue_owner') == row.get('queue_owner'): current['queued'] = False
            self.records.update(TASKS, key, release)

    def __enter__(self):
        try:
            return self._acquire()
        except BaseException:
            # __exit__ is not called when __enter__ fails after acquiring a slot.
            if self.slot is not None:
                self.__exit__()
            else:
                self.records.update(TASKS, self.key, lambda row: row.update(queued=False), default={})
            raise

    def _acquire(self):
        self._cleanup()
        with self.records._connection(write=True) as connection:
            connection.execute('BEGIN IMMEDIATE')
            row = self.records._read(connection, TASKS, self.key) or {}
            count = connection.execute("SELECT COUNT(*) FROM records WHERE namespace=? "
                "AND json_extract(payload,'$.queued')=1", (TASKS,)).fetchone()[0]
            if not row.get('queued') and count >= MAX_QUEUED:
                connection.rollback(); raise RuntimeError('Checklist 本地准备队列已满，请稍后重试')
            self.records._save(connection, TASKS, self.key,
                {**row, 'queued': True, 'last_quality_job_id': self.job['job_id'], 'queue_owner': {
                    'owner_pid': os.getpid(), 'owner_identity': _process_identity(os.getpid()),
                    'hostname': socket.gethostname()}}, row.get('storage_revision', 0) + 1)
            connection.commit()
        last_progress = 0
        while True:
            self.external_check(); self._cleanup()
            with self.records._connection(write=True) as connection:
                connection.execute('BEGIN IMMEDIATE')
                slots = [(str(i), self.records._read(connection, SLOTS, str(i)) or {})
                         for i in range(GLOBAL_CONCURRENCY)]
                same_task = any(row.get('state') == 'active' and row.get('task_key') == self.key
                                for _, row in slots)
                available = next(((i, row) for i, row in slots if row.get('state') != 'active'), None)
                if available and not same_task:
                    ident, old = available
                    row = {'state': 'active', 'token': self.token, 'task_key': self.key,
                           'quality_job_id': self.job['job_id'], 'owner_pid': os.getpid(),
                           'owner_identity': _process_identity(os.getpid()), 'hostname': socket.gethostname(),
                           'acquired_at': utc_now(), 'child': {}}
                    self.records._save(connection, SLOTS, ident, row, old.get('storage_revision', 0) + 1)
                    self.slot = ident
                connection.commit()
            if self.slot is not None:
                return self
            if time.monotonic() - last_progress >= 2:
                if self.progress: self.progress(stage='checklist_queued')
                last_progress = time.monotonic()
            time.sleep(.2)

    def save(self, namespace, key, changes):
        self.external_check()
        with self.records._connection(write=True) as connection:
            connection.execute('BEGIN IMMEDIATE')
            owner = self.records._read(connection, SLOTS, self.slot) or {}
            if owner.get('state') != 'active' or owner.get('token') != self.token:
                connection.rollback(); raise LocalChecklistRevoked('Checklist 迟到结果被拒绝')
            current = self.records._read(connection, namespace, key) or {}
            saved = self.records._save(connection, namespace, key, {**current, **changes},
                                        current.get('storage_revision', 0) + 1)
            connection.commit()
            return saved

    def child(self, metadata):
        return self.save(SLOTS, self.slot, {'child': metadata})

    def __exit__(self, *unused):
        if self.slot is None: return
        with self.records._connection(write=True) as connection:
            connection.execute('BEGIN IMMEDIATE')
            row = self.records._read(connection, SLOTS, self.slot) or {}
            if row.get('token') == self.token:
                child = row.get('child') or {}
                # Keep uncertain child ownership durable instead of making a new launch safe-looking.
                state = 'released' if not child or child.get('exited') is True else 'active'
                self.records._save(connection, SLOTS, self.slot,
                    {**row, 'state': state, 'released_at': utc_now()}, row.get('storage_revision', 0) + 1)
            connection.commit()
        self.records.update(TASKS, self.key, lambda row: row.update(queued=False), default={})


def _child(root, options, spec, work, lease, progress):
    """Run a gated child; drain output even after a log sink error, then stop it."""
    from .quality_checklist_config import child_environment
    lease.check()
    existing = (lease.records.get(SLOTS, lease.slot) or {}).get('child') or {}
    if existing.get('pid') and existing.get('exited') is not True:
        raise LocalChecklistCleanupUnconfirmed('旧 Checklist 子进程退出未确认，禁止覆盖进程身份')
    io_path(work).mkdir(parents=True, exist_ok=True)
    request = work / 'request.json'; result_path = work / 'result.json'
    _atomic(request, spec)
    io_path(result_path).unlink(missing_ok=True)
    environment = child_environment(root, options)
    environment.update(ADF_CHECKLIST_EVENTS='1', PYTHONUNBUFFERED='1', PYTHONDONTWRITEBYTECODE='1')
    process = None; reader = None; errors = queue.Queue(); events = queue.Queue(maxsize=100)
    metadata = {}; launch_released = False
    observation_lock = threading.Lock()
    observation = {'last': time.monotonic(), 'active': {}}
    # Fail before starting a model process if logging is unwritable.
    sink = io_path(work / 'worker.log').open('wb')
    def flush_events():
        # Bound each pass even while a live child keeps publishing. A short
        # child may exit before the first poll, so the exit path uses this too.
        for _ in range(events.maxsize):
            try:
                event = events.get_nowait()
            except queue.Empty:
                break
            lease.check()
            if progress:
                progress(checklist_event=event, current_model=event.get('model'),
                         current_model_role=event.get('role'), request_phase=event.get('stage'))

    def drain():
        size = 0; pending = b''; write_failed = False; dropping = False
        secret_values = [environment.get(role.get('api_key_env', ''), '')
                         for role in options['evaluator_config'].values()]
        def line_received(line):
            nonlocal size, write_failed
            if len(line) > 8192:
                line = b'[oversized log line omitted]\n'
            text = line.decode('utf-8', errors='replace')
            for secret in secret_values:
                if secret: text = text.replace(secret, '[REDACTED]')
            safe = str(sanitize(text))
            if not write_failed and size < 2 * 1024 * 1024:
                try:
                    encoded = safe.encode('utf-8')[:2 * 1024 * 1024 - size]
                    sink.write(encoded); sink.flush(); size += len(encoded)
                except OSError as exc:
                    errors.put(exc); write_failed = True
            if safe.startswith(EVENT_PREFIX):
                try:
                    event = json.loads(safe[len(EVENT_PREFIX):])
                    with observation_lock:
                        observation['last'] = time.monotonic()
                        if event.get('kind') == 'request_start':
                            observation['active'][event.get('call_id')] = observation['last']
                        elif event.get('kind') in {'request_end', 'request_error'}:
                            observation['active'].pop(event.get('call_id'), None)
                    try: events.put_nowait(event)
                    except queue.Full: pass  # UI samples may coalesce; supervision never loses a request end.
                except ValueError: pass
        try:
            while True:
                block = process.stdout.read1(8192)
                if not block: break
                if dropping:
                    if b'\n' not in block: continue
                    block = block.split(b'\n', 1)[1]; dropping = False
                pending += block
                while b'\n' in pending:
                    line, pending = pending.split(b'\n', 1)
                    line_received(line + b'\n')
                if len(pending) > 262144:
                    # Omit overlong opaque output completely; never split a secret.
                    line_received(b'[oversized log line omitted]\n')
                    pending = b''; dropping = True
            if pending: line_received(pending)
        except BaseException as exc:
            errors.put(exc)
        finally:
            try:
                sink.close()
                if process.stdout: process.stdout.close()
            except OSError as exc:
                errors.put(exc)
    try:
        lease.check()
        process = subprocess.Popen([options['python_executable'], '-B', '-u',
            str(RUNTIME / 'run_quality_checklist.py'), '--spec', str(request), '--wait-for-go'],
            cwd=str(RUNTIME), env=environment, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        metadata = process_metadata(process)
        lease.child({**metadata, 'launch_gate': True, 'launch_released': False})
        reader = threading.Thread(target=drain, name='quality-checklist-log', daemon=True); reader.start()
        lease.check(); process.stdin.write(b'GO\n'); process.stdin.flush(); launch_released = True
        lease.child({**metadata, 'launch_gate': True, 'launch_released': True})
        # These are idle/request budgets, not an aggregate job deadline.
        last_poll = 0
        while process.poll() is None:
            lease.check()
            if not errors.empty(): raise RuntimeError('Checklist 日志写入失败：' + str(errors.get()))
            flush_events()
            now = time.monotonic()
            with observation_lock:
                active_requests = dict(observation['active']); last_event = observation['last']
            if active_requests and now - min(active_requests.values()) > 180:
                raise TimeoutError('Checklist 单次模型请求超过监管预算 180 秒')
            if not active_requests and now - last_event > 900:
                raise TimeoutError('Checklist 当前阶段 900 秒没有进展，已保留检查点')
            if progress and now - last_poll >= 5:
                progress(checklist_event={'kind': 'heartbeat', 'elapsed_seconds': round(now - last_event, 1)})
                last_poll = now
            time.sleep(.1)
        lease.check()
        if reader: reader.join(timeout=3)
        flush_events()
        if not errors.empty(): raise RuntimeError('Checklist 日志写入失败：' + str(errors.get()))
        result = _read(result_path) if io_path(result_path).is_file() else {}
        if process.returncode or result.get('ok') is not True:
            raise RuntimeError(str(sanitize(result.get('error') or 'Checklist 子进程退出但未返回有效结果'))[:2000])
        return result
    finally:
        if process is not None:
            stopped = stop_owned_process(process)
            if reader: reader.join(timeout=3)
            if process.stdin: process.stdin.close()
            if process.stdout and (reader is None or not reader.is_alive()):
                process.stdout.close()
            # During cancellation the external guard may already be revoked.
            try:
                with lease.records._connection(write=True) as connection:
                    connection.execute('BEGIN IMMEDIATE')
                    row = lease.records._read(connection, SLOTS, lease.slot) or {}
                    if row.get('token') == lease.token:
                        lease.records._save(connection, SLOTS, lease.slot,
                            {**row, 'child': {**metadata, 'launch_gate': True,
                                            'launch_released': launch_released, 'exited': stopped,
                                            'cleanup_failed': not stopped}}, row.get('storage_revision', 0) + 1)
                    connection.commit()
            finally:
                if reader is None or not reader.is_alive(): sink.close()
                if not stopped:
                    raise LocalChecklistCleanupUnconfirmed('Checklist 子进程退出未确认，禁止重复派发')
        if reader is None or not reader.is_alive(): sink.close()


def _reference(entry, task, work, lease):
    if entry.get('source_error'):
        raise ValueError(str(entry['source_error']))
    source_dir = Path(entry['source_directory'])
    for path in (source_dir, *source_dir.parents):
        if is_link_or_junction(path): raise ValueError('source_link: 原件目录包含链接')
    source_dir = resolve_path(source_dir)
    source = source_dir / '_trajectory_for_evaluate.json'
    metadata = _read(source)
    if not isinstance(metadata, dict) or not isinstance(metadata.get('actions_flat'), list):
        raise ValueError('trajectory_invalid: 原件缺少动作列表')
    if ''.join(str(metadata.get('task', '')).split()) != ''.join(task['task'].split()):
        raise ValueError('manifest task differs from source trajectory')
    reference_path = work / 'input.json'
    if io_path(reference_path).is_file():
        descriptor = _read(reference_path)
        if descriptor.get('trajectory_path') != str(source):
            raise ValueError('source_changed: 原件目录身份变化')
        verify_reference(descriptor)
    else:
        files = []
        for parent, folders, names in os.walk(io_path(source_dir), followlinks=False):
            lease.check()
            for name in [*folders, *names]:
                if is_link_or_junction(Path(parent) / name): raise ValueError('source_link: 原件包含链接')
            folders[:] = sorted(folder for folder in folders if folder not in SKIP)
            for name in sorted(names):
                if name in IGNORED or name.endswith('.pyc'): continue
                path = logical_path(Path(parent) / name)
                files.append({'path': path.relative_to(source_dir).as_posix(),
                              'size': io_path(path).stat().st_size, 'sha256': _sha(path, lease.check)})
        descriptor = {'version': 1, 'input_mode': 'reference', 'evidence_root': str(source_dir),
                      'trajectory_path': str(source), 'source_files': files,
                      'trajectory_sha256': next(x['sha256'] for x in files if x['path'] == source.name),
                      'collection_evidence_issues': []}
        verify_reference(descriptor); lease.check(); _atomic(reference_path, descriptor, immutable=True)
    return {'trajectory_path': str(source), 'trajectory_sha256': descriptor['trajectory_sha256'],
            'input_reference_path': str(reference_path), 'input_reference_sha256': _sha(reference_path)}


def _definition_valid(definition, task, profile):
    from .ChecklistEvaluator.frozen_checklist import validate
    validate(definition)
    if (not isinstance(definition, dict) or definition.get('definition_digest') != digest(
            {k: v for k, v in definition.items() if k != 'definition_digest'})):
        raise ValueError('frozen Checklist definition digest mismatch')
    for key in ('task_id', 'case_id', 'task', 'key_elements'):
        if definition.get(key) != task[key]: raise ValueError('frozen Checklist task identity differs')
    for key in ('profile_digest', 'runtime_digest'):
        if definition.get(key) != profile[key]: raise ValueError('frozen Checklist profile identity differs')
    items = definition.get('items')
    if (definition.get('source') not in {'gold', 'llm'} or not isinstance(items, list) or not items or
            any(not isinstance(x, dict) or not isinstance(x.get('id'), str) or not x['id'] for x in items) or
            len({x['id'] for x in items}) != len(items)):
        raise ValueError('invalid frozen Checklist items')


def _result(report, definition):
    if (''.join(str(report.get('task') or '').split()) != ''.join(definition['task'].split()) or
            report.get('key_elements_sha256') != hashlib.sha256(definition['key_elements'].encode('utf-8')).hexdigest()):
        raise ValueError('report task or key elements differs from frozen definition')
    summary = report['checklist_summary']; analysis = report.get('failure_analysis') or {}
    items = summary['items']
    if ([(item['id'], item.get('description')) for item in items] !=
            [(item['id'], item.get('description')) for item in definition['items']] or
            report.get('checklist_source') != definition['source'] or
            report.get('checklist_metadata', {}).get('definition_digest') != definition['definition_digest']):
        raise ValueError('report Checklist differs from frozen definition')
    evidence = {x.get('checklist_id'): x.get('evidence') for key in ('passed_items', 'failed_items')
                for x in analysis.get(key, []) if isinstance(x, dict)}
    result = {**summary, 'status': 'complete', 'source': definition['source'],
        'definition_digest': definition['definition_digest'], 'definition_ids': [x['id'] for x in definition['items']],
        'verdict': report['verdict'], 'items': [{**x, 'checked': True, 'result': x['status'],
                                             'evidence': evidence.get(x['id'])} for x in items]}
    from .quality_fusion import fuse
    contract = fuse({'global_score': 3}, result, definition_digest=definition['definition_digest'])
    if not contract['valid']:
        raise ValueError('Checklist fusion contract rejected: ' + ','.join(contract['invalid_reasons']))
    return result


def evaluate_task(root, job, task_manifest, input_digest, *, progress=None, check=None):
    options = job['checklist_options']; key = task_key(job, task_manifest, input_digest)
    if options.get('execution_backend') != 'platform' or options.get('output_policy') != 'quality_only_v1':
        raise ValueError('Checklist 本地执行配置未冻结')
    records = RecordStore(root)
    directory = Path(root) / 'system' / 'qc' / key[:24]
    profile = verify_snapshot(options['profile_snapshot'], RUNTIME)
    if profile['runtime_digest'] != options['runtime_digest']:
        raise ValueError('frozen Checklist runtime differs')
    expected = [x['trajectory_id'] for x in task_manifest['trajectories']]
    if not expected or len(set(expected)) != len(expected): raise ValueError('invalid Checklist trajectory identities')
    if check: check()
    def notify(**changes):
        if progress: progress(current_task=task_manifest['task_id'], **changes)
    results = {}
    with _Lease(root, key, job, check, notify) as lease:
        task = {k: task_manifest[k] for k in ('task_id', 'case_id', 'task', 'key_elements')}
        _atomic(directory / 'task.json', {'task': _stable_manifest(task_manifest), 'input_digest': input_digest,
                'options': options}, immutable=True)
        saved = records.get(TASKS, key) or {}
        if all(entry.get('source_error') for entry in task_manifest['trajectories']):
            for entry in task_manifest['trajectories']:
                ident = entry['trajectory_id']; result = _failure(ValueError(str(entry['source_error'])))
                results[ident] = result
                lease.save(ENTRIES, key + ':' + digest(ident)[:24],
                           {'task_key': key, 'trajectory_id': ident, 'result': result})
            lease.save(TASKS, key, {'queued': False, 'complete': False, 'finished_at': utc_now()})
            notify(stage='checklist_evaluating', completed_checklist_trajectories=len(expected),
                   total_checklist_trajectories=len(expected))
            return results
        definition_path = directory / 'definition.json'
        common = {'evaluator_config': options['evaluator_config'], 'profile_snapshot': options['profile_snapshot'],
                  'task': task, 'definition_path': str(definition_path), 'output_policy': options['output_policy']}
        definition = saved.get('definition')
        try:
            notify(stage='checklist_definition', total_checklist_trajectories=len(expected), completed_checklist_trajectories=0)
            if definition:
                _definition_valid(definition, task, profile)
                if _read(definition_path) != definition: raise ValueError('frozen Checklist definition file changed')
            else:
                generated = _child(root, options, {**common, 'stage': 'definition'}, directory / 'd', lease, notify)
                definition = generated['definition']; _definition_valid(definition, task, profile)
                lease.save(TASKS, key, {'definition': definition, 'input_digest': input_digest,
                                      'task_id': task['task_id'], 'expected_trajectories': expected})
            for index, entry in enumerate(task_manifest['trajectories']):
                lease.check(); ident = entry['trajectory_id']; entry_key = key + ':' + digest(ident)[:24]
                work = directory / digest(ident)[:16]
                notify(stage='checklist_preparing', current_trajectory=ident,
                       completed_checklist_trajectories=index, total_checklist_trajectories=len(expected))
                try:
                    reference = _reference(entry, task, work, lease)
                    prior = records.get(ENTRIES, entry_key) or {}
                    attempt = int(prior.get('attempt') or 1)
                    output = work / ('e' + str(attempt))
                    session = 'q_' + digest([key, ident, attempt])[:24]
                    report_path = prior.get('report_path')
                    if not report_path and io_path(output).is_dir():
                        candidates = list(io_path(output).glob('_report_*.json'))
                        if candidates:
                            report_path = str(logical_path(max(candidates, key=lambda p: (p.stat().st_mtime_ns, p.name))))
                    validation = dict(config_digest=config_digest(options['evaluator_config']), session_id=session,
                        checklist_policy='auto', **{name: profile[name] for name in ('profile_id', 'profile_digest', 'runtime_digest')})
                    if report_path and not resolve_path(report_path).is_relative_to(resolve_path(output)):
                        valid = {'valid': False, 'reason': 'cached report escaped its attempt output'}
                    else:
                        valid = validate_report(report_path, reference['trajectory_path'], **validation) if report_path else {'valid': False}
                    if valid['valid']:
                        try: _result(_read(report_path), definition)
                        except (ValueError, TypeError, KeyError) as exc:
                            valid = {'valid': False, 'reason': str(exc)}
                    if report_path and not valid['valid'] and valid.get('status') != 'incomplete':
                        # Retain damaged/incompatible evidence; repair this item in the same retry.
                        attempt += 1; output = work / ('e' + str(attempt))
                        session = 'q_' + digest([key, ident, attempt])[:24]
                        validation['session_id'] = session
                        history = [*prior.get('report_history', []),
                                   {'path': report_path, 'reason': valid['reason'], 'attempt': attempt - 1}]
                        lease.save(ENTRIES, entry_key, {'report_history': history, 'report_path': None})
                    lease.save(ENTRIES, entry_key, {'attempt': attempt, 'session_id': session,
                                                  'output_dir': str(output)})
                    if not valid['valid']:
                        notify(stage='checklist_evaluating', current_trajectory=ident)
                        value = _child(root, options, {**common, 'stage': 'evaluate', 'case': reference,
                            'definition_digest': definition['definition_digest'], 'output_dir': str(output),
                            'session_id': session}, work / 'w', lease, notify)
                        report_path = value.get('evaluation', {}).get('report_path')
                        if not report_path or value.get('exit_code') != 0:
                            raise RuntimeError(value.get('evaluation', {}).get('reason') or 'Checklist 未返回完整结果')
                    report_file = resolve_path(report_path)
                    if not report_file.is_relative_to(resolve_path(output)): raise ValueError('Checklist report escaped task output')
                    validated = validate_report(report_file, reference['trajectory_path'], **validation)
                    if not validated['valid']: raise ValueError('Checklist report rejected: ' + validated['reason'])
                    verify_reference(_read(reference['input_reference_path']))
                    result = _result(_read(report_file), definition)
                    lease.save(ENTRIES, entry_key, {'task_key': key, 'trajectory_id': ident,
                        'report_path': str(report_file), 'reference': reference, 'result': result})
                except LocalChecklistCleanupUnconfirmed:
                    raise
                except Exception as exc:
                    lease.check()  # Cancellation/revocation is never converted into a trajectory failure.
                    result = _failure(exc, definition)
                    lease.save(ENTRIES, entry_key, {'task_key': key, 'trajectory_id': ident, 'result': result})
                results[ident] = result
                notify(stage='checklist_evaluating', current_trajectory=ident,
                       completed_checklist_trajectories=index + 1, total_checklist_trajectories=len(expected))
        except LocalChecklistCleanupUnconfirmed as exc:
            # Keep completed entries and make the remainder explicit without
            # launching any further child or replacing unresolved ownership.
            for ident in expected:
                if ident in results: continue
                lease.save(ENTRIES, key + ':' + digest(ident)[:24],
                    {'task_key': key, 'trajectory_id': ident, 'result': _failure(exc, definition)})
            raise
        except Exception as exc:
            lease.check()
            for ident in expected:
                if ident in results: continue
                result = _failure(exc, definition)
                lease.save(ENTRIES, key + ':' + digest(ident)[:24], {'task_key': key, 'trajectory_id': ident, 'result': result})
                results[ident] = result
        lease.save(TASKS, key, {'queued': False, 'complete': all(x['status'] == 'complete' for x in results.values()),
                               'finished_at': utc_now()})
    return results


def _failure(exc, definition=None):
    result = {'status': 'error', 'error': str(sanitize(f'{type(exc).__name__}: {exc}'))[:2000],
              'failure': failure_from_exception(exc)}
    if definition:
        result.update(definition_digest=definition['definition_digest'], source=definition['source'],
                      definition_ids=[x['id'] for x in definition['items']],
                      total=len(definition['items']), passed=0, failed=0, unchecked=len(definition['items']))
    return result
