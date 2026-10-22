import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import yaml

from atraxis.doctor import diagnose


class SetupClientTests(unittest.TestCase):
    def test_real_independent_client_settings_checked_without_rpc_or_ambient_override(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            def private(name, value):
                path = root / name
                path.write_text(value); path.chmod(0o600)
            private('users', 'reader:synthetic')
            private('token', 'a' * 64)
            private('client.yml', yaml.safe_dump(dict(origin='https://engine.example.test',token_file='token')))
            private('mapping.yml', yaml.safe_dump(dict(version=1,identities=[dict(username='reader',subject='alice',organization='demo',atropos_client='client.yml')])))
            private('server.yml', yaml.safe_dump(dict(general=dict(auth_basic_file='users',rules_web_file='mapping.yml'))))
            with patch('atropos_client.client.RuleClient._call', side_effect=AssertionError('no RPC')), patch.dict(os.environ, {'ATROPOS_TOKEN':'invalid-ambient-token','ATROPOS_SERVER_URL':'http://untrusted.example.test'}):
                report, _ = diagnose(root / 'server.yml')
            self.assertTrue(report['configuration_valid'])
            self.assertIn(dict(component='rules',state='passed',code='profile_valid',advice='Local profile and supported referenced files are valid; remote authority is not verified.'), report['checks'])
            (root / 'token').unlink()
            report, code = diagnose(root / 'server.yml')
            self.assertEqual(code, 1)
            self.assertIn('client_unavailable', [c['code'] for c in report['checks']])
            self.assertNotIn('a' * 64, json.dumps(report))
