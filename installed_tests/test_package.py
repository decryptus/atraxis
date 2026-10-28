"""Acceptance of the built base package, run outside the source checkout."""
import base64
import hashlib
import http.client
import importlib.metadata
import importlib.util
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import sysconfig
import tempfile
import time
import unittest

import atraxis


class InstalledPackageTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(Path(atraxis.__file__).resolve().is_relative_to(
            Path(sysconfig.get_path('purelib')).resolve()), 'Atraxis must be installed, not imported from the checkout')
        temporary = tempfile.TemporaryDirectory(prefix='atraxis-installed-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.environment = {key: os.environ[key] for key in ('PATH', 'LANG', 'LC_ALL') if key in os.environ}
        self.environment.update(PYTHONNOUSERSITE='1', PYTHONUNBUFFERED='1')
        self.command = [str(Path(sys.executable).parent / 'atraxis')]

    def cli(self, *args, code=0):
        result = subprocess.run(self.command + list(args), cwd=self.root, env=self.environment,
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, code, result.stderr)
        return result

    def accounts(self, filename, *args, code=0):
        return self.cli('accounts', '--store', str(self.root / filename), *args, code=code)

    def test_base_package_has_no_embedded_engine_and_explicit_commands(self):
        for module in ('atropos', 'zen', 'atropos_client', 'textual'):
            self.assertIsNone(importlib.util.find_spec(module), module + ' unexpectedly installed by base package')
        self.assertIn('sonicprobe==0.3.62', importlib.metadata.requires('atraxis'))
        self.assertIn('check-config', self.cli('--help').stdout)
        self.cli(code=2)
        self.cli('check-config', '--config', str(self.root / 'absent.yml'), code=1)

    def test_source_onboarding_and_inventory_grant_use_installed_base_cli(self):
        self.accounts('accounts.db', 'init', '--subject', 'operator', '--administrator')
        def private_file(name, value):
            path = self.root / name
            path.write_text(json.dumps(value))
            path.chmod(0o600)
            return str(path)
        def reviewed(command, config, request):
            path = private_file('request.json', request)
            review = json.loads(self.cli(command, '--config', config, 'check', '--file', path).stdout)
            result = json.loads(self.cli(command, '--config', config, 'apply', '--file', path,
                                         '--expected-revision', review['revision'], '--confirm').stdout)
            self.assertTrue(result['saved'])
            return result
        private_file('source.json', dict(username='reader', password='synthetic-private-secret'))
        users = self.root / 'users'
        users.write_text('operator:{SHA}' + base64.b64encode(hashlib.sha1(b'synthetic').digest()).decode() + '\n')
        users.chmod(0o600)
        source_origin = 'https://monitor.invalid'
        private_file('configuration-admin.json', dict(version=1, store='accounts.db',
            identities=[dict(uid=os.getuid(), subject='operator', organization='default')],
            administrators=[dict(subject='operator', organization='default')],
            connections=dict(monitor=dict(kind='monit_docker', origin=source_origin, credentials_file='source.json'))))
        daemon = private_file('daemon.json', dict(general=dict(auth_basic_file='users',
            configuration_web_file='configuration-admin.json'), sources=[]))
        inventory = private_file('inventory.json', dict(version=3, store='accounts.db',
            identities=[dict(uid=os.getuid(), subject='operator')],
            servers=[dict(id='host-a', organization='default', sources=['monitor-a'])],
            resource_groups=[], grants=[], audit=[],
            administrators=[dict(subject='operator', organization='default', targets=[dict(kind='server', id='host-a')])]))
        result = reviewed('sources-admin', daemon, dict(operation='source.add', source_id='monitor-a',
            connection='monitor', timeout=2, poll_interval=15, max_age=45))
        self.assertFalse(result['runtime_changed'])
        self.cli('check-config', '--config', daemon)
        state = json.loads(self.cli('inventory-admin', '--config', inventory, 'show').stdout)
        self.assertEqual(state['organizations'][0]['grants'], [])
        reviewed('inventory-admin', inventory, dict(operation='grant.add', organization='default',
            subject_kind='user', subject='operator', targets=[dict(kind='source', id='monitor-a')]))
        state = json.loads(self.cli('inventory-admin', '--config', inventory, 'show').stdout)
        grant = state['organizations'][0]['grants'][0]
        reviewed('inventory-admin', inventory, dict(operation='grant.remove', grant=grant['id']))
        state = json.loads(self.cli('inventory-admin', '--config', inventory, 'show').stdout)
        self.assertEqual(state['organizations'][0]['grants'], [])
        self.assertEqual(len(state['audit']), 2)
        self.assertIsNone(importlib.util.find_spec('textual'))

    def test_default_sqlite_cli_backup_restore_preserves_directory_and_audit(self):
        initialized = json.loads(self.accounts('accounts.db', 'init', '--subject', 'operator', '--administrator').stdout)
        before = json.loads(self.accounts('accounts.db', 'show').stdout)
        audit = json.loads(self.accounts('accounts.db', 'audit').stdout)
        self.assertEqual(initialized['revision'], before['revision'])
        self.assertTrue(before['directory']['entity_delegations'])
        self.assertTrue(audit)
        with sqlite3.connect(self.root / 'accounts.db') as connection:
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertTrue({'users', 'organizations', 'organization_memberships', 'account_audit'} <= tables)
            self.assertEqual(connection.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
        archive = self.root / 'backup.json'
        backed_up = json.loads(self.accounts('accounts.db', 'backup', '--output', str(archive)).stdout)
        self.assertEqual(archive.stat().st_mode & 0o777, 0o600)
        self.accounts('restored.db', 'restore', '--input', str(archive), code=2)
        self.assertFalse((self.root / 'restored.db').exists())
        restored = json.loads(self.accounts('restored.db', 'restore', '--input', str(archive), '--confirm-services-stopped').stdout)
        self.assertEqual(backed_up, restored)
        self.assertEqual(json.loads(self.accounts('restored.db', 'show').stdout), before)
        self.assertEqual(json.loads(self.accounts('restored.db', 'audit').stdout), audit)
        original = (self.root / 'restored.db').read_bytes()
        self.accounts('restored.db', 'restore', '--input', str(archive), '--confirm-services-stopped', code=2)
        self.assertEqual((self.root / 'restored.db').read_bytes(), original)

    def test_installed_daemon_authentication_web_and_graceful_shutdown(self):
        digest = base64.b64encode(hashlib.sha1(b'synthetic-password').digest()).decode()
        credentials = self.root / 'users'
        credentials.write_text('reader:{SHA}' + digest + '\n')
        credentials.chmod(0o600)
        with socket.socket() as reservation:
            reservation.bind(('127.0.0.1', 0))
            port = reservation.getsockname()[1]
        config = self.root / 'atraxis.yml'
        config.write_text('general:\n  auth_basic_file: users\n  listen_addr: 127.0.0.1\n  listen_port: %d\nsources: []\n' % port)
        self.assertEqual(self.cli('check-config', '--config', str(config)).stdout.strip(), 'Configuration valid')
        authorization = 'Basic ' + base64.b64encode(b'reader:synthetic-password').decode()

        def request(path, authenticated=True):
            connection = http.client.HTTPConnection('127.0.0.1', port, timeout=1)
            try:
                connection.request('GET', path, headers={'Authorization': authorization} if authenticated else {})
                response = connection.getresponse()
                return response.status, response.read()
            finally:
                connection.close()

        with (self.root / 'daemon.log').open('w+') as log:
            process = subprocess.Popen(self.command + ['serve', '--config', str(config)],
                                       cwd=self.root, env=self.environment, stdout=log, stderr=log)
            try:
                deadline = time.monotonic() + 15
                while True:
                    self.assertIsNone(process.poll(), 'Installed daemon exited before readiness')
                    try:
                        if request('/healthz')[0] == 200:
                            break
                    except (OSError, http.client.HTTPException):
                        pass
                    self.assertLess(time.monotonic(), deadline, 'Installed daemon startup timed out')
                    time.sleep(.05)
                self.assertEqual(json.loads(request('/healthz')[1]), {'alive': True})
                for path in ('/healthz', '/readyz', '/v1/status', '/ui'):
                    self.assertEqual(request(path, authenticated=False)[0], 401)
                self.assertEqual(request('/readyz')[0], 503)
                self.assertFalse(json.loads(request('/v1/status')[1])['ready'])
                status, page = request('/ui')
                self.assertEqual(status, 200)
                self.assertIn(b'No sources configured', page)
            finally:
                if process.poll() is None:
                    process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
                    self.fail('Installed daemon failed to stop gracefully')
            self.assertEqual(process.returncode, 0)

    def test_installed_setup_preview_write_and_offline_diagnosis(self):
        auth = self.root / 'users'; auth.write_text('reader:synthetic'); auth.chmod(0o600)
        destination = self.root / 'server.yml'
        args = ('setup', '--output', str(destination), '--auth-file', str(auth))
        self.assertIn('Preview only', self.cli(*args).stdout)
        self.assertFalse(destination.exists())
        self.cli(*args, '--write')
        self.cli('check-config', '--config', str(destination))
        report = json.loads(self.cli('doctor', '--config', str(destination), '--json', code=2).stdout)
        self.assertTrue(report['configuration_valid'])
        self.assertIsNone(report['sources_ready'])
        self.assertFalse(report['probe_requested'])
        self.cli(*args, '--write', code=1)

    def test_installed_offline_retention_for_account_archive_and_configuration(self):
        self.accounts('source.db', 'init', '--subject', 'alice', '--administrator')
        archive=self.root/'archive.json'
        self.accounts('source.db', 'backup', '--output', str(archive))
        original=archive.read_bytes()
        report=json.loads(self.cli('retention','--accounts-backup',str(archive)).stdout)
        self.assertEqual(report['kind'],'account_archive')
        self.assertEqual(report['capacity']['audit_entries']['used'],1)
        self.assertFalse(report['purge_supported']);self.assertEqual(archive.read_bytes(),original)
        config=self.root/'config.yml'
        config.write_text('general:\n  auth_basic_file: absent\nsources: []\n');config.chmod(0o600)
        report=json.loads(self.cli('retention','--config',str(config)).stdout)
        self.assertFalse(report['runtime_validated']);self.assertFalse((self.root/'absent').exists())
