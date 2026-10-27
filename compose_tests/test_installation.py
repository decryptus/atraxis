"""Real operator Compose, clean volumes and synthetic verified HTTPS only."""
import base64
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import secrets
import ssl
import subprocess
import tempfile
import time
import unittest
import urllib.error
import urllib.request
import uuid

import yaml
from cold_volume import fingerprint, pack, restore
from worker_profiles import exercise_workers, assert_restored_workers
from operator_access import exercise_operator_access, ACCOUNT_SNAPSHOT
from recovery_transfer import capture
import inventory_recovery

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('network', ROOT / 'scripts/docker-network-acceptance.py')
network = importlib.util.module_from_spec(spec)
spec.loader.exec_module(network)


class InstallationTests(unittest.TestCase):
    def test_verified_https_recreation_and_cold_restore_into_empty_volumes(self):
        with tempfile.TemporaryDirectory(prefix='atraxis-compose-') as temporary:
            root = Path(temporary)
            root.chmod(0o755)
            site = root / 'site'
            site.mkdir(mode=0o755)
            for name in ('web', 'engine', 'web-tls', 'engine-tls'):
                (site / name).mkdir()
            ca = network.Authority('disposable compose CA')
            for role, host in (('web', 'localhost'), ('engine', 'engine-tls')):
                ca.leaf(site / (role + '-tls'), 'server', host)
            # Existing leaf helper names .crt/.key exactly as the operator proxy expects.
            password, token, bob_token = [secrets.token_urlsafe(32) for _ in range(3)]
            bob_password = password
            def write(role, name, content):
                path = site / role / name
                path.write_text(content if isinstance(content, str) else yaml.safe_dump(content))
            digest = base64.b64encode(hashlib.sha1(password.encode()).digest()).decode()
            write('web', 'users.htpasswd', 'alice:{SHA}' + digest + '\nbob:{SHA}' + digest + '\n')
            write('web', 'atraxis.yml', dict(general=dict(listen_addr='127.0.0.1', listen_port=9810,
                max_workers=4, auth_basic_file='/etc/atraxis/users.htpasswd',
                rules_web_file='/etc/atraxis/web-rules.yml'), sources=[]))
            write('web', 'web-rules.yml', dict(version=1,
                accounts_file=dict(backend='sqlite', url='sqlite:////var/lib/atraxis/accounts.db'),
                identities=[dict(username=user, subject=user, organization='lab',
                    atropos_client='engine-client.yml' if user == 'alice' else 'bob-client.yml')
                    for user in ('alice', 'bob')]))
            write('web', 'engine-client.yml', dict(server=dict(url='https://engine-tls:8443'),
                token_file='engine.token', ca_file='engine-ca.pem', timeout=15))
            write('web', 'engine.token', token)
            write('web', 'bob-client.yml', dict(server=dict(url='https://engine-tls:8443'),
                token_file='bob.token', ca_file='engine-ca.pem', timeout=15))
            write('web', 'bob.token', bob_token)
            write('web', 'engine-ca.pem', ca.pem().decode())
            write('engine', 'engine.yml', dict(version=1, namespace='composeacceptance',
                storage=dict(backend='sqlite', url='sqlite:////var/lib/atropos/engine.db'),
                queue=dict(backend='storage'), identities=[dict(token_sha256=hashlib.sha256(secret.encode()).hexdigest(),
                    subject=user, organization='lab') for user, secret in (('alice', token), ('bob', bob_token))],
                resources=[dict(organization='lab', kind='server', id='host')],
                grants=[dict(subject='alice', organization='lab', rule_kind='intervention',
                    operations=['read', 'create', 'save'], scopes=[['server', 'host']], providers=['internal']),
                    dict(subject='bob', organization='lab', rule_kind='intervention', operations=['read'],
                         scopes=[['server', 'host']], providers=['internal'])]))
            # Test-owned files only; the product does not provision credentials.
            for directory in site.iterdir():
                os.chown(directory, 10001, 10001)
                directory.chmod(0o700)
                for path in directory.iterdir():
                    os.chown(path, 10001, 10001)
                    path.chmod(0o600)
            env = dict(os.environ, ATROPOS_SOURCE=str(ROOT / '.acceptance/engine'),
                ATRAXIS_SITE_DIR=str(site), ATRAXIS_BIND_ADDRESS='127.0.0.1', ATRAXIS_HTTPS_PORT='0')
            project = 'compose-test-' + uuid.uuid4().hex[:12]
            restored_project = project + '-restored'
            prefix = ['docker', 'compose', '-p', project, '-f', str(ROOT / 'deploy/compose/compose.yml')]
            def compose(*args, timeout=120, check=True):
                result = subprocess.run(prefix + list(args), env=env, capture_output=True,
                    text=True, timeout=timeout)
                if check and result.returncode:
                    self.fail('compose_command_failed\n' + result.stdout + result.stderr)
                return result
            try:
                compose('config', '--quiet')
                compose('build', timeout=900)
                compose('up', '-d', '--no-build', 'web-tls', 'engine-tls')
                # Explicit, narrowly scoped initialization of new disposable volumes.
                for role in ('web', 'engine'):
                    compose('run', '--rm', '--no-deps', '--user', '0:0', '--cap-add', 'CHOWN', '--entrypoint', 'sh', role,
                        '-c', 'chmod 700 /var/lib/' + ('atraxis' if role == 'web' else 'atropos') +
                        ' && chown 10001:10001 /var/lib/' + ('atraxis' if role == 'web' else 'atropos'))
                accounts = '''from atraxis.accounts import Directory, default_directory
from atraxis.account_store import AccountStore
import json, sys
s = AccountStore('/var/lib/atraxis/accounts.db')
if sys.argv[1] == 'init':
    d = default_directory('alice').document()
    d['organizations'][0]['id'] = 'lab'
    d['memberships'][0]['organization'] = 'lab'
    d['users'].append(dict(id='bob', name='Bob', enabled=True))
    d['memberships'].append(dict(user='bob', organization='lab', group=None))
    s.apply(Directory(d), None)
elif sys.argv[1] == 'revoke':
    old = s.snapshot()
    d = old['directory']
    d['users'][1]['enabled'] = False
    s.apply(Directory(d), old['revision'])
print(json.dumps(s.snapshot(), sort_keys=True))
'''
                compose('run', '--rm', '--no-deps', '--entrypoint', 'python', 'web', '-c', accounts, 'init')
                compose('up', '-d', '--no-build')
                def request(path, secret=None, username='alice', authority=None):
                    secret = (bob_password if username == 'bob' else password) if secret is None else secret
                    trust = ca if authority is None else authority
                    port = compose('port', 'web-tls', '8443').stdout.strip().rsplit(':', 1)[1]
                    auth = base64.b64encode((username + ':' + secret).encode()).decode()
                    request = urllib.request.Request('https://localhost:' + port + path,
                        headers={'Authorization': 'Basic ' + auth})
                    try:
                        with urllib.request.urlopen(request, timeout=5,
                            context=ssl.create_default_context(cadata=trust.pem().decode())) as response:
                            body = response.read(65537)
                            self.assertLessEqual(len(body), 65536)
                            return response.status, body
                    except urllib.error.HTTPError as error:
                        body = error.read(65537)
                        self.assertLessEqual(len(body), 65536)
                        return error.code, body
                deadline = time.monotonic() + 60
                while True:
                    try:
                        if request('/healthz')[0] == 200:
                            break
                    except (OSError, ValueError):
                        pass
                    if time.monotonic() > deadline:
                        self.fail('compose_startup_deadline')
                    time.sleep(0.5)
                self.assertEqual(request('/healthz', 'wrong-password')[0], 401)
                self.assertEqual(request('/readyz')[0], 503)  # No invented ready inventory.
                self.assertEqual(request('/ui/rules')[0], 200)
                probe = '''from atropos_client.client import RuleClient
import json, sys
c = RuleClient('/etc/atraxis/engine-client.yml', expected_actor={'subject':'alice','organization':'lab'})
try:
    if sys.argv[1] == 'create':
        rule = c.create_from_templates(['cpu-high'], name='compose-rule', rule_kind='intervention', selections=[['server','host']])
        print(json.dumps(rule, sort_keys=True))
    else:
        print(json.dumps(dict(rule=c.read('compose-rule', sys.argv[1]),
            history=c.history('compose-rule'), audit=c.audit('compose-rule')), sort_keys=True))
finally:
    c.close()
'''
                created = json.loads(compose('exec', '-T', 'web', 'python', '-c', probe, 'create').stdout)
                revision = created['revision']
                before = json.loads(compose('exec', '-T', 'web', 'python', '-c', probe, revision).stdout)
                self.assertEqual(request('/ui/rules?name=compose-rule', username='bob')[0], 200)
                account_before = json.loads(compose('exec', '-T', 'web', 'python', '-c', accounts, 'revoke').stdout)
                self.assertEqual(len(account_before['audit']), 2)
                self.assertEqual(request('/ui/rules?name=compose-rule', username='bob')[0], 403)
                password, ca, reader_snapshot = exercise_operator_access(
                    self, compose, site, ca, network, request, password, revision)
                compose('up', '-d', '--no-build', '--force-recreate')
                # Transport startup probes are read-only; a mutation is never retried.
                deadline = time.monotonic() + 60
                while True:
                    result = compose('exec', '-T', 'web', 'python', '-c', probe, revision, check=False)
                    if result.returncode == 0:
                        break
                    if time.monotonic() > deadline:
                        self.fail('compose_recreation_deadline')
                    time.sleep(0.5)
                self.assertEqual(json.loads(result.stdout), before)
                self.assertEqual(request('/ui/rules')[0], 200)
                self.assertEqual(request('/ui/rules?name=compose-rule', username='bob')[0], 403)
                integrity = "import sqlite3; c=sqlite3.connect('file:/var/lib/atropos/engine.db?mode=ro',uri=True); assert c.execute('pragma integrity_check').fetchall()==[('ok',)]; assert c.execute('pragma foreign_key_check').fetchall()==[]; c.close()"
                compose('exec', '-T', 'engine', 'python', '-c', integrity)
                def check_runtime():
                    ids = compose('ps', '-q').stdout.split()
                    self.assertEqual(len(ids), 4)
                    inspected = json.loads(subprocess.run(['docker', 'inspect', *ids],
                        capture_output=True, text=True, check=True, timeout=30).stdout)
                    for container in inspected:
                        self.assertFalse(container['State']['OOMKilled'])
                        self.assertTrue(container['HostConfig']['ReadonlyRootfs'])
                        self.assertEqual(container['HostConfig']['Memory'], 512 * 1024 * 1024)
                        self.assertEqual(container['HostConfig']['PidsLimit'], 128)
                        self.assertEqual(container['Config']['User'], '10001:10001')
                check_runtime()
                worker_evidence = exercise_workers(self, compose, site, revision)
                before = json.loads(compose('exec', '-T', 'web', 'python', '-c', probe, revision).stdout)
                check_runtime()
                inventory_request = lambda path, username, password: request(path, password, username)
                inventory_evidence, reader_snapshot = inventory_recovery.prepare(
                    self, compose, site, ca, inventory_request)
                compose('stop')
                self.assertEqual(compose('ps', '--status', 'running', '-q').stdout.strip(), '')
                backup = root / 'backup'
                backup.mkdir(mode=0o700)
                os.chown(backup, 10001, 10001)
                archive_script = (ROOT / 'compose_tests/cold_volume.py').read_text()
                def volume(operation, owner, role, digest=None, check=True):
                    name = owner + '_' + role + '-data'
                    subprocess.run(['docker', 'volume', 'inspect', name], capture_output=True,
                        text=True, check=True, timeout=30)
                    args = ['docker', 'run', '--rm', '-i', '--network', 'none', '--read-only',
                        '--user', '10001:10001', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
                        '--memory', '512m', '--cpus', '2', '--pids-limit', '128',
                        '--mount', 'type=volume,source=' + name + ',target=/data' +
                            (',readonly' if operation != 'restore' else ''),
                        '--mount', 'type=bind,source=' + str(backup) + ',target=/backup' +
                            (',readonly' if operation != 'pack' else ''),
                        '--entrypoint', 'python', 'atraxis-install-web:local', '-', operation, role]
                    if digest is not None:
                        args.append(digest)
                    result = subprocess.run(args, input=archive_script, capture_output=True,
                        text=True, timeout=60)
                    if check and result.returncode:
                        self.fail('cold_volume_operation_failed\n' + result.stdout + result.stderr)
                    return json.loads(result.stdout) if check else result
                source_fingerprints = {role: volume('fingerprint', project, role) for role in ('web', 'engine')}
                archive_digests = {role: volume('pack', project, role) for role in ('web', 'engine')}
                site_fingerprint = fingerprint(site)
                site_digest = pack(site, backup / 'site.tar')
                restored_site = root / 'restored-site'
                restored_site.mkdir(mode=0o755)
                self.assertEqual(restore(restored_site, backup / 'site.tar', site_digest), site_fingerprint)
                env['ATRAXIS_SITE_DIR'] = str(restored_site)
                prefix[3] = restored_project
                compose('up', '-d', '--no-build', 'web-tls', 'engine-tls')
                for role in ('web', 'engine'):
                    directory = '/var/lib/' + ('atraxis' if role == 'web' else 'atropos')
                    compose('run', '--rm', '--no-deps', '--user', '0:0', '--cap-add', 'CHOWN',
                        '--entrypoint', 'sh', role, '-c', 'chmod 700 ' + directory +
                        ' && chown 10001:10001 ' + directory)
                    self.assertEqual(volume('fingerprint', restored_project, role), {})
                    # Wrong evidence cannot populate an empty destination.
                    refused = volume('restore', restored_project, role, '0' * 64, check=False)
                    self.assertNotEqual(refused.returncode, 0)
                    self.assertIn('archive_digest_mismatch', refused.stderr)
                    self.assertEqual(volume('fingerprint', restored_project, role), {})
                    self.assertEqual(volume('restore', restored_project, role, archive_digests[role]),
                        source_fingerprints[role])
                    refused = volume('restore', restored_project, role, archive_digests[role], check=False)
                    self.assertNotEqual(refused.returncode, 0)
                    self.assertIn('restore_destination_not_empty', refused.stderr)
                    self.assertEqual(volume('fingerprint', restored_project, role), source_fingerprints[role])
                compose('up', '-d', '--no-build')
                deadline = time.monotonic() + 60
                while True:
                    result = compose('exec', '-T', 'web', 'python', '-c', probe, revision, check=False)
                    if result.returncode == 0:
                        break
                    if time.monotonic() > deadline:
                        self.fail('cold_restore_startup_deadline')
                    time.sleep(0.5)
                self.assertEqual(json.loads(result.stdout), before)
                assert_restored_workers(self, compose, worker_evidence)
                self.assertEqual(json.loads(compose('exec', '-T', 'web', 'python', '-c', accounts, 'snapshot').stdout), account_before)
                self.assertEqual(json.loads(compose('exec', '-T', 'web', 'python', '-c', ACCOUNT_SNAPSHOT).stdout), reader_snapshot)
                self.assertEqual(request('/ui/rules?name=compose-rule')[0], 200)
                self.assertEqual(request('/ui/rules?name=compose-rule', username='bob')[0], 403)
                self.assertEqual(request('/healthz', 'wrong-password')[0], 401)
                inventory_recovery.unavailable(self, inventory_request, inventory_evidence)
                compose('exec', '-T', 'engine', 'python', '-c', integrity)
                compose('exec', '-T', 'web', 'python', '-c', integrity.replace('/var/lib/atropos/engine.db', '/var/lib/atraxis/accounts.db'))
                # A restored configuration is not a cached authorization grant.
                engine_config = restored_site / 'engine/engine.yml'
                document = yaml.safe_load(engine_config.read_text())
                document['grants'] = []
                pending = engine_config.with_suffix('.next')
                pending.write_text(yaml.safe_dump(document))
                pending.chmod(0o600)
                os.chown(pending, 10001, 10001)
                pending.replace(engine_config)
                self.assertEqual(request('/ui/rules?name=compose-rule')[0], 403)
                check_runtime()
                self.assertEqual(fingerprint(site), site_fingerprint)
                for role in ('web', 'engine'):
                    self.assertEqual(volume('fingerprint', project, role), source_fingerprints[role])
                    self.assertEqual(hashlib.sha256((backup / (role + '.tar')).read_bytes()).hexdigest(), archive_digests[role])
                if os.environ.get('ATRAXIS_EXPORT_RECOVERY') == '1':
                    capture(backup, dict(revision=revision, rule=before, accounts=account_before,
                        reader=reader_snapshot, workers=worker_evidence, password=password,
                        bob_password=bob_password, ca=ca.pem().decode(), inventory=inventory_evidence), project,
                        dict(archive_digests, site=site_digest), dict(source_fingerprints, site=site_fingerprint))
                print('COMPOSE_REPORT verified_tls=true cases=1 skipped=0 recovery_verified=true cold_restore_verified=true account_audit_preserved=true revoked_access_denied=true current_engine_rights_checked=true source_unchanged=true sqlite_integrity=true synthetic_only=true', flush=True)
            finally:
                # Only these two unique test-owned projects may be removed.
                for owner in (restored_project, project):
                    prefix[3] = owner
                    env['ATRAXIS_SITE_DIR'] = str(root / 'restored-site' if owner == restored_project and (root / 'restored-site').exists() else site)
                    compose('--profile', '*', 'down', '--volumes', '--remove-orphans', check=False)
            if os.environ.get('ATRAXIS_EXPORT_RECOVERY') == '1':
                for owner in (restored_project, project):
                    prefix[3] = owner
                    self.assertEqual(compose('ps', '-a', '-q').stdout.strip(), '')
                    for role in ('web', 'engine'):
                        self.assertNotEqual(subprocess.run(['docker', 'volume', 'inspect',
                            owner + '_' + role + '-data'], capture_output=True, timeout=30).returncode, 0)
                print('RECOVERY_SOURCE_RELEASED projects_removed=true volumes_removed=true synthetic_only=true', flush=True)
