"""Exercise the documented access journey on the disposable Compose project only."""
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import ssl
import subprocess
import time
import urllib.error

import yaml

ROOT = Path(__file__).resolve().parents[1]
READ_LIMIT = 65536
DEADLINE = 60


def private(path, value):
    """Publish a complete test-owned file with the service's actual permissions."""
    data = value if isinstance(value, bytes) else value.encode()
    pending = path.with_name(path.name + '.' + secrets.token_hex(6) + '.next')
    try:
        fd = os.open(pending, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'wb') as stream:
            os.fchown(stream.fileno(), 10001, 10001)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        pending.replace(path)
    finally:
        pending.unlink(missing_ok=True)


def login_line(username, password):
    # SHA1 is confined to these disposable test credentials, never provisioning advice.
    return username + ':{SHA}' + base64.b64encode(hashlib.sha1(password.encode()).digest()).decode() + '\n'


def await_read(callback, predicate, label):
    deadline = time.monotonic() + DEADLINE
    while time.monotonic() < deadline:
        try:
            value = callback()
            if predicate(value):
                return value
        except (OSError, ValueError):
            pass
        time.sleep(0.5)
    raise AssertionError('operator_read_deadline:' + label)


CLIENT = '''from atropos_client.client import RuleClient
import json, sys
c = RuleClient(sys.argv[1], expected_actor={'subject':'alice','organization':'lab'})
try:
    print(json.dumps({'rule': c.read('compose-rule', sys.argv[2])}, sort_keys=True))
except PermissionError:
    print(json.dumps({'denied': True}))
finally:
    c.close()
'''
ACCOUNT_SNAPSHOT = '''from atraxis.account_store import AccountStore
import json
print(json.dumps(AccountStore('/var/lib/atraxis/accounts.sqlite3').snapshot(), sort_keys=True))
'''


def exercise_operator_access(case, compose, site, authority, network, request, password, revision):
    """No external sources, notifications, execution workers or live deployment."""
    original_config = (site / 'web/atraxis.yml').read_text()
    source = site.parent / 'operator-source'
    source.mkdir(mode=0o700)
    os.chown(source, 10001, 10001)
    authority.leaf(source, 'ok', 'operator-source.test')
    for path in source.iterdir():
        os.chown(path, 10001, 10001)
        path.chmod(0o600)
    private(source / 'fixture.py', (ROOT / 'scripts/network/fixture.py').read_bytes())
    container = 'operator-source-' + secrets.token_hex(8)
    # Discover only this already-created fixture project's network.
    proxy = compose('ps', '-q', 'web-tls').stdout.strip()
    inspected = json.loads(subprocess.run(['docker', 'inspect', proxy], capture_output=True,
        text=True, check=True, timeout=20).stdout)[0]
    networks = inspected['NetworkSettings']['Networks']
    case.assertEqual(len(networks), 1)
    project_network = next(iter(networks))
    started = False
    try:
        subprocess.run(['docker', 'run', '-d', '--name', container, '--network', project_network,
            '--network-alias', 'operator-source.test', '--user', '10001:10001', '--read-only',
            '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges', '--memory', '64m',
            '--cpus', '0.25', '--pids-limit', '32', '--log-driver', 'none',
            '--tmpfs', '/tmp:rw,nosuid,nodev,size=8m,uid=10001,gid=10001,mode=0700',
            '--mount', 'type=bind,source=' + str(source) + ',target=/fixtures,readonly',
            '--entrypoint', 'python', 'atraxis-install-web:local', '/fixtures/fixture.py', 'source'],
            check=True, capture_output=True, text=True, timeout=30)
        started = True
        reader_password = secrets.token_urlsafe(32)
        login_file = site / 'web/users.htpasswd'
        private(login_file, login_file.read_text() + login_line('reader-login', reader_password))
        # Adding a login file entry does not change the already-running authentication set.
        case.assertEqual(request('/healthz', reader_password, 'reader-login')[0], 401)
        case.assertEqual(request('/healthz', password)[0], 200)
        compose('--profile', '*', 'stop')
        case.assertEqual(compose('ps', '--status', 'running', '-q').stdout.strip(), '')
        # Offline commands share the proxy namespace, which is started separately.
        compose('up', '-d', '--no-build', 'web-tls', 'engine-tls')
        compose('run', '--rm', '--no-deps', 'web', 'accounts', '--store',
            '/var/lib/atraxis/accounts.sqlite3', 'init', '--subject', 'reader')
        contexts = json.loads(compose('run', '--rm', '--no-deps', 'web', 'accounts', '--store',
            '/var/lib/atraxis/accounts.sqlite3', 'contexts', '--subject', 'reader').stdout)
        case.assertEqual(contexts, [dict(organization='default', tenant='default', groups=[])])
        # Use the actual guide's YAML instead of maintaining a parallel example.
        text = (ROOT / 'docs/operator-journey.md').read_text()
        profile = yaml.safe_load(re.search(r'```yaml\n(.*?)```', text, re.S).group(1))
        private(site / 'web/inventory-access.yml', yaml.safe_dump(profile))
        private(site / 'web/source.json', json.dumps(dict(username='source-reader', password='synthetic-source-password')))
        private(site / 'web/source-ca.pem', authority.pem())
        config = yaml.safe_load(original_config)
        config['general']['inventory_access_file'] = '/etc/atraxis/inventory-access.yml'
        config['sources'] = [dict(source_id=name, kind='monit_docker', origin='https://operator-source.test:8443',
            credentials_file='/etc/atraxis/source.json', ca_file='/etc/atraxis/source-ca.pem',
            poll_interval=2, timeout=2, max_age=60) for name in ('monitor-a', 'hidden-monitor')]
        private(site / 'web/atraxis.yml', yaml.safe_dump(config))
        compose('run', '--rm', '--no-deps', 'web', 'doctor', '--config', '/etc/atraxis/atraxis.yml')
        compose('run', '--rm', '--no-deps', 'web', 'doctor', '--config', '/etc/atraxis/atraxis.yml', '--probe-sources')
        compose('up', '-d', '--no-build', '--force-recreate')
        def reader(path='/v1/status?organization=default'):
            return request(path, reader_password, 'reader-login')
        status, body = await_read(reader, lambda result: result[0] == 200 and json.loads(result[1])['ready'], 'scoped-source')
        payload = json.loads(body)
        case.assertEqual([row['source_id'] for row in payload['sources']], ['monitor-a'])
        case.assertEqual(payload['context']['subject'], 'reader')
        case.assertNotIn(b'hidden-monitor', body)
        case.assertEqual(reader('/ui?organization=default')[0], 200)
        case.assertEqual(reader('/readyz?organization=default')[0], 200)
        case.assertEqual(reader('/v1/status?organization=other')[0], 403)
        case.assertEqual(request('/v1/status?organization=default', password, 'alice')[0], 403)
        case.assertEqual(reader('/ui/rules')[0], 403)
        snapshot = json.loads(compose('exec', '-T', 'web', 'python', '-c', ACCOUNT_SNAPSHOT).stdout)
        case.assertEqual(snapshot['directory']['delegations'], [])
        case.assertEqual(snapshot['directory']['entity_delegations'], [])
        # Review and apply a real revocation with the exact current revision.
        candidate = snapshot['directory']
        candidate['users'][0]['enabled'] = False
        private(site / 'web/reader-revocation.json', json.dumps(candidate))
        command = ('exec', '-T', 'web', 'atraxis', 'accounts', '--store', '/var/lib/atraxis/accounts.sqlite3')
        reviewed = json.loads(compose(*command, 'check', '--file', '/etc/atraxis/reader-revocation.json').stdout)
        compose(*command, 'apply', '--file', '/etc/atraxis/reader-revocation.json',
            '--expected-revision', reviewed['revision'], '--confirm')
        case.assertEqual(reader()[0], 403)
        reader_snapshot = json.loads(compose('exec', '-T', 'web', 'python', '-c', ACCOUNT_SNAPSHOT).stdout)
        case.assertEqual(len(reader_snapshot['audit']), 2)
        case.assertNotEqual(compose(*command, 'init', '--subject', 'reader', check=False).returncode, 0)
        case.assertEqual(json.loads(compose('exec', '-T', 'web', 'python', '-c', ACCOUNT_SNAPSHOT).stdout), reader_snapshot)

        def engine_read(profile_name='engine-client.yml'):
            return json.loads(compose('exec', '-T', 'web', 'python', '-c', CLIENT,
                '/etc/atraxis/' + profile_name, revision).stdout)
        before_rule = engine_read()
        before_ids = sorted(compose('ps', '-q').stdout.split())
        old_token = (site / 'web/engine.token').read_bytes()
        private(site / 'web/previous.token', old_token)
        previous = yaml.safe_load((site / 'web/engine-client.yml').read_text())
        previous['token_file'] = 'previous.token'
        private(site / 'web/previous-client.yml', yaml.safe_dump(previous))
        token = secrets.token_urlsafe(32)
        engine_config = site / 'engine/engine.yml'
        policy = yaml.safe_load(engine_config.read_text())
        old_policy = json.loads(json.dumps(policy))
        policy['identities'][0]['token_sha256'] = hashlib.sha256(token.encode()).hexdigest()
        private(engine_config, yaml.safe_dump(policy))
        case.assertEqual(engine_read('previous-client.yml'), {'denied': True})
        case.assertEqual(request('/ui/rules?name=compose-rule', password)[0], 403)
        private(site / 'web/engine.token', token)
        case.assertEqual(engine_read(), before_rule)
        case.assertEqual(engine_read('previous-client.yml'), {'denied': True})
        case.assertEqual(request('/ui/rules?name=compose-rule', password)[0], 200)
        case.assertEqual(sorted(compose('ps', '-q').stdout.split()), before_ids)
        policy['identities'][0]['token_sha256'] = old_policy['identities'][0]['token_sha256']
        case.assertEqual(policy, old_policy)  # Authentication material was the only change.

        # Planned login rotation: stop access before publishing the new credential.
        compose('--profile', '*', 'stop')
        case.assertEqual(compose('ps', '--status', 'running', '-q').stdout.strip(), '')
        new_password = secrets.token_urlsafe(32)
        lines = login_file.read_text().splitlines(keepends=True)
        private(login_file, ''.join(login_line('alice', new_password) if line.startswith('alice:') else line for line in lines))
        compose('up', '-d', '--no-build', '--force-recreate')
        await_read(lambda: request('/healthz', new_password), lambda r: r[0] == 200, 'rotated-login')
        case.assertEqual(request('/healthz', password)[0], 401)
        case.assertEqual(request('/healthz', password, 'bob')[0], 200)
        await_read(lambda: request('/ui/rules?name=compose-rule', new_password),
            lambda r: r[0] == 200, 'rotated-login-engine-read')
        case.assertEqual(reader()[0], 403)

        renewed = network.Authority('renewed disposable compose CA')
        staged = site.parent / 'renewed-tls'
        staged.mkdir(mode=0o700)
        for role, host in (('web', 'localhost'), ('engine', 'engine-tls')):
            directory = staged / role
            directory.mkdir(mode=0o700)
            renewed.leaf(directory, 'server', host)
        compose('--profile', '*', 'stop')
        case.assertEqual(compose('ps', '--status', 'running', '-q').stdout.strip(), '')
        for role in ('web', 'engine'):
            for suffix in ('crt', 'key'):
                private(site / (role + '-tls') / ('server.' + suffix), (staged / role / ('server.' + suffix)).read_bytes())
        private(site / 'web/previous-ca.pem', authority.pem())
        private(site / 'web/engine-ca.pem', renewed.pem())
        compose('up', '-d', '--no-build', '--force-recreate')
        await_read(lambda: request('/healthz', new_password, authority=renewed), lambda r: r[0] == 200, 'renewed-web-tls')
        await_read(lambda: request('/ui/rules?name=compose-rule', new_password, authority=renewed),
            lambda r: r[0] == 200, 'renewed-engine-read')
        try:
            request('/healthz', new_password, authority=authority)
        except urllib.error.URLError as error:
            case.assertIsInstance(error.reason, ssl.SSLCertVerificationError)
        else:
            case.fail('old_web_ca_still_trusted')
        tls_probe = '''import socket, ssl
for ca, allowed in (('previous-ca.pem', False), ('engine-ca.pem', True)):
    try:
        with socket.create_connection(('engine-tls',8443),timeout=5) as raw:
            with ssl.create_default_context(cafile='/etc/atraxis/'+ca).wrap_socket(raw,server_hostname='engine-tls'):
                assert allowed
    except ssl.SSLCertVerificationError:
        assert not allowed
print('ENGINE_TLS_RENEWAL verified=true')
'''
        compose('exec', '-T', 'web', 'python', '-c', tls_probe)
        case.assertEqual(engine_read(), before_rule)
        case.assertEqual(engine_read('previous-client.yml'), {'denied': True})
        case.assertEqual(request('/ui/rules?name=compose-rule', new_password, authority=renewed)[0], 200)
        case.assertEqual(json.loads(compose('exec', '-T', 'web', 'python', '-c', ACCOUNT_SNAPSHOT).stdout), reader_snapshot)
        log = subprocess.run(['docker', 'exec', container, 'python', '-c',
            "from pathlib import Path; import sys; sys.stdout.write(Path('/tmp/requests.jsonl').read_text()[:65537])"],
            capture_output=True, text=True, check=True, timeout=15).stdout
        case.assertLessEqual(len(log), READ_LIMIT)
        rows = [json.loads(line) for line in log.splitlines()]
        case.assertTrue(rows)
        case.assertTrue(all(row['method'] == 'GET' and row['path'] == '/v1/status' and row['authorized'] for row in rows))
        # Remove only this temporary inventory extension before the pre-existing worker drill.
        # Keep the newly rotated credentials, TLS and account evidence for cold restoration.
        compose('--profile', '*', 'stop')
        private(site / 'web/atraxis.yml', original_config)
        compose('up', '-d', '--no-build', '--force-recreate')
        await_read(lambda: request('/healthz', new_password, authority=renewed), lambda r: r[0] == 200, 'baseline-restored')
        print('OPERATOR_ACCESS_REPORT scoped_source=true wrong_context_denied=true no_administrator=true '
              'membership_revocation=true login_restart_verified=true engine_token_no_restart=true '
              'old_credentials_denied=true web_and_engine_ca_renewed=true state_preserved=true synthetic_only=true', flush=True)
        return new_password, renewed, reader_snapshot
    finally:
        if started:
            subprocess.run(['docker', 'rm', '-f', container], capture_output=True, text=True, timeout=30, check=True)
