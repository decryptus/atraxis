"""Source configuration contracts, without live sources or credentials."""
import json
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch

import yaml

from atraxis.config import ConfigurationError, load_config
from atraxis.source_config import InvalidSourceConfiguration, parse_sources


class SourceConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / 'atraxis.yml'
        self.secret = self.root / 'source.json'
        self.secret.write_text(json.dumps({'username': 'reader', 'password': 'secret-marker'}))
        (self.root / 'users').write_text('inbound-fixture')
        self.source = {'source_id': 'stable-installation', 'kind': 'monit_docker',
                       'origin': 'https://agent.example.org', 'credentials_file': 'source.json'}

    def load(self, **changes):
        self.path.write_text(yaml.safe_dump({'general': {'auth_basic_file': 'users'},
                                           'sources': [dict(self.source, **changes)]}))
        return load_config(self.path).sources[0]

    def test_source_defaults_and_fixed_read_routes(self):
        source = self.load()
        self.assertEqual(source.url, 'https://agent.example.org/v1/status')
        self.assertEqual(source.credentials_file, str(self.secret))
        self.assertEqual((source.timeout, source.poll_interval, source.max_age), (10, 60, 180))
        self.assertEqual(self.load(kind='certlord').url,
                         'https://agent.example.org/api/certificates/observations')
        self.assertNotIn('secret-marker', repr(source))
        self.assertNotIn('source.json', repr(source))

    def test_identity_is_independent_of_origin(self):
        first = self.load()
        second = self.load(origin='https://replacement.example.org:8443/')
        self.assertEqual(first.source_id, second.source_id)
        self.assertEqual(second.origin, 'https://replacement.example.org:8443')

    def test_http_requires_explicit_literal_loopback(self):
        for origin in ('http://127.0.0.1:9810', 'http://[::1]:9810'):
            with self.subTest(origin=origin):
                self.assertEqual(self.load(origin=origin, allow_loopback_http=True).origin, origin)
                with self.assertRaises(ConfigurationError):
                    self.load(origin=origin)
        for origin in ('http://localhost', 'http://127.1', 'http://2130706433',
                       'http://agent.example.org', 'http://192.0.2.1'):
            with self.subTest(origin=origin), self.assertRaises(ConfigurationError):
                self.load(origin=origin, allow_loopback_http=True)

    def test_origin_rejects_credentials_routes_queries_and_ambiguous_syntax(self):
        for origin in ('https://reader:secret@agent.example.org', 'https://agent.example.org/path',
                       'https://agent.example.org?x=1', 'https://agent.example.org#',
                       'https://agent.example.org?', ' https://agent.example.org',
                       'https://agent.example.org\n', 'https://agent.example.org:',
                       'https://agent.example.org:0', 'https://agent.example.org:65536',
                       'https://agent.example.org:%38', 'https://[fe80::1%25eth0]',
                       'https://agent.example.org\\evil', 'ftp://agent.example.org'):
            with self.subTest(origin=origin), self.assertRaises(ConfigurationError):
                self.load(origin=origin)
        self.assertEqual(self.load(origin='https://éxample.org').origin,
                         'https://xn--xample-9ua.org')

    def test_count_unique_identity_and_source_types(self):
        for sources in ([self.source, self.source], [dict(self.source, source_id=str(n)) for n in range(33)],
                        [dict(self.source, kind='unknown')], [dict(self.source, source_id=' ')],
                        [dict(self.source, source_id='secret\nmarker')], [dict(self.source, typo=1)]):
            with self.subTest(sources=len(sources)), self.assertRaises(InvalidSourceConfiguration):
                parse_sources(sources, self.path)
        sources = [dict(self.source, source_id=str(n)) for n in range(32)]
        self.assertEqual(len(parse_sources(sources, self.path)), 32)

    def test_bounds_types_and_timing_consistency(self):
        invalid = {'timeout': [True, 0, 61, 1.5, '5'], 'poll_interval': [False, 0, 3601, 5],
                   'max_age': [0, 59, 86401], 'max_resources': [0, 10001],
                   'max_response_bytes': [1023, 4194305], 'max_clock_skew': [-1, 301],
                   'allow_loopback_http': [1, 'true', True]}
        for name, values in invalid.items():
            for value in values:
                with self.subTest(field=name, value=value), self.assertRaises(ConfigurationError):
                    self.load(**{name: value})
        source = self.load(timeout=1, poll_interval=1, max_age=1,
                           max_response_bytes=1024, max_resources=1, max_clock_skew=0)
        self.assertEqual(source.max_clock_skew, 0)

    def test_credentials_format_limits_and_safe_errors(self):
        values = [b'', b'x' * 8193, b'not-json-secret-marker', b'[]',
                  b'{"username":"reader","username":"other","password":"secret-marker"}',
                  b'{"username":"reader"}',
                  b'{"username":"reader","password":123}',
                  b'{"username":"reader","password":"\\ud800"}',
                  b'{"username":"reader","password":"secret-marker","extra":1}',
                  b'{"username":"reader:admin","password":"secret-marker"}']
        for value in values:
            self.secret.write_bytes(value)
            with self.assertRaises(ConfigurationError) as error:
                self.load()
            self.assertEqual(str(error.exception), 'invalid_configuration')
        self.secret.unlink()
        with self.assertRaises(ConfigurationError):
            self.load()

    def test_custom_ca_requires_valid_readable_pem(self):
        ca = self.root / 'ca.pem'
        ca.write_text('not a certificate')
        with self.assertRaises(ConfigurationError):
            self.load(ca_file='ca.pem')
        with self.assertRaises(ConfigurationError):
            self.load(ca_file='absent.pem')
        # Use the system trust bundle, without contacting an endpoint.
        import ssl
        path = ssl.get_default_verify_paths().cafile
        self.assertTrue(path)
        self.assertEqual(self.load(ca_file=path).ca_file, path)
        with self.assertRaises(ConfigurationError):
            self.load(origin='http://127.0.0.1', allow_loopback_http=True, ca_file=path)

    def test_validation_never_contacts_sources(self):
        with patch.object(socket, 'getaddrinfo', side_effect=AssertionError('no DNS')):
            self.load()
            load_config(self.path)
