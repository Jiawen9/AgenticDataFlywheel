"""Isolated task-definition / trajectory evaluator for quality jobs."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
from types import SimpleNamespace
try:
    from .file_access import io_path, logical_path
except ImportError:
    from file_access import io_path, logical_path
from runtime_events import emit

from runtime_config import build_child_environment
from evaluation_profiles import verify_snapshot, IDENTITY_ENV
from frozen_checklist import digest, load, ENV, DIGEST_ENV
from run_evaluation import run_worker, RedactedStream, redact


def atomic(path, value):
    path = io_path(path)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(temporary, path)


def execute(spec):
    if spec.get('output_policy') != 'quality_only_v1':
        raise ValueError('unsupported quality output policy')
    os.environ['ADF_CHECKLIST_OUTPUT_POLICY'] = spec['output_policy']
    emit('stage', stage=spec['stage'], output_policy=spec['output_policy'])
    profile = verify_snapshot(spec['profile_snapshot'])
    os.environ[IDENTITY_ENV] = json.dumps(profile, ensure_ascii=False)
    os.environ['GUI_AGENT_GOLD_CHECKLIST_PATH'] = profile['gold_path']
    os.environ['GUI_AGENT_EVALUATOR_RULES_DIR'] = profile['rules_dir']
    os.environ['GUI_AGENT_EVALUATOR_STRICT_RULES'] = 'true'
    if spec['stage'] == 'definition':
        output = io_path(spec['definition_path'])
        if output.is_file():
            definition = load(output)
            if (any(definition.get(key) != spec['task'][key] for key in ('task_id', 'case_id', 'task', 'key_elements'))
                    or any(definition.get(key) != profile[key] for key in ('profile_digest', 'runtime_digest'))):
                raise ValueError('cached checklist definition belongs to different frozen input')
            return {'definition': definition}
        from agent.evaluator.tools.registry import EvaluatorContext
        from agent.evaluator.tools.generate_checklist import generate_verification_checklist
        from agent.models.llm_client import LLMClient
        task = spec['task']
        ctx = EvaluatorContext(trajectory={'task': task['task'], 'task_id': task['case_id'],
                                          'case_id': task['case_id']}, session_id='quality-definition',
                               _text_llm=LLMClient(), key_elements=task['key_elements'])
        result = generate_verification_checklist(ctx, {'task': task['task']})
        if result.get('error') or not result.get('checklist'):
            raise ValueError('checklist_generation_failed: ' + str(result.get('error', 'empty checklist')))
        definition = {'schema_version': 1, 'task_id': task['task_id'], 'case_id': task['case_id'],
                      'task': task['task'], 'key_elements': task['key_elements'],
                      'source': result['source'], 'metadata': result.get('metadata', {}),
                      'items': result['checklist'], 'profile_digest': profile['profile_digest'],
                      'runtime_digest': profile['runtime_digest']}
        definition['definition_digest'] = digest(definition)
        atomic(output, definition)
        return {'definition': definition}
    definition = load(spec['definition_path'], spec['definition_digest'])
    os.environ[ENV] = spec['definition_path']
    os.environ[DIGEST_ENV] = spec['definition_digest']
    case = spec['case']
    args = SimpleNamespace(output_dir=spec['output_dir'], trajectory_path=case['trajectory_path'],
        case_id=spec['task']['case_id'], session_id=spec['session_id'], key_elements=spec['task']['key_elements'],
        resume_auto=True, checklist_source='auto', profile_snapshot=spec['profile_snapshot'],
        expected_trajectory_sha256=case['trajectory_sha256'], input_reference=case['input_reference_path'],
        input_reference_sha256=case['input_reference_sha256'])
    result, code = run_worker(args)
    report = {}
    if result.get('report_path'):
        path = io_path(result['report_path']).resolve()
        if not path.is_relative_to(io_path(spec['output_dir']).resolve()):
            raise ValueError('evaluator report escaped output directory')
        report = json.loads(path.read_text(encoding='utf-8-sig'))
        items = report.get('checklist_summary', {}).get('items', [])
        if ([x.get('id') for x in items] != [x['id'] for x in definition['items']]
                or report.get('checklist_metadata', {}).get('definition_digest') != definition['definition_digest']):
            raise ValueError('report checklist differs from frozen definition')
    return {'evaluation': result, 'exit_code': code, 'report': report}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--spec', required=True)
    parser.add_argument('--wait-for-go', action='store_true')
    args = parser.parse_args()
    if args.wait_for_go:
        import sys
        if sys.stdin.readline().strip() != 'GO':
            return 75
        # The supervising platform keeps this pipe open. Its abrupt death must
        # stop just this isolated model worker, including native vision threads.
        import threading
        parent_fd = sys.stdin.fileno()
        if os.name == 'nt':
            import ctypes
            import msvcrt
            from ctypes import wintypes
            parent_handle = msvcrt.get_osfhandle(parent_fd)
            kernel = ctypes.WinDLL('kernel32', use_last_error=True)
            kernel.PeekNamedPipe.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD,
                ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD)]
            kernel.PeekNamedPipe.restype = wintypes.BOOL
        def watch_parent():
            import time
            try:
                while True:
                    if os.name == 'nt':
                        # A blocking CRT read can hold stdin's lock while NumPy
                        # loads its native module. Peek the pipe without that lock.
                        available = wintypes.DWORD()
                        if not kernel.PeekNamedPipe(parent_handle, None, 0, None, ctypes.byref(available), None):
                            break
                        time.sleep(.2)
                    else:
                        import select
                        if select.select([parent_fd], [], [], .2)[0] and not os.read(parent_fd, 4096):
                            break
            finally:
                os._exit(75)
        threading.Thread(target=watch_parent, name='checklist-parent-watch', daemon=True).start()
    spec_path = io_path(args.spec).resolve()
    spec = json.loads(spec_path.read_text(encoding='utf-8'))
    result_path = spec_path.with_name('result.json')
    try:
        environment = build_child_environment(spec['evaluator_config'], os.environ)
        environment.pop(ENV, None); environment.pop(DIGEST_ENV, None)
        os.environ.clear(); os.environ.update(environment)
        import sys
        sys.stdout = RedactedStream(sys.stdout, environment)
        sys.stderr = RedactedStream(sys.stderr, environment)
        result = execute(spec)
        atomic(result_path, {'ok': True, **result})
        return 0
    except Exception as exc:
        atomic(result_path, {'ok': False, 'error': redact(type(exc).__name__ + ': ' + str(exc), dict(os.environ))})
        return 1


if __name__ == '__main__':
    code = main()
    import sys
    if '--wait-for-go' in sys.argv:
        # The managed stdin watchdog is intentionally blocked in a pipe read.
        # Windows CRT stream finalization can wait for that read. All result
        # files are already atomically committed and stdout/stderr are flushed;
        # exit the isolated worker directly instead of finalizing stdin.
        sys.stdout.flush(); sys.stderr.flush()
        os._exit(code)
    raise SystemExit(code)
