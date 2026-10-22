"""Complete allowlisted projections; no upstream secrets or mutable references."""
from dataclasses import FrozenInstanceError
import json
import unittest

from atraxis.models import public_fields
from atraxis.parsing import InvalidPayload, parse_monit
from atraxis.service import ReadService
from test_collection import result, source
from test_observations import COMMON, NOW, certlord, certificate_parse, iso, monit


def filesystem():
    return dict(group='data', path='/var/lib/data', disk_usage=120, disk_available=None,
                disk_total=200, disk_percent=60, inode_usage=5, inode_available=5,
                inode_total=10, inode_percent=50, fs_mode='ro', fs_readable=1,
                fs_writable=0, fs_executable=None)


def tls():
    return dict(status='failed', checked_at=iso(-300), error_code='fingerprint_mismatch',
        expected_fingerprint_sha256='a' * 64, observed_fingerprint_sha256='b' * 64,
        target_sha256='c' * 64, private_key='secret-marker', hostname='secret-host')


class ProjectionTests(unittest.TestCase):
    def test_runtime_policy_and_filesystem_facts(self):
        payload = monit()
        payload['containers'][0].update(pid=321, oom_events=0, starts_recent=None,
            pids_current=3, pids_limit=10, pids_percent=30, event_window_seconds=60,
            event_window_end=int(NOW), event_history_complete=0, manual_actions_protected=True,
            restart_attempts=2, restart_limit=3, maintenance_active=True,
            maintenance_until=NOW + 600, filesystems=[filesystem()])
        fields = public_fields(parse_monit(payload, **COMMON).resources[0].fields)
        self.assertTrue(fields['manual_actions_protected'])
        self.assertTrue(fields['maintenance_active'])
        self.assertEqual(fields['restart_attempts'], 2)
        self.assertEqual(fields['event_history_complete'], 0)
        self.assertIsNone(fields['starts_recent'])
        self.assertEqual(fields['filesystems'][0], filesystem())

    def test_missing_additions_remain_unknown(self):
        fields = public_fields(parse_monit(monit(), **COMMON).resources[0].fields)
        for name in ('manual_actions_protected', 'maintenance_active', 'filesystems',
                     'restart_limit', 'event_history_complete'):
            self.assertIsNone(fields[name])
        payload = monit()
        payload['containers'][0]['filesystems'] = []
        self.assertEqual(public_fields(parse_monit(payload, **COMMON).resources[0].fields)['filesystems'], [])

    def test_policy_types_and_limits_are_strict(self):
        for name, value in (('pid', True), ('event_history_complete', True), ('event_history_complete', 2),
                ('manual_actions_protected', 1), ('maintenance_active', 'true'), ('restart_limit', 0),
                ('restart_attempts', -1), ('maintenance_until', float('nan')), ('pids_percent', -1)):
            with self.subTest(name=name, value=value):
                payload = monit()
                payload['containers'][0][name] = value
                with self.assertRaises(InvalidPayload):
                    parse_monit(payload, **COMMON)

    def test_filesystem_identity_types_and_collection_budget(self):
        for samples in ([filesystem(), filesystem()], [dict(filesystem(), fs_mode='maybe')],
                        [dict(filesystem(), fs_readable=True)], [dict(filesystem(), disk_total=-1)], {}):
            with self.subTest(samples=samples):
                payload = monit()
                payload['containers'][0]['filesystems'] = samples
                with self.assertRaises(InvalidPayload):
                    parse_monit(payload, **COMMON)
        payload = monit()
        payload['containers'] = [dict(payload['containers'][0], id=identity * 64,
            filesystems=[dict(filesystem(), path='/data/' + str(i)) for i in range(2)]) for identity in ('a', 'b')]
        with self.assertRaises(InvalidPayload):
            parse_monit(payload, **dict(COMMON, max_resources=2))

    def test_nested_snapshots_are_immutable_and_secrets_excluded(self):
        payload = monit()
        payload['containers'][0]['filesystems'] = [dict(filesystem(), token='secret-marker')]
        snapshot = parse_monit(payload, **COMMON)
        sample = dict(snapshot.resources[0].fields)['filesystems'][0]
        with self.assertRaises(FrozenInstanceError):
            sample.fields = ()
        payload['containers'][0]['filesystems'][0]['path'] = '/changed'
        fields = public_fields(snapshot.resources[0].fields)
        self.assertEqual(fields['filesystems'][0]['path'], '/var/lib/data')
        self.assertNotIn('secret-marker', json.dumps(fields))

    def test_source_progress_is_preserved_even_on_not_ready_response(self):
        payload = dict(monit(), ready=False, containers=[], last_cycle_success=False,
            errors_total=2, cycles_total=5, last_error_code=150,
            actions={'executed': 1, 'cooldown': 2, 'pending': 3, 'restart-limit': 4,
                     'maintenance': 5, 'dry-run': 6}, manual_actions={'actor': 'secret-marker'})
        service = ReadService((source(),), clock=lambda: 100.)
        service.record('a', result(payload=payload))
        first = service.status()['sources'][0]
        self.assertIsNone(first['last_valid_snapshot'])
        metadata = first['last_attempt']['source_metadata']
        self.assertEqual(metadata['cycles_total'], 5)
        self.assertEqual(metadata['actions']['executed'], 1)
        metadata['actions']['executed'] = 999
        self.assertEqual(service.status()['sources'][0]['last_attempt']['source_metadata']['actions']['executed'], 1)
        self.assertNotIn('secret-marker', json.dumps(first))

    def test_verification_and_retry_are_historical_facts(self):
        payload = certlord()
        payload['certificates'][0].update(verification=tls(), last_issuance_retry_at=iso(-120))
        snapshot = certificate_parse(payload)
        fields = public_fields(snapshot.resources[0].fields)
        self.assertTrue(snapshot.authoritative)  # Failed TLS is not collection failure.
        self.assertEqual(fields['verification']['status'], 'failed')
        self.assertEqual(fields['verification']['checked_at'], iso(-300))
        self.assertEqual(fields['last_issuance_retry_at'], iso(-120))
        self.assertNotIn('secret', json.dumps(fields))
        self.assertEqual(public_fields(snapshot.metadata)['completed_timestamp_seconds'], NOW - 1)
        payload['certificates'][0]['verification']['status'] = 'verified'
        self.assertEqual(fields['verification']['status'], 'failed')

    def test_absent_tls_and_retry_are_null(self):
        fields = public_fields(certificate_parse(certlord()).resources[0].fields)
        self.assertIsNone(fields['verification'])
        self.assertIsNone(fields['last_issuance_retry_at'])

    def test_bad_verification_or_retry_fails_whole_observation(self):
        cases = [('verification', dict(tls(), status='unknown')),
                 ('verification', dict(tls(), checked_at='2026-01-01')),
                 ('verification', dict(tls(), target_sha256='bad')),
                 ('verification', dict(tls(), error_code='arbitrary-secret-error')),
                 ('last_issuance_retry_at', 'not-a-date')]
        for key, value in cases:
            with self.subTest(key=key, value=value):
                payload = certlord()
                payload['certificates'][0][key] = value
                with self.assertRaisesRegex(InvalidPayload, '^invalid_payload$'):
                    certificate_parse(payload)
