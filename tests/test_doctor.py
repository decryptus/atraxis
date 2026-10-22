from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import yaml

from atraxis.cli import main
from atraxis.doctor import diagnose
from test_observations import monit


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args): pass
    def do_GET(self):
        self.server.calls.append((self.command, self.path))
        self.server.entered.set()
        if self.server.block:
            self.server.release.wait(5)
        self.send_response(self.server.status)
        self.send_header('Content-Type', 'application/json')
        if self.server.status == 302: self.send_header('Location', '/must-not-follow')
        self.end_headers()
        payload = self.server.payload
        try: self.wfile.write(json.dumps(payload).encode())
        except OSError: pass


class DoctorTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / 'users').write_text('reader:synthetic-hash')
        (self.root / 'source.json').write_text(json.dumps(dict(username='reader', password='secret-marker')))
        self.path = self.root / 'config.yml'
        self.document = dict(general=dict(auth_basic_file='users'), sources=[])
        self.save()

    def save(self):
        self.path.write_text(yaml.safe_dump(self.document))

    def source(self, **changes):
        return dict(dict(source_id='lab', kind='monit_docker', origin='https://lab.example.test',
                         credentials_file='source.json', timeout=1), **changes)

    def codes(self, report):
        return {check['code'] for check in report['checks']}

    def server(self):
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        server.status, server.payload, server.calls = 200, monit(), []
        server.block = False
        server.entered, server.release = threading.Event(), threading.Event()
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        def close():
            server.release.set()
            server.shutdown(); server.server_close(); thread.join(2)
        self.addCleanup(close)
        return server

    def test_cli_sigterm_cancels_a_running_probe(self):
        server = self.server(); server.block = True
        self.document['sources'] = [self.source(origin='http://127.0.0.1:%d' % server.server_port,
                                               allow_loopback_http=True, timeout=10)]
        self.save()
        process = subprocess.Popen([sys.executable, '-m', 'atraxis', 'doctor', '--config', str(self.path),
                                    '--probe-sources', '--json'], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            self.assertTrue(server.entered.wait(5))
            process.terminate()
            output, error = process.communicate(timeout=3)
            self.assertEqual(process.returncode, 143, error.decode())
            self.assertIn('cancelled', self.codes(json.loads(output)))
            self.assertEqual(server.calls, [('GET', '/v1/status')])
        finally:
            if process.poll() is None: process.kill()
            process.communicate(timeout=3)

    def test_empty_configuration_is_valid_but_not_ready(self):
        report, code = diagnose(self.path)
        self.assertEqual(code, 2)
        self.assertTrue(report['configuration_valid'])
        self.assertIsNone(report['sources_ready'])
        self.assertTrue({'no_sources', 'shared_inventory'} <= self.codes(report))

    def test_offline_diagnosis_never_resolves_or_contacts_sources(self):
        self.document['sources'] = [self.source()]; self.save()
        with patch('socket.getaddrinfo', side_effect=AssertionError('no DNS')), patch('atraxis.transport.HTTPTransport.fetch', side_effect=AssertionError('no reads')):
            report, code = diagnose(self.path)
        self.assertTrue(report['configuration_valid'])
        self.assertFalse(report['probe_requested'])
        self.assertIsNone(report['sources_ready'])
        self.assertIn('configured', self.codes(report))
        self.assertNotIn('secret-marker', json.dumps(report))

    def test_invalid_document_and_separate_runtime_source_profile_failures_are_safe(self):
        self.path.write_text('general: [broken')
        report, code = diagnose(self.path)
        self.assertEqual(code, 1); self.assertIn('invalid_document', self.codes(report))
        self.document['general'].update(auth_basic_file='missing', accounts_web_file='absent', accounts_web_origin='https://app.example.test')
        self.document['sources'] = [self.source(origin='https://user:secret-marker@example.test')]; self.save()
        with patch('atraxis.transport.HTTPTransport.fetch', side_effect=AssertionError('no reads')):
            report, code = diagnose(self.path, probe=True)
        self.assertEqual(code, 1)
        self.assertFalse(report['configuration_valid'])
        self.assertTrue({'invalid_runtime', 'invalid_sources', 'invalid_profile'} <= self.codes(report))
        self.assertNotIn('secret-marker', json.dumps(report))

    def test_duplicate_sources_block_all_network_reads(self):
        self.document['sources'] = [self.source(), self.source()]; self.save()
        with patch('atraxis.transport.HTTPTransport.fetch', side_effect=AssertionError('no reads')):
            report, code = diagnose(self.path, probe=True)
        self.assertEqual(code, 1); self.assertIn('invalid_sources', self.codes(report))

    def test_real_probe_validates_freshness_failure_and_recovery_without_retry(self):
        server = self.server()
        self.document['sources'] = [self.source(origin='http://127.0.0.1:%d' % server.server_port,
                                               allow_loopback_http=True)]; self.save()
        for status, payload, expected in [(200, monit(), 'fresh'), (401, {'secret':'raw-body-marker'}, 'authentication_error'),
                (302, {}, 'redirect_refused'), (200, {'private':'raw-body-marker'}, 'invalid_payload'),
                (200, dict(monit(), age_seconds=999), 'not_current'), (200, monit(), 'fresh')]:
            server.status, server.payload = status, payload
            before = len(server.calls)
            report, code = diagnose(self.path, probe=True)
            self.assertIn(expected, self.codes(report))
            self.assertEqual(report['sources_ready'], expected == 'fresh')
            self.assertEqual(server.calls[before:], [('GET', '/v1/status')])
            self.assertNotIn('raw-body-marker', json.dumps(report))
        self.assertEqual(code, 2)  # Shared inventory still needs explicit operator attention.

    def test_real_certlord_probe_uses_its_fixed_route_and_current_timestamp(self):
        server = self.server()
        now = time.time()
        stamp = datetime.fromtimestamp(now, timezone.utc).isoformat()
        server.payload = dict(schema_version=1, started_at=stamp, completed_at=stamp,
                              completed_timestamp_seconds=now, certificates=[])
        self.document['sources'] = [self.source(kind='certlord', origin='http://127.0.0.1:%d' % server.server_port,
                                               allow_loopback_http=True)]; self.save()
        report, _ = diagnose(self.path, probe=True)
        self.assertTrue(report['sources_ready'])
        self.assertEqual(server.calls, [('GET', '/api/certificates/observations')])

    def test_source_failure_is_isolated_and_cancellation_prevents_requests(self):
        good, bad = self.server(), self.server()
        bad.status = 503
        self.document['sources'] = [self.source(source_id=name, origin='http://127.0.0.1:%d' % server.server_port,
                                               allow_loopback_http=True) for name, server in [('good',good),('bad',bad)]]
        self.save()
        completed = threading.Event()
        report, code = diagnose(self.path, probe=True, cancel=completed)
        self.assertFalse(completed.is_set())
        outcomes = {c['component']:c['code'] for c in report['checks']}
        self.assertEqual(outcomes['source:good'], 'fresh')
        self.assertEqual(outcomes['source:bad'], 'source_unavailable')
        self.assertFalse(report['sources_ready']); self.assertEqual(code, 2)
        cancel = threading.Event(); cancel.set()
        report, _ = diagnose(self.path, probe=True, cancel=cancel)
        self.assertEqual(len(good.calls), 1); self.assertEqual(len(bad.calls), 1)
        self.assertEqual(sum(c['code'] == 'cancelled' for c in report['checks']), 2)

    def test_local_account_profiles_checked_without_writes_and_external_adapters_not_loaded(self):
        from atraxis.account_store import AccountStore
        from atraxis.accounts import default_directory
        store = AccountStore(self.root / 'accounts.db'); store.apply(default_directory('alice'), None)
        before = (self.root / 'accounts.db').read_bytes()
        profile = self.root / 'accounts.yml'
        data = dict(version=1,store='accounts.db',identities=[dict(username='reader',subject='alice')])
        profile.write_text(yaml.safe_dump(data)); profile.chmod(0o600)
        self.document['general'].update(accounts_web_file='accounts.yml', accounts_web_origin='https://app.example.test')
        self.save()
        report, _ = diagnose(self.path)
        self.assertTrue(report['configuration_valid'])
        self.assertEqual((self.root / 'accounts.db').read_bytes(), before)
        data['store'] = dict(backend='external', url='opaque://secret-marker')
        profile.write_text(yaml.safe_dump(data))
        with patch('atraxis.account_storage.entry_points', side_effect=AssertionError('no plugin loading')):
            report, _ = diagnose(self.path)
        self.assertIn('external_accounts_unchecked', self.codes(report))
        self.assertNotIn('secret-marker', json.dumps(report))

    def test_invalid_source_does_not_hide_other_components_or_create_account_storage(self):
        profile = self.root / 'accounts.yml'
        profile.write_text(yaml.safe_dump(dict(version=1,store='absent.db',identities=[dict(username='reader',subject='alice')])))
        profile.chmod(0o600)
        self.document['general'].update(accounts_web_file='accounts.yml', accounts_web_origin='https://app.example.test')
        self.save()
        report, code = diagnose(self.path)
        self.assertEqual(code, 1); self.assertIn('invalid_accounts', self.codes(report))
        self.assertFalse((self.root / 'absent.db').exists())

    def test_cli_json_has_fixed_codes_and_no_credentials_or_raw_exceptions(self):
        self.document['sources'] = [self.source(credentials_file='secret-marker')]; self.save()
        output, error = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(error):
            self.assertEqual(main(['doctor','--config',str(self.path),'--json']), 1)
        report = json.loads(output.getvalue())
        self.assertIn('invalid_sources', self.codes(report))
        self.assertNotIn('secret-marker', output.getvalue() + error.getvalue())
