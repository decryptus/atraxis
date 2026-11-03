"""Synthetic preparation qualification; no production network or service changes."""
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from ipaddress import ip_address
import json
import os
from pathlib import Path
import socket
import ssl
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from atraxis.installation.engine import Engine, serialize
from atraxis.installation.filesystem import Root, DIRECTORY_FLAGS, open_root
from atraxis.installation import material as material_check, probe
from atraxis.installation.tls import validate_tls
from atraxis.installation.validation import Authority, InstallError, decode, parse, origin, credentials
from test_observations import monit, certlord, NOW

SECRET = "synthetic-'\"; DROP TABLE accounts; $(touch forbidden)"
LOGIN = {'username': 'operator', 'password': SECRET}


def stalled_material_worker(_pipe, *_args):
    time.sleep(30)  # Disposable worker must be killed well before this returns.


def material(*, name='127.0.0.1', expired=False):
    now = datetime.now(timezone.utc)
    key = ec.generate_private_key(ec.SECP256R1())
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'Disposable installation CA')])
    ca = (x509.CertificateBuilder().subject_name(ca_name).issuer_name(ca_name)
          .public_key(ca_key.public_key()).serial_number(x509.random_serial_number())
          .not_valid_before(now - timedelta(days=2)).not_valid_after(now + timedelta(days=2))
          .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
          .sign(ca_key, hashes.SHA256()))
    try: san = x509.IPAddress(ip_address(name))
    except ValueError: san = x509.DNSName(name)
    cert = (x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)]))
            .issuer_name(ca.subject).public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(days=2))
            .not_valid_after(now + timedelta(days=-1 if expired else 1))
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.SubjectAlternativeName([san]), critical=False)
            .sign(ca_key, hashes.SHA256()))
    return (cert.public_bytes(serialization.Encoding.PEM),
            key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                              serialization.NoEncryption()), ca.public_bytes(serialization.Encoding.PEM))


def plan(url='https://source.example.test'):
    return {'version': 1, 'name': 'pilot', 'listen_address': '127.0.0.1', 'listen_port': 18443,
            'server_name': '127.0.0.1',
            'sources': [{'source_id': 'monit', 'kind': 'monit_docker', 'origin': url}]}


def policy(url='https://source.example.test'):
    return Authority((url,), ('127.0.0.0/8',))


class ValidationTests(unittest.TestCase):
    def test_paths_extras_modes_and_noncanonical_values_are_rejected(self):
        for key, values in {'name': ('/', '/tmp', '/root', '../x', 'a/b', 'a\\b', '.', '..', 'x\x00'),
                            'listen_port': (True, '8080', 0, 80, 65536),
                            'listen_address': ('0.0.0.0', '127.1', '127.000.0.1', '::', '224.0.0.1'),
                            'server_name': ('example.test\n', 'Example.test', '*.example.test')}.items():
            for value in values:
                with self.subTest(key=key, value=value), self.assertRaises(InstallError):
                    item = plan(); item[key] = value; parse(item)
        with self.assertRaises(InstallError) as raised:
            parse(dict(plan(), server_name='bad host'))
        self.assertEqual(raised.exception.field, 'server_name')
        for key in ('root', 'command', 'sql', 'advanced', 'ca_file', 'environment'):
            with self.subTest(key=key), self.assertRaises(InstallError):
                parse(dict(plan(), **{key: '/root'}))

    def test_urls_cannot_smuggle_credentials_routes_or_syntax(self):
        for url in ('http://example.test', 'https://u:p@example.test', 'https://example.test/',
                    'https://example.test/x', 'https://example.test?x=y', 'https://example.test#x',
                    'https://example.test:0443', 'https://127.1', 'https://2130706433',
                    'https://169.254.169.254', 'https://[::ffff:127.0.0.1]',
                    'https://[fe80::1%25eth0]', 'https://example.test\\@evil.test',
                    'https://${HOST}', 'https://EXAMPLE.test', ' https://example.test',
                    'https://example.test\nAuthorization: secret'):
            with self.subTest(url=url), self.assertRaisesRegex(InstallError, '^invalid_origin$'):
                origin(url)
        for url in ('https://example.test', 'https://127.0.0.1:443', 'https://[::1]:8443'):
            self.assertEqual(origin(url), url)

    def test_json_is_bounded_unique_and_typed(self):
        for raw in (b'{"a":1,"a":2}', b'{"a":NaN}', b'{"a":Infinity}', b'\xff', b' ' * 16385,
                    b'[' * 1200 + b']' * 1200):
            with self.subTest(size=len(raw)), self.assertRaises(InstallError): decode(raw)
        for raw in (b'null', b'[]', b'"text"', b'1'):
            with self.assertRaises(InstallError): parse(decode(raw))
        item = plan(); item['sources'] *= 2
        with self.assertRaises(InstallError): parse(item)
        item = plan(); item['sources'][0]['kind'] = 'os.system'
        with self.assertRaises(InstallError): parse(item)

    def test_host_authority_cannot_be_expanded_by_the_form(self):
        engine = Engine(policy())
        self.assertEqual(engine.preview(serialize(plan()))['status'], 'unverified')
        for change in ({'listen_address': '127.0.0.2'}, {'sources': plan('https://source.example.test:444')['sources']}):
            with self.assertRaises(InstallError): engine.preview(serialize(dict(plan(), **change)))
        for ip in ('169.254.169.254', '10.0.0.1', '0.0.0.0', '::ffff:127.0.0.1'):
            with self.assertRaises(InstallError): policy().check_address(ip)
        with self.assertRaises(InstallError): Authority(policy().origins, ('0.0.0.0/0',)).validate()

    def test_secrets_are_data_and_never_in_errors(self):
        self.assertEqual(credentials(LOGIN, login=True)['password'], SECRET)
        for value in ({'username': 'a:b', 'password': SECRET},
                      {'username': 'operator', 'password': SECRET + '\n'},
                      {'username': 'operator', 'password': 'x' * 4097},
                      {'username': 'operator', 'password': '\ud800'}):
            with self.assertRaises(InstallError) as raised: credentials(value)
            self.assertNotIn(SECRET, str(raised.exception) + repr(raised.exception) + str(raised.exception.report()))


class FilesystemTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name); os.chmod(self.path, 0o700)
        self.root = Root(os.open(self.path, DIRECTORY_FLAGS))
        self.addCleanup(self.temp.cleanup); self.addCleanup(self.root.close)

    def test_create_only_private_files_and_manifest_last(self):
        order = []
        original = os.open
        def opened(name, *args, **kwargs):
            order.append(name); return original(name, *args, **kwargs)
        with patch('atraxis.installation.filesystem.os.open', side_effect=opened):
            self.root.create('new', {'complete.json': b'{}', 'plan.json': b'{}'})
        self.assertEqual(order[-1], 'complete.json')
        self.assertEqual((self.path / 'new').stat().st_mode & 0o777, 0o700)
        self.assertTrue(all(p.stat().st_mode & 0o777 == 0o600 for p in (self.path / 'new').iterdir()))
        with self.assertRaisesRegex(InstallError, 'destination_exists'):
            self.root.create('new', {'complete.json': b'changed'})
        self.assertEqual((self.path / 'new/complete.json').read_bytes(), b'{}')

    def test_form_paths_and_file_names_cannot_escape(self):
        for name in ('/', '/tmp', '/root', '../escape', 'a/b', '..', 'x\\y'):
            with self.assertRaises(InstallError): self.root.create(name, {'complete.json': b'{}'})
        for name in ('../escape', '/tmp/file', 'source-../x.json', 'source-a/b.ca.pem', 'config.yml'):
            with self.assertRaises(InstallError): self.root.create('new', {name: b'x', 'complete.json': b'{}'})
        self.assertEqual(list(self.path.iterdir()), [])

    def test_symlink_and_hardlink_destinations_are_not_followed(self):
        target = self.path / 'target'; target.write_bytes(b'original')
        (self.path / 'link').symlink_to(target)
        os.link(target, self.path / 'hard')
        for name in ('link', 'hard'):
            with self.assertRaises(InstallError): self.root.create(name, {'complete.json': b'{}'})
        self.assertEqual(target.read_bytes(), b'original')

    def test_mode_is_rechecked_and_partial_failure_never_reused(self):
        os.chmod(self.path, 0o755)
        with self.assertRaisesRegex(InstallError, 'unsafe_root'):
            self.root.create('new', {'complete.json': b'{}'})
        os.chmod(self.path, 0o700)
        with patch('atraxis.installation.filesystem.os.fsync', side_effect=OSError(SECRET)):
            with self.assertRaisesRegex(InstallError, '^write_failed$'):
                self.root.create('partial', {'plan.json': b'{}', 'complete.json': b'{}'})
        self.assertFalse((self.path / 'partial/complete.json').exists())
        with self.assertRaisesRegex(InstallError, 'destination_exists'):
            self.root.create('partial', {'complete.json': b'{}'})

    def test_marker_write_failure_is_uncertain(self):
        with patch('atraxis.installation.filesystem.os.fsync', side_effect=OSError(SECRET)):
            with self.assertRaisesRegex(InstallError, '^publication_uncertain$'):
                self.root.create('partial', {'complete.json': b'{}'})

    def test_symlink_swap_after_mkdir_is_refused(self):
        original = os.mkdir
        outside = self.path / 'outside'; outside.mkdir()
        def swapped(name, *args, **kwargs):
            original(name, *args, **kwargs)
            (self.path / name).rmdir(); (self.path / name).symlink_to(outside)
        with patch('atraxis.installation.filesystem.os.mkdir', side_effect=swapped):
            with self.assertRaises(InstallError): self.root.create('swap', {'complete.json': b'{}'})
        self.assertEqual(list(outside.iterdir()), [])

    def test_root_walk_refuses_symlink_ancestors(self):
        # Substitute only the trusted host constant, never a form path.
        (self.path / 'alias').symlink_to(self.path, target_is_directory=True)
        with patch('atraxis.installation.filesystem.INSTALLATION_ROOT', str(self.path / 'alias')):
            with self.assertRaisesRegex(InstallError, 'unsafe_root'): open_root()

    def test_concurrent_prepare_has_only_one_winner(self):
        outcomes = []
        def attempt():
            try: outcomes.append(self.root.create('race', {'complete.json': b'{}'})['status'])
            except InstallError as error: outcomes.append(error.code)
        threads = [threading.Thread(target=attempt) for _ in range(2)]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        self.assertCountEqual(outcomes, ['prepared', 'destination_exists'])


class TLSTests(unittest.TestCase):
    def test_material_worker_sanitizes_parser_errors_and_rejects_unbounded_input(self):
        pipe = unittest.mock.Mock()
        with patch('atraxis.installation.material.validate_tls', side_effect=RuntimeError(SECRET)):
            material_check._worker(pipe, b'', b'', 'example.test', ())
        pipe.send_bytes.assert_called_once_with(b'invalid_tls')
        with patch('atraxis.installation.material.run') as spawn:
            with self.assertRaises(InstallError): material_check.check(b'x' * 131073, b'key', 'example.test', (b'ca',))
            spawn.assert_not_called()

    def test_key_hostname_expiry_and_extra_material(self):
        cert, key, ca = material()
        validate_tls(cert + ca, key, '127.0.0.1')
        other_cert, other_key, _ = material()
        expired, expired_key, _ = material(expired=True)
        for pem, private, name in ((cert, other_key, '127.0.0.1'), (cert, key, 'wrong.example.test'),
                                  (expired, expired_key, '127.0.0.1'), (cert + b'\nexec arbitrary', key, '127.0.0.1'),
                                  (cert, key + key, '127.0.0.1'), (cert, key, '127.0.0.2')):
            with self.assertRaisesRegex(InstallError, '^invalid_tls$'): validate_tls(pem, private, name)
        with self.assertRaisesRegex(InstallError, '^invalid_tls$'): validate_tls(cert, SECRET.encode(), '127.0.0.1')


class ProbeTests(unittest.TestCase):
    def test_dns_all_answers_checked_before_connect(self):
        answers = [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', ('127.0.0.1', 443)),
                   (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', ('169.254.169.254', 443))]
        with patch.object(socket, 'getaddrinfo', return_value=answers), patch.object(socket, 'socket') as network:
            with self.assertRaises(InstallError): probe.resolve('source.example.test', 443, policy())
            network.assert_not_called()
        with patch.object(socket, 'getaddrinfo', return_value=answers[:1]) as dns:
            endpoint = probe.resolve('source.example.test', 443, policy())
        self.assertEqual(endpoint, (socket.AF_INET, '127.0.0.1')); self.assertEqual(dns.call_count, 1)

    def test_connect_uses_numeric_address_and_original_tls_hostname(self):
        tls = unittest.mock.Mock()
        raw = unittest.mock.Mock()
        with patch.object(socket, 'socket', return_value=raw), patch.object(socket, 'getaddrinfo') as dns:
            connection = probe.PinnedHTTPS('source.example.test', 443, (socket.AF_INET, '127.0.0.1'), tls)
            connection.connect()
        raw.connect.assert_called_once_with(('127.0.0.1', 443)); dns.assert_not_called()
        tls.wrap_socket.assert_called_once_with(raw, server_hostname='source.example.test')

    def test_worker_sanitizes_unexpected_errors(self):
        pipe = unittest.mock.Mock()
        with patch('atraxis.installation.probe._request', side_effect=RuntimeError(SECRET)):
            probe._worker(pipe, None, None, None, None)
        pipe.send_bytes.assert_called_once_with(b'probe_failed')


class PreparationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.path = Path(self.temp.name)
        self.path.chmod(0o700)
        self.root = Root(os.open(self.path, DIRECTORY_FLAGS))
        self.addCleanup(self.temp.cleanup); self.addCleanup(self.root.close)
        self.cert, self.key, self.ca = material()
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0)); self.port = sock.getsockname()[1]
        self.document = plan(); self.document['listen_port'] = self.port
        self.inputs = dict(login=LOGIN, source_credentials={'monit': LOGIN}, certificate=self.cert,
                           private_key=self.key, source_cas={'monit': self.ca})

    def test_prepare_revalidates_after_preview_and_port_is_reserved_during_probe(self):
        engine = Engine(policy(), self.root)
        engine.preview(serialize(self.document))
        changed = deepcopy(self.document); changed['name'] = '../escape'
        with patch('atraxis.installation.engine.probe.check') as check:
            with self.assertRaises(InstallError): engine.prepare(serialize(changed), **self.inputs)
            check.assert_not_called()
        def verify(*_args):
            with socket.socket() as sock, self.assertRaises(OSError): sock.bind(('127.0.0.1', self.port))
        with patch('atraxis.installation.engine.probe.check', side_effect=verify):
            result = engine.prepare(serialize(self.document), **self.inputs)
        self.assertEqual(result, {'status': 'prepared', 'name': 'pilot', 'services_started': False})
        self.assertNotIn(SECRET, str(result) + repr(engine))
        bundle = self.path / 'pilot'
        manifest = json.loads((bundle / 'complete.json').read_bytes())
        for name, digest in manifest['sha256'].items():
            self.assertEqual(hashlib.sha256((bundle / name).read_bytes()).hexdigest(), digest)
        self.assertEqual(json.loads((bundle / 'login.json').read_bytes())['password'], SECRET)
        self.assertFalse((self.path / 'forbidden').exists())
        with socket.socket() as sock: sock.bind(('127.0.0.1', self.port))

    def test_bad_material_or_occupied_port_does_not_write_or_probe(self):
        engine = Engine(policy(), self.root)
        for change in ({'private_key': b'bad'}, {'source_credentials': {}}, {'source_cas': {'monit': b'bad'}}):
            with patch('atraxis.installation.engine.probe.check') as check:
                with self.assertRaises(InstallError): engine.prepare(serialize(self.document), **dict(self.inputs, **change))
                check.assert_not_called()
        with socket.socket() as occupied, patch('atraxis.installation.engine.probe.check') as check:
            occupied.bind(('127.0.0.1', self.port))
            with self.assertRaisesRegex(InstallError, 'port_unavailable'): engine.prepare(serialize(self.document), **self.inputs)
            check.assert_not_called()
        self.assertEqual(list(self.path.iterdir()), [])

    def test_material_timeout_precedes_network_and_filesystem_effects(self):
        import multiprocessing
        before = {p.pid for p in multiprocessing.active_children()}
        with patch('atraxis.installation.material.WORKER_TIMEOUT', 0.2), patch('atraxis.installation.material._worker', stalled_material_worker), patch('atraxis.installation.engine.probe.check') as network:
            with self.assertRaisesRegex(InstallError, 'validation_timeout'):
                Engine(policy(), self.root).prepare(serialize(self.document), **self.inputs)
            network.assert_not_called()
        self.assertEqual(list(self.path.iterdir()), [])
        self.assertEqual({p.pid for p in multiprocessing.active_children()}, before)

    def test_probe_failure_writes_nothing_and_releases_port(self):
        with patch('atraxis.installation.engine.probe.check', side_effect=InstallError('authentication_failed')):
            with self.assertRaises(InstallError): Engine(policy(), self.root).prepare(serialize(self.document), **self.inputs)
        self.assertEqual(list(self.path.iterdir()), [])
        with socket.socket() as sock: sock.bind(('127.0.0.1', self.port))


@contextmanager
def https_fixture(*, status=200, payload=None, require_auth=True, delay=0, name='127.0.0.1'):
    cert, key, ca = material(name=name)
    seen = []
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append((self.path, self.headers.get('Authorization')))
            if delay: time.sleep(delay)
            selected = status
            expected = 'Basic ' + probe.base64.b64encode(('operator:' + SECRET).encode()).decode()
            if require_auth and self.headers.get('Authorization') != expected: selected = 401
            self.send_response(selected)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Location', 'https://169.254.169.254/credentials')
            self.end_headers()
            data = payload() if callable(payload) else payload
            try: self.wfile.write(data if type(data) is bytes else serialize(data))
            except (BrokenPipeError, ssl.SSLError): pass
        def log_message(self, *_args): pass
    with tempfile.TemporaryDirectory() as folder:
        pem = Path(folder) / 'server.pem'; pem.write_bytes(cert + key)
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); tls.load_cert_chain(pem)
        server.socket = tls.wrap_socket(server.socket, server_side=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try: yield 'https://127.0.0.1:' + str(server.server_port), ca, seen
        finally: server.shutdown(); server.server_close(); thread.join()


def fresh_certlord():
    payload = certlord()
    shift = time.time() - NOW
    for key in ('started_at', 'completed_at'):
        payload[key] = (datetime.fromisoformat(payload[key]) + timedelta(seconds=shift)).isoformat()
    payload['completed_timestamp_seconds'] += shift
    for item in payload['certificates']:
        item['observed_at'] = (datetime.fromisoformat(item['observed_at']) + timedelta(seconds=shift)).isoformat()
    return payload


class LiveProbeTests(unittest.TestCase):
    def run_probe(self, url, ca, *, kind='monit_docker', secret=LOGIN):
        document = plan(url); document['sources'][0]['kind'] = kind
        probe.check(parse(document).sources[0], secret, ca, policy(url))

    def test_verified_tls_auth_fixed_routes_and_both_adapter_semantics(self):
        for kind, payload, route in (('monit_docker', monit, '/v1/status'),
                                     ('certlord', fresh_certlord, '/api/certificates/observations')):
            with self.subTest(kind=kind), https_fixture(payload=payload) as (url, ca, seen):
                self.run_probe(url, ca, kind=kind)
                self.assertEqual(len(seen), 1); self.assertEqual(seen[0][0], route)

    def test_complete_preparation_with_real_https_and_no_partial_bundle_on_failure(self):
        with tempfile.TemporaryDirectory() as folder, https_fixture(payload=monit) as (url, ca, seen):
            os.chmod(folder, 0o700)
            root = Root(os.open(folder, DIRECTORY_FLAGS))
            try:
                document = plan(url)
                with socket.socket() as sock:
                    sock.bind(('127.0.0.1', 0)); document['listen_port'] = sock.getsockname()[1]
                cert, key, _ = material()
                inputs = dict(login=LOGIN, source_credentials={'monit': LOGIN}, certificate=cert,
                              private_key=key, source_cas={'monit': ca})
                engine = Engine(policy(url), root)
                bad = dict(inputs, source_credentials={'monit': {'username': 'operator', 'password': 'wrong'}})
                with self.assertRaisesRegex(InstallError, 'authentication_failed'):
                    engine.prepare(serialize(document), **bad)
                self.assertEqual(list(Path(folder).iterdir()), [])
                result = engine.prepare(serialize(document), **inputs)
                self.assertEqual(result['status'], 'prepared')
                self.assertFalse(result['services_started'])
                self.assertTrue((Path(folder) / 'pilot/complete.json').is_file())
                self.assertEqual(len(seen), 2)
            finally:
                root.close()

    def test_http_status_payload_and_staleness_fail_closed(self):
        stale = monit(); stale['age_seconds'] = 1000
        cases = ((302, monit(), 'redirect_refused'), (401, SECRET.encode(), 'authentication_failed'),
                 (200, b'{"api_version":1,"api_version":1}', None), (200, {'status': 'ok'}, None),
                 (200, stale, 'source_unready'), (200, b'x' * (probe.RESPONSE_BYTES + 1), 'invalid_response'))
        for status, payload, code in cases:
            with self.subTest(status=status, code=code), https_fixture(status=status, payload=payload) as (url, ca, seen):
                with self.assertRaises(InstallError) as raised: self.run_probe(url, ca)
                if code: self.assertEqual(raised.exception.code, code)
                self.assertNotIn(SECRET, str(raised.exception.report()))
                self.assertEqual(len(seen), 1)

    def test_wrong_credentials_untrusted_ca_and_hostname_are_rejected(self):
        with https_fixture(payload=monit) as (url, ca, seen):
            with self.assertRaisesRegex(InstallError, 'authentication_failed'):
                self.run_probe(url, ca, secret={'username': 'operator', 'password': 'wrong'})
            with self.assertRaisesRegex(InstallError, 'tls_failed'): self.run_probe(url, material()[2])
            self.assertEqual(len(seen), 1)
        with https_fixture(payload=monit, name='wrong.example.test') as (url, ca, seen):
            with self.assertRaisesRegex(InstallError, 'tls_failed'): self.run_probe(url, ca)
            self.assertEqual(seen, [])

    def test_timeout_covers_the_whole_request_and_worker_is_reaped(self):
        import multiprocessing
        before = {p.pid for p in multiprocessing.active_children()}
        with https_fixture(payload=monit, delay=2) as (url, ca, _seen):
            start = time.monotonic()
            with patch('atraxis.installation.probe.PROBE_TIMEOUT', 0.4):
                with self.assertRaisesRegex(InstallError, 'probe_timeout'): self.run_probe(url, ca)
            self.assertLess(time.monotonic() - start, 2)
        self.assertEqual({p.pid for p in multiprocessing.active_children()}, before)
