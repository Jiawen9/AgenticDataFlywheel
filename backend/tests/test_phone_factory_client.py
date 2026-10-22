from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend import phonefactory_client


class PhoneFactoryClientCaptureTests(unittest.TestCase):
    def test_cli_always_sends_explicit_logprob_boolean_and_preserves_sampling(self):
        with tempfile.TemporaryDirectory() as temporary:
            task, apps = Path(temporary) / 'tasks.xlsx', Path(temporary) / 'apps.json'
            task.write_bytes(b'workbook')
            apps.write_text('[]', encoding='utf-8')
            for enabled in (False, True):
                for sampling in (False, True):
                    with self.subTest(enabled=enabled, sampling=sampling):
                        command = ['start-run', str(task), str(apps), '--temperature', '0.35', '--top-p', '0.75']
                        if enabled:
                            command.append('--capture-logprobs')
                        if sampling:
                            command.append('--sampling')
                        args = phonefactory_client.build_parser().parse_args(command)
                        with patch.object(phonefactory_client, 'post_multipart', return_value={'ok': True}) as post:
                            self.assertEqual(phonefactory_client.cmd_start_run(args), {'ok': True})
                        path, fields, files = post.call_args.args
                        self.assertEqual(path, '/start_run')
                        self.assertEqual(fields['capture_logprobs'], str(enabled).lower())
                        self.assertEqual(fields['sampling_enabled'], str(sampling).lower())
                        self.assertEqual(fields['temperature'], '0.35')
                        self.assertEqual(fields['top_p'], '0.75')
                        self.assertEqual(files[0], ('task_file', 'tasks.xlsx', b'workbook'))


if __name__ == '__main__':
    unittest.main()
