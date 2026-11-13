"""Small, immutable Checklist source metadata, independent of display IDs."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from .file_io import io_path


def source_metadata(directory, identity=None):
    identity = identity or {}
    path = io_path(Path(directory) / '_trajectory_for_evaluate.json')
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        raw = None
    original = json.loads(raw.decode('utf-8-sig')) if raw is not None else {}
    if not isinstance(original, dict):
        raise ValueError('轨迹元数据不是 JSON 对象')
    case_id = (identity.get('collection_case_id') or identity.get('case_id') or
               original.get('collection_case_id') or original.get('original_case_id') or
               original.get('case_id') or original.get('task_id') or identity.get('task_id') or '')
    return {'checklist_input_version': 1, 'collection_case_id': str(case_id),
            'key_elements': original.get('key_elements', ''),
            'source_metadata_sha256': hashlib.sha256(raw).hexdigest() if raw is not None else None}


def annotation_identities(payload, task_id):
    """Read already frozen rows, not a current filesystem or display-name guess."""
    result = {}
    for rows in (payload or {}).get('sheets', {}).values():
        for row in rows:
            if str(row.get('task_id') or '') != str(task_id):
                continue
            trajectory = str(row.get('trajectory_id') or '')
            case = str(row.get('collection_case_id') or '')
            if not trajectory or not case:
                continue
            if trajectory in result and result[trajectory] != case:
                raise ValueError('冻结标注中同一轨迹关联多个原始用例编号')
            result[trajectory] = case
    return result
