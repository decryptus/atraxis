"""Common adapter boundary and independent source lifecycles."""
from copy import deepcopy
from dataclasses import fields, replace
import subprocess
import sys
import unittest
from urllib.parse import parse_qs, urlsplit

from atraxis.models import Freshness
from atraxis.parsing import InvalidPayload, parse_certlord, parse_monit
from atraxis.prometheus import parse_prometheus
from atraxis.service import ReadService
from atraxis.source_adapters import ADAPTERS, DecodeContext, get_adapter
from test_collection import result, source
from test_observations import NOW, certlord, monit
from test_prometheus import SPEC, response


class SourceAdapterTests(unittest.TestCase):
    def context(self, **changes):
        context = DecodeContext('a', NOW, 100., 60, 10, 2, 0., deepcopy(SPEC))
        return replace(context, **changes)

    def test_existing_projections_and_inputs_are_preserved(self):
        context = self.context()
        cases = (
            ('monit_docker', monit(), parse_monit,
             dict(context.arguments(), transport_elapsed=0.)),
            ('certlord', certlord(), parse_certlord,
             dict(context.arguments(), max_clock_skew=2)),
            ('prometheus', response(), parse_prometheus,
             dict(context.arguments(), max_clock_skew=2, metric=SPEC)),
        )
        for kind, payload, parser, arguments in cases:
            with self.subTest(kind=kind):
                original = deepcopy(payload)
                self.assertEqual(get_adapter(kind).decode(payload, context),
                                 parser(payload, **arguments))
                self.assertEqual(payload, original)

    def test_decoding_retains_limits_and_source_timing(self):
        for kind, payload in (('monit_docker', monit()), ('certlord', certlord()),
                              ('prometheus', response())):
            with self.subTest(kind=kind), self.assertRaises(InvalidPayload):
                get_adapter(kind).decode(payload, self.context(max_resources=0))
        stale = get_adapter('monit_docker').decode(monit(), self.context(transport_elapsed=60))
        self.assertEqual(stale.freshness, Freshness.STALE)
        future = get_adapter('prometheus').decode(response(NOW+3), self.context())
        self.assertEqual(future.freshness, Freshness.UNKNOWN)

    def test_routes_and_selection_stay_bounded(self):
        document = dict(metric=deepcopy(SPEC), max_age=60, timeout=2, max_resources=10)
        adapter = get_adapter('prometheus')
        configured = adapter.configure(document, max_age=60, timeout=2, max_resources=10)
        self.assertEqual(configured, SPEC)
        configured['match']['job'] = 'changed'
        self.assertEqual(document['metric'], SPEC)
        route = urlsplit(adapter.read_route(document))
        self.assertEqual(route.path, '/api/v1/query')
        self.assertEqual(parse_qs(route.query), dict(query=['orders_pending{job="covenant-orders"}[60s]'],
                                                   timeout=['2s'], limit=['11']))
        for kind, route in (('monit_docker', '/v1/status'),
                            ('certlord', '/api/certificates/observations')):
            with self.subTest(kind=kind):
                adapter = get_adapter(kind)
                self.assertEqual(adapter.read_route({}), route)
                with self.assertRaises(InvalidPayload):
                    adapter.configure(document, max_age=60, timeout=2, max_resources=10)

    def test_multiple_prometheus_sources_keep_identity_failure_and_recovery_isolated(self):
        now = [100.]
        sources = (source('east', kind='prometheus', metric=deepcopy(SPEC)),
                   source('west', kind='prometheus', metric=deepcopy(SPEC)))
        service = ReadService(sources, clock=lambda: now[0])
        service.set_running(True)
        for item in sources:
            service.record(item.source_id, result(item.source_id, response()))
        east, west = service.status()['sources']
        left = east['last_valid_snapshot']['resources'][0]
        right = west['last_valid_snapshot']['resources'][0]
        self.assertEqual(left['native_id'], right['native_id'])
        self.assertNotEqual(left['source_id'], right['source_id'])
        empty = response(); empty['data']['result'] = []
        failures = (result('east', outcome='timeout'), result('west', response(value='99')),
                    result('east', response(NOW-61)), result('east', empty))
        for failure in failures:
            with self.subTest(outcome=failure.outcome, payload=failure.payload):
                service.record('east', failure)
                east, west = service.status()['sources']
                self.assertFalse(east['current'])
                self.assertTrue(west['current'])
                self.assertEqual(east['last_valid_snapshot']['resources'][0]['fields']['value'], 12)
                self.assertEqual(west['last_valid_snapshot']['resources'][0]['fields']['value'], 12)
                service.record('east', result('east', response(value='0')))
                self.assertTrue(service.status()['ready'])
                service.record('east', result('east', response()))
        now[0] += 61
        self.assertFalse(any(s['current'] for s in service.status()['sources']))

    def test_registry_is_explicit_and_immutable(self):
        for kind in ('unknown', 'atraxis_inventory', 'os.system', None, []):
            with self.subTest(kind=kind), self.assertRaises(InvalidPayload):
                get_adapter(kind)
        with self.assertRaises(TypeError):
            ADAPTERS['unknown'] = object()
        self.assertFalse({'origin', 'credentials_file', 'ca_file'} &
                         {field.name for field in fields(DecodeContext)})

    def test_adapter_imports_do_not_require_transport_or_presentation(self):
        script = '''
import importlib.abc
import sys
class Block(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname in ('atraxis.cli', 'atraxis.web', 'atraxis.transport',
                        'atraxis.source_config', 'httpdis', 'dwho', 'socket'):
            raise AssertionError('unexpected adapter dependency: ' + fullname)
sys.meta_path.insert(0, Block())
from atraxis.source_adapters import ADAPTERS
assert set(ADAPTERS) == {'monit_docker', 'certlord', 'prometheus', 'prometheus_monit'}
'''
        completed = subprocess.run([sys.executable, '-c', script], capture_output=True, text=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)
