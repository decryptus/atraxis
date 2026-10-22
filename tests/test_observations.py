import copy
from datetime import datetime, timezone
import unittest

from atraxis.models import Freshness, SnapshotStore
from atraxis.parsing import InvalidPayload, parse_certlord, parse_monit


NOW = 1791054000.0
COMMON = dict(source_id='source-a', received_at=NOW, monotonic=100.0,
              max_age=60.0, max_resources=10)


def iso(offset):
    return datetime.fromtimestamp(NOW + offset, timezone.utc).isoformat()


def monit():
    return dict(api_version=1, ready=True, running=True, last_cycle_success=True,
                cycles_total=2, errors_total=0, age_seconds=5,
                last_success_at=NOW - 5, last_cycle_finished_at=NOW - 5,
                containers=[dict(id='a' * 64, name='demo', status='running',
                                 health='none', cpu_percent=250, mem_usage=None)])


def certlord():
    return dict(schema_version=1, started_at=iso(-10), completed_at=iso(-1),
                completed_timestamp_seconds=NOW - 1,
                certificates=[dict(certificate_id='12345678-1234-4234-8234-123456789abc',
                                   domains=['example.test'], version=1, status='exists',
                                   origin='external', renewal_owner='external', pending_action='none',
                                   material_state='missing', not_before=None, not_after=None,
                                   not_after_timestamp_seconds=None, fingerprint_sha256=None,
                                   issuance_retry_count=None, observed_at=iso(-5))])


def certificate_parse(payload, **changes):
    return parse_certlord(payload, **dict(COMMON, max_clock_skew=2, **changes))


class MonitTests(unittest.TestCase):
    def test_running_ready_and_nullable_measurements(self):
        result = parse_monit(monit(), **COMMON)
        self.assertTrue(result.authoritative)
        self.assertEqual(dict(result.resources[0].fields)['cpu_percent'], 250)
        self.assertIsNone(dict(result.resources[0].fields)['mem_usage'])

    def test_source_scoped_identity(self):
        first = parse_monit(monit(), **COMMON)
        second = parse_monit(monit(), **dict(COMMON, source_id='source-b'))
        self.assertNotEqual(first.resources[0].key, second.resources[0].key)

    def test_replacement_identity(self):
        payload = monit()
        first = parse_monit(payload, **COMMON)
        payload['containers'][0]['id'] = 'b' * 64
        second = parse_monit(payload, **COMMON)
        self.assertNotEqual(first.resources[0].key, second.resources[0].key)

    def test_unknown_fields_not_projected(self):
        payload = monit()
        payload['secret'] = 'private'
        payload['containers'][0]['secret'] = 'private'
        self.assertNotIn('private', repr(parse_monit(payload, **COMMON)))

    def test_duplicate_identity_rejected(self):
        payload = monit()
        payload['containers'] *= 2
        with self.assertRaises(InvalidPayload):
            parse_monit(payload, **COMMON)

    def test_invalid_values(self):
        for field, value in [('api_version', True), ('ready', 1), ('age_seconds', float('nan')),
                             ('age_seconds', -1), ('age_seconds', 10 ** 1000),
                             ('cycles_total', True), ('containers', None)]:
            with self.subTest(field=field, value_type=type(value).__name__):
                payload = monit()
                payload[field] = value
                with self.assertRaisesRegex(InvalidPayload, '^invalid_payload$'):
                    parse_monit(payload, **COMMON)

    def test_age_advances_monotonically(self):
        result = parse_monit(monit(), **COMMON)
        self.assertEqual(result.freshness_at(155), Freshness.FRESH)
        self.assertEqual(result.freshness_at(156), Freshness.STALE)
        self.assertEqual(result.freshness_at(99), Freshness.UNKNOWN)

    def test_limit_fails_whole_collection(self):
        payload = monit()
        payload['containers'] *= 11
        with self.assertRaises(InvalidPayload):
            parse_monit(payload, **COMMON)

    def test_not_ready_cannot_expose_containers(self):
        payload = monit()
        payload['ready'] = False
        with self.assertRaises(InvalidPayload):
            parse_monit(payload, **COMMON)


class CertificateTests(unittest.TestCase):
    def test_missing_material_is_observable(self):
        result = certificate_parse(certlord())
        self.assertTrue(result.authoritative)
        self.assertIsNone(dict(result.resources[0].fields)['not_after'])

    def test_successful_empty_inventory(self):
        payload = certlord()
        payload['certificates'] = []
        result = certificate_parse(payload)
        self.assertTrue(result.authoritative)
        self.assertEqual(result.resources, ())

    def test_per_record_age(self):
        payload = certlord()
        payload['started_at'] = iso(-120)
        payload['certificates'][0]['observed_at'] = iso(-100)
        result = certificate_parse(payload)
        self.assertEqual(result.freshness, Freshness.STALE)
        self.assertFalse(result.authoritative)

    def test_future_source_clock_is_unknown(self):
        payload = certlord()
        payload.update(started_at=iso(10), completed_at=iso(20), completed_timestamp_seconds=NOW + 20)
        payload['certificates'][0]['observed_at'] = iso(15)
        result = certificate_parse(payload)
        self.assertEqual(result.freshness, Freshness.UNKNOWN)
        self.assertFalse(result.authoritative)

    def test_bad_record_fails_collection(self):
        for field, value in [('certificate_id', 'INVALID'), ('version', True),
                             ('domains', []), ('observed_at', '2026-01-01T00:00:00'),
                             ('material_state', 'bad'), ('not_after', iso(100))]:
            with self.subTest(field=field):
                payload = certlord()
                payload['certificates'][0][field] = value
                with self.assertRaises(InvalidPayload):
                    certificate_parse(payload)

    def test_readable_expired_material(self):
        payload = certlord()
        payload['certificates'][0].update(material_state='readable', not_before=iso(-1000),
                                         not_after=iso(-100), not_after_timestamp_seconds=NOW - 100,
                                         fingerprint_sha256='a' * 64)
        result = certificate_parse(payload)
        self.assertTrue(result.authoritative)
        self.assertLess(dict(result.resources[0].fields)['not_after_timestamp_seconds'], NOW)

    def test_duplicate_uuid(self):
        payload = certlord()
        payload['certificates'] *= 2
        with self.assertRaises(InvalidPayload):
            certificate_parse(payload)

    def test_timestamp_mismatch(self):
        payload = certlord()
        payload['completed_timestamp_seconds'] += 1
        with self.assertRaises(InvalidPayload):
            certificate_parse(payload)

    def test_input_mutation_cannot_change_result(self):
        payload = certlord()
        original = copy.deepcopy(payload)
        result = certificate_parse(payload)
        self.assertEqual(payload, original)
        payload['certificates'][0]['domains'].append('other.test')
        self.assertEqual(dict(result.resources[0].fields)['domains'], ('example.test',))


class StoreTests(unittest.TestCase):
    def test_failed_source_preserves_previous_but_not_current(self):
        store = SnapshotStore()
        result = parse_monit(monit(), **COMMON)
        store.accept(result)
        store.fail('source-a')
        self.assertEqual(store.view('source-a', 101), (result, False))

    def test_not_ready_does_not_clear_inventory(self):
        store = SnapshotStore()
        good = parse_monit(monit(), **COMMON)
        store.accept(good)
        payload = monit()
        payload.update(ready=False, containers=[], last_cycle_success=False)
        store.accept(parse_monit(payload, **COMMON))
        self.assertEqual(store.view('source-a', 101), (good, False))

    def test_empty_success_replaces_old_inventory(self):
        store = SnapshotStore()
        store.accept(parse_monit(monit(), **COMMON))
        payload = monit()
        payload['containers'] = []
        empty = parse_monit(payload, **COMMON)
        store.accept(empty)
        self.assertEqual(store.view('source-a', 101), (empty, True))

    def test_source_isolation_and_recovery(self):
        store = SnapshotStore()
        first = parse_monit(monit(), **COMMON)
        second = parse_monit(monit(), **dict(COMMON, source_id='source-b'))
        store.accept(first)
        store.accept(second)
        store.fail('source-a')
        self.assertEqual(store.view('source-b', 101), (second, True))
        store.accept(first)
        self.assertEqual(store.view('source-a', 101), (first, True))

    def test_aged_and_absent_source(self):
        store = SnapshotStore()
        self.assertEqual(store.view('missing', 100), (None, False))
        result = parse_monit(monit(), **COMMON)
        store.accept(result)
        self.assertEqual(store.view('source-a', 200), (result, False))
