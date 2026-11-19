import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.preprocessing_revision import compatible_revision
from backend.preprocessing_service import digest, processing_config


# Immutable fingerprints of the six source files in the LF checkpoint release.
_RELEASE_FILES = {
    'export_vla_trajectories.py': 'd48a3cca027f2e1ff01a24a247c6f1a4c88722d56fabd62faa63b1ca027f1eb4',
    'trajectories_preprocessing.py': 'f00791a7b8dca415de8c09545892ae294faebadc235993c0740826681a215f5e',
    'preprocessing_service.py': 'ca82ed8d14ca77c9988f43b897f6376c516f797098a78d4d4e0e5efe2f8a04ea',
    'bounding_box/qwen_reviewer.py': 'efef7ef758d9a49e97769557e58c680a636302e290a669bdc6d75a8d37580389',
    'bounding_box/build_annotations.py': '27f6c9ffb3ae784ede86affb2dcec97058bae69e024d3dcfc69c5ac280cd8ae5',
    'bounding_box/action_box.py': '1909dc3d53b97f856cc50d295ca8688ebdacb848e177c93066ce80e3f0f5f4db',
}
_PREDECESSOR = '68ffa0bb5dbec7a9c94376e45a2ab49ced4b276933deab00b1a76c718a7fa4e3'


class PreprocessingRevisionTests(unittest.TestCase):
    def test_mixed_checkout_only_updates_checkpoint_sources(self):
        # The reference checkout has mixed EOL bytes in the unchanged reviewer.
        # It must recover its own prior digest when just the two changed files
        # are checked out as LF or CRLF, never that of an all-LF checkout.
        prior = '871041e1397b7a0bedd51b8955bf2b53e86741809427196a9ba12ac1fbbc0c9a'
        for revision in ('7e21b94ef68d34a09bdd367011f32ae448e4a08264aefbb10b7912b879090f55',
                         'c8b32d98671a2ac456aeaddb93dd5210a69cef4bb0ba573eb282b87e09354c6a'):
            self.assertEqual(compatible_revision(revision), prior)

    def test_resume_release_reuses_exact_prior_frozen_config_and_cache_identity(self):
        from backend import preprocessing_service as service

        root = Path(service.__file__).parent
        with tempfile.TemporaryDirectory() as tmp:
            env = Path(tmp) / 'model.env'
            def config(model, url):
                env.write_text(f'MODEL_NAME={model}\nMODEL_URL={url}\nYUNAI_API_KEY=private\n', encoding='utf-8')
                with patch.object(service, 'fingerprint', side_effect=lambda path: _RELEASE_FILES[path.relative_to(root).as_posix()]):
                    return processing_config(env)

            previous = {'model': 'a', 'base_url': 'https://a.invalid', 'max_review_rounds': 4,
                        'pipeline_revision': _PREDECESSOR, 'configuration_error': None}
            self.assertEqual(config('a', 'https://a.invalid'), previous)
            self.assertNotEqual(digest(config('b', 'https://a.invalid')), digest(previous))
            self.assertNotEqual(digest(config('a', 'https://b.invalid')), digest(previous))

    def test_altering_any_rule_file_preserves_a_distinct_build_identity(self):
        for name in _RELEASE_FILES:
            with self.subTest(name=name):
                revision = digest({**_RELEASE_FILES, name: 'different-file'})
                self.assertEqual(compatible_revision(revision), revision)
                self.assertNotEqual(revision, _PREDECESSOR)
