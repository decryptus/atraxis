import base64
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
import hashlib
import http.client
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

import yaml

from atraxis.cli import main
from atraxis.config import ConfigurationError, load_config
from atraxis.server import _Handler, build_context


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'atraxis.yml'
        self.secret = Path(self.temp.name) / 'users'
        hashed = base64.b64encode(hashlib.sha1(b'fixture-password').digest()).decode()
        self.secret.write_text('reader:{SHA}' + hashed + '\n')
        self.config_text = 'general:\n  auth_basic_file: users\nsources: []\n'
        self.path.write_text(self.config_text)

    def test_valid_relative_secret_and_defaults(self):
        config = load_config(self.path)
        self.assertEqual(config.auth_basic_file, str(self.secret))
        self.assertEqual(config.listen_addr, '127.0.0.1')
        self.assertEqual(config.listen_port, 9810)

    def test_reject_invalid_configs(self):
        values = ['[]', 'general: {}', 'general: &a {auth_basic_file: users}\nsources: *a',
                  self.config_text + 'sources: []\n',
                  self.config_text.replace('sources: []', 'sources: [{}]'),
                  self.config_text.replace('general:', 'unexpected:'),
                  self.config_text.replace('auth_basic_file: users', 'auth_basic_file: users\n  listen_port: true'),
                  self.config_text.replace('auth_basic_file: users', 'auth_basic_file: users\n  listen_addr: 0.0.0.0'),
                  'general: !!python/object/apply:os.system [echo unsafe]']
        for value in values:
            with self.subTest(value=value):
                self.path.write_text(value)
                with self.assertRaises(ConfigurationError):
                    load_config(self.path)

    def test_explicit_proxy_binding(self):
        self.path.write_text(self.config_text.replace('auth_basic_file: users',
                             'auth_basic_file: users\n  listen_addr: 0.0.0.0\n  behind_tls_proxy: true'))
        self.assertEqual(load_config(self.path).listen_addr, '0.0.0.0')

    def test_schema_enforces_scalar_types_and_ranges(self):
        invalid = {
            'listen_port': [True, False, 0, -1, 65536, 1.0, '9810', '', None, [], {}],
            'max_workers': [True, False, 0, -1, 33, 4.0, '4', '', None, [], {}],
            'behind_tls_proxy': [0, 1, 'true', '', None, [], {}],
            'listen_addr': [123, True, '', None, [], {}, '::1', 'not-an-address'],
            'auth_basic_file': ['', None, True, 12, [], {}],
        }
        for field, values in invalid.items():
            for value in values:
                with self.subTest(field=field, value=value):
                    general = {'auth_basic_file': 'users', field: value}
                    self.path.write_text(yaml.safe_dump({'general': general}))
                    with self.assertRaises(ConfigurationError):
                        load_config(self.path)

    def test_schema_accepts_boundaries_without_coercion(self):
        for port, workers in ((1, 1), (65535, 32)):
            with self.subTest(port=port, workers=workers):
                self.path.write_text(yaml.safe_dump({'general': {
                    'auth_basic_file': 'users', 'listen_port': port,
                    'max_workers': workers, 'behind_tls_proxy': False}}))
                config = load_config(self.path)
                self.assertEqual((config.listen_port, config.max_workers), (port, workers))
                self.assertIs(type(config.listen_port), int)
                self.assertIs(config.behind_tls_proxy, False)

    def test_schema_rejects_unknown_keys_and_nonempty_sources(self):
        documents = [None, True, [], {'general': []}, {'general': {}},
                     {'general': {'auth_basic_file': 'users', 'typo': 1}},
                     {'general': {'auth_basic_file': 'users'}, 'typo': 1}]
        for source in (None, '', {}, True, ['source'], [{}]):
            documents.append({'general': {'auth_basic_file': 'users'}, 'sources': source})
        for document in documents:
            with self.subTest(document=document):
                self.path.write_text(yaml.safe_dump(document))
                with self.assertRaises(ConfigurationError):
                    load_config(self.path)

    def test_duplicate_nested_keys_and_aliases_remain_rejected(self):
        for document in (
                'general: {auth_basic_file: users, listen_port: 1, listen_port: 2}',
                'general: {auth_basic_file: &name users, listen_addr: *name}',
                'general: {1: users}',
                'general: {auth_basic_file: !~~fixedStr users}'):
            with self.subTest(document=document):
                self.path.write_text(document)
                with self.assertRaises(ConfigurationError):
                    load_config(self.path)

    def test_schema_errors_do_not_disclose_values_or_unknown_keys(self):
        for general in ({'auth_basic_file': 'users', 'listen_port': 'secret-value-marker'},
                        {'auth_basic_file': 'users', 'secret-key-marker': 'secret-value-marker'}):
            self.path.write_text(yaml.safe_dump({'general': general}))
            output = io.StringIO()
            with self.assertLogs('sonicprobe.xys', level='ERROR') as logs, redirect_stderr(output):
                self.assertEqual(main(['check-config', '--config', str(self.path)]), 1)
            text = output.getvalue() + '\n'.join(logs.output)
            self.assertNotIn('secret-value-marker', text)
            self.assertNotIn('secret-key-marker', text)

    def test_authentication_file_checks_follow_schema_validation(self):
        for content in (b'', b'x' * 65537):
            self.secret.write_bytes(content)
            with self.assertRaises(ConfigurationError):
                load_config(self.path)
        self.secret.unlink()
        with self.assertRaises(ConfigurationError):
            load_config(self.path)

    def test_plain_yaml_loader_does_not_accept_schema_tags(self):
        with self.assertRaises(yaml.YAMLError):
            yaml.safe_load('value: !~~between(1,32) 4')
        self.assertEqual(yaml.safe_load('value: 4'), {'value': 4})

    def test_missing_and_oversized_config(self):
        self.path.unlink()
        with self.assertRaises(ConfigurationError):
            load_config(self.path)
        self.path.write_text('#' * 65537)
        with self.assertRaises(ConfigurationError):
            load_config(self.path)

    def test_check_config_does_not_start_server(self):
        output = io.StringIO()
        with patch('atraxis.server.serve', side_effect=AssertionError('must not start')), redirect_stdout(output):
            self.assertEqual(main(['check-config', '--config', str(self.path)]), 0)
        self.assertEqual(output.getvalue(), 'Configuration valid\n')

    def test_errors_do_not_echo_configuration(self):
        self.path.write_text('general: {secret: do-not-leak}')
        output = io.StringIO()
        with redirect_stderr(output):
            self.assertEqual(main(['check-config', '--config', str(self.path)]), 1)
        self.assertNotIn('do-not-leak', output.getvalue())

    def test_no_command_does_not_start_daemon(self):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
            main([])
        self.assertEqual(error.exception.code, 2)

    def test_serve_dispatches_explicitly(self):
        with patch('atraxis.server.serve') as serve:
            self.assertEqual(main(['serve', '--config', str(self.path)]), 0)
        serve.assert_called_once()

    def test_core_imports_do_not_load_http_or_cli(self):
        code = "import atraxis.models, atraxis.parsing, sys; assert not any(x in sys.modules for x in ('atraxis.cli', 'httpdis', 'curses'))"
        result = subprocess.run([sys.executable, '-c', code], capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr.decode())


class HttpRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        hashed = base64.b64encode(hashlib.sha1(b'fixture-password').digest()).decode()
        (root / 'users').write_text('reader:{SHA}' + hashed + '\n')
        (root / 'config.yml').write_text('general:\n  auth_basic_file: users\n')
        config = replace(load_config(root / 'config.yml'), listen_port=0)
        self.context = build_context(config)
        started = threading.Event()
        # Attach the startup signal to an existing route; no test-only API route.
        self.context.named_commands['GET /healthz'].at_start = lambda options: started.set()
        self.errors = []
        def run():
            try:
                self.context.run(http_req_handler=_Handler)
            except Exception as error:
                self.errors.append(error)
                started.set()
        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop)
        self.assertTrue(started.wait(5), 'server did not start')
        self.assertFalse(self.errors, self.errors)
        self.port = self.context.server.server_address[1]

    def stop(self):
        self.context.stop()
        self.thread.join(7)
        self.assertFalse(self.thread.is_alive(), 'server did not stop')

    def request(self, path, method='GET', authorized=True):
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=3)
        headers = {}
        if authorized:
            headers['Authorization'] = 'Basic ' + base64.b64encode(b'reader:fixture-password').decode()
        try:
            connection.request(method, path, headers=headers)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def test_authenticated_health(self):
        code, headers, body = self.request('/healthz')
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body), {'alive': True})
        self.assertEqual(headers['Cache-Control'], 'no-store')

    def test_all_read_routes_require_authentication(self):
        for path in ('/healthz', '/readyz', '/v1/status', '/ui'):
            with self.subTest(path=path):
                self.assertEqual(self.request(path, authorized=False)[0], 401)

    def test_web_inventory_is_authenticated_read_only_html(self):
        code, headers, body = self.request('/ui')
        headers = {key.lower(): value for key, value in headers.items()}
        self.assertEqual(code, 200)
        self.assertEqual(headers['content-type'], 'text/html; charset=utf-8')
        self.assertEqual(headers['cache-control'], 'no-store')
        self.assertEqual(headers['x-frame-options'], 'DENY')
        self.assertIn("frame-ancestors 'none'", headers['content-security-policy'])
        self.assertIn(b'No sources configured', body)
        self.assertEqual(self.request('/ui', method='HEAD')[2], b'')
        self.assertEqual(self.request('/ui', method='POST')[0], 405)
        self.assertNotIn(b'Inventory overview', self.request('/ui', authorized=False)[2])

    def test_not_ready_until_collectors_exist(self):
        code, _, body = self.request('/readyz')
        self.assertEqual(code, 503)
        self.assertEqual(json.loads(body)['reason'], 'collectors_not_configured')
        code, _, body = self.request('/v1/status')
        self.assertEqual(code, 200)
        self.assertFalse(json.loads(body)['ready'])

    def test_head_and_no_mutation_route(self):
        self.assertEqual(self.request('/v1/status', method='HEAD')[2], b'')
        self.assertEqual(self.request('/v1/actions', method='POST')[0], 404)
