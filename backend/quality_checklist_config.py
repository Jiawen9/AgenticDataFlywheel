"""Local Checklist settings and immutable rule/model execution options.

No capability, model or filesystem requests are sent to the phone collector.
Only credential environment-variable names are stored in records.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import uuid

from .data_store import RecordStore
from .file_io import io_path, resolve_path
from .phone_factory import PhoneFactoryError
from .quality_fusion import SCORING_VERSION

BACKEND_DIR = Path(__file__).resolve().parent
RUNTIME_ROOT = BACKEND_DIR / 'ChecklistEvaluator'


def _environment():
    values = {}
    path = BACKEND_DIR / '.env'
    if io_path(path).is_file():
        for line in io_path(path).read_text(encoding='utf-8-sig').splitlines():
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                key, value = line.split('=', 1)
                values[key.strip()] = value.strip().strip('\"').strip("'")
    return {**values, **os.environ}


def imported_profile_root(root=None):
    from .data_store import DATA_ROOT
    configured = _environment().get('ADF_CHECKLIST_PROFILES_DIR')
    return resolve_path(Path(configured) if configured else
                        Path(root or DATA_ROOT) / 'resources' / 'checklist_profiles')


def profile_root(root=None):
    configured = _environment().get('ADF_CHECKLIST_PROFILES_DIR')
    imported = imported_profile_root(root)
    # A failed first import may leave an empty root or a private staging folder.
    # Only a published directory selects that root; a damaged published profile
    # must remain visible as an error rather than silently selecting other rules.
    published = (io_path(imported).is_dir() and any(
        p.is_dir() and not p.name.startswith('.') for p in io_path(imported).iterdir()))
    return resolve_path(imported if configured else
                        imported if published else RUNTIME_ROOT / 'profiles')


def _profiles(root=None):
    from .ChecklistEvaluator.evaluation_profiles import list_profiles
    return list_profiles(profile_root(root))


def _normalize(value, *, required=False):
    from .model_iteration import normalize_config
    value = deepcopy(value)
    if isinstance(value, dict):
        for role in ('text', 'vision'):
            item = value.get(role)
            if isinstance(item, dict) and ('api_key_env' not in item or
                    isinstance(item['api_key_env'], str) and not item['api_key_env'].strip()):
                item['api_key_env'] = 'PHONE_FACTORY_EVALUATOR_' + role.upper() + '_API_KEY'
    try:
        return normalize_config(value, required=required)
    except PhoneFactoryError as exc:
        raise PhoneFactoryError(str(exc).replace('采集服务器', '平台后端'), exc.status) from exc


def _saved(root, records):
    from .model_iteration import default_config
    saved = records.get('quality_settings', 'checklist') or {}
    if 'evaluator_config' not in saved:
        # Copy only once. Never let subsequent model-iteration edits change quality defaults.
        legacy = records.get('model_iter_settings', 'evaluator') or {}
        config = _normalize(legacy.get('config') or default_config())
        def initialize(current):
            if 'evaluator_config' not in current:
                current.update(evaluator_config=config, settings_version=2,
                               initialized_from='model_iter_settings')
        saved = records.update('quality_settings', 'checklist', initialize, default={})
    return saved


def settings(root, *, store=None):
    records = store.records if store is not None else RecordStore(root)
    saved = _saved(root, records)
    available = _profiles(root)
    profiles = available['profiles']
    selected = next((row for row in profiles if row['id'] == saved.get('evaluation_profile_id')
                     and row['digest'] == saved.get('evaluation_profile_digest')), None)
    if not saved.get('evaluation_profile_id') and len(profiles) == 1:
        selected = profiles[0]
    config = _normalize(saved['evaluator_config'])
    configured = all(config[role]['base_url'] and config[role]['model'] for role in ('text', 'vision'))
    reason = ''
    if not selected:
        reason = ('原冻结规则尚未导入平台或摘要不匹配，请导入相同版本；不会改用其它规则'
                  if saved.get('evaluation_profile_id') else '请导入并选择平台本地 Checklist 规则')
    return {'execution_backend': 'platform', 'settings_version': 2,
            'evaluation_profile_id': selected['id'] if selected else saved.get('evaluation_profile_id', ''),
            'evaluation_profile_digest': selected['digest'] if selected else saved.get('evaluation_profile_digest', ''),
            'evaluator_config': config, 'model_configured': bool(configured),
            'profiles': profiles, 'profile_errors': available.get('errors', []),
            'supported': True, 'reason': reason}


def save_settings(root, value, *, store=None):
    if (not isinstance(value, dict) or not value or
            set(value) - {'evaluation_profile_id', 'evaluation_profile_digest', 'evaluator_config'}):
        raise PhoneFactoryError('质检设置只接受本地评测模型和规则版本，不接受密钥值')
    records = store.records if store is not None else RecordStore(root)
    _saved(root, records)
    changes = {}
    if {'evaluation_profile_id', 'evaluation_profile_digest'} & set(value):
        if not {'evaluation_profile_id', 'evaluation_profile_digest'} <= set(value):
            raise PhoneFactoryError('规则版本与摘要必须同时提供')
        if not any(row['id'] == value['evaluation_profile_id'] and row['digest'] == value['evaluation_profile_digest']
                   for row in _profiles(root)['profiles']):
            raise PhoneFactoryError('本地规则版本不存在或已变化，请刷新后选择', 409)
        changes.update({name: value[name] for name in ('evaluation_profile_id', 'evaluation_profile_digest')})
    if 'evaluator_config' in value:
        changes['evaluator_config'] = _normalize(value['evaluator_config'], required=True)
    records.update('quality_settings', 'checklist', lambda current: current.update(changes), default={})
    return settings(root, store=store)


def child_environment(root, options):
    """Private child environment; actual credentials never enter job/options JSON."""
    from .ChecklistEvaluator.runtime_config import build_child_environment
    environment = _environment()
    environment.update(ADF_DATA_ROOT=str(root), PYTHONUNBUFFERED='1', PYTHONDONTWRITEBYTECODE='1',
                       PYTHONIOENCODING='utf-8')
    return build_child_environment(options['evaluator_config'], environment)


def _python():
    value = _environment().get('ADF_CHECKLIST_PYTHON') or sys.executable
    path = Path(value)
    if not path.is_absolute() or not io_path(path).is_file():
        raise PhoneFactoryError('ADF_CHECKLIST_PYTHON 必须指向平台后端可用 Python 的绝对路径')
    return str(resolve_path(path))


def preflight(root, options):
    """Bounded local import check; does not call a model or a phone."""
    try:
        environment = child_environment(root, options)
    except ValueError as exc:
        raise PhoneFactoryError('平台 Checklist 配置未就绪：' + str(exc)) from exc
    python = options['python_executable']
    try:
        result = subprocess.run([python, '-u', str(RUNTIME_ROOT / 'preflight.py')],
            cwd=str(RUNTIME_ROOT), env=environment, capture_output=True, text=True,
            encoding='utf-8', errors='replace', timeout=20, check=False,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PhoneFactoryError('平台 Checklist Python 环境检查失败：' + type(exc).__name__) from exc
    try:
        report = json.loads(result.stdout.strip())
    except (ValueError, TypeError):
        raise PhoneFactoryError('平台 Checklist 环境检查未返回有效结果，请检查解释器及依赖')
    if result.returncode or report.get('ok') is not True:
        raise PhoneFactoryError('平台 Checklist 缺少或无法导入依赖：' + ', '.join(report.get('missing', ['unknown'])))


def _freeze_profile(root, options):
    from .ChecklistEvaluator.evaluation_profiles import freeze_profile, verify_snapshot
    base = resolve_path(Path(root) / 'system' / 'qc_profiles')
    io_path(base).mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256(json.dumps({k: options[k] for k in
        ('evaluation_profile_id', 'evaluation_profile_digest', 'runtime_digest')}, sort_keys=True).encode()).hexdigest()[:24]
    target = base / key
    saved = target / 'profile_snapshot.json'
    if io_path(saved).is_file():
        return verify_snapshot(saved, RUNTIME_ROOT)
    temporary = base / ('.p_' + uuid.uuid4().hex[:12])
    try:
        value = freeze_profile(profile_root(root), options['evaluation_profile_id'], temporary,
            runtime_root=RUNTIME_ROOT, expected_digest=options['evaluation_profile_digest'])
        # Relocate only the small descriptor. Rule contents and their digest are unchanged.
        for field in ('snapshot_root', 'gold_path', 'rules_dir'):
            relative = Path(value[field]).relative_to(temporary)
            value[field] = str(target / relative)
        io_path(temporary / 'profile_snapshot.json').write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
        try:
            os.rename(io_path(temporary), io_path(target))
        except OSError:
            if not io_path(saved).is_file():
                raise
        return verify_snapshot(saved, RUNTIME_ROOT)
    finally:
        if io_path(temporary).exists():
            if resolve_path(temporary).parent != base:
                raise RuntimeError('profile staging path escaped owned directory')
            shutil.rmtree(io_path(temporary))


def _local_options(root, frozen):
    from .ChecklistEvaluator.evaluation_profiles import runtime_digest
    options = {'version': 2, 'execution_backend': 'platform', 'scoring_version': SCORING_VERSION,
               'evaluator_config': _normalize(frozen['evaluator_config'], required=True),
               'evaluation_profile_id': frozen['evaluation_profile_id'],
               'evaluation_profile_digest': frozen['evaluation_profile_digest'],
               'runtime_digest': runtime_digest(RUNTIME_ROOT), 'output_policy': 'quality_only_v1',
               'python_executable': _python()}
    if not any(row['id'] == options['evaluation_profile_id'] and row['digest'] == options['evaluation_profile_digest']
               for row in _profiles(root)['profiles']):
        raise PhoneFactoryError('原冻结 Checklist 规则尚未导入平台或摘要不符；请导入相同规则后重试', 409)
    preflight(root, options)
    options['profile_snapshot'] = _freeze_profile(root, options)
    return options


def freeze_options(root, *, store=None):
    current = settings(root, store=store)
    if current['reason'] or not current['evaluation_profile_id']:
        raise PhoneFactoryError(current['reason'] or '请选择本地 Checklist 规则', 409)
    return _local_options(root, current)


def migrate_options(root, previous):
    """A successor inherits old defaults; never rewrite a historical frozen job."""
    if previous is None:
        return None
    if previous.get('execution_backend') == 'platform':
        from .ChecklistEvaluator.evaluation_profiles import verify_snapshot
        verify_snapshot(previous['profile_snapshot'], RUNTIME_ROOT)
        preflight(root, previous)
        return deepcopy(previous)
    return _local_options(root, previous)
