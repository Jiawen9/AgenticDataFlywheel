"""Import exact rule contents locally; no collectors, credentials or services are changed."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import shutil
from uuid import uuid4

from backend.file_io import io_path, resolve_path
from backend.ChecklistEvaluator.evaluation_profiles import _load_profile, _digest, list_profiles


def import_profile(source_root, destination_root, profile_id, expected_digest):
    source_root, destination_root = resolve_path(source_root), resolve_path(destination_root)
    source, _, files = _load_profile(source_root, profile_id)
    if _digest(files) != expected_digest:
        raise ValueError('源规则摘要与原质检冻结摘要不一致，未导入')
    io_path(destination_root).mkdir(parents=True, exist_ok=True)
    target = destination_root / profile_id
    if io_path(target).exists():
        existing = next((row for row in list_profiles(destination_root)['profiles'] if row['id'] == profile_id), None)
        if existing and existing['digest'] == expected_digest:
            return {'ok': True, 'reused': True, 'profile_id': profile_id, 'digest': expected_digest,
                    'destination': str(target)}
        raise ValueError('目标已有同编号不同内容的规则；请选择新规则目录，不覆盖现场规则')
    temporary = destination_root / ('.import_' + uuid4().hex[:12])
    try:
        staged = temporary / profile_id
        for relative in files:
            copied = io_path(staged / relative)
            copied.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(io_path(source / relative), copied)
        # Validate the exact copied bytes, then publish the directory atomically.
        _, _, actual = _load_profile(temporary, profile_id)
        if _digest(actual) != expected_digest:
            raise ValueError('导入后的规则摘要不一致')
        os.rename(io_path(staged), io_path(target))
        return {'ok': True, 'reused': False, 'profile_id': profile_id, 'digest': expected_digest,
                'destination': str(target)}
    finally:
        if io_path(temporary).exists():
            if resolve_path(temporary).parent != destination_root:
                raise RuntimeError('import staging escaped destination')
            shutil.rmtree(io_path(temporary))


def main():
    from backend.quality_checklist_config import imported_profile_root
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-profiles', required=True)
    parser.add_argument('--profile-id', required=True)
    parser.add_argument('--expected-digest', required=True)
    parser.add_argument('--destination-profiles', default=str(imported_profile_root()))
    args = parser.parse_args()
    print(json.dumps(import_profile(args.source_profiles, args.destination_profiles,
                                   args.profile_id, args.expected_digest), ensure_ascii=False))


if __name__ == '__main__':
    main()
