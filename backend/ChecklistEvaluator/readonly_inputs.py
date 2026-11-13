"""Read-only evaluation input descriptors; never materialize source evidence."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
try:
    from .file_access import io_path, logical_path
except ImportError:
    from file_access import io_path, logical_path

REFERENCE_ENV = 'PHONE_FACTORY_EVALUATION_REFERENCE'
OUTPUT_ENV = 'PHONE_FACTORY_EVALUATION_OUTPUT'
SKIP = {'.git', '.venv', '__pycache__', '_prefetch_staging', 'evaluation_work',
        'evaluation_inputs', 'evaluation_reports', 'evaluation_artifacts', 'evaluation_profile'}
IGNORED = {'config.py', '.env', 'pid.txt', '_worker_pid.json', '_prepared_input.json'}


def checksum(path):
    value = hashlib.sha256()
    with io_path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''): value.update(chunk)
    return value.hexdigest()


def checked_path(root, relative):
    value = PurePosixPath(str(relative).replace('\\', '/'))
    if value.is_absolute() or not value.parts or any(part in {'.', '..'} or ':' in part for part in value.parts):
        raise ValueError('source_changed: invalid input reference path')
    path = root.joinpath(*value.parts)
    for part in (path, *path.parents):
        if part.is_symlink() or (hasattr(part, 'is_junction') and part.is_junction()):
            raise ValueError('source_changed: reference links are forbidden')
        if part.exists() and getattr(part.lstat(), 'st_file_attributes', 0) & 1024:
            raise ValueError('source_changed: reference reparse points are forbidden')
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError('source_changed: reference escapes source directory')
    return path


def load_reference(path, expected='', trajectory=None):
    if expected and checksum(path) != expected:
        raise ValueError('source_changed: reference descriptor digest differs')
    data = json.loads(io_path(path).read_text(encoding='utf-8-sig'))
    if data.get('version') != 1 or data.get('input_mode') != 'reference':
        raise ValueError('invalid read-only input descriptor')
    root = io_path(data['evidence_root'])
    if not root.is_absolute(): raise ValueError('invalid evidence root')
    target = checked_path(root, Path(data['trajectory_path']).name)
    if target.resolve() != io_path(data['trajectory_path']).resolve() or (trajectory and target.resolve() != io_path(trajectory).resolve()):
        raise ValueError('source_changed: reference trajectory identity differs')
    return data


def verify_reference(data):
    root = io_path(data['evidence_root'])
    if not root.is_dir(): raise ValueError('source_missing: evaluation source directory no longer exists')
    expected = {entry['path']: entry for entry in data['source_files']}
    if len(expected) != len(data['source_files']): raise ValueError('source_changed: duplicate input paths')
    actual = set()
    try:
        for parent, folders, names in os.walk(root, followlinks=False):
            for folder in folders: checked_path(root, (Path(parent) / folder).relative_to(root).as_posix())
            folders[:] = sorted(folder for folder in folders if folder not in SKIP)
            for name in names:
                if name in IGNORED or name.endswith('.pyc'): continue
                relative = (Path(parent) / name).relative_to(root).as_posix()
                path = checked_path(root, relative)
                if not path.is_file(): continue
                actual.add(relative)
                item = expected.get(relative)
                if item is None or path.stat().st_size != item['size'] or checksum(path) != item['sha256']:
                    raise ValueError('source_changed: evaluation source file changed: ' + relative)
        if actual != set(expected): raise ValueError('source_missing: evaluation source file is missing')
    except FileNotFoundError as exc:
        raise ValueError('source_missing: evaluation source disappeared') from exc


def adapt_reference(trajectory, trajectory_path, descriptor):
    """Existing optional-evidence adaptation, in memory; the whole root stays readable."""
    from agent.evaluator.resource_paths import ARTIFACT_KEYS, bind_snapshot_artifacts, resolve_artifact
    root = io_path(trajectory_path).resolve().parent
    resolver = {'_case_dir': str(root), '_strict_artifacts': True}
    image_keys = {'screenshot_before', 'screenshot_after', 'action_overlay_path', 'screenshot_path', 'image_path', 'screenshot', 'final_orch_screenshot'}
    def readable(path, key):
        try:
            if key in image_keys:
                from PIL import Image
                with Image.open(path) as picture: picture.verify()
            elif key.startswith(('ui_tree', 'ui_dump')) or key == 'xml_path':
                import xml.etree.ElementTree as ET
                xml_path = Path(path)
                if xml_path.stat().st_size > 8 * 1024 * 1024: return False
                text = xml_path.read_text(encoding='utf-8-sig').lstrip()
                if '<!DOCTYPE' in text.upper() or '<!ENTITY' in text.upper(): return False
                tree = ET.fromstring(text)
                return tree.tag == 'hierarchy' and tree.find('.//node') is not None
            return True
        except (OSError, ValueError, SyntaxError): return False
    def visit(value):
        if isinstance(value, list): return [visit(item) for item in value]
        if not isinstance(value, dict): return value
        result = {}
        for key, item in value.items():
            if key in ARTIFACT_KEYS and isinstance(item, str) and item.strip() and not item.lstrip().startswith('<'):
                try:
                    path = resolve_artifact(resolver, item, declared=True)
                    result[key] = path if readable(path, key) else ''
                except ValueError:
                    result[key] = ''
            else: result[key] = visit(item)
        return result
    adapted = visit(trajectory)
    for action in adapted.get('actions_flat', []):
        step = action.get('global_step')
        if not action.get('screenshot_before') and type(step) is int and step > 0:
            path = root / f'step{step:03d}_vla_input.jpg'
            if path.is_file() and readable(path, 'screenshot_before'): action['screenshot_before'] = str(path)
    adapted['collection_evidence_issues'] = descriptor.get('collection_evidence_issues', [])
    return bind_snapshot_artifacts(adapted, trajectory_path)
