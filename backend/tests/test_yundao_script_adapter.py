"""Real local subprocess fixtures only; never upload or access a real token."""
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from backend.data_publishing import internal_uploader as adapter
from backend.data_publishing.script_progress import parse_progress, redact


class ProgressTests(unittest.TestCase):
    def test_speed_without_total_never_claims_percentage(self):
        result = parse_progress("速度 12.4 MiB/s 已上传 98%")
        self.assertEqual(result["speed_text"], "12.4 MiB/s")
        self.assertIsNone(result["percent"])
        self.assertIsNone(result["total_bytes"])

    def test_only_explicit_byte_totals_are_used(self):
        result = parse_progress("2MiB / 8MiB 4MB/s")
        self.assertEqual(result["transferred_bytes"], 2 * 1024 * 1024)
        self.assertEqual(result["total_bytes"], 8 * 1024 * 1024)
        self.assertEqual(result["percent"], 25)
        self.assertEqual(parse_progress("123/246 bytes")["percent"], 50)
        for value in ("100/200 files", "2MB/1MB", "0B/0B", "100% complete"):
            self.assertIsNone(parse_progress(value)["percent"])

    def test_secrets_are_redacted_before_truncation(self):
        secret = "private-fake-value"
        message = 'app_token="other key" Authorization: Bearer another-secret password=hello '
        message += "https://user:pass@example.test/?token=query-secret " + secret
        value = redact(message, secret)
        for item in (secret, "other key", "another-secret", "hello", "user:pass", "query-secret"):
            self.assertNotIn(item, value)
        self.assertNotIn(secret, parse_progress("x" * 999 + secret, secret)["message"])
        self.assertNotIn("\x1b", parse_progress("\x1b[32mspeed 1MB/s\x1b[0m")["message"])


@unittest.skipIf(adapter.psutil is None, "psutil backend dependency is not installed")
class ScriptAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(prefix="云道 space ")
        self.root = Path(self.temp.name)
        self.source = self.root / "完整轨迹 space"
        self.source.mkdir()
        (self.source / "步骤.png").write_bytes(b"not-validated-by-upload")
        (self.source / "nested").mkdir()
        (self.source / "nested" / "response.json").write_text("unchanged", encoding="utf-8")
        self.script = self.root / "uploader 测试.py"
        self.marker = self.root / "executed.json"
        self.config_root = patch.object(adapter, "BACKEND_DIR", self.root)
        self.config_root.start()
        self.environment = patch.dict(os.environ, {
            "ADF_YUNDAO_PYTHON": sys.executable,
            "ADF_YUNDAO_SCRIPT": str(self.script),
            "ADF_YUNDAO_APP_TOKEN": "fake-private-token",
            "ADF_YUNDAO_REGION": "test-region",
            "ADF_YUNDAO_BUCKET": "test-bucket",
            "ADF_YUNDAO_PREFIX": "test/前缀/",
        })
        self.environment.start()
        self.progress, self.processes = [], []
        self.context = adapter.DirectoryUploadContext(self.source, "prefix/batch/用例/轨迹/",
                                                     "test-region", "test-bucket", "r1", "item1")

    def tearDown(self):
        self.environment.stop()
        self.config_root.stop()
        self.temp.cleanup()

    def create_script(self, body):
        self.script.write_text("import sys, os, json, time\nfrom pathlib import Path\n" + body, encoding="utf-8")

    def on_process(self, record):
        self.processes.append(record)
        self.assertFalse(self.marker.exists(), "Uploader must not run before registration")
        self.assertEqual(adapter.probe_process(record), "alive")
        self.assertEqual(adapter.probe_process({**record, "create_time": record["create_time"] - 50}), "dead")

    def upload(self):
        return adapter.upload_directory(self.context, self.progress.append, self.on_process)

    def test_full_directory_argv_crlf_unicode_and_stdout_stderr(self):
        self.create_script(r'''
args = dict(arg[2:].split('=', 1) for arg in sys.argv[1:] if '=' in arg)
assert '--show_speed' in sys.argv
assert Path(args['local_folder_absolute_path']).is_absolute()
assert (Path(args['local_folder_absolute_path']) / 'nested' / 'response.json').read_text() == 'unchanged'
assert args['bucket_path'] == 'prefix/batch/用例/轨迹/'
assert args['region'] == 'test-region'
assert args['bucket_name'] == 'test-bucket'
assert args.pop('app_token') == 'fake-private-token'
Path('executed.json').write_text(json.dumps(args), encoding='utf-8')
print('速度 1MiB/4MiB 12.5 MiB/s', end='\r', flush=True)
time.sleep(1.05)
print('stderr app_token=fake-private-token', file=sys.stderr, flush=True)
text = ('x' * 4095 + '中文输出').encode('utf-8')
os.write(sys.stdout.fileno(), text[:4096])
os.write(sys.stdout.fileno(), text[4096:] + b'\r\n')
print('完成 4MiB/4MiB 8 MiB/s', flush=True)
''')
        result = self.upload()
        self.assertTrue(result.success)
        self.assertEqual(len(self.processes), 1)
        self.assertTrue(self.marker.exists())
        self.assertEqual(self.progress[0]["percent"], 25)
        self.assertEqual(self.progress[-1]["percent"], 100)
        self.assertIn("中文输出", json.dumps(self.progress[-1], ensure_ascii=False))
        self.assertNotIn("fake-private-token", json.dumps(self.progress))
        self.assertNotIn("fake-private-token", json.dumps(self.processes))
        self.assertEqual(adapter.probe_process(self.processes[0]), "dead")
        self.assertEqual((self.source / "步骤.png").read_bytes(), b"not-validated-by-upload")

    def test_capabilities_never_contain_credentials_or_command(self):
        self.create_script("pass\n")
        value = adapter.get_configuration()
        self.assertTrue(value["configured"])
        self.assertTrue(value["directory_upload"])
        self.assertEqual(value["default_prefix"], "test/前缀/")
        self.assertNotIn("fake-private-token", json.dumps(value))
        self.assertNotIn(str(self.script), str(value))

    def test_dotenv_fallback_and_environment_priority(self):
        self.create_script("pass\n")
        env_file = self.root / ".env"
        env_file.write_text(
            "# isolate from actual backend settings\n"
            f'ADF_YUNDAO_PYTHON="{sys.executable}"\n'
            f'ADF_YUNDAO_SCRIPT="{self.script}"\n'
            "ADF_YUNDAO_APP_TOKEN='dotenv-private-token'\n"
            "ADF_YUNDAO_REGION=dotenv-region\nADF_YUNDAO_BUCKET=dotenv-bucket\n"
            "ADF_YUNDAO_PREFIX=from-dotenv/\nOTHER_SECRET=must-not-load\n",
            encoding="utf-8-sig")
        clean_environment = {key: value for key, value in os.environ.items()
                             if not key.startswith("ADF_YUNDAO_")}
        with patch.object(adapter, "BACKEND_DIR", self.root):
            with patch.dict(os.environ, clean_environment, clear=True):
                capability = adapter.get_configuration()
                self.assertTrue(capability["configured"])
                self.assertEqual(capability["region"], "dotenv-region")
                self.assertEqual(capability["default_prefix"], "from-dotenv/")
                self.assertNotIn("dotenv-private-token", json.dumps(capability))
                self.assertNotIn("OTHER_SECRET", adapter._configuration())
                with patch.dict(os.environ, {"ADF_YUNDAO_PREFIX": "from-environment/"}):
                    self.assertEqual(adapter.get_configuration()["default_prefix"], "from-environment/")
                with patch.dict(os.environ, {"ADF_YUNDAO_APP_TOKEN": ""}):
                    self.assertFalse(adapter.get_configuration()["configured"])

    def test_long_source_directory_check_keeps_logical_argv(self):
        from backend.file_io import io_path
        source = self.root / ("long_source_" * 8) / ("测试_" * 25) / ("depth_" * 14)
        io_path(source).mkdir(parents=True)
        io_path(source / "keep.txt").write_text("source", encoding="utf-8")
        self.context = adapter.DirectoryUploadContext(source, self.context.bucket_path,
                                                     "test-region", "test-bucket", "r1", "long")
        self.create_script(r'''
args = dict(arg[2:].split('=', 1) for arg in sys.argv[1:] if '=' in arg)
assert not args['local_folder_absolute_path'].startswith('\\\\?\\')
assert len(args['local_folder_absolute_path']) > 260
print('complete')
''')
        self.assertTrue(self.upload().success)

    def test_failed_registration_does_not_execute_upload(self):
        self.create_script("Path('executed.json').write_text('BAD')\n")
        def reject(record):
            self.processes.append(record)
            raise OSError("fake-private-token must not escape")
        with self.assertRaises(adapter.UploadError) as caught:
            adapter.upload_directory(self.context, self.progress.append, reject)
        self.assertNotIn("fake-private-token", str(caught.exception))
        self.assertFalse(self.marker.exists())
        self.assertEqual(adapter.probe_process(self.processes[0]), "dead")

    def test_parent_eof_without_registration_never_starts_script(self):
        self.create_script("Path('executed.json').write_text('BAD')\n")
        child = subprocess.Popen([sys.executable, "-u", "-c", adapter._BOOTSTRAP, "nonce", str(self.script)],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        stdout, stderr = child.communicate(b"", timeout=5)
        self.assertEqual(child.returncode, 73)
        self.assertFalse(self.marker.exists())

    def test_progress_storage_failure_stops_registered_process(self):
        self.create_script("print('1MiB/4MiB 1MiB/s', flush=True)\n"
                           "time.sleep(10)\nPath('executed.json').write_text('BAD')\n")
        def reject_progress(value):
            raise OSError("fake-private-token")
        with self.assertRaises(adapter.UploadError) as caught:
            adapter.upload_directory(self.context, reject_progress, self.on_process)
        self.assertNotIn("fake-private-token", str(caught.exception))
        self.assertEqual(adapter.probe_process(self.processes[0]), "dead")
        self.assertFalse(self.marker.exists())

    def test_unknown_process_identity_is_not_reported_dead(self):
        self.assertEqual(adapter.probe_process({}), "unknown")
        record = {"pid": os.getpid(), "create_time": 1, "executable": sys.executable,
                  "script": str(self.script), "nonce": "n"}
        with patch.object(adapter.psutil, "Process", side_effect=adapter.psutil.AccessDenied()):
            self.assertEqual(adapter.probe_process(record), "unknown")

    def test_nonzero_exit_and_bounded_output_are_safe(self):
        self.create_script("print('x' * 20000 + 'fake-private-token')\n"
                           "print('app_token=fake-private-token rejected', file=sys.stderr)\n"
                           "raise SystemExit(9)\n")
        with self.assertRaisesRegex(adapter.UploadError, "退出码 9") as caught:
            self.upload()
        self.assertNotIn("fake-private-token", str(caught.exception))
        self.assertNotIn("fake-private-token", json.dumps(self.progress))
        self.assertLess(len(json.dumps(self.progress)), 8000)

    def test_destination_configuration_change_prevents_wrong_target(self):
        self.create_script("Path('executed.json').write_text('BAD')\n")
        with patch.dict(os.environ, {"ADF_YUNDAO_BUCKET": "other"}):
            with self.assertRaisesRegex(adapter.UploadError, "当前配置不同"):
                self.upload()
        self.assertFalse(self.marker.exists())


if __name__ == "__main__":
    unittest.main()
