"""Non-interactive client contract with separate credentials and fixed reads."""
from contextlib import redirect_stderr, redirect_stdout
import copy
import io
import json
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch

import yaml

from atraxis.cli import main
from atraxis.client import read_inventory, render_inventory, validate_inventory
from atraxis.client_config import load_client_config
from atraxis.config import ConfigurationError
from atraxis.parsing import InvalidPayload
from atraxis.service import ReadService
from atraxis.transport import TransportResult
from test_collection import source, result


def inventory(ready=True):
    service = ReadService((source(),), clock=lambda: 100.)
    service.set_running(True)
    if ready:
        service.record('a', result())
    return service.status()


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / 'client.yml'
        (self.root / 'client.json').write_text(json.dumps({'username': 'reader', 'password': 'private-marker'}))
        self.document = dict(origin='https://atraxis.example.test', credentials_file='client.json')
        self.path.write_text(yaml.safe_dump(self.document))

    def invoke(self, command, payload, outcome='success', flags=()):
        output, error = io.StringIO(), io.StringIO()
        observation = TransportResult('atraxis-api', outcome, 1, 2, 100, payload)
        with patch('atraxis.client.HTTPTransport.fetch', return_value=observation) as fetch, \
                patch('atraxis.cli.load_config', side_effect=AssertionError('no daemon configuration')), \
                patch('atraxis.server.serve', side_effect=AssertionError('no daemon startup')), \
                redirect_stdout(output), redirect_stderr(error):
            code = main([command, '--config', str(self.path), *flags])
        self.assertEqual(fetch.call_args.args[0].kind, 'atraxis_inventory')
        return code, output.getvalue(), error.getvalue()

    def test_client_configuration_is_separate_offline_and_bounded(self):
        with patch.object(socket, 'getaddrinfo', side_effect=AssertionError('no DNS')):
            config = load_client_config(self.path)
        self.assertEqual(config.credentials_file, str(self.root / 'client.json'))
        self.assertEqual(config.max_response_bytes, 4194304)
        self.assertNotIn('private-marker', repr(config))
        for changes in ({'origin': 'http://192.0.2.1', 'allow_loopback_http': True},
                        {'origin': 'https://user:pass@atraxis.example.test'}, {'timeout': True},
                        {'timeout': 61}, {'max_response_bytes': 4194305}, {'sources': []},
                        {'origin': 'https://atraxis.example.test/other'}, {'ca_file': 'missing'}):
            with self.subTest(changes=changes):
                self.path.write_text(yaml.safe_dump(dict(self.document, **changes)))
                with self.assertRaises(ConfigurationError):
                    load_client_config(self.path)
        for raw in ('origin: a\norigin: b\n', 'origin: &a x\ncredentials_file: *a\n', '#' * 65537):
            self.path.write_text(raw)
            with self.assertRaises(ConfigurationError):
                load_client_config(self.path)

    def test_scoped_envelope_empty_scope_and_context_validation(self):
        payload=inventory()
        payload['context']=dict(subject='alice', organization='operations', organizations=[dict(id='operations',name='Operations')])
        self.assertEqual(validate_inventory(payload)['context']['organization'],'operations')
        payload.update(sources=[],ready=False,reason='no_visible_sources')
        code,output,error=self.invoke('inventory',payload,flags=('--json',))
        self.assertEqual(code,2);self.assertEqual(error,'');self.assertEqual(json.loads(output)['sources'],[])
        payload['context']['organization']='hidden'
        with self.assertRaises(InvalidPayload):validate_inventory(payload)
        del payload['context']
        with self.assertRaises(InvalidPayload):validate_inventory(payload)

    def test_status_and_inventory_output_and_exit_codes(self):
        code, output, error = self.invoke('status', inventory())
        self.assertEqual(code, 0)
        self.assertIn('READY', output)
        self.assertIn('current', output)
        self.assertEqual(error, '')
        code, output, _ = self.invoke('inventory', inventory())
        self.assertEqual(code, 0)
        self.assertIn('a' * 64, output)
        code, output, error = self.invoke('status', inventory(False), flags=('--json',))
        self.assertEqual(code, 2)
        self.assertIsNone(json.loads(output)['sources'][0]['last_valid_snapshot'])
        self.assertEqual(error, '')

    def test_errors_do_not_echo_raw_payload_or_configuration(self):
        for outcome in ('authentication_error', 'timeout', 'response_too_large', 'redirect_refused'):
            with self.subTest(outcome=outcome):
                code, output, error = self.invoke('inventory', {'secret': 'private-marker'}, outcome)
                self.assertEqual(code, 1)
                self.assertEqual(output, '')
                self.assertNotIn('private-marker', error)
                self.assertIn(outcome, error)
        code, output, error = self.invoke('inventory', {'private-marker': 'wrong-schema'})
        self.assertEqual((code, output), (1, ''))
        self.assertIn('invalid_payload', error)
        self.assertNotIn('private-marker', error)

    def test_unknown_fields_are_not_echoed_and_terminal_controls_rejected(self):
        payload = inventory()
        payload['token'] = 'private-marker'
        fields = payload['sources'][0]['last_valid_snapshot']['resources'][0]['fields']
        fields['private_key'] = 'private-marker'
        clean = validate_inventory(payload)
        self.assertNotIn('private-marker', render_inventory(clean, as_json=True))
        fields['name'] = 'demo\x1b[31m'
        with self.assertRaises(InvalidPayload):
            validate_inventory(payload)

    def test_inconsistent_readiness_identity_and_snapshot_rejected(self):
        payloads = []
        base = inventory()
        bad = copy.deepcopy(base)
        bad['ready'] = False
        payloads.append(bad)
        bad = copy.deepcopy(base)
        bad['sources'][0]['last_valid_snapshot'] = None
        payloads.append(bad)
        bad = copy.deepcopy(base)
        bad['sources'][0]['last_attempt']['outcome'] = 'timeout'
        payloads.append(bad)
        bad = copy.deepcopy(base)
        bad['sources'][0]['last_attempt']['observation_freshness'] = 'stale'
        payloads.append(bad)
        bad = copy.deepcopy(base)
        bad['sources'][0]['last_valid_snapshot']['resources'][0]['source_id'] = 'other'
        payloads.append(bad)
        bad = copy.deepcopy(base)
        bad['sources'] *= 2
        payloads.append(bad)
        for payload in payloads:
            with self.subTest(payload=payload), self.assertRaises(InvalidPayload):
                validate_inventory(payload)

    def test_historical_inventory_is_visibly_labelled(self):
        service = ReadService((source(),), clock=lambda: 100.)
        service.set_running(True)
        service.record('a', result())
        service.record('a', result(outcome='timeout'))
        code, output, _ = self.invoke('inventory', service.status())
        self.assertEqual(code, 2)
        self.assertIn('historical', output)
        self.assertIn('unavailable', output)
        self.assertIn('a' * 64, output)

    def test_cli_transfer_time_rechecks_freshness_and_exit_state(self):
        observation = TransportResult('atraxis-api', 'success', 1, 2, 100, inventory(), 60.)
        with patch('atraxis.client.HTTPTransport.fetch', return_value=observation):
            payload, error = read_inventory(load_client_config(self.path))
        self.assertIsNone(error)
        self.assertFalse(payload['ready'])
        self.assertEqual(payload['reason'], 'sources_not_current')
        self.assertFalse(payload['sources'][0]['current'])
        self.assertEqual(payload['sources'][0]['last_valid_snapshot']['freshness'], 'stale')
        self.assertEqual(payload['sources'][0]['last_valid_snapshot']['age_seconds'], 65.)
