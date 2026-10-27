"""Recover an explicitly scoped source using only disposable TLS fixtures."""
import json
import os
from pathlib import Path
import secrets
import subprocess

import yaml

from operator_access import ACCOUNT_SNAPSHOT, await_read, login_line, private

ROOT = Path(__file__).resolve().parents[1]
SOURCE_HOST = 'inventory-recovery.test'
SOURCE_ORIGIN = 'https://' + SOURCE_HOST + ':8443'
SOURCE_ID = 'monitor-a'
HIDDEN_SOURCE = 'hidden-monitor'
LOGIN = 'recovery-reader-login'
SUBJECT = 'recovery-reader'
REVOKED_LOGIN = 'reader-login'
STATUS_PATH = '/v1/status?organization=default'
READY_PATH = '/readyz?organization=default'
MAX_LOG_BYTES = 65536
SOURCE_PASSWORD = 'synthetic-source-password'
FIXTURE_IMAGE = 'atraxis-install-web:local'
FIXTURE_MODES = frozenset({'ok', 'silent', 'redirect'})
UNAVAILABLE = frozenset({'timeout', 'transport_error'})


class SourceFixture:
    """Own only this test's new container on its existing Compose network."""
    def __init__(self, case, compose, site):
        self.case, self.compose, self.site = case, compose, site
        self.name = 'recovery-source-' + secrets.token_hex(8)
        self.started = False

    def start(self):
        proxy = self.compose('ps', '-q', 'web-tls').stdout.strip()
        inspected = json.loads(subprocess.run(['docker', 'inspect', proxy], capture_output=True,
            text=True, check=True, timeout=20).stdout)[0]
        networks = inspected['NetworkSettings']['Networks']
        self.case.assertEqual(len(networks), 1)
        subprocess.run(['docker', 'run', '-d', '--name', self.name, '--network', next(iter(networks)),
            '--network-alias', SOURCE_HOST, '--user', '10001:10001', '--read-only',
            '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges', '--memory', '64m',
            '--cpus', '0.25', '--pids-limit', '32', '--log-driver', 'none',
            '--tmpfs', '/tmp:rw,nosuid,nodev,size=8m,uid=10001,gid=10001,mode=0700',
            '--mount', 'type=bind,source=' + str(self.site / 'inventory-fixture') + ',target=/fixtures,readonly',
            '--entrypoint', 'python', FIXTURE_IMAGE, '/fixtures/fixture.py', 'source'],
            capture_output=True, text=True, check=True, timeout=30)
        self.started = True

    def mode(self, value):
        if value not in FIXTURE_MODES:
            raise ValueError('invalid_recovery_source_mode')
        subprocess.run(['docker', 'exec', self.name, 'python', '-c',
            "from pathlib import Path; import sys; Path('/tmp/mode').write_text(sys.argv[1])", value],
            capture_output=True, text=True, check=True, timeout=15)

    def verify_reads(self):
        result = subprocess.run(['docker', 'exec', self.name, 'python', '-c',
            "import sys; sys.stdout.buffer.write(open('/tmp/requests.jsonl', 'rb').read(int(sys.argv[1]) + 1))",
            str(MAX_LOG_BYTES)],
            capture_output=True, text=True, check=True, timeout=15)
        self.case.assertLessEqual(len(result.stdout.encode()), MAX_LOG_BYTES)
        rows = [json.loads(line) for line in result.stdout.splitlines()]
        self.case.assertTrue(rows)
        self.case.assertTrue(all(r['method'] == 'GET' and r['path'] == '/v1/status'
            and r['authorized'] for r in rows))
        return len(rows)

    def close(self):
        if self.started:
            subprocess.run(['docker', 'rm', '-f', self.name], capture_output=True,
                text=True, check=True, timeout=30)
            self.started = False


def reader(request, evidence, path=STATUS_PATH, revoked=False):
    return request(path, username=REVOKED_LOGIN if revoked else LOGIN,
        password=evidence['revoked_password'] if revoked else evidence['password'])


def read_status(case, request, evidence):
    code, raw = reader(request, evidence)
    case.assertEqual(code, 200)
    case.assertNotIn(HIDDEN_SOURCE.encode(), raw)
    for secret in (evidence['password'], evidence['revoked_password'], SOURCE_PASSWORD):
        case.assertNotIn(secret.encode(), raw)
    payload = json.loads(raw)
    case.assertEqual(payload['context']['subject'], SUBJECT)
    case.assertEqual(payload['context']['organization'], 'default')
    case.assertEqual([s['source_id'] for s in payload['sources']], [SOURCE_ID])
    return payload


def await_status(case, request, evidence, predicate, label):
    return await_read(lambda: read_status(case, request, evidence), predicate, label)


def current(case, payload):
    case.assertTrue(payload['ready'])
    row, = payload['sources']
    case.assertTrue(row['current'])
    case.assertEqual(row['availability'], 'available')
    case.assertEqual(row['last_attempt']['outcome'], 'success')
    snapshot = row['last_valid_snapshot']
    case.assertEqual(snapshot['freshness'], 'fresh')
    case.assertLessEqual(snapshot['age_seconds'], snapshot['max_age_seconds'])
    source_time = snapshot['source_metadata']['last_cycle_finished_at']
    case.assertLessEqual(source_time, snapshot['received_at'])
    case.assertEqual(snapshot['source_metadata']['last_success_at'], source_time)
    resource, = snapshot['resources']
    case.assertEqual(resource['native_id'], 'a' * 64)
    case.assertEqual(resource['fields']['cpu_percent'], 250)
    case.assertIsNone(resource['fields']['mem_usage'])
    return snapshot


def refusals(case, request, evidence):
    case.assertEqual(reader(request, evidence, revoked=True)[0], 403)
    case.assertEqual(reader(request, evidence, '/v1/status?organization=other')[0], 403)
    case.assertEqual(reader(request, evidence, '/ui/rules')[0], 403)


def prepare(case, compose, site, authority, request):
    """Leave the selected profile active in the stopped, cold-backed-up site."""
    fixture_dir = site / 'inventory-fixture'
    fixture_dir.mkdir(mode=0o700)
    os.chown(fixture_dir, 10001, 10001)
    authority.leaf(fixture_dir, 'ok', SOURCE_HOST)
    for path in fixture_dir.iterdir():
        os.chown(path, 10001, 10001)
        path.chmod(0o600)
    private(fixture_dir / 'fixture.py', (ROOT / 'scripts/network/fixture.py').read_bytes())
    evidence = dict(password=secrets.token_urlsafe(32), revoked_password=secrets.token_urlsafe(32))
    before = json.loads(compose('exec', '-T', 'web', 'python', '-c', ACCOUNT_SNAPSHOT).stdout)
    candidate = json.loads(json.dumps(before['directory']))
    case.assertFalse(next(u for u in candidate['users'] if u['id'] == 'reader')['enabled'])
    candidate['users'].append(dict(id=SUBJECT, name='Recovery reader', enabled=True))
    candidate['memberships'].append(dict(user=SUBJECT, organization='default', group=None))
    private(site / 'web/recovery-reader.json', json.dumps(candidate))
    command = ('exec', '-T', 'web', 'atraxis', 'accounts', '--store', '/var/lib/atraxis/accounts.sqlite3')
    reviewed = json.loads(compose(*command, 'check', '--file', '/etc/atraxis/recovery-reader.json').stdout)
    case.assertEqual(reviewed['revision'], before['revision'])
    compose(*command, 'apply', '--file', '/etc/atraxis/recovery-reader.json',
        '--expected-revision', reviewed['revision'], '--confirm')
    accounts = json.loads(compose('exec', '-T', 'web', 'python', '-c', ACCOUNT_SNAPSHOT).stdout)
    case.assertEqual(accounts['audit'][:-1], before['audit'])
    case.assertEqual(accounts['directory']['delegations'], [])
    case.assertEqual(accounts['directory']['entity_delegations'], [])
    login_file = site / 'web/users.htpasswd'
    lines = login_file.read_text().splitlines(keepends=True)
    private(login_file, ''.join(login_line(REVOKED_LOGIN, evidence['revoked_password'])
        if line.startswith(REVOKED_LOGIN + ':') else line for line in lines)
        + login_line(LOGIN, evidence['password']))
    profile_file = site / 'web/inventory-access.yml'
    profile = yaml.safe_load(profile_file.read_text())
    profile['identities'].append(dict(username=LOGIN, subject=SUBJECT))
    profile['grants'].append(dict(organization='default', subject_kind='user', subject=SUBJECT,
        targets=[dict(kind='source', id=SOURCE_ID)]))
    private(profile_file, yaml.safe_dump(profile))
    private(site / 'web/source-ca.pem', authority.pem())
    config_file = site / 'web/atraxis.yml'
    config = yaml.safe_load(config_file.read_text())
    config['general']['inventory_access_file'] = '/etc/atraxis/inventory-access.yml'
    config['sources'] = [dict(source_id=name, kind='monit_docker', origin=SOURCE_ORIGIN,
        credentials_file='/etc/atraxis/source.json', ca_file='/etc/atraxis/source-ca.pem',
        poll_interval=2, timeout=2, max_age=60) for name in (SOURCE_ID, HIDDEN_SOURCE)]
    private(config_file, yaml.safe_dump(config))
    source = SourceFixture(case, compose, site)
    try:
        source.start()
        compose('up', '-d', '--no-build', '--pull', 'never', '--no-deps', '--force-recreate', 'web')
        await_read(lambda: reader(request, evidence, '/healthz'), lambda r: r[0] == 200, 'recovery-reader-login')
        payload = await_status(case, request, evidence, lambda p: p['ready'], 'recovery-source-baseline')
        baseline = current(case, payload)
        refusals(case, request, evidence)
        case.assertEqual(reader(request, evidence, READY_PATH)[0], 200)
        reads = source.verify_reads()
        compose('--profile', '*', 'stop')
        case.assertEqual(compose('ps', '--status', 'running', '-q').stdout.strip(), '')
        return dict(evidence, baseline=baseline, source_reads=reads), accounts
    finally:
        source.close()


def unavailable(case, request, evidence):
    payload = await_status(case, request, evidence, lambda p:
        p['sources'][0]['availability'] == 'unavailable', 'recovered-source-unavailable')
    case.assertFalse(payload['ready'])
    case.assertFalse(payload['sources'][0]['current'])
    case.assertIsNone(payload['sources'][0]['last_valid_snapshot'])
    case.assertIn(payload['sources'][0]['last_attempt']['outcome'], UNAVAILABLE)
    case.assertEqual(reader(request, evidence, READY_PATH)[0], 503)
    refusals(case, request, evidence)


def revoke_restored_reader(case, compose, site, request, evidence):
    before = json.loads(compose('exec', '-T', 'web', 'python', '-c', ACCOUNT_SNAPSHOT).stdout)
    candidate = json.loads(json.dumps(before['directory']))
    next(u for u in candidate['users'] if u['id'] == SUBJECT)['enabled'] = False
    private(site / 'web/recovery-revocation.json', json.dumps(candidate))
    command = ('exec', '-T', 'web', 'atraxis', 'accounts', '--store', '/var/lib/atraxis/accounts.sqlite3')
    reviewed = json.loads(compose(*command, 'check', '--file', '/etc/atraxis/recovery-revocation.json').stdout)
    case.assertEqual(reviewed['revision'], before['revision'])
    compose(*command, 'apply', '--file', '/etc/atraxis/recovery-revocation.json',
        '--expected-revision', reviewed['revision'], '--confirm')
    after = json.loads(compose('exec', '-T', 'web', 'python', '-c', ACCOUNT_SNAPSHOT).stdout)
    case.assertEqual(after['audit'][:-1], before['audit'])
    case.assertNotEqual(after['revision'], before['revision'])
    for path in (STATUS_PATH, READY_PATH, '/ui?organization=default'):
        code, raw = reader(request, evidence, path)
        case.assertEqual(code, 403)
        case.assertNotIn(b'network-demo', raw)
        case.assertNotIn(SOURCE_ID.encode(), raw)


def recover(case, compose, site, request, evidence):
    unavailable(case, request, evidence)
    source = SourceFixture(case, compose, site)
    try:
        source.start()
        payload = await_status(case, request, evidence, lambda p: p['ready'], 'reconnected-source')
        first = current(case, payload)
        case.assertGreater(first['received_at'], evidence['baseline']['received_at'])
        case.assertGreater(first['source_metadata']['last_cycle_finished_at'],
            evidence['baseline']['source_metadata']['last_cycle_finished_at'])
        case.assertEqual(first['resources'], evidence['baseline']['resources'])
        case.assertEqual(reader(request, evidence, READY_PATH)[0], 200)
        case.assertEqual(reader(request, evidence, '/ui?organization=default')[0], 200)
        refusals(case, request, evidence)
        # A fresh successful scrape must never hide a failed later attempt.
        source.mode('silent')
        failed = await_status(case, request, evidence, lambda p:
            p['sources'][0]['last_attempt']['outcome'] in UNAVAILABLE, 'restored-source-timeout')
        row = failed['sources'][0]
        case.assertFalse(failed['ready'])
        case.assertFalse(row['current'])
        case.assertEqual(row['last_valid_snapshot']['resources'], first['resources'])
        retained = row['last_valid_snapshot']['received_at']
        case.assertEqual(reader(request, evidence, READY_PATH)[0], 503)
        source.mode('redirect')
        redirected = await_status(case, request, evidence, lambda p:
            p['sources'][0]['last_attempt']['outcome'] == 'redirect_refused', 'restored-source-redirect')
        case.assertFalse(redirected['ready'])
        case.assertFalse(redirected['sources'][0]['current'])
        case.assertEqual(redirected['sources'][0]['last_valid_snapshot']['received_at'], retained)
        source.mode('ok')
        restored = current(case, await_status(case, request, evidence, lambda p: p['ready'], 'source-current-again'))
        case.assertGreater(restored['received_at'], retained)
        refusals(case, request, evidence)
        reads = source.verify_reads()
        revoke_restored_reader(case, compose, site, request, evidence)
        return dict(active_profile_preserved=True, initially_unavailable=True,
            fresh_source_timestamp=True, received_timestamp_separate=True,
            source_scope_preserved=True, revoked_reader_denied=True,
            restored_account_revocation=True,
            wrong_organization_denied=True, engine_rights_not_granted=True,
            timeout_retains_history=True, redirect_refused=True,
            current_after_reconnection=True, source_requests_get_only=True,
            source_requests=reads, synthetic_only=True, live_source_qualified=False)
    finally:
        source.close()
