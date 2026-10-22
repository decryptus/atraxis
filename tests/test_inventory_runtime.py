"""Actual HTTPdis daemon, source HTTP workers and authenticated inventory."""
import base64
from datetime import datetime, timezone
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import http.client
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest

import yaml

from test_collection import wait_for
from test_observations import certlord, monit


class SourceHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        with self.server.lock:
            self.server.requests.append((self.command, self.path,
                                         self.headers.get('Authorization')))
            mode = self.server.mode
        self.server.entered.set()
        if mode == 'silent':
            self.server.release.wait(10)
            return
        payload = monit() if self.path == '/v1/status' else certlord()
        if self.path != '/v1/status':
            now = time.time()
            stamp = datetime.fromtimestamp(now, timezone.utc).isoformat()
            payload.update(started_at=stamp, completed_at=stamp,
                           completed_timestamp_seconds=now)
            payload['certificates'][0]['observed_at'] = stamp
        if mode == 'invalid':
            payload = {'secret-marker': 'invalid'}
        body = json.dumps(payload).encode()
        try:
            self.send_response(mode if type(mode) is int else 200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except OSError:
            pass


class InventoryRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.monitor = self.upstream()
        self.certificates = self.upstream()
        hashed = base64.b64encode(hashlib.sha1(b'fixture-password').digest()).decode()
        (self.root / 'users').write_text('reader:{SHA}' + hashed + '\n')
        (self.root / 'outbound.json').write_text(json.dumps(
            {'username': 'source-reader', 'password': 'outbound-secret-marker'}))
        self.config = {'general': {'auth_basic_file': 'users'}, 'sources': [
            dict(source_id=name, kind=kind,
                 origin='http://127.0.0.1:%s' % server.server_port,
                 allow_loopback_http=True, credentials_file='outbound.json',
                 poll_interval=1, timeout=1, max_age=60)
            for name, kind, server in (('monitor', 'monit_docker', self.monitor),
                                      ('certificates', 'certlord', self.certificates))]}

    def upstream(self):
        server = ThreadingHTTPServer(('127.0.0.1', 0), SourceHandler)
        server.daemon_threads = True
        server.mode, server.requests = 'ok', []
        server.lock = threading.Lock()
        server.entered, server.release = threading.Event(), threading.Event()
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        def close():
            server.release.set()
            server.shutdown()
            server.server_close()
            thread.join(2)
        self.addCleanup(close)
        return server

    def start(self):
        # Reserve then release a loopback port for the child daemon.
        with socket.socket() as reserve:
            reserve.bind(('127.0.0.1', 0))
            self.port = reserve.getsockname()[1]
        self.config['general']['listen_port'] = self.port
        path = self.root / 'config.yml'
        path.write_text(yaml.safe_dump(self.config))
        self.process = subprocess.Popen([sys.executable, '-m', 'atraxis', 'serve',
            '--config', str(path)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        def cleanup():
            if self.process.poll() is None:
                self.process.terminate()
            try:
                self.process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.communicate()
                self.fail('daemon did not stop')
        self.addCleanup(cleanup)
        wait_for(lambda: self.request('/healthz')[0] == 200, timeout=5)

    def request(self, path='/v1/status', method='GET', authorized=True):
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=2)
        try:
            headers = {}
            if authorized:
                headers['Authorization'] = 'Basic ' + base64.b64encode(
                    b'reader:fixture-password').decode()
            connection.request(method, path, headers=headers)
            response = connection.getresponse()
            body = response.read()
            return response.status, body
        except (OSError, http.client.HTTPException):
            return None, b''
        finally:
            connection.close()

    def status(self):
        code, body = self.request()
        self.assertEqual(code, 200)
        return json.loads(body)

    def test_authenticated_inventory_failure_recovery_and_read_only_routes(self):
        self.start()
        wait_for(lambda: self.status()['ready'], timeout=5)
        self.assertEqual(self.request('/readyz')[0], 200)
        initial = self.status()
        for path in ('/healthz', '/readyz', '/v1/status'):
            code, body = self.request(path, authorized=False)
            self.assertEqual(code, 401)
            self.assertNotIn(b'last_valid_snapshot', body)
        self.assertEqual(self.request(method='HEAD'), (200, b''))
        self.assertEqual(self.request(method='POST')[0], 405)
        self.assertEqual(self.request('/v1/actions', method='POST')[0], 404)
        with self.monitor.lock:
            self.monitor.mode = 401
        wait_for(lambda: self.status()['sources'][0]['last_attempt']['outcome'] == 'authentication_error')
        failed = self.status()
        code, page = self.request('/ui')
        self.assertEqual(code, 200)
        self.assertIn(b'Historical snapshot', page)
        self.assertIn(b'Current at page load', page)
        self.assertIn(b'authentication error', page)
        self.assertNotIn(b'outbound-secret-marker', page)
        self.assertFalse(failed['ready'])
        self.assertEqual(self.request('/readyz')[0], 503)
        self.assertTrue(failed['sources'][1]['current'])
        self.assertEqual(failed['sources'][0]['last_valid_snapshot']['resources'],
                         initial['sources'][0]['last_valid_snapshot']['resources'])
        with self.monitor.lock:
            self.monitor.mode = 'invalid'
        wait_for(lambda: self.status()['sources'][0]['last_attempt']['outcome'] == 'invalid_payload')
        self.assertNotIn('secret-marker', json.dumps(self.status()))
        with self.monitor.lock:
            self.monitor.mode = 'ok'
        wait_for(lambda: self.status()['ready'])
        expected_auth = 'Basic ' + base64.b64encode(
            b'source-reader:outbound-secret-marker').decode()
        for upstream, route in ((self.monitor, '/v1/status'),
                                (self.certificates, '/api/certificates/observations')):
            with upstream.lock:
                self.assertTrue(upstream.requests)
                self.assertTrue(all(item == ('GET', route, expected_auth)
                                    for item in upstream.requests))

    def test_sigterm_cancels_inflight_source_and_stops_daemon(self):
        self.monitor.mode = 'silent'
        for config in self.config['sources']:
            config.update(timeout=10, poll_interval=10)
        self.start()
        self.assertTrue(self.monitor.entered.wait(3))
        wait_for(lambda: self.status()['sources'][1]['current'])
        before = time.monotonic()
        self.process.terminate()
        stdout, stderr = self.process.communicate(timeout=4)
        self.assertEqual(self.process.returncode, 0, stderr.decode())
        self.assertLess(time.monotonic() - before, 4.)
        self.assertNotIn(b'outbound-secret-marker', stdout + stderr)

    def test_invalid_inbound_authentication_starts_no_source_reads(self):
        (self.root / 'users').write_text('not-an-htpasswd-entry\n')
        path = self.root / 'config.yml'
        path.write_text(yaml.safe_dump(self.config))
        completed = subprocess.run([sys.executable, '-m', 'atraxis', 'serve',
            '--config', str(path)], capture_output=True, timeout=5)
        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(self.monitor.requests, [])
        self.assertEqual(self.certificates.requests, [])

    def client_config(self, port):
        (self.root / 'client.json').write_text(json.dumps(
            {'username': 'reader', 'password': 'fixture-password'}))
        path = self.root / 'client.yml'
        path.write_text(yaml.safe_dump(dict(origin='http://127.0.0.1:%s' % port,
            allow_loopback_http=True, credentials_file='client.json', timeout=10)))
        return path

    def test_real_cli_reads_atraxis_with_separate_credentials_and_exit_codes(self):
        self.start()
        wait_for(lambda: self.status()['ready'], timeout=5)
        client_path = self.client_config(self.port)
        command = [sys.executable, '-m', 'atraxis', 'inventory', '--config', str(client_path), '--json']
        success = subprocess.run(command, capture_output=True, timeout=5)
        self.assertEqual(success.returncode, 0, success.stderr.decode())
        payload = json.loads(success.stdout)
        self.assertTrue(payload['ready'])
        self.assertEqual(len(payload['sources']), 2)
        with self.monitor.lock:
            self.monitor.mode = 503
        wait_for(lambda: not self.status()['ready'])
        degraded = subprocess.run(command, capture_output=True, timeout=5)
        self.assertEqual(degraded.returncode, 2, degraded.stderr.decode())
        self.assertFalse(json.loads(degraded.stdout)['sources'][0]['current'])
        (self.root / 'client.json').write_text(json.dumps({'username': 'reader', 'password': 'wrong'}))
        denied = subprocess.run(command, capture_output=True, timeout=5)
        self.assertEqual(denied.returncode, 1)
        self.assertEqual(denied.stdout, b'')
        self.assertIn(b'authentication_error', denied.stderr)

    def test_cli_sigterm_cancels_blocked_http_worker(self):
        self.monitor.mode = 'silent'
        path = self.client_config(self.monitor.server_port)
        process = subprocess.Popen([sys.executable, '-m', 'atraxis', 'status',
            '--config', str(path)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        def cleanup():
            if process.poll() is None:
                process.kill()
            process.communicate()
        self.addCleanup(cleanup)
        self.assertTrue(self.monitor.entered.wait(3))
        process.terminate()
        stdout, stderr = process.communicate(timeout=4)
        self.assertEqual(process.returncode, 143, stderr.decode())
        self.assertEqual(stdout, b'')
