#!/usr/bin/env python3
"""Full installed Atraxis → Atropos/SQLite+Redis → Auton journey in containers.

Synthetic source and alert contracts, real services, private ephemeral CA, no host
ports or external effects. Run through unittest; missing prerequisites fail.
"""
import argparse
import base64
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import secrets
import subprocess
import tempfile
import time
import unittest
import uuid
from types import SimpleNamespace

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import yaml

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('network_acceptance', ROOT / 'scripts/docker-network-acceptance.py')
network = importlib.util.module_from_spec(spec)
spec.loader.exec_module(network)
STATE_TMPFS = ['--tmpfs', '/state:rw,nosuid,nodev,size=128m,uid=10001,gid=10001,mode=0700']
PERSISTENT_ROLES = ('web', 'operator', 'engine', 'redis', 'auton', 'alert')
IMAGES = dict(web='atraxis-chain:ci', operator='atraxis-chain:ci', engine='atropos-chain:ci',
              redis='atropos-chain:ci', auton='auton-chain:ci', alert='atraxis-acceptance:ci', source='atraxis-acceptance:ci')
OPTIONS = SimpleNamespace(auton_source=Path(os.environ['AUTON_TEST_SOURCE'])) if 'AUTON_TEST_SOURCE' in os.environ else None
OPERATIONS = ['read', 'create', 'update', 'validate', 'test', 'import', 'export', 'save',
              'activate', 'deactivate', 'rollback', 'evaluate', 'archive', 'unarchive', 'delete', 'execute']


def docker(*args, expected=0, timeout=40, input=None):
    result = subprocess.run(['docker', *args], input=input, capture_output=True, text=True, timeout=timeout)
    if expected is not None and result.returncode != expected:
        raise AssertionError('Docker acceptance command failed:\n' + result.stdout + result.stderr)
    return result


def write(root, name, document):
    (root / name).write_text(json.dumps(document) if not isinstance(document, str) else document)


def fixtures(root, *, certificate_hours=2, auton_result_ttl=None):
    ca = network.Authority('chain acceptance CA')
    password, token, alert_token, storage_password = [secrets.token_urlsafe(32) for _ in range(4)]
    key = Ed25519PrivateKey.generate()
    public = key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    private = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
    for role in ('source', 'web', 'operator', 'engine', 'redis', 'auton', 'alert'):
        directory = root / role
        directory.mkdir()
        (directory / 'ca.pem').write_bytes(ca.pem())
    for role, port in (('source', None), ('web', 9810), ('engine', 8082), ('auton', 8666), ('alert', 8081)):
        directory = root / (role if port is None else role + '-proxy')
        directory.mkdir(exist_ok=True)
        ca.leaf(directory, 'ok', role + '.test', lifetime_hours=certificate_hours)
        if port is not None:
            write(directory, 'nginx.conf', '''
pid /tmp/nginx.pid;
error_log /dev/stderr warn;
events { worker_connections 128; }
http {
  access_log /tmp/access.log;
  client_body_temp_path /tmp/client;
  proxy_temp_path /tmp/proxy;
  fastcgi_temp_path /tmp/fastcgi;
  uwsgi_temp_path /tmp/uwsgi;
  scgi_temp_path /tmp/scgi;
  server {
    listen 8443 ssl;
    ssl_certificate /fixtures/ok.crt;
    ssl_certificate_key /fixtures/ok.key;
    ssl_protocols TLSv1.2 TLSv1.3;
    location / {
      proxy_pass http://127.0.0.1:%s;
      proxy_set_header X-Forwarded-User "";
      proxy_set_header X-Remote-User "";
      proxy_next_upstream off;
      proxy_connect_timeout 2s;
      proxy_read_timeout 8s;
    }
  }
}
''' % port)
    digest = base64.b64encode(hashlib.sha1(password.encode()).digest()).decode()
    write(root / 'web', 'users', 'alice:{SHA}' + digest + '\n')
    write(root / 'web', 'source.json', dict(username='source-reader', password='synthetic-source-password'))
    write(root / 'web', 'access.yml', dict(version=3, store='accounts.db', administrators=[], audit=[],
        servers=[], resource_groups=[], identities=[dict(username='alice', subject='alice')],
        grants=[dict(organization='demo', subject_kind='user', subject='alice', targets=[dict(kind='source', id='monit')])]))
    write(root / 'web', 'web.yml', dict(general=dict(auth_basic_file='users', inventory_access_file='access.yml'),
        sources=[dict(source_id='monit', kind='monit_docker', origin='https://source.test:8443',
                      credentials_file='source.json', ca_file='ca.pem', timeout=2, poll_interval=2, max_age=15)]))
    write(root / 'operator', 'alice.token', token)
    write(root / 'operator', 'alice.yml', dict(origin='https://engine.test:8443', token_file='alice.token', ca_file='ca.pem', timeout=5))
    write(root / 'operator', 'inventory.json', dict(username='alice', password=password))
    write(root / 'operator', 'inventory.yml', dict(origin='https://web.test:8443', credentials_file='inventory.json', ca_file='ca.pem', timeout=5))
    write(root / 'operator', 'observation.pem', private)
    write(root / 'operator', 'facts.json', dict(cpu_percent=250))
    write(root / 'operator', 'operator.yml', dict(version=2,
        identities=[dict(uid=10001, subject='alice', organization='demo', atropos_client='alice.yml')],
        accounts_file='accounts.db', inventory_client='inventory.yml',
        observation_signing=dict(key_id='collector', private_key_file='observation.pem'),
        bindings=[dict(organization='demo', target=['container', 'web'], source_id='monit', native_id='a'*64)]))
    write(root / 'engine', 'observation.pub', public)
    write(root / 'engine', 'executor.password', password)
    write(root / 'engine', 'alert.token', alert_token)
    write(root / 'engine', 'bindings.yml', [])
    write(root / 'engine', 'engine.yml', dict(version=1, namespace='chainacceptance',
        storage=dict(backend='sqlite', url='sqlite:////state/engine.db'),
        queue=dict(backend='redis', url='redis://:' + storage_password + '@127.0.0.1:6379/0'),
        identities=[dict(token_sha256=hashlib.sha256(token.encode()).hexdigest(), subject='alice', organization='demo')],
        resources=[dict(organization='demo', kind='server', id='lab'),
                   dict(organization='demo', kind='container', id='web', server_id='lab')],
        grants=[dict(subject='alice', organization='demo', rule_kind='intervention', operations=OPERATIONS,
                     scopes=[['server', 'lab'], ['container', 'web']], providers=['internal', 'upload'],
                     actions=['ops.run', 'ops.notify'])],
        dispatch_bindings='bindings.yml', observation_keys={'collector': 'observation.pub'},
        test_alert_delivery=dict(backend='webhook', url='https://alert.test:8443', token_file='alert.token', ca_file='ca.pem'),
        execution_adapters={
            'ops.run': dict(backend='auton', target='demo/container/web', origin='https://auton.test:8443',
                endpoint='approved', username='operator', password_file='executor.password', ca_file='ca.pem', simulation='noop'),
            'ops.notify': dict(backend='alertmanager', target='demo/container/web', origin='https://alert.test:8443',
                token_file='alert.token', ca_file='ca.pem', labels={'alertname': 'InterventionComplete'}, simulation='test_receiver')}))
    write(root / 'redis', 'redis.conf', 'bind 127.0.0.1\nport 6379\ndir /state\nsave ""\nappendonly yes\nappendfsync always\nmaxmemory-policy noeviction\nrequirepass ' + storage_password + '\n')
    write(root / 'auton', 'users', 'operator:{SHA}' + digest + '\n')
    config = yaml.safe_load((OPTIONS.auton_source / 'etc/auton/auton.yml.example').read_text())
    config['general'].update(listen_addr='127.0.0.1', listen_port=8666, max_life_time=0, max_requests=0,
        auth_basic='Acceptance', auth_basic_file='/state/users',
        job_storage=dict(backend='sqlite', path='/state/jobs.db', timeout=3))
    if auton_result_ttl is not None:
        config['general']['result_ttl'] = auton_result_ttl
    config.pop('import_modules', None)
    config['modules'] = yaml.safe_load((OPTIONS.auton_source / 'etc/auton/modules/job.yml').read_text())
    config['endpoints'] = {'approved': dict(plugin='subproc', config=dict(prog='/usr/local/bin/python', timeout=90))}
    write(root / 'auton', 'auton.yml', config)
    write(root / 'alert', 'alert.token', alert_token)
    # Synthetic test credentials only; private runtime copies are owned by UID
    # 10001 with mode 0600. Each service only mounts its own disposable inputs.
    root.chmod(0o755)
    for path in root.rglob('*'):
        path.chmod(0o755 if path.is_dir() else 0o644)


class ChainFixture(unittest.TestCase):
    certificate_hours = 2
    auton_result_ttl = None

    def setUp(self):
        if OPTIONS is None:
            raise RuntimeError("Set AUTON_TEST_SOURCE to the pinned Auton checkout")
        temporary = tempfile.TemporaryDirectory(prefix='atraxis-chain-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        fixtures(self.root, certificate_hours=self.certificate_hours, auton_result_ttl=self.auton_result_ttl)
        self.prefix = 'atraxis-chain-' + uuid.uuid4().hex
        self.network = self.prefix + '-net'
        self.containers = []
        self.volumes = []
        self.addCleanup(self.close)
        docker('network', 'create', '--internal', self.network)
        self.state_volumes = {role: self.create_volume(role, 'state') for role in PERSISTENT_ROLES}
        for role in ('source', 'web', 'engine', 'redis', 'auton', 'alert', 'operator'):
            self.start_role(role)
        self.wait(lambda: self.exec('operator', 'python', '/node.py', 'catalog').returncode == 0)
        self.wait(lambda: self.inventory()['ready'])
        self.active = self.json('operator', 'python', '/node.py', 'activate')
        revisions = self.engine('execution-adapters')
        binding = dict(organization='demo', rule='observed', revision=self.active['revision'],
            result=dict(decision='review', matched_rule='combined'), input={}, max_age_seconds=300,
            adapter_revisions={name: entry['revision'] for name, entry in revisions.items()},
            observation=dict(key_id='collector', source_id='monit', native_id='a'*64, filesystem=None, max_age_seconds=60),
            workflow=dict(version=1, name='container-journey', start='run', steps={
                'run': {'action': 'ops.run', 'timeout': 5, 'with': {'args': ['-c',
                    "with open('/state/effects', 'a') as stream: stream.write('done\\n')"]}, 'next': 'notify'},
                'notify': {'action': 'ops.notify', 'timeout': 5, 'with': {'subject': 'Intervention completed'}}}))
        self.exec('engine', 'python', '-c',
            'from pathlib import Path; import sys; p=Path("/state/bindings.yml"); p.write_text(sys.stdin.read()); p.chmod(0o600)',
            input=json.dumps([binding]))
        self.binding = self.engine('rules-bindings')[0]['id']

    def create_volume(self, role, suffix):
        volume = self.name(role) + '-' + suffix
        docker('volume', 'create', volume)
        self.volumes.append(volume)
        provisioner = self.name(role) + '-volume-init'
        self.containers.append(provisioner)
        docker('run', '--name', provisioner, *network.HARDENING,
            '--user', '0:0', '--cap-add', 'CHOWN', '--cap-add', 'FOWNER', '--network', 'none',
            '-v', volume + ':/state', '--entrypoint', 'python', IMAGES[role], '-c',
            'import os; os.chown("/state", 10001, 10001); os.chmod("/state", 0o700)')
        docker('rm', provisioner)
        self.containers.remove(provisioner)
        return volume

    def start_role(self, role):
        network_args = (['--network', 'container:' + self.name('engine')] if role == 'redis' else
                        ['--network', self.network, '--network-alias', role + '.test'])
        state = ['-v', self.state_volumes[role] + ':/state'] if role in PERSISTENT_ROLES else STATE_TMPFS
        command = ['/network-fixture.py', 'source'] if role == 'source' else ['/node.py', 'start', role]
        self.containers.append(self.name(role))
        docker('run', '-d', '--name', self.name(role), *network.HARDENING, *state, *network_args,
            '-v', str(self.root / role) + ':/fixtures:ro',
            '-v', str(ROOT / 'scripts/chain/node.py') + ':/node.py:ro',
            '-v', str(ROOT / 'scripts/network/fixture.py') + ':/network-fixture.py:ro',
            '--entrypoint', 'python', IMAGES[role], *command)
        if role in ('web', 'engine', 'auton', 'alert'):
            proxy = role + '-proxy'
            self.containers.append(self.name(proxy))
            docker('run', '-d', '--name', self.name(proxy), *network.HARDENING,
                '--network', 'container:' + self.name(role),
                '-v', str(self.root / proxy) + ':/fixtures:ro', '--entrypoint', 'nginx',
                'atraxis-ingress:ci', '-c', '/fixtures/nginx.conf', '-g', 'daemon off;')

    def recreate(self):
        roles = ('engine', 'redis', 'auton')
        before = {role: docker('inspect', '--format', '{{.Id}}', self.name(role)).stdout for role in roles}
        # Namespace-sharing containers must be removed with their parent. Stop
        # at a completed worker checkpoint; this is not an in-flight crash test.
        for role in ('engine-proxy', 'redis', 'engine', 'auton-proxy', 'auton'):
            name = self.name(role)
            docker('stop', '--time', '10', name)
            self.assertEqual(docker('inspect', '--format', '{{.State.ExitCode}}', name).stdout.strip(), '0')
            docker('rm', name)
            self.containers.remove(name)
        for role in roles:
            self.start_role(role)
            self.assertNotEqual(docker('inspect', '--format', '{{.Id}}', self.name(role)).stdout, before[role])
        self.wait(lambda: self.exec('operator', 'python', '/node.py', 'catalog').returncode == 0)
        self.wait(lambda: self.exec('engine', 'python', '-c',
            'import json, redis; from pathlib import Path; c=json.loads(Path("/state/engine.yml").read_text()); '
            'assert redis.Redis.from_url(c["queue"]["url"]).ping()').returncode == 0)
        self.wait(lambda: isinstance(self.json('engine', 'python', '/node.py', 'auton-jobs'), list))
        self.assertEqual(self.engine('rules-bindings')[0]['id'], self.binding)

    def start_worker(self):
        name = self.name('worker')
        self.containers.append(name)
        docker('run', '-d', '--name', name, *network.HARDENING,
            '--network', 'container:' + self.name('engine'),
            '-v', self.state_volumes['engine'] + ':/state', '--entrypoint', 'python',
            IMAGES['engine'], '-m', 'atropos.cli', 'rules-worker', '--config', '/state/engine.yml', '--once')

    def kill_role(self, role):
        name = self.name(role)
        self.assertEqual(docker('inspect', '--format', '{{.State.Running}}', name).stdout.strip(), 'true')
        docker('kill', '--signal', 'KILL', name)
        self.assertEqual(docker('wait', name).stdout.strip(), '137')

    def remove_role(self, role):
        name = self.name(role)
        docker('rm', name)
        self.containers.remove(name)

    def recreate_killed_auton(self):
        name = self.name('auton')
        before = docker('inspect', '--format', '{{.Id}}', name).stdout
        docker('stop', '--time', '10', self.name('auton-proxy'))
        self.remove_role('auton-proxy')
        self.remove_role('auton')
        self.start_role('auton')
        self.assertNotEqual(docker('inspect', '--format', '{{.Id}}', name).stdout, before)
        self.wait(lambda: len(self.auton_jobs()) == 1)

    def auton_jobs(self):
        return self.json('engine', 'python', '/node.py', 'auton-jobs')

    def blocked_action(self):
        # Persist the synthetic effect before exposing the barrier. The external
        # job remains live until the test releases it or kills its container.
        program = """import os, time
from pathlib import Path
with open('/state/effects', 'a') as stream:
    stream.write('done\\n')
    stream.flush()
    os.fsync(stream.fileno())
Path('/state/action-started').touch()
deadline = time.monotonic() + 60
while not Path('/state/release-action').exists() and time.monotonic() < deadline:
    time.sleep(.05)
assert Path('/state/release-action').exists(), 'acceptance_barrier_expired'
"""
        self.exec('engine', 'python', '-c',
            'import json, sys; from pathlib import Path; p=Path("/state/bindings.yml"); b=json.loads(p.read_text()); '
            'b[0]["workflow"]["steps"]["run"].update(timeout=60, **{"with": {"args": ["-c", sys.stdin.read()]}}); '
            'p.write_text(json.dumps(b))', input=program)
        self.binding = self.engine('rules-bindings')[0]['id']

    def start_blocked_run(self, event):
        self.blocked_action()
        receipt = self.dispatch(event)
        self.admin('resolve', receipt['id'], '--outcome', 'failed', expected=2)
        self.assertEqual(self.status(event), receipt)
        self.start_worker()
        self.wait(lambda: self.exec('auton', 'python', '-c',
            'from pathlib import Path; assert Path("/state/action-started").exists()').returncode == 0)
        self.assertEqual(self.effects(), 1)
        self.assertEqual(self.status(event)['status'], 'running')
        jobs = self.auton_jobs()
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]['status'], 'processing')
        self.admin('resolve', receipt['id'], '--outcome', 'succeeded', expected=2)
        self.assertEqual(self.status(event)['status'], 'running')
        self.assertEqual(self.deliveries(), [])
        return receipt, jobs[0]['uid']

    def check_uncertainty_and_target_lock(self, event, receipt):
        uncertain = self.status(event)
        self.assertEqual(uncertain['id'], receipt['id'])
        self.assertEqual(uncertain['status'], 'uncertain')
        self.assertEqual([h['status'] for h in uncertain['history']], ['running', 'uncertain'])
        self.assertEqual([h['step'] for h in uncertain['history']], ['run', 'run'])
        # A new, independently authorized event can queue, but the retained
        # uncertain owner must prevent it from acquiring this target.
        other = event + '-following'
        following = self.dispatch(other)
        self.assertNotEqual(following['id'], receipt['id'])
        self.worker()
        self.assertEqual(self.status(other)['status'], 'queued')
        self.assertEqual(self.status(other)['history'], [])
        records = self.auton_records()
        self.assertEqual(len(records), 1)
        self.recreate()
        self.assertEqual(self.status(event), uncertain)
        self.assertEqual(self.auton_records(), records)
        for _ in range(3):
            self.worker()
        self.assertEqual(self.status(event), uncertain)
        self.assertEqual(self.status(other)['status'], 'queued')
        self.assertEqual(self.status(other)['history'], [])
        self.assertEqual(self.effects(), 1)
        self.assertEqual(self.deliveries(), [])
        self.assertEqual(len(self.auton_jobs()), 1)

    def admin(self, *arguments, expected=0):
        result = self.exec('engine', 'python', '/node.py', 'engine-admin', *arguments, expected=expected)
        return json.loads(result.stdout) if expected == 0 else None

    def resolve_after_inspection(self, event, receipt, outcome):
        # These fixture-specific checks are the independent operator inspection.
        # The product CLI cannot establish an arbitrary external effect's outcome.
        jobs = self.auton_jobs()
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]['status'], 'complete')
        self.assertEqual(self.effects(), 1)
        self.assertEqual(self.deliveries(), [])
        if outcome == 'succeeded':
            self.assertEqual(jobs[0]['return_code'], 0)
            self.assertFalse(jobs[0]['execution_uncertain'])
        else:
            self.assertEqual(jobs[0]['outcome'], 'job.interrupted')
            self.assertTrue(jobs[0]['execution_uncertain'])
            # The old executor and its children were killed; inspect the one
            # partial synthetic effect before choosing a terminal failure.
        before = self.admin('status', receipt['id'])
        records = self.auton_records()
        resolved = self.admin('resolve', receipt['id'], '--outcome', outcome)
        self.assertEqual(resolved['status'], outcome)
        self.assertEqual(resolved['resolution'], 'operator-confirmed terminal outcome')
        for field in ('id', 'target', 'history', 'attempts', 'results', 'execution', 'attribution'):
            self.assertEqual(resolved[field], before[field])
        self.assertEqual(resolved['attempts'], {'run': 1})
        self.assertEqual(resolved['results'], {})
        self.assertEqual(self.status(event)['status'], outcome)
        self.assertEqual(self.effects(), 1)
        self.assertEqual(self.deliveries(), [])
        self.assertEqual(self.auton_records(), records)
        # An accidental repeat cannot change the terminal decision or unlock a
        # target now owned by other work. Neither path resumes the old graph.
        for repeated in ('succeeded', 'failed'):
            self.admin('resolve', receipt['id'], '--outcome', repeated, expected=2)
        self.assertEqual(self.admin('status', receipt['id']), resolved)
        self.recreate()
        self.assertEqual(self.admin('status', receipt['id']), resolved)
        self.assertEqual(self.auton_records(), records)
        self.assertEqual(self.effects(), 1)
        self.assertEqual(self.deliveries(), [])
        return resolved, jobs[0]['uid']

    def cancel_expired_following(self, event):
        following = event + '-following'
        expires = self.status(following)['observation']['expires_at']
        self.wait(lambda: time.time() >= expires)
        def cancelled():
            self.worker()
            return self.status(following)['status'] == 'cancelled'
        self.wait(cancelled)
        self.assertEqual(self.status(following)['error'], 'execution_authorization_denied')
        self.assertEqual(self.effects(), 1)
        self.assertEqual(self.deliveries(), [])

    def auton_records(self):
        return self.json('auton', 'python', '-c',
            'import sqlite3, json; c=sqlite3.connect("file:/state/jobs.db?mode=ro", uri=True); '
            'assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"; '
            'print(json.dumps(c.execute("SELECT uid, status, ended_at, record FROM jobs ORDER BY uid").fetchall()))')

    def name(self, role):
        return self.prefix + '-' + role

    def exec(self, role, *command, expected=0, input=None):
        return docker('exec', *(['-i'] if input is not None else []), self.name(role), *command,
                      expected=expected, input=input)

    def json(self, role, *command):
        return json.loads(self.exec(role, *command).stdout)

    def engine(self, command, *args):
        result = self.exec('engine', 'python', '-m', 'atropos.cli', command, '--config', '/state/engine.yml', *args)
        return json.loads(result.stdout) if result.stdout.strip() else None

    def operator(self, *args, expected=0):
        result = self.exec('operator', 'atraxis', 'rules', '--operator-config', '/state/operator.yml', *args, expected=expected)
        return json.loads(result.stdout) if expected == 0 and result.stdout.strip() else None

    def inventory(self):
        return self.json('operator', 'python', '/node.py', 'inventory')

    def wait(self, probe, timeout=30):
        deadline = time.monotonic() + timeout
        last_error = None
        while time.monotonic() < deadline:
            try:
                if probe():
                    return
            except (AssertionError, ValueError, OSError) as error:
                last_error = error
            time.sleep(.25)
        self.fail('Container readiness deadline exceeded: ' + (str(last_error)[-1500:] if last_error else 'state not reached'))

    def dispatch(self, event, expected=0):
        return self.operator('observe-dispatch', 'observed', '--target', 'container:web', '--binding', self.binding,
            '--expected-generation', self.active['generation'], '--event-id', event, '--confirm', expected=expected)

    def worker(self, test=False):
        return self.engine('rules-worker', '--once', *(['--test'] if test else []))

    def status(self, event):
        return self.json('operator', 'python', '/node.py', 'status', event)

    def effects(self):
        return int(self.exec('auton', 'python', '-c',
            'from pathlib import Path; p=Path("/state/effects"); print(len(p.read_text().splitlines()) if p.exists() else 0)').stdout)

    def deliveries(self):
        return self.json('alert', 'python', '-c',
            'from pathlib import Path; import json; p=Path("/state/deliveries.jsonl"); print(json.dumps([json.loads(s) for s in p.read_text().splitlines()] if p.exists() else []))')

    def disconnect(self, role):
        docker('network', 'disconnect', self.network, self.name(role))

    def reconnect(self, role):
        docker('network', 'connect', '--alias', role + '.test', self.network, self.name(role))

    def close(self):
        for name in reversed(self.containers):
            logs = docker('logs', name, expected=None)
            if logs.returncode == 0 and (logs.stdout or logs.stderr):
                print(name, logs.stdout + logs.stderr)
            docker('rm', '-f', name, expected=None)
        docker('network', 'rm', self.network, expected=None)
        for volume in reversed(self.volumes):
            docker('volume', 'rm', volume)

    def account_snapshot(self, role):
        return self.json(role, 'python', '-c',
            'import json; from atraxis.account_store import AccountStore; '
            'print(json.dumps(AccountStore("/state/accounts.db").snapshot()))')

    def recovery_helper(self, command, role, volume, backup):
        name = self.name('recovery-helper')
        self.containers.append(name)
        docker('run', '--name', name, *network.HARDENING, '--network', 'none',
            '-v', volume + ':/state' + (':ro' if command != 'restore' else ''),
            '-v', backup + ':/backup' + (':ro' if command != 'capture' else ''),
            '-v', str(ROOT / 'scripts/chain/recovery.py') + ':/recovery.py:ro',
            '--entrypoint', 'python', IMAGES[role], '/recovery.py', command, role)
        self.remove_role('recovery-helper')

    def restore_fresh_installation(self):
        # No automatic workers or producers exist in this fixture. Freeze
        # account administration, pause execution, then capture before stopping
        # every original service. No requests are made between these captures.
        paused = self.engine('storage-pause')
        self.assertTrue(paused['execution_paused'])
        self.assertTrue(paused['test_execution_paused'])
        self.engine('storage-backup', '--output', '/state/backup.json')
        for role in ('web', 'operator'):
            self.exec(role, 'atraxis', 'accounts', '--store', '/state/accounts.db',
                      'backup', '--output', '/state/backup.json')
        old_ids = {role: docker('inspect', '--format', '{{.Id}}', self.name(role)).stdout
                   for role in PERSISTENT_ROLES}
        for role in ('operator', 'web-proxy', 'web', 'engine-proxy', 'redis', 'engine',
                     'auton-proxy', 'auton', 'alert-proxy', 'alert'):
            docker('stop', '--time', '10', self.name(role))
            self.assertEqual(docker('inspect', '--format', '{{.State.ExitCode}}', self.name(role)).stdout.strip(), '0', role)
            self.remove_role(role)
        backup = self.create_volume('engine', 'backup')
        for role in PERSISTENT_ROLES:
            self.recovery_helper('capture', role, self.state_volumes[role], backup)
        # Destroy original volumes before restoration: no source database, AOF,
        # configuration, token or private signing key can be reused accidentally.
        for role in PERSISTENT_ROLES:
            old = self.state_volumes[role]
            docker('volume', 'rm', old)
            self.volumes.remove(old)
            fresh = self.create_volume(role, 'restored')
            self.assertNotEqual(fresh, old)
            self.state_volumes[role] = fresh
            self.recovery_helper('restore', role, fresh, backup)
            for source in (self.root / role).iterdir():
                source.unlink()  # Restart cannot recover missing configuration from old fixtures.
        # Hold only an inert network namespace, without starting the engine API.
        anchor = self.name('engine')
        self.containers.append(anchor)
        docker('run', '-d', '--name', anchor, *network.HARDENING,
            '--network', self.network, '-v', self.state_volumes['engine'] + ':/state',
            '--entrypoint', 'python', IMAGES['engine'], '-c',
            'import signal,time; signal.signal(signal.SIGTERM,lambda *a:exit(0)); time.sleep(3600)')
        self.start_role('redis')
        self.wait(lambda: self.exec('engine', 'python', '-c',
            'import json,redis; from pathlib import Path; c=json.loads(Path("/state/engine.yml").read_text()); '
            'r=redis.Redis.from_url(c["queue"]["url"]); assert r.ping(); assert r.dbsize()==0').returncode == 0)
        restored = self.engine('storage-restore', '--input', '/state/backup.json', '--confirm-source-stopped')
        self.assertEqual(restored['execution'], 'paused')
        for role in ('redis', 'engine'):
            docker('stop', '--time', '10', self.name(role))
            self.assertEqual(docker('inspect', '--format', '{{.State.ExitCode}}', self.name(role)).stdout.strip(), '0')
            self.remove_role(role)
        for role in ('web', 'engine', 'redis', 'auton', 'alert', 'operator'):
            self.start_role(role)
            self.assertNotEqual(docker('inspect', '--format', '{{.Id}}', self.name(role)).stdout, old_ids[role])
        self.wait(lambda: self.exec('operator', 'python', '/node.py', 'catalog').returncode == 0)
        self.wait(lambda: self.inventory()['ready'])
        self.wait(lambda: isinstance(self.auton_jobs(), list))
        self.assertEqual(self.engine('rules-bindings')[0]['id'], self.binding)
        paused = self.engine('storage-status')
        self.assertTrue(paused['execution_paused'])
        self.assertTrue(paused['test_execution_paused'])
        self.assertIsNone(paused['backup_operation'])


class ChainTests(ChainFixture):
    def test_full_backup_to_empty_volumes_preserves_uncertainty_and_authority(self):
        # Non-default account state and a second audit revision detect accidental
        # bootstrap/reseeding instead of restoration of the actual directory.
        for role in ('web', 'operator'):
            self.exec(role, 'python', '-c',
                'from atraxis.account_store import AccountStore; from atraxis.accounts import Directory; '
                's=AccountStore("/state/accounts.db"); d=s.snapshot(); '
                'd["directory"]["users"].append(dict(id="disabled",name="Disabled recovery account",enabled=False)); '
                's.apply(Directory(d["directory"]),d["revision"])')
        accounts = {role: self.account_snapshot(role) for role in ('web', 'operator')}
        completed = self.dispatch('backup-completed')
        self.worker(); self.worker()
        self.assertEqual(self.status('backup-completed')['status'], 'succeeded')
        completed_state = self.admin('status', completed['id'])
        uncertain = self.dispatch('backup-uncertain')
        self.worker()
        self.exec('alert', 'python', '-c', 'from pathlib import Path; Path("/state/lose-ack").touch()')
        self.worker()
        self.assertEqual(self.status('backup-uncertain')['status'], 'uncertain')
        self.exec('alert', 'python', '-c', 'from pathlib import Path; Path("/state/lose-ack").unlink()')
        uncertain_state = self.admin('status', uncertain['id'])
        self.engine('storage-pause')
        queued = self.dispatch('backup-queued')  # Bounded admission remains open.
        simulated = self.operator('test-dispatch', 'observed', '--target', 'container:web', '--binding', self.binding,
                                  '--input', '/state/facts.json', '--event-id', 'backup-test')
        records, deliveries = self.auton_records(), self.deliveries()
        evidence = self.json('operator', 'python', '/node.py', 'rule-evidence', self.active['revision'])
        self.assertEqual(len(records), 2)
        self.assertEqual(len(deliveries), 2)
        self.restore_fresh_installation()
        self.assertEqual(self.json('operator', 'python', '/node.py', 'rule-evidence', self.active['revision']), evidence)
        for role, before in accounts.items():
            self.assertEqual(self.account_snapshot(role), before)
        self.assertEqual(self.admin('status', completed['id']), completed_state)
        self.assertEqual(self.admin('status', uncertain['id']), uncertain_state)
        self.assertEqual(self.status('backup-queued'), queued)
        self.assertEqual(self.auton_records(), records)
        self.assertEqual(self.deliveries(), deliveries)
        self.assertEqual(self.effects(), 2)
        for test in (False, True):
            self.worker(test=test)
        self.assertEqual(self.status('backup-queued'), queued)
        test_state = self.operator('test-dispatch-status', 'observed', '--event-id', 'backup-test')
        self.assertEqual(test_state['status'], 'queued')
        self.assertEqual(test_state['test_context'], simulated['test_context'])
        # Explicit resume alone cannot release an uncertain target lock.
        self.engine('storage-resume', '--confirm-reconciled')
        self.worker(); self.worker()
        self.assertEqual(self.status('backup-queued'), queued)
        self.assertEqual(self.admin('status', uncertain['id']), uncertain_state)
        # A separately authorized simulation uses its own namespace/target lock.
        self.worker(test=True); self.worker(test=True)
        test_state = self.operator('test-dispatch-status', 'observed', '--event-id', 'backup-test')
        self.assertEqual(test_state['status'], 'succeeded')
        self.assertEqual(test_state['results']['notify']['alert']['test_context'], simulated['test_context'])
        self.assertEqual(self.effects(), 2)
        self.assertEqual(self.auton_records(), records)
        self.assertEqual(self.deliveries()[:2], deliveries)
        self.assertEqual(len(self.deliveries()), 3)
        # Current execute rights must still govern new work after recovery. Use a
        # fresh signed proof so an expired backup observation cannot mask denial.
        self.admin('resolve', uncertain['id'], '--outcome', 'failed')
        expires = queued['observation']['expires_at']
        self.wait(lambda: time.time() >= expires)
        def expired_cancelled():
            self.worker()
            return self.status('backup-queued')['status'] == 'cancelled'
        self.wait(expired_cancelled)
        fresh = self.dispatch('backup-fresh')
        self.exec('engine', 'python', '-c',
            'import json; from pathlib import Path; p=Path("/state/engine.yml"); c=json.loads(p.read_text()); '
            'c["grants"][0]["operations"].remove("execute"); p.write_text(json.dumps(c))')
        self.assertGreater(fresh['observation']['expires_at'], time.time() + 5)
        self.worker()
        self.assertEqual(self.status('backup-fresh')['status'], 'cancelled')
        self.assertEqual(self.status('backup-fresh')['error'], 'execution_authorization_denied')
        self.assertEqual(self.admin('status', completed['id']), completed_state)
        self.assertEqual(self.effects(), 2)
        self.assertEqual(self.auton_records(), records)
        self.assertEqual(len(self.deliveries()), 3)

    def test_observation_failure_recovery_real_execution_and_isolated_test(self):
        preview = self.operator('observe-dispatch-plan', 'observed', '--target', 'container:web', '--binding', self.binding)
        self.assertEqual(preview['facts'], {'cpu_percent': 250})
        self.assertEqual(self.effects(), 0)
        self.exec('operator', 'python', '/node.py', 'manual-denied', self.binding, self.active['generation'])
        self.disconnect('source')
        self.wait(lambda: not self.inventory()['ready'])
        self.dispatch('unavailable', expected=2)
        self.worker()
        self.assertEqual(self.effects(), 0)
        self.assertEqual(self.deliveries(), [])
        self.reconnect('source')
        self.wait(lambda: self.inventory()['ready'])
        receipt = self.dispatch('real')
        self.assertTrue(receipt['observation']['verified_at_admission'])
        self.worker()
        self.assertEqual(self.effects(), 1)
        self.assertEqual(self.deliveries(), [])
        self.worker()
        self.assertEqual(self.status('real')['status'], 'succeeded')
        self.assertEqual(self.deliveries()[0]['body'][0]['annotations']['atropos_run_id'], receipt['id'])
        self.worker()
        self.assertEqual(self.effects(), 1)
        self.assertEqual(len(self.deliveries()), 1)
        # Test mode must not even need the real executor credential.
        self.exec('engine', 'python', '-c', 'from pathlib import Path; Path("/state/executor.password").unlink()')
        simulated = self.operator('test-dispatch', 'observed', '--target', 'container:web', '--binding', self.binding,
                                 '--input', '/state/facts.json', '--event-id', 'simulation')
        self.worker()
        self.assertEqual(len(self.deliveries()), 1)
        self.worker(test=True)
        self.worker(test=True)
        status = self.operator('test-dispatch-status', 'observed', '--event-id', 'simulation')
        self.assertEqual(status['status'], 'succeeded')
        self.assertEqual(status['results']['run']['test_context'], simulated['test_context'])
        self.assertEqual(status['results']['notify']['alert']['test_context'], simulated['test_context'])
        self.assertEqual(self.deliveries()[1]['path'], '/v1/test-alerts')
        self.assertEqual(self.deliveries()[1]['body']['test_context'], simulated['test_context'])
        self.assertEqual(self.effects(), 1)

    def test_executor_network_failure_never_replays_after_recovery(self):
        self.dispatch('executor-cut')
        self.disconnect('auton')
        self.worker()
        status = self.status('executor-cut')['status']
        self.assertIn(status, ('failed', 'uncertain'))
        self.assertEqual(self.effects(), 0)
        self.reconnect('auton')
        self.worker()
        self.worker()
        self.assertEqual(self.status('executor-cut')['status'], status)
        self.assertEqual(self.effects(), 0)
        self.assertEqual(self.deliveries(), [])

    def test_worker_sigkill_during_external_action_retains_uncertainty_and_lock(self):
        event = 'worker-crash'
        receipt, job_id = self.start_blocked_run(event)
        self.kill_role('worker')
        self.remove_role('worker')
        self.assertEqual(self.status(event)['status'], 'running')
        self.assertEqual(self.auton_jobs()[0]['status'], 'processing')
        self.exec('auton', 'python', '-c', 'from pathlib import Path; Path("/state/release-action").touch()')
        self.wait(lambda: self.auton_jobs()[0]['status'] == 'complete')
        job = self.auton_jobs()[0]
        self.assertEqual(job['uid'], job_id)
        self.assertEqual(job['return_code'], 0)
        self.assertFalse(job['execution_uncertain'])
        # Let the actual 30-second lease expire. Do not rewrite SQL timestamps,
        # shorten the product lease or turn external success into an engine ack.
        def recovered():
            self.worker()
            return self.status(event)['status'] == 'uncertain'
        # Redis may reclaim just before the SQL lease expires and then wait
        # another 30-second delivery interval. Allow both independent leases.
        self.wait(recovered, timeout=75)
        self.assertEqual(self.status(event)['error'], 'worker lease expired; action outcome requires reconciliation')
        self.check_uncertainty_and_target_lock(event, receipt)
        resolved, old_job = self.resolve_after_inspection(event, receipt, 'succeeded')
        self.cancel_expired_following(event)
        following = event + '-fresh'
        self.dispatch(following)
        def next_completed():
            self.worker()
            return self.status(following)['status'] == 'succeeded'
        self.wait(next_completed)
        self.assertEqual(self.effects(), 2)
        jobs = self.auton_jobs()
        self.assertEqual(len(jobs), 2)
        self.assertEqual(len({job['uid'] for job in jobs}), 2)
        self.assertIn(old_job, {job['uid'] for job in jobs})
        deliveries = self.deliveries()
        self.assertEqual(len(deliveries), 1)
        self.assertEqual(deliveries[0]['body'][0]['annotations']['atropos_run_id'], self.status(following)['id'])
        self.worker()
        self.assertEqual(self.admin('status', receipt['id']), resolved)
        self.assertEqual(self.effects(), 2)
        self.assertEqual(len(self.deliveries()), 1)

    def test_executor_sigkill_marks_interrupted_job_without_replay(self):
        event = 'executor-crash'
        receipt, job_id = self.start_blocked_run(event)
        self.kill_role('auton')
        self.recreate_killed_auton()
        job = self.auton_jobs()[0]
        self.assertEqual(job['uid'], job_id)
        self.assertEqual(job['status'], 'complete')
        self.assertEqual(job['outcome'], 'job.interrupted')
        self.assertTrue(job['execution_uncertain'])
        self.wait(lambda: docker('inspect', '--format', '{{.State.Running}}', self.name('worker')).stdout.strip() == 'false', timeout=75)
        self.assertEqual(docker('wait', self.name('worker')).stdout.strip(), '0')
        self.remove_role('worker')
        self.assertEqual(self.status(event)['status'], 'uncertain')
        self.check_uncertainty_and_target_lock(event, receipt)
        resolved, old_job = self.resolve_after_inspection(event, receipt, 'failed')
        self.cancel_expired_following(event)
        following = event + '-fresh'
        self.dispatch(following)
        # Revoke only after this new, independently confirmed observation is
        # admitted. Check freshness so a stale proof cannot explain the denial.
        self.exec('engine', 'python', '-c',
            'import json; from pathlib import Path; p=Path("/state/engine.yml"); c=json.loads(p.read_text()); '
            'c["grants"][0]["operations"].remove("execute"); p.write_text(json.dumps(c))')
        self.assertGreater(self.status(following)['observation']['expires_at'], time.time() + 5)
        def next_cancelled():
            self.worker()
            return self.status(following)['status'] == 'cancelled'
        self.wait(next_cancelled)
        self.assertEqual(self.status(following)['error'], 'execution_authorization_denied')
        self.assertEqual(self.effects(), 1)
        self.assertEqual(self.deliveries(), [])
        self.assertEqual([job['uid'] for job in self.auton_jobs()], [old_job])
        self.assertEqual(self.admin('status', receipt['id']), resolved)

    def test_queued_checkpoint_and_completed_work_survive_recreation(self):
        receipt = self.dispatch('persistent')
        queued = self.status('persistent')
        self.assertEqual(queued['status'], 'queued')
        self.recreate()
        self.assertEqual(self.status('persistent'), queued)
        self.assertEqual(self.effects(), 0)
        self.worker()
        checkpoint = self.status('persistent')
        self.assertEqual(checkpoint['status'], 'queued')
        self.assertEqual(checkpoint['history'][-1]['step'], 'run')
        self.assertEqual(checkpoint['history'][-1]['status'], 'queued')
        self.assertEqual(self.effects(), 1)
        self.assertEqual(self.deliveries(), [])
        records = self.auton_records()
        self.assertEqual(len(records), 1)
        self.recreate()
        self.assertEqual(self.status('persistent'), checkpoint)
        self.assertEqual(self.auton_records(), records)
        self.assertEqual(len(self.json('engine', 'python', '/node.py', 'auton-jobs')), 1)
        self.worker()
        completed = self.status('persistent')
        self.assertEqual(completed['status'], 'succeeded')
        self.assertEqual(self.deliveries()[0]['body'][0]['annotations']['atropos_run_id'], receipt['id'])
        self.recreate()
        self.assertEqual(self.status('persistent'), completed)
        self.assertEqual(self.auton_records(), records)
        self.worker()
        self.worker()
        self.assertEqual(self.effects(), 1)
        self.assertEqual(len(self.deliveries()), 1)

    def test_revoked_execution_stays_revoked_after_recreation(self):
        self.dispatch('revoked')
        self.exec('engine', 'python', '-c',
            'import json; from pathlib import Path; p=Path("/state/engine.yml"); c=json.loads(p.read_text()); '
            'c["grants"][0]["operations"].remove("execute"); p.write_text(json.dumps(c))')
        self.recreate()
        self.worker()
        status = self.status('revoked')
        self.assertEqual(status['status'], 'cancelled')
        self.assertEqual(status['error'], 'execution_authorization_denied')
        self.exec('engine', 'python', '-c',
            'import json; from pathlib import Path; p=Path("/state/engine.yml"); c=json.loads(p.read_text()); '
            'c["grants"][0]["operations"].append("execute"); p.write_text(json.dumps(c))')
        self.recreate()
        self.worker()
        self.assertEqual(self.status('revoked'), status)
        self.assertEqual(self.effects(), 0)
        self.assertEqual(self.deliveries(), [])

    def test_lost_alert_ack_retains_uncertainty_without_resend(self):
        self.dispatch('lost-alert')
        self.worker()
        self.assertEqual(self.effects(), 1)
        self.exec('alert', 'python', '-c', 'from pathlib import Path; Path("/state/lose-ack").touch()')
        self.worker()
        self.assertEqual(self.status('lost-alert')['status'], 'uncertain')
        self.assertEqual(len(self.deliveries()), 1)
        before = self.status('lost-alert')
        records = self.auton_records()
        self.exec('alert', 'python', '-c', 'from pathlib import Path; Path("/state/lose-ack").unlink()')
        self.recreate()
        self.assertEqual(self.status('lost-alert'), before)
        self.assertEqual(self.auton_records(), records)
        self.worker()
        self.worker()
        self.assertEqual(self.status('lost-alert')['status'], 'uncertain')
        self.assertEqual(len(self.deliveries()), 1)
        self.assertEqual(self.effects(), 1)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--auton-source', required=True, type=Path)
    OPTIONS = parser.parse_args()
    unittest.main(argv=[__file__, '-v'])
