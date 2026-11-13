"""Task-level immutable checklist definitions for platform quality jobs.

Only this optional transport adapter is new. Evidence checks, audit and the
native final-verdict calculation remain owned by the evaluator.
"""
from __future__ import annotations
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
try:
    from .file_access import io_path, logical_path
except ImportError:
    from file_access import io_path, logical_path

ENV = 'PHONE_FACTORY_QUALITY_CHECKLIST_DEFINITION'
DIGEST_ENV = 'PHONE_FACTORY_QUALITY_CHECKLIST_DIGEST'


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':')).encode('utf-8')).hexdigest()


def validate(value):
    if not isinstance(value, dict) or value.get('schema_version') != 1:
        raise ValueError('invalid frozen checklist definition')
    body = {key: val for key, val in value.items() if key != 'definition_digest'}
    if digest(body) != value.get('definition_digest'):
        raise ValueError('frozen checklist definition digest differs')
    items = value.get('items')
    if (value.get('source') not in {'gold', 'llm'} or not isinstance(items, list) or not items
            or any(not isinstance(x, dict) or not isinstance(x.get('id'), str)
                   or not x['id'] or not x.get('description') for x in items)
            or len({x['id'] for x in items}) != len(items)):
        raise ValueError('invalid frozen checklist items/source')
    return value


def load(path, expected=''):
    value = validate(json.loads(io_path(path).read_text(encoding='utf-8-sig')))
    if expected and value['definition_digest'] != expected:
        raise ValueError('frozen checklist does not match queued task')
    return value


def apply(ctx):
    path = os.environ.get(ENV)
    if not path:
        return None
    value = load(path, os.environ.get(DIGEST_ENV, ''))
    from agent.evaluator.gold_checklist import normalize_task_text
    if normalize_task_text(ctx.trajectory.get('task', '')) != normalize_task_text(value['task']):
        raise ValueError('frozen checklist task differs from trajectory')
    if getattr(ctx, 'key_elements', '') != value.get('key_elements', ''):
        raise ValueError('frozen checklist key elements differ')
    metadata = deepcopy(value.get('metadata') or {})
    metadata.update(definition_digest=value['definition_digest'], quality_task_id=value['task_id'])
    # Repeated tool calls must not erase evidence already gathered in this run.
    if ctx.checklist and ctx.checklist_metadata.get('definition_digest') == value['definition_digest']:
        return {'checklist': ctx.checklist, 'count': len(ctx.checklist),
                'source': ctx.checklist_source, 'metadata': ctx.checklist_metadata}
    items = deepcopy(value['items'])
    for item in items:
        item.update(checked=False, result=None, evidence=None)
    ctx.checklist, ctx.checklist_source, ctx.checklist_metadata = items, value['source'], metadata
    return {'checklist': items, 'count': len(items), 'source': value['source'], 'metadata': metadata}


def validate_resume(payload):
    path = os.environ.get(ENV)
    if not path:
        return
    value = load(path, os.environ.get(DIGEST_ENV, ''))
    if payload.get('checklist_metadata', {}).get('definition_digest') != value['definition_digest']:
        raise ValueError('resume checkpoint uses a different frozen checklist')
