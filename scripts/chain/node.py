"""Disposable service bootstrap and client probes; never a production entrypoint."""
import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import http.client
import json
import os
from pathlib import Path
import shutil
import signal
import ssl
import sys
import time

ROOT = Path('/state')
CASES = [dict(name='High CPU', input=dict(cpu_percent=95),
              expected=dict(decision='review', matched_rule='combined'))]


def private(path, data):
    path.write_text(json.dumps(data) if not isinstance(data, str) else data)
    path.chmod(0o600)


def bootstrap(role):
    marker = ROOT / 'initialized.json'
    if marker.exists():
        if json.loads(marker.read_text()) != {'version': 1, 'role': role}:
            raise RuntimeError('acceptance_state_role_mismatch')
    else:
        if any(ROOT.iterdir()):
            raise RuntimeError('acceptance_state_incomplete')
        for source in Path('/fixtures').iterdir():
            if source.is_file():
                destination = ROOT / source.name
                shutil.copyfile(source, destination)
                destination.chmod(0o600)
        if role in ('web', 'operator'):
            from atraxis.accounts import default_directory, Directory
            from atraxis.account_store import AccountStore
            directory = default_directory('alice').document()
            directory['organizations'][0]['id'] = 'demo'
            directory['memberships'][0]['organization'] = 'demo'
            AccountStore(ROOT / 'accounts.db').apply(Directory(directory), None)
        pending = ROOT / 'initialized.next'
        private(pending, dict(version=1, role=role))
        pending.replace(marker)
    commands = {
        'web': ['python', '-m', 'atraxis', 'serve', '--config', '/state/web.yml'],
        'engine': ['python', '-m', 'atropos.cli', 'rules-serve', '--config', '/state/engine.yml', '--port', '8082'],
        'auton': ['autond', '-f', '-c', '/state/auton.yml', '-p', '/tmp/auton.pid', '--logfile', '/state/auton.log'],
        'redis': ['redis-server', '/state/redis.conf'],
    }
    if role in ('operator', 'alert'):
        # These fixture processes are PID 1, so install an explicit TERM
        # handler. Product daemons install their own after exec below.
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    if role == 'operator':
        # The owning test/job bounds the fixture lifetime; TERM exits cleanly.
        while True:
            signal.pause()
    elif role == 'alert':
        ThreadingHTTPServer(('127.0.0.1', 8081), AlertHandler).serve_forever()
    else:
        os.execvp(commands[role][0], commands[role])


class AlertHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        if self.headers.get('Authorization') != 'Bearer ' + (ROOT / 'alert.token').read_text():
            self.send_error(401)
            return
        if self.path not in ('/api/v2/alerts', '/v1/test-alerts'):
            self.send_error(404)
            return
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        with (ROOT / 'deliveries.jsonl').open('a') as stream:
            stream.write(json.dumps(dict(path=self.path, body=body)) + '\n')
        if (ROOT / 'lose-ack').exists():
            self.close_connection = True
            return
        response = (dict(version=1, accepted=True, test_context=body['test_context'],
                         delivery_id=body['delivery_id']) if self.path == '/v1/test-alerts' else None)
        raw = json.dumps(response).encode() if response else b''
        self.send_response(202 if response else 200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def client():
    from atropos_client.client import RuleClient
    return RuleClient(ROOT / 'alice.yml', expected_actor=dict(subject='alice', organization='demo'))


def inventory():
    connection = http.client.HTTPSConnection('web.test', 8443, timeout=3,
        context=ssl.create_default_context(cafile=str(ROOT / 'ca.pem')))
    try:
        auth = json.loads((ROOT / 'inventory.json').read_text())
        headers = {'Authorization': 'Basic ' + base64.b64encode(
            (auth['username'] + ':' + auth['password']).encode()).decode()}
        connection.request('GET', '/v1/status?organization=demo', headers=headers)
        response = connection.getresponse()
        assert response.status == 200
        return json.loads(response.read())
    finally:
        connection.close()


def auton_jobs():
    connection = http.client.HTTPSConnection('auton.test', 8443, timeout=3,
        context=ssl.create_default_context(cafile=str(ROOT / 'ca.pem')))
    try:
        password = (ROOT / 'executor.password').read_text()
        headers = {'Authorization': 'Basic ' + base64.b64encode(
            ('operator:' + password).encode()).decode()}
        connection.request('GET', '/jobs', headers=headers)
        response = connection.getresponse()
        assert response.status == 200
        return json.loads(response.read())['jobs']
    finally:
        connection.close()


def engine_admin(arguments):
    # Test-only bridge to the existing trusted-host CLI. Resolve is an operator
    # assertion after external inspection, never a remotely delegated action.
    if not arguments or arguments[0] not in ('status', 'resolve', 'queue-status', 'queue-configure'):
        raise ValueError('unsupported_acceptance_admin_command')
    from atropos.rule_operator import load_operator_config
    document, _ = load_operator_config(ROOT / 'engine.yml', remote=True)
    environment = {k: v for k, v in os.environ.items()
        if not k.startswith(('ATROPOS_STORAGE_', 'ATROPOS_QUEUE_')) and k != 'ATROPOS_REDIS_URL'}
    for role in ('storage', 'queue'):
        for key, value in document[role].items():
            environment['ATROPOS_' + role.upper() + '_' + key.upper()] = (
                json.dumps(value) if key == 'options' else value)
    os.execvpe('python', ['python', '-m', 'atropos.cli', '--namespace', document['namespace'],
                         *arguments], environment)


def main():
    command = sys.argv[1]
    if command == 'start':
        bootstrap(sys.argv[2])
    elif command == 'engine-admin':
        engine_admin(sys.argv[2:])
    elif command == 'auton-jobs':
        print(json.dumps(auton_jobs()))
    elif command == 'inventory':
        print(json.dumps(inventory()))
    elif command == 'activate':
        c = client()
        try:
            rule = c.create_from_templates(['cpu-high'], name='observed', rule_kind='intervention',
                                           selections=[['container', 'web']])
            report = c.test('observed', rule['revision'], CASES)
            assert report['passed']
            active = c.activate('observed', rule['revision'], test_id=report['id'], expected_generation=None)
            print(json.dumps(dict(revision=rule['revision'], generation=active['generation'])))
        finally:
            c.close()
    elif command == 'rule-evidence':
        c = client()
        try:
            active = c.active('observed')
            print(json.dumps(dict(active=active, rule=c.read('observed', sys.argv[2]), audit=c.audit('observed'))))
        finally:
            c.close()
    elif command == 'catalog':
        c = client()
        try:
            assert c.catalog()
        finally:
            c.close()
    elif command == 'status':
        c = client()
        try:
            print(json.dumps(c.dispatch_status('observed', sys.argv[2])))
        finally:
            c.close()
    elif command == 'manual-denied':
        c = client()
        try:
            try:
                c.dispatch('observed', ['container', 'web'], {'cpu_percent': 250}, sys.argv[2],
                           expected_generation=sys.argv[3], event_id='manual')
            except PermissionError:
                print('Manual admission denied')
            else:
                raise AssertionError('manual_admission_accepted')
        finally:
            c.close()
    else:
        raise ValueError('unknown_acceptance_command')


if __name__ == '__main__':
    main()
