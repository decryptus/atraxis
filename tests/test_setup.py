import base64
from contextlib import redirect_stderr, redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from atraxis.cli import main
from atraxis.config import ConfigurationError, load_config
from atraxis.setup import candidate, write_config


class SetupTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.auth = self.root / 'users'
        digest = base64.b64encode(hashlib.sha1(b'synthetic-secret').digest()).decode()
        self.auth.write_text('reader:{SHA}' + digest + '\n')
        self.auth.chmod(0o600)
        self.credentials = self.root / 'credentials.json'
        self.credentials.write_text(json.dumps(dict(username='reader', password='source-secret-marker')))
        self.credentials.chmod(0o600)
        self.output = self.root / 'config.yml'

    def values(self, **changes):
        return dict(dict(output=self.output, auth_file=self.auth, source_id='lab',
                         source_kind='monit_docker', source_origin='https://lab.example.test',
                         source_credentials_file=self.credentials), **changes)

    def test_preview_is_offline_and_never_creates_destination_or_discloses_secrets(self):
        output, error = io.StringIO(), io.StringIO()
        args = ['setup', '--output', str(self.output), '--auth-file', str(self.auth),
                '--source-id', 'lab', '--source-kind', 'monit_docker',
                '--source-origin', 'https://lab.example.test', '--source-credentials-file', str(self.credentials)]
        with patch('socket.getaddrinfo', side_effect=AssertionError('no DNS')), redirect_stdout(output), redirect_stderr(error):
            self.assertEqual(main(args), 0)
        self.assertFalse(self.output.exists())
        self.assertNotIn('source-secret-marker', output.getvalue() + error.getvalue())
        self.assertNotIn(self.auth.read_text().strip(), output.getvalue())
        self.assertIn('Preview only', output.getvalue())
        self.assertIn('shares inventory', error.getvalue())

    def test_explicit_write_is_private_complete_and_loadable(self):
        path, document = candidate(**self.values())
        self.assertTrue(write_config(path, document))
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        loaded = load_config(path)
        self.assertEqual(loaded.sources[0].source_id, 'lab')
        self.assertEqual(loaded.auth_basic_file, str(self.auth))
        self.assertEqual(set(self.root.iterdir()), {self.auth, self.credentials, self.output})

    def test_existing_file_and_symlink_are_never_replaced(self):
        path, document = candidate(**self.values())
        path.write_text('existing')
        with self.assertRaises(FileExistsError): write_config(path, document)
        self.assertEqual(path.read_text(), 'existing')
        path.unlink(); path.symlink_to(self.auth)
        original = self.auth.read_bytes()
        with self.assertRaises(FileExistsError): write_config(path, document)
        self.assertTrue(path.is_symlink())
        self.assertEqual(self.auth.read_bytes(), original)
        self.assertFalse(list(self.root.glob('.atraxis-setup-*')))

    def test_incomplete_source_http_and_nonloopback_listener_require_explicit_settings(self):
        for changes in (dict(source_origin=None), dict(source_origin='http://lab.example.test'),
                        dict(source_origin='https://reader:secret@lab.example.test'), dict(listen_addr='0.0.0.0'),
                        dict(source_origin='http://127.0.0.1:8000'), dict(source_ca_file=self.credentials)):
            with self.subTest(changes=changes), self.assertRaises(ValueError): candidate(**self.values(**changes))
        path, document = candidate(**self.values(listen_addr='0.0.0.0', behind_tls_proxy=True,
                                    source_origin='http://127.0.0.1:8000', allow_source_loopback_http=True))
        self.assertTrue(document['general']['behind_tls_proxy'])
        self.assertTrue(document['sources'][0]['allow_loopback_http'])

    def test_recheck_and_failed_write_leave_no_partial_file(self):
        path, document = candidate(**self.values())
        self.credentials.unlink()
        with self.assertRaises(ValueError): write_config(path, document)
        self.assertFalse(path.exists())
        self.credentials.write_text(json.dumps(dict(username='reader', password='synthetic')))
        with patch('atraxis.setup.os.fsync', side_effect=OSError('private-error-marker')), self.assertRaises(OSError):
            write_config(path, document)
        self.assertFalse(path.exists())
        self.assertFalse(list(self.root.glob('.atraxis-setup-*')))

    def test_publication_sync_failure_keeps_complete_file_and_refuses_retry(self):
        path, document = candidate(**self.values())
        with patch('atraxis.setup.os.fsync', side_effect=[None, OSError('disk')]), self.assertRaises(OSError):
            write_config(path, document)
        self.assertEqual(load_config(path).sources[0].source_id, 'lab')
        with self.assertRaises(FileExistsError): write_config(path, document)

    def test_unsafe_and_symlink_parent_refused_without_modification(self):
        parent = self.root / 'public'; parent.mkdir(); parent.chmod(0o777)
        path, document = candidate(**self.values(output=parent / 'config.yml'))
        with self.assertRaises(ConfigurationError): write_config(path, document)
        parent.chmod(0o700)
        alias = self.root / 'alias'; alias.symlink_to(parent, target_is_directory=True)
        with self.assertRaises(OSError): write_config(alias / 'config.yml', document)
        self.assertEqual(list(parent.iterdir()), [])

    def test_cli_write_error_is_safe_and_empty_setup_reports_not_ready(self):
        output, error = io.StringIO(), io.StringIO()
        args = ['setup', '--output', str(self.output), '--auth-file', str(self.auth), '--write']
        with redirect_stdout(output), redirect_stderr(error):
            self.assertEqual(main(args), 0)
            self.assertEqual(main(args), 1)
        self.assertIn('not be ready', error.getvalue())
        self.assertIn('never replaced', error.getvalue())
        self.assertEqual(load_config(self.output).sources, ())
