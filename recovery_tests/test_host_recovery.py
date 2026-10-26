"""Restore synthetic state on a different CI VM without the original source host."""
import base64
import json
import os
from pathlib import Path
import ssl
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
import uuid

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'compose_tests'))
from cold_volume import restore
from operator_access import ACCOUNT_SNAPSHOT, CLIENT as READ_RULE, private
from recovery_transfer import TRANSFER, IMAGES, command, confirm_images, identity, verify
from worker_profiles import CLIENT, INSPECT, assert_restored_workers
import uncertain_recovery

READ_LIMIT = 65536
DEADLINE_SECONDS = 60
HTTPS_ORIGIN = 'https://localhost:{port}'
RULE_PATH = '/ui/rules?name=compose-rule'
PENDING_EVENT = 'recovery-pending'
CONFIG_PATH = '/etc/atropos/engine.yml'
ACCOUNT_SNAPSHOTS = '''from atraxis.account_store import AccountStore
import json
print(json.dumps(AccountStore('/var/lib/atraxis/accounts.db').snapshot(), sort_keys=True))
'''
RULE_SNAPSHOT = '''from atropos_client.client import RuleClient
import json, sys
c = RuleClient('/etc/atraxis/engine-client.yml', expected_actor={'subject':'alice','organization':'lab'})
try:
    print(json.dumps(dict(rule=c.read('compose-rule', sys.argv[1]),
        history=c.history('compose-rule'), audit=c.audit('compose-rule')), sort_keys=True))
finally:
    c.close()
'''
INTEGRITY = '''import sqlite3, sys
for path in sys.argv[1:]:
    with sqlite3.connect('file:'+path+'?mode=ro',uri=True) as connection:
        assert connection.execute('pragma integrity_check').fetchall()==[('ok',)]
        assert connection.execute('pragma foreign_key_check').fetchall()==[]
print('SQLITE_INTEGRITY verified=true')
'''
REVOKED_ACTION = '''from atropos.rule_execution import configured_runtime
import sys
with configured_runtime('/etc/atropos/engine.yml') as runtime:
    try:
        runtime.worker().authorizer(runtime.store.get(sys.argv[1]))
    except PermissionError as error:
        assert str(error) == 'dispatch_action_denied'
    else:
        raise AssertionError('restored_action_still_authorized')
print('RESTORED_ACTION_PERMISSION denied=true')
'''


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class HostRecoveryTests(unittest.TestCase):
    def test_restore_on_another_host_without_rebuilding_or_resuming(self):
        destination = identity()
        commit = command('git', '-C', str(ROOT), 'rev-parse', 'HEAD')
        expected = os.environ['ATRAXIS_TRANSFER_SHA256']
        manifest = verify(TRANSFER, expected, commit, destination)
        context = json.loads((TRANSFER / 'context.json').read_text())
        # Only test-owned transfer files; make the read-only helper mount private.
        for path in [TRANSFER, *TRANSFER.iterdir()]:
            os.chown(path, 10001, 10001)
            path.chmod(0o700 if path.is_dir() else 0o600)
        for name in IMAGES:
            self.assertNotEqual(subprocess.run(['docker', 'image', 'inspect', name],
                capture_output=True, timeout=30).returncode, 0)
        for role in ('web', 'engine'):
            self.assertNotEqual(subprocess.run(['docker', 'volume', 'inspect',
                manifest['source_project'] + '_' + role + '-data'],
                capture_output=True, timeout=30).returncode, 0)
        # No registry pull or image rebuild: use the exact source-run image IDs.
        command('docker', 'image', 'load', '--input', str(TRANSFER / 'images.tar'), timeout=180)
        confirm_images(manifest)
        with tempfile.TemporaryDirectory(prefix='atraxis-host-recovery-') as temporary:
            root = Path(temporary)
            root.chmod(0o755)
            site = root / 'site'
            site.mkdir(mode=0o755)
            self.assertEqual(restore(site, TRANSFER / 'site.tar',
                manifest['files']['site.tar']['sha256']), manifest['fingerprints']['site'])
            project = 'recovery-test-' + uuid.uuid4().hex[:12]
            prefix = ['docker', 'compose', '-p', project, '-f', str(ROOT / 'deploy/compose/compose.yml')]
            env = dict(os.environ, ATROPOS_SOURCE=str(ROOT), ATRAXIS_SITE_DIR=str(site),
                ATRAXIS_BIND_ADDRESS='127.0.0.1', ATRAXIS_HTTPS_PORT='0')

            def compose(*args, timeout=120, check=True):
                result = subprocess.run(prefix + list(args), env=env,
                    capture_output=True, text=True, timeout=timeout)
                if check and result.returncode:
                    self.fail('host_recovery_compose_failed')
                return result

            def request(path, username='alice', password=None):
                secret = context['password'] if password is None else password
                port = compose('port', 'web-tls', '8443').stdout.strip().rsplit(':', 1)[1]
                auth = base64.b64encode((username + ':' + secret).encode()).decode()
                req = urllib.request.Request(HTTPS_ORIGIN.format(port=port) + path,
                    headers={'Authorization': 'Basic ' + auth})
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                    NoRedirect(), urllib.request.HTTPSHandler(context=ssl.create_default_context(cadata=context['ca'])))
                try:
                    response = opener.open(req, timeout=5)
                except urllib.error.HTTPError as error:
                    response = error
                with response:
                    body = response.read(READ_LIMIT + 1)
                    self.assertLessEqual(len(body), READ_LIMIT)
                    return response.code, body

            def read_client(operation, **arguments):
                return json.loads(compose('exec', '-T', 'web', 'python', '-c', CLIENT,
                    operation, json.dumps(arguments)).stdout)

            def wait_read(callback, predicate):
                deadline = time.monotonic() + DEADLINE_SECONDS
                while time.monotonic() < deadline:
                    try:
                        value = callback()
                        if predicate(value):
                            return value
                    except (OSError, ValueError):
                        pass
                    time.sleep(0.5)
                self.fail('host_recovery_read_deadline')

            def volume(operation, role, archive_digest=None, check=True):
                args = ['docker', 'run', '--rm', '-i', '--network', 'none', '--read-only',
                    '--user', '10001:10001', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
                    '--memory', '512m', '--cpus', '2', '--pids-limit', '128',
                    '--mount', 'type=volume,source=' + project + '_' + role + '-data,target=/data' +
                        (',readonly' if operation != 'restore' else ''),
                    '--mount', 'type=bind,source=' + str(TRANSFER) + ',target=/backup,readonly',
                    '--entrypoint', 'python', manifest['images'][IMAGES[0]], '-', operation, role]
                if archive_digest is not None:
                    args.append(archive_digest)
                result = subprocess.run(args, input=(ROOT / 'compose_tests/cold_volume.py').read_text(),
                    capture_output=True, text=True, timeout=60)
                if check and result.returncode:
                    self.fail('host_recovery_volume_failed')
                return json.loads(result.stdout) if check else result

            try:
                compose('config', '--quiet')
                compose('up', '-d', '--no-build', '--pull', 'never', 'web-tls', 'engine-tls')
                for role, directory in (('web', '/var/lib/atraxis'), ('engine', '/var/lib/atropos')):
                    compose('run', '--rm', '--no-deps', '--pull', 'never', '--user', '0:0', '--cap-add', 'CHOWN',
                        '--entrypoint', 'sh', role, '-c', 'chmod 700 ' + directory + ' && chown 10001:10001 ' + directory)
                    self.assertEqual(volume('fingerprint', role), {})
                    self.assertEqual(volume('restore', role, manifest['files'][role + '.tar']['sha256']),
                        manifest['fingerprints'][role])
                    refused = volume('restore', role, manifest['files'][role + '.tar']['sha256'], check=False)
                    self.assertNotEqual(refused.returncode, 0)
                    self.assertIn('restore_destination_not_empty', refused.stderr)
                    self.assertEqual(volume('fingerprint', role), manifest['fingerprints'][role])
                compose('up', '-d', '--no-build', '--pull', 'never')
                wait_read(lambda: request('/healthz'), lambda result: result[0] == 200)
                wait_read(lambda: request(RULE_PATH), lambda result: result[0] == 200)
                self.assertEqual(request('/healthz', password='invalid')[0], 401)
                self.assertEqual(request('/healthz', password=context['bob_password'])[0], 401)
                self.assertEqual(request(RULE_PATH, 'bob', context['bob_password'])[0], 403)
                self.assertEqual(request('/readyz')[0], 503)
                self.assertEqual(json.loads(compose('exec', '-T', 'web', 'python', '-c',
                    RULE_SNAPSHOT, context['revision']).stdout), context['rule'])
                self.assertEqual(json.loads(compose('exec', '-T', 'web', 'python', '-c',
                    ACCOUNT_SNAPSHOTS).stdout), context['accounts'])
                self.assertEqual(json.loads(compose('exec', '-T', 'web', 'python', '-c',
                    ACCOUNT_SNAPSHOT).stdout), context['reader'])
                previous = json.loads(compose('exec', '-T', 'web', 'python', '-c', READ_RULE,
                    '/etc/atraxis/previous-client.yml', context['revision']).stdout)
                self.assertEqual(previous, {'denied': True})
                assert_restored_workers(self, compose, context['workers'])
                compose('exec', '-T', 'engine', 'python', '-c', INTEGRITY, '/var/lib/atropos/engine.db')
                compose('exec', '-T', 'web', 'python', '-c', INTEGRITY,
                    '/var/lib/atraxis/accounts.db', '/var/lib/atraxis/accounts.sqlite3')
                ids = compose('ps', '-q').stdout.split()
                self.assertEqual(len(ids), 4)
                containers = json.loads(command('docker', 'inspect', *ids))
                for container in containers:
                    self.assertIn(container['Image'], manifest['images'].values())
                    self.assertEqual(container['Config']['User'], '10001:10001')
                    self.assertTrue(container['HostConfig']['ReadonlyRootfs'])
                    self.assertEqual(container['HostConfig']['RestartPolicy']['Name'], 'no')
                    self.assertFalse(container['State']['OOMKilled'])
                    self.assertEqual(container['HostConfig']['Memory'], 512 * 1024 * 1024)
                    self.assertEqual(container['HostConfig']['NanoCpus'], 2 * 1000000000)
                    self.assertEqual(container['HostConfig']['PidsLimit'], 128)
                    self.assertFalse(container['HostConfig']['Privileged'])
                    self.assertIn('ALL', container['HostConfig']['CapDrop'])
                # Starting a worker after restoration must not remove the stored pause.
                compose('--profile', 'execution', 'up', '-d', '--no-build', '--pull', 'never', '--no-deps', 'worker')
                self.assertEqual(len(compose('ps', '-q', 'worker').stdout.split()), 1)
                for _ in range(3):
                    self.assertEqual(read_client('status', event=PENDING_EVENT), context['workers'][PENDING_EVENT])
                    time.sleep(1)
                uncertain_recovery.preserved(self, compose, context['workers']['uncertain'], probe=True)
                compose('stop', 'worker')
                # Explicitly revoke the synthetic action before the test operator resumes.
                config = site / 'engine/engine.yml'
                policy = yaml.safe_load(config.read_text())
                policy['grants'][0]['actions'] = []
                private(config, yaml.safe_dump(policy))
                # Check the actual denial reason: age may also have expired in transit.
                compose('exec', '-T', 'engine', 'python', '-c', REVOKED_ACTION,
                    context['workers'][PENDING_EVENT]['id'])
                compose('exec', '-T', 'engine', 'atropos', 'storage-resume', '--config', CONFIG_PATH, '--confirm-reconciled')
                compose('--profile', 'execution', 'up', '-d', '--no-build', '--pull', 'never', '--no-deps', 'worker')
                refused = wait_read(lambda: read_client('status', event=PENDING_EVENT), lambda result: result['status'] == 'cancelled')
                self.assertEqual(refused['id'], context['workers'][PENDING_EVENT]['id'])
                run = json.loads(compose('exec', '-T', 'engine', 'python', '-c', INSPECT, 'run', refused['id']).stdout)
                self.assertEqual(run['results'], {})
                self.assertEqual(run['error'], 'execution_authorization_denied')
                compose('stop', 'worker')
                uncertain_evidence = uncertain_recovery.reconcile(
                    self, compose, site, context['workers']['uncertain'])
                compose('--profile', '*', 'stop')
                self.assertEqual(compose('ps', '--status', 'running', '-q').stdout.strip(), '')
                confirm_images(manifest)
                # Original transfer evidence is read-only throughout restoration.
                verify(TRANSFER, expected, commit, destination)
                evidence = dict(version=1, source_commit=commit, manifest_sha256=expected,
                    different_boot=True, different_vm=True, no_image_rebuild=True,
                    daemon_id_reused=manifest['source']['daemon'] == destination['daemon'],
                    archive_bytes_and_modes_verified=True, rule_and_account_audits_preserved=True,
                    revoked_login_token_membership_denied=True, terminal_work_preserved=True,
                    queued_work_and_pause_preserved=True, post_restore_action_revocation_enforced=True,
                    synthetic_only=True, uncertain_work_exercised=True, live_site_qualified=False,
                    uncertainty=uncertain_evidence, external_target_survival_qualified=False)
                output = ROOT / 'acceptance-artifacts'
                output.mkdir(exist_ok=True)
                (output / 'host-recovery.json').write_text(json.dumps(evidence, indent=2) + '\n')
                print('HOST_RECOVERY_REPORT ' + json.dumps(evidence, sort_keys=True), flush=True)
            finally:
                compose('--profile', '*', 'down', '--volumes', '--remove-orphans', check=False)
