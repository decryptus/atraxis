"""Deployable configuration and a real native TLS inventory on a disposable port."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from atraxis.installation import deployment
from atraxis.installation.engine import Engine, serialize
from atraxis.installation.filesystem import DIRECTORY_FLAGS, Root
from atraxis.installation.validation import InstallError, parse
from test_installation import LOGIN, material, https_fixture, monit, plan, policy


class DeploymentTests(unittest.TestCase):
    def test_compose_scopes_mounts_and_contains_no_plaintext_login(self):
        data = parse(plan())
        with patch('os.geteuid', return_value=10001):
            files = deployment.render('compose', data, LOGIN, Path('/srv/atraxis/pilot'))
        self.assertNotIn(LOGIN['password'].encode(), b''.join(files.values()))
        self.assertIn(b'$6$rounds=200000$', files['users.htpasswd'])
        compose = json.loads(files['compose.yaml'])
        self.assertEqual(set(compose['services']), {'web', 'tls'})
        web, tls = (compose['services'][k] for k in ('web', 'tls'))
        self.assertNotIn('ports', web)
        self.assertEqual(tls['ports'], ['127.0.0.1:18443:8443'])
        web_sources = {v['source'] for v in web['volumes']}
        self.assertNotIn('./server.key', web_sources)
        self.assertEqual(web_sources, {'./atraxis.yml','./users.htpasswd',
                                     './source-monit.json','./source-monit.ca.pem'})
        self.assertTrue(all(v['read_only'] and not v['bind']['create_host_path']
                            for v in tls['volumes'] + web['volumes']))

    def test_mode_must_be_host_authorized_before_effects(self):
        with self.assertRaisesRegex(InstallError, 'deployment_denied'):
            Engine(policy()).prepare(serialize(plan()), login=LOGIN, source_credentials={},
                                     certificate=b'', private_key=b'', source_cas={}, deployment='systemd')

    def test_partial_manager_failure_has_no_retry(self):
        data = parse(plan())
        with patch.object(deployment, '_run', side_effect=InstallError('manager_unavailable')) as run:
            with self.assertRaisesRegex(InstallError, 'startup_uncertain'):
                deployment.start('systemd', data, Path('/srv/pilot'))
        self.assertEqual(run.call_count, 1)

    def test_native_service_real_tls_and_sources(self):
        with tempfile.TemporaryDirectory() as folder, https_fixture(payload=monit) as (url, ca, seen):
            root = Root(os.open(folder, DIRECTORY_FLAGS))
            self.addCleanup(root.close)
            doc = plan(url)
            with socket.socket() as sock:
                sock.bind(('127.0.0.1', 0))
                doc['listen_port'] = sock.getsockname()[1]
            cert, key, _ = material()
            def launch(mode, parsed, directory):
                self.assertEqual(mode, 'systemd')
                self.assertNotIn(LOGIN['password'].encode(), (directory / 'users.htpasswd').read_bytes())
                self.assertFalse((directory / 'login.json').exists())
                # Use the source checkout in tests, even in an uninstalled repository.
                env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1]))
                command = [sys.executable, '-m', 'atraxis', 'serve', '--config', 'atraxis.yml',
                           '--tls-cert', 'server.crt', '--tls-key', 'server.key']
                daemon = subprocess.Popen(command, cwd=directory, env=env,
                                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                def stop():
                    daemon.terminate()
                    daemon.wait(timeout=15)
                self.addCleanup(stop)
            # CI exercises actual systemd and Docker. This test isolates the service payload.
            with patch.object(deployment, 'preflight'), patch.object(deployment, 'start', side_effect=launch):
                result = Engine(policy(url), root, deployment_modes=('systemd',)).prepare(
                    serialize(doc), login=LOGIN, source_credentials={'monit': LOGIN}, certificate=cert,
                    private_key=key, source_cas={'monit': ca}, deployment='systemd')
            self.assertTrue(result['services_started'])
            self.assertGreaterEqual(len(seen), 2)
