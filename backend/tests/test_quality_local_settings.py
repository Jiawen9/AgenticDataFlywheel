from copy import deepcopy
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from backend.data_store import RecordStore
from backend import quality_checklist_config as config
from backend.phone_factory import PhoneFactoryError


def models(name='frozen'):
    return {role: {'base_url': 'http://127.0.0.1:19000/v1/', 'model': name + '-' + role,
                   'api_key_env': 'TEST_QC_' + role.upper()} for role in ('text', 'vision')}


class LocalQualitySettingsTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.records = RecordStore(self.root)
        self.store = SimpleNamespace(records=self.records,
            _remote_result=lambda *_: self.fail('quality settings must never contact the collector'))
        self.env = {**os.environ, 'TEST_QC_TEXT': 'test-private-text', 'TEST_QC_VISION': 'test-private-vision',
                    'ADF_CHECKLIST_PYTHON': sys.executable}

    def test_copy_existing_model_configuration_once_and_keep_independent(self):
        self.records.put('model_iter_settings', 'evaluator', {'config': models()})
        first = config.settings(self.root, store=self.store)
        self.records.put('model_iter_settings', 'evaluator', {'config': models('later')})
        second = config.settings(self.root, store=self.store)
        self.assertEqual(first['evaluator_config'], second['evaluator_config'])
        self.assertEqual(second['execution_backend'], 'platform')
        config.save_settings(self.root, {'evaluator_config': models('quality')}, store=self.store)
        self.assertEqual(config.settings(self.root)['evaluator_config'], models('quality'))
        self.assertEqual(self.records.get('model_iter_settings', 'evaluator')['config'], models('later'))

    def test_partial_save_keeps_selected_profile_and_rejects_secret_values(self):
        profile = config._profiles()['profiles'][0]
        selection = {'evaluation_profile_id': profile['id'], 'evaluation_profile_digest': profile['digest']}
        config.save_settings(self.root, selection)
        saved = config.save_settings(self.root, {'evaluator_config': models()})
        self.assertEqual(saved['evaluation_profile_id'], profile['id'])
        with self.assertRaises(PhoneFactoryError):
            config.save_settings(self.root, {'api_key': 'must-not-be-saved'})
        with self.assertRaises(PhoneFactoryError):
            config.save_settings(self.root, {'evaluation_profile_id': profile['id']})
        wrong = models()
        wrong['text']['api_key_env'] = 'not an env name'
        with self.assertRaisesRegex(PhoneFactoryError, '平台后端'):
            config.save_settings(self.root, {'evaluator_config': wrong})

    def test_optional_environment_names_use_role_defaults_without_changing_input(self):
        value = models()
        value['text'].pop('api_key_env')
        value['vision']['api_key_env'] = ''
        before = deepcopy(value)
        saved = config.save_settings(self.root, {'evaluator_config': value})
        self.assertEqual(saved['evaluator_config']['text']['api_key_env'], 'PHONE_FACTORY_EVALUATOR_TEXT_API_KEY')
        self.assertEqual(saved['evaluator_config']['vision']['api_key_env'], 'PHONE_FACTORY_EVALUATOR_VISION_API_KEY')
        self.assertEqual(value, before)

    def test_exact_profile_import_and_failed_first_import_does_not_hide_bundled_rules(self):
        from backend.ChecklistEvaluator.import_profile import import_profile
        from backend.ChecklistEvaluator import import_profile as importer
        from backend.ChecklistEvaluator.evaluation_profiles import list_profiles
        source = config.RUNTIME_ROOT / 'profiles'
        profile = list_profiles(source)['profiles'][0]
        destination = self.root / 'resources' / 'checklist_profiles'
        with patch.object(config, '_environment', return_value={}), patch.object(importer.shutil, 'copyfile', side_effect=PermissionError('occupied')):
            with self.assertRaises(PermissionError):
                import_profile(source, destination, profile['id'], profile['digest'])
            self.assertEqual(config.profile_root(self.root), source)
            self.assertEqual(len(config._profiles(self.root)['profiles']), len(list_profiles(source)['profiles']))
        with patch.object(config, '_environment', return_value={}):
            imported = import_profile(source, destination, profile['id'], profile['digest'])
            self.assertFalse(imported['reused'])
            self.assertEqual(config.profile_root(self.root), destination)
            self.assertTrue(import_profile(source, destination, profile['id'], profile['digest'])['reused'])
            self.assertEqual(list_profiles(destination)['profiles'], list_profiles(source)['profiles'])
        with self.assertRaisesRegex(ValueError, '摘要'):
            import_profile(source, destination, profile['id'], '0' * 64)
        self.assertFalse(any(p.name.startswith('.import_') for p in destination.iterdir()))

    def test_explicit_missing_or_damaged_published_rules_do_not_fall_back(self):
        imported = self.root / 'resources' / 'checklist_profiles'
        (imported / 'broken').mkdir(parents=True)
        with patch.object(config, '_environment', return_value={}):
            self.assertEqual(config.profile_root(self.root), imported)
            self.assertEqual(config._profiles(self.root)['profiles'], [])
            self.assertTrue(config._profiles(self.root)['errors'])
        explicit = self.root / 'missing'
        with patch.object(config, '_environment', return_value={'ADF_CHECKLIST_PROFILES_DIR': str(explicit)}):
            self.assertEqual(config.profile_root(self.root), explicit)
            self.assertEqual(config.imported_profile_root(self.root), explicit)

    def test_legacy_migration_inherits_frozen_models_and_keeps_old_record(self):
        profile = config._profiles()['profiles'][0]
        old = {'version': 1, 'collector_server': 'http://unreachable.invalid:9011',
               'evaluation_profile_id': profile['id'], 'evaluation_profile_digest': profile['digest'],
               'evaluator_config': models('original')}
        before = deepcopy(old)
        config.save_settings(self.root, {'evaluator_config': models('new-default')})
        with patch.object(config, '_environment', return_value=self.env), patch.object(config, 'preflight'):
            local = config.migrate_options(self.root, old)
            again = config.migrate_options(self.root, old)
        self.assertEqual(local, again)
        self.assertEqual(old, before)
        self.assertEqual(local['evaluator_config'], models('original'))
        self.assertNotIn('collector_server', local)
        self.assertEqual(local['execution_backend'], 'platform')
        self.assertEqual(local['profile_snapshot']['profile_digest'], profile['digest'])
        persisted = json.dumps(local)
        self.assertNotIn('test-private', persisted)
        self.assertEqual(config.migrate_options(self.root, None), None)

    def test_preflight_missing_environment_fails_before_subprocess(self):
        with patch.object(config, '_environment', return_value={}), patch.object(config.subprocess, 'run') as run:
            with self.assertRaisesRegex(PhoneFactoryError, 'TEST_QC_TEXT'):
                config.preflight(self.root, {'evaluator_config': models(), 'python_executable': sys.executable})
            run.assert_not_called()

    def test_preflight_returns_dependency_names_and_never_echoes_stderr(self):
        result = SimpleNamespace(returncode=1, stdout=json.dumps({'ok': False, 'missing': ['cv2']}),
                                 stderr='test-private-text')
        with patch.object(config, '_environment', return_value=self.env), patch.object(config.subprocess, 'run', return_value=result):
            with self.assertRaisesRegex(PhoneFactoryError, 'cv2') as raised:
                config.preflight(self.root, {'evaluator_config': models(), 'python_executable': sys.executable})
        self.assertNotIn('test-private', str(raised.exception))

    def test_missing_original_rule_does_not_fall_back_to_an_available_default(self):
        old = {'evaluation_profile_id': 'missing-original', 'evaluation_profile_digest': 'f' * 64,
               'evaluator_config': models()}
        with patch.object(config, '_environment', return_value=self.env), patch.object(config, 'preflight') as probe:
            with self.assertRaisesRegex(PhoneFactoryError, '原冻结'):
                config.migrate_options(self.root, old)
            probe.assert_not_called()


if __name__ == '__main__':
    unittest.main()
