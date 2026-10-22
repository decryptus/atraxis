"""Synthetic source and independent client for the isolated Docker acceptance."""
import base64
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import http.client
import json
from pathlib import Path
import socket
import ssl
import sys
import time

ROOT = Path('/fixtures')
SOURCE_AUTH = 'Basic ' + base64.b64encode(b'source-reader:synthetic-source-password').decode()
CLIENT_AUTH = 'Basic ' + base64.b64encode(b'reader:synthetic-reader-password').decode()
MODE = Path('/tmp/mode')
LOG = Path('/tmp/requests.jsonl')


def mode():
    return MODE.read_text() if MODE.exists() else 'ok'


class Source(ThreadingHTTPServer):
    daemon_threads = True

    def get_request(self):
        connection, address = self.socket.accept()
        connection.settimeout(2)
        selection = mode()
        variant = selection if selection in ('wrong-host', 'expired', 'untrusted') else 'ok'
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(ROOT / (variant + '.crt'), ROOT / (variant + '.key'))
        try:
            return context.wrap_socket(connection, server_side=True), address
        except Exception:
            connection.close()
            raise


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        authorized = self.headers.get('Authorization') == SOURCE_AUTH
        with LOG.open('a') as stream:
            stream.write(json.dumps(dict(method=self.command, path=self.path,
                                         authorized=authorized, mode=mode())) + '\n')
        if not authorized:
            self.send_error(401)
            return
        if mode() == 'silent':
            time.sleep(5)
            return
        if mode() == 'redirect':
            self.send_response(302)
            self.send_header('Location', 'https://certificates.test:8443/credential-trap')
            self.send_header('Content-Length', '0')
            self.end_headers()
            return
        now = time.time()
        stamp = datetime.fromtimestamp(now, timezone.utc).isoformat()
        if self.path == '/v1/status':
            payload = dict(api_version=1, ready=True, running=True, last_cycle_success=True,
                cycles_total=2, errors_total=0, age_seconds=0,
                last_success_at=now, last_cycle_finished_at=now,
                containers=[dict(id='a' * 64, name='network-demo', status='running',
                                 health='none', cpu_percent=250, mem_usage=None)])
        elif self.path == '/api/certificates/observations':
            payload = dict(schema_version=1, started_at=stamp, completed_at=stamp,
                completed_timestamp_seconds=now,
                certificates=[dict(certificate_id='12345678-1234-4234-8234-123456789abc',
                    domains=['example.test'], version=1, status='exists', origin='external',
                    renewal_owner='external', pending_action='none', material_state='missing',
                    not_before=None, not_after=None, not_after_timestamp_seconds=None,
                    fingerprint_sha256=None, issuance_retry_count=None, observed_at=stamp)])
        else:
            self.send_error(404)
            return
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except OSError:
            pass

    def do_POST(self):
        with LOG.open('a') as stream:
            stream.write(json.dumps(dict(method='POST', path=self.path)) + '\n')
        self.send_error(405)


def request(path='/v1/status', *, headers=None, ca='ca.pem', host='gateway.test'):
    context = ssl.create_default_context(cafile=str(ROOT / ca))
    connection = http.client.HTTPSConnection(host, 8443, timeout=3, context=context)
    try:
        connection.request('GET', path, headers=headers if headers is not None else
                           {'Authorization': CLIENT_AUTH})
        response = connection.getresponse()
        body = response.read()
        assert b'synthetic-source-password' not in body
        assert b'synthetic-reader-password' not in body
        return response.status, body
    finally:
        connection.close()


def await_state(expected):
    deadline = time.monotonic() + 25
    while time.monotonic() < deadline:
        try:
            code, body = request()
            assert code == 200, code
            status = json.loads(body)
            sources = {item['source_id']: item for item in status['sources']}
            monitor, certificates = sources['monitor'], sources['certificates']
            if expected == 'healthy':
                match = status['ready'] and monitor['current'] and certificates['current']
            else:
                outcome = monitor['last_attempt']['outcome']
                allowed = ('timeout', 'transport_error') if expected == 'disconnected' else (expected,)
                match = not status['ready'] and not monitor['current'] and outcome in allowed
                match = match and certificates['current']
            if match:
                assert request('/readyz')[0] == (200 if expected == 'healthy' else 503)
                resources = monitor['last_valid_snapshot']['resources']
                assert len(resources) == 1
                baseline = Path('/tmp/baseline.json')
                if baseline.exists():
                    assert resources == json.loads(baseline.read_text())
                else:
                    assert expected == 'healthy'
                    baseline.write_text(json.dumps(resources))
                print('State passed:', expected, flush=True)
                return
        except (OSError, http.client.HTTPException):
            pass
        time.sleep(.2)
    raise AssertionError('state_not_observed: ' + expected)


def main():
    action = sys.argv[1]
    if action == 'source':
        Source(('0.0.0.0', 8443), Handler).serve_forever()
    elif action == 'wait':
        await_state(sys.argv[2])
    elif action == 'security':
        for path in ('/healthz', '/readyz', '/v1/status'):
            assert request(path, headers={})[0] == 401
            assert request(path, headers={'X-Forwarded-User': 'reader',
                'X-Remote-User': 'reader', 'X-Forwarded-For': '127.0.0.1'})[0] == 401
        for ca, host in (('untrusted-ca.pem', 'gateway.test'), ('ca.pem', 'wrong.test')):
            try:
                request(ca=ca, host=host)
            except ssl.SSLCertVerificationError:
                pass
            else:
                raise AssertionError('invalid_certificate_accepted')
        for address in sys.argv[2:]:
            try:
                with socket.create_connection((address, 9810), timeout=1):
                    pass
            except OSError:
                pass
            else:
                raise AssertionError('backend_reachable_from_client')
        print('TLS, authentication, forwarded identity and backend isolation: passed')
    else:
        raise ValueError('unknown_fixture_action')


if __name__ == '__main__':
    main()
