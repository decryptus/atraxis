"""Synthetic loopback servers only; never contact configured real installations."""
import base64
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from atraxis.source_config import parse_sources
from atraxis.transport import HTTPTransport
from atraxis import transport_worker


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        self.server.requests.append((self.command, self.path, self.headers.get('Authorization')))
        self.server.entered.set()
        mode = self.server.mode
        try:
            if mode == 'headers':
                self.server.release.wait(5)
            self.send_response(mode if type(mode) is int else 200)
            self.send_header('Content-Type', 'text/plain' if mode == 'type' else 'application/json')
            if mode == 302:
                self.send_header('Location', self.server.redirect)
            if mode == 'gzip':
                self.send_header('Content-Encoding', 'gzip')
            if mode == 'declared_large':
                self.send_header('Content-Length', '99999999')
            if mode == 'truncated':
                self.send_header('Content-Length', '100')
            if mode == 'duplicate_length':
                self.send_header('Content-Length', '2')
                self.send_header('Content-Length', '3')
            if mode in ('chunked', 'large_chunked'):
                self.send_header('Transfer-Encoding', 'chunked')
            self.end_headers()
            if mode == 'silent':
                self.server.release.wait(5)
                return
            if mode == 'trickle':
                for _ in range(40):
                    self.wfile.write(b' ')
                    self.wfile.flush()
                    if self.server.release.wait(.1):
                        break
                return
            body = {'invalid': b'private-body-marker', 'duplicate': b'{"a":1,"a":2}',
                    'nonfinite': b'{"a":NaN}', 'overflow': b'{"a":1e999}',
                    'array': b'[]',
                    'large': b'{"a":"' + b'x' * 2048 + b'"}',
                    'large_chunked': b'x' * 2048}.get(mode, b'{"ok":true}')
            if mode in ('chunked', 'large_chunked'):
                self.wfile.write(('%x\r\n' % len(body)).encode() + body + b'\r\n0\r\n\r\n')
            else:
                self.wfile.write(body)
        except (OSError, ssl.SSLError):
            pass


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.credentials = self.root / 'credentials.json'
        self.credentials.write_text(json.dumps({'username': 'reader', 'password': 'secret-marker'}))
        self.transport = HTTPTransport(2)

    def server(self, mode='ok', cert=None, key=None):
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        server.daemon_threads = True
        server.mode, server.requests = mode, []
        server.entered, server.release = threading.Event(), threading.Event()
        server.redirect = 'http://127.0.0.1:1/never'
        if cert is not None:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(cert, key)
            server.socket = context.wrap_socket(server.socket, server_side=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        def close():
            server.release.set()
            server.shutdown()
            server.server_close()
            thread.join(2)
        self.addCleanup(close)
        return server

    def source(self, server, **changes):
        document = dict(source_id='source-a', kind='monit_docker',
                        origin='http://127.0.0.1:%s' % server.server_port,
                        allow_loopback_http=True, credentials_file=str(self.credentials),
                        timeout=2, max_response_bytes=1024)
        document.update(changes)
        return parse_sources([document], self.root / 'config.yml')[0]

    def test_fixed_get_credentials_and_ambient_settings_ignored(self):
        server, proxy = self.server(), self.server()
        (self.root / '.netrc').write_text('default login ambient password ambient-secret\n')
        env = {key: 'http://127.0.0.1:%s' % proxy.server_port
               for key in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'http_proxy', 'https_proxy')}
        env.update(NETRC=str(self.root / '.netrc'), NO_PROXY='')
        with patch.dict(os.environ, env):
            for kind, route in [('monit_docker', '/v1/status'),
                                ('certlord', '/api/certificates/observations')]:
                result = self.transport.fetch(self.source(server, kind=kind))
                self.assertEqual(result.outcome, 'success')
                self.assertEqual(result.payload, {'ok': True})
                self.assertGreater(result.duration_seconds, 0)
                self.assertEqual(server.requests[-1], ('GET', route, 'Basic ' +
                                 base64.b64encode(b'reader:secret-marker').decode()))
        self.assertEqual(proxy.requests, [])
        self.assertNotIn('secret-marker', repr(result))
        self.assertLessEqual(result.attempted_at, result.received_at)

    def test_status_codes_and_redirect_never_forward_credentials(self):
        server, redirect = self.server(), self.server()
        server.redirect = 'http://127.0.0.1:%s/v1/status' % redirect.server_port
        for status, expected in [(302, 'redirect_refused'), (401, 'authentication_error'),
                                 (403, 'forbidden'), (404, 'endpoint_unavailable'),
                                 (410, 'endpoint_unavailable'), (503, 'source_error'),
                                 (204, 'source_error')]:
            server.mode = status
            with self.subTest(status=status):
                result = self.transport.fetch(self.source(server))
                self.assertEqual(result.outcome, expected)
                self.assertIsNone(result.payload)
        self.assertEqual(redirect.requests, [])

    def test_body_framing_size_and_json_validation(self):
        server = self.server()
        cases = [('large', 'response_too_large'), ('declared_large', 'response_too_large'),
                 ('large_chunked', 'response_too_large'), ('chunked', 'success')]
        cases += [(mode, 'invalid_payload') for mode in
                  ('type', 'gzip', 'invalid', 'duplicate', 'nonfinite', 'overflow', 'array',
                   'duplicate_length', 'truncated')]
        for mode, expected in cases:
            server.mode = mode
            with self.subTest(mode=mode):
                result = self.transport.fetch(self.source(server))
                self.assertEqual(result.outcome, expected)
                self.assertNotIn('private-body-marker', repr(result))

    def test_silent_headers_body_and_trickle_share_total_deadline_and_recover(self):
        for mode in ('headers', 'silent', 'trickle'):
            with self.subTest(mode=mode):
                server = self.server(mode)
                source = self.source(server, timeout=1)
                started = time.monotonic()
                self.assertEqual(self.transport.fetch(source).outcome, 'timeout')
                self.assertLess(time.monotonic() - started, 2.5)
                server.release.set()
                server.mode = 'ok'
                self.assertEqual(self.transport.fetch(source).outcome, 'success')

    def test_busy_overlap_isolation_and_cancellation_release_capacity(self):
        slow, fast = self.server('silent'), self.server()
        source = self.source(slow, timeout=5)
        cancel = threading.Event()
        with ThreadPoolExecutor(2) as executor:
            pending = executor.submit(self.transport.fetch, source, cancel)
            self.assertTrue(slow.entered.wait(3))
            self.assertEqual(self.transport.fetch(source).outcome, 'busy')
            other = self.source(fast, source_id='source-b')
            self.assertEqual(self.transport.fetch(other).outcome, 'success')
            cancel.set()
            self.assertEqual(pending.result(timeout=2).outcome, 'cancelled')
        slow.release.set()
        slow.mode = 'ok'
        self.assertEqual(self.transport.fetch(source).outcome, 'success')
        cancel.set()
        count = len(slow.requests)
        self.assertEqual(self.transport.fetch(source, cancel).outcome, 'cancelled')
        self.assertEqual(len(slow.requests), count)

    def test_global_capacity_is_bounded(self):
        transport = HTTPTransport(1)
        slow = self.server('silent')
        source = self.source(slow, timeout=5)
        cancel = threading.Event()
        with ThreadPoolExecutor(1) as executor:
            pending = executor.submit(transport.fetch, source, cancel)
            self.assertTrue(slow.entered.wait(3))
            self.assertEqual(transport.fetch(replace(source, source_id='b')).outcome, 'busy')
            cancel.set()
            self.assertEqual(pending.result(timeout=2).outcome, 'cancelled')
        for invalid in (True, 0, 33, 1.5):
            with self.assertRaises(ValueError):
                HTTPTransport(invalid)

    def test_credentials_reloaded_and_errors_redacted(self):
        server = self.server()
        source = self.source(server)
        self.credentials.write_text('{"username":"other","password":"rotated"}')
        self.assertEqual(self.transport.fetch(source).outcome, 'success')
        self.assertEqual(server.requests[-1][2], 'Basic ' + base64.b64encode(b'other:rotated').decode())
        self.credentials.write_text('{"password":"secret-marker"}')
        result = self.transport.fetch(source)
        self.assertEqual(result.outcome, 'configuration_error')
        self.assertNotIn('secret-marker', repr(result))
        self.assertEqual(len(server.requests), 1)

    def certificate(self, name, san):
        cert, key = self.root / (name + '.pem'), self.root / (name + '.key')
        subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes',
                        '-days', '1', '-subj', '/CN=fixture', '-addext', 'subjectAltName=' + san,
                        '-keyout', str(key), '-out', str(cert)], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
        return cert, key

    def test_real_tls_trust_and_hostname_verification(self):
        cert, key = self.certificate('valid', 'IP:127.0.0.1')
        server = self.server(cert=cert, key=key)
        source = self.source(server, origin='https://127.0.0.1:%s' % server.server_port,
                             allow_loopback_http=False, ca_file=str(cert))
        self.assertEqual(self.transport.fetch(source).outcome, 'success')
        self.assertEqual(self.transport.fetch(replace(source, ca_file=None)).outcome, 'tls_error')
        wrong_cert, wrong_key = self.certificate('wrong', 'DNS:wrong.invalid')
        wrong = self.server(cert=wrong_cert, key=wrong_key)
        mismatch = replace(source, origin='https://127.0.0.1:%s' % wrong.server_port,
                           ca_file=str(wrong_cert))
        self.assertEqual(self.transport.fetch(mismatch).outcome, 'tls_error')
        self.assertEqual(wrong.requests, [])

    def test_worker_dns_failure_is_safe(self):
        server = self.server()
        from dataclasses import asdict
        with patch('socket.getaddrinfo', side_effect=OSError('secret-marker')):
            result = transport_worker.fetch(asdict(self.source(server)))
        self.assertEqual(result, {'outcome': 'transport_error'})
        self.assertEqual(server.requests, [])

    def test_blocked_dns_is_killed_and_reaped_before_recovery(self):
        server = self.server()
        source = self.source(server, timeout=1)
        real_popen = subprocess.Popen
        children = []

        def launch(command, **kwargs):
            # A real worker with a synthetic resolver that never returns within
            # the deadline; no external DNS or network request is made.
            code = ('import socket,time; '
                    'socket.getaddrinfo=lambda *a,**k: time.sleep(30); '
                    'from atraxis.transport_worker import main; main()')
            child = real_popen([sys.executable, '-c', code], **kwargs)
            children.append(child)
            return child

        started = time.monotonic()
        with patch('atraxis.transport.subprocess.Popen', side_effect=launch):
            result = self.transport.fetch(source)
        self.assertEqual(result.outcome, 'timeout')
        self.assertLess(time.monotonic() - started, 2.5)
        self.assertEqual(server.requests, [])
        self.assertIsNotNone(children[0].poll())
        self.assertEqual(self.transport.fetch(source).outcome, 'success')

    def test_invalid_runtime_budgets_do_not_spawn(self):
        source = self.source(self.server())
        with patch('atraxis.transport.subprocess.Popen') as spawn:
            for value in (True, 0, 61, float('nan'), float('inf')):
                self.assertEqual(self.transport.fetch(replace(source, timeout=value)).outcome,
                                 'configuration_error')
            for value in (True, 0, 4194305):
                self.assertEqual(self.transport.fetch(replace(source, max_response_bytes=value)).outcome,
                                 'configuration_error')
            spawn.assert_not_called()
