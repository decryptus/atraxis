"""Synthetic collection, retention and lifecycle contracts."""
from dataclasses import replace
import json
import threading
import time
import unittest
from unittest.mock import Mock

from atraxis.collection import PollingCoordinator
from atraxis.service import ReadService
from atraxis.source_config import SourceConfig
from atraxis.transport import TransportResult
from test_observations import NOW, certlord, monit


def source(source_id='a', **changes):
    base = SourceConfig(source_id, 'monit_docker', 'https://example.test',
                        '/synthetic/secret', None, 1, 1, 60, 4096, 10, 2)
    return replace(base, **changes)


def result(source_id='a', payload=None, outcome='success', monotonic=100.):
    return TransportResult(source_id, outcome, NOW, NOW, monotonic,
                           monit() if payload is None and outcome == 'success' else payload)


def wait_for(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.01)
    raise AssertionError('condition did not become true')


class InventoryTests(unittest.TestCase):
    def setUp(self):
        self.now = 100.
        self.sources = (source(), source('b', kind='certlord'))
        self.service = ReadService(self.sources, clock=lambda: self.now)
        self.service.set_running(True)

    def test_initial_state_unknown_not_empty_success(self):
        status = self.service.status()
        self.assertFalse(status['ready'])
        for entry in status['sources']:
            self.assertEqual(entry['availability'], 'unknown')
            self.assertIsNone(entry['last_attempt'])
            self.assertIsNone(entry['last_valid_snapshot'])
            self.assertFalse(entry['current'])

    def test_source_transfer_time_cannot_refresh_an_old_observation(self):
        self.service.record('a', result())
        self.service.record('a', replace(result(), duration_seconds=60.))
        first = self.service.status()['sources'][0]
        self.assertFalse(first['current'])
        self.assertEqual(first['last_attempt']['observation_freshness'], 'stale')
        self.assertEqual(first['last_valid_snapshot']['age_seconds'], 5.)
        self.assertEqual(first['last_valid_snapshot']['max_age_seconds'], 60)

    def test_two_kinds_provenance_and_nullable_metrics(self):
        self.service.record('a', result())
        self.service.record('b', result('b', certlord()))
        status = self.service.status()
        self.assertTrue(status['ready'])
        first, second = status['sources']
        container = first['last_valid_snapshot']['resources'][0]
        self.assertEqual(container['source_id'], 'a')
        self.assertIsNone(container['fields']['mem_usage'])
        self.assertEqual(second['last_valid_snapshot']['resources'][0]['kind'], 'certificate')
        self.assertNotIn('secret', json.dumps(status))
        # Returned dictionaries cannot mutate subsequent views.
        container['fields']['name'] = 'changed'
        self.assertEqual(self.service.status()['sources'][0]['last_valid_snapshot']
                         ['resources'][0]['fields']['name'], 'demo')

    def test_failure_retains_data_isolates_source_and_recovers(self):
        self.service.record('a', result())
        self.service.record('b', result('b', certlord()))
        self.service.record('a', result(outcome='timeout'))
        first, second = self.service.status()['sources']
        self.assertFalse(first['current'])
        self.assertEqual(first['availability'], 'unavailable')
        self.assertTrue(first['last_valid_snapshot']['resources'])
        self.assertTrue(second['current'])
        self.assertFalse(self.service.readiness()['ready'])
        self.service.record('a', result())
        self.assertTrue(self.service.readiness()['ready'])

    def test_age_progresses_without_new_collection(self):
        self.service.record('a', result())
        self.now = 156.
        first = self.service.status()['sources'][0]
        self.assertFalse(first['current'])
        self.assertEqual(first['last_valid_snapshot']['freshness'], 'stale')
        self.assertEqual(first['last_valid_snapshot']['age_seconds'], 61.)
        self.now = 99.
        first = self.service.status()['sources'][0]
        self.assertIsNone(first['last_valid_snapshot']['age_seconds'])
        self.assertEqual(first['last_valid_snapshot']['freshness'], 'unknown')

    def test_stale_unknown_and_invalid_response_preserve_last_valid(self):
        self.service.record('a', result())
        old = self.service.status()['sources'][0]['last_valid_snapshot']
        for payload, freshness in ((dict(monit(), age_seconds=100), 'stale'),
                (dict(monit(), ready=False, containers=[], last_cycle_success=False), 'unknown'),
                ({'private-marker': 'not valid'}, None)):
            with self.subTest(freshness=freshness):
                self.service.record('a', result(payload=payload))
                first = self.service.status()['sources'][0]
                self.assertFalse(first['current'])
                self.assertEqual(first['last_valid_snapshot'], old)
                self.assertEqual(first['last_attempt']['observation_freshness'], freshness)
                self.assertNotIn('private-marker', json.dumps(first))
        self.assertEqual(first['last_attempt']['outcome'], 'invalid_payload')

    def test_authoritative_empty_replaces_previous_inventory(self):
        self.service.record('a', result())
        self.service.record('a', result(payload=dict(monit(), containers=[])))
        first = self.service.status()['sources'][0]
        self.assertTrue(first['current'])
        self.assertEqual(first['last_valid_snapshot']['resources'], [])

    def test_wrong_source_identity_and_unknown_errors_are_safe(self):
        for observation in (result('b'), result(outcome='secret-exception-marker')):
            self.service.record('a', observation)
            first = self.service.status()['sources'][0]
            self.assertEqual(first['last_attempt']['outcome'], 'collector_error')
            self.assertIsNone(first['last_valid_snapshot'])
            self.assertNotIn('secret-exception-marker', json.dumps(first))

    def test_stopped_collector_cannot_claim_current_inventory(self):
        self.service.record('a', result())
        self.service.record('b', result('b', certlord()))
        self.service.set_running(False)
        status = self.service.status()
        self.assertFalse(status['ready'])
        self.assertEqual(status['reason'], 'collector_stopped')
        self.assertTrue(all(not item['current'] for item in status['sources']))


class CoordinatorTests(unittest.TestCase):
    def test_slow_source_does_not_block_other_source_and_stop_cancels(self):
        sources = (source(), source('b'))
        service = ReadService(sources)
        entered, cancelled = threading.Event(), threading.Event()
        class Transport:
            def fetch(self, item, cancel):
                if item.source_id == 'a':
                    entered.set()
                    cancel.wait(3)
                    cancelled.set()
                    return result('a', outcome='cancelled')
                return result('b', monotonic=time.monotonic())
        collector = PollingCoordinator(sources, service, transport=Transport())
        self.addCleanup(collector.stop)
        collector.start()
        self.assertTrue(entered.wait(2))
        wait_for(lambda: service.status()['sources'][1]['current'])
        self.assertIsNone(service.status()['sources'][0]['last_valid_snapshot'])
        start = time.monotonic()
        collector.stop()
        self.assertLess(time.monotonic() - start, 1.)
        self.assertTrue(cancelled.is_set())
        self.assertFalse(service.status()['collecting'])
        self.assertFalse(collector._thread.is_alive())
        collector.stop()  # Idempotent lifecycle.
        collector.start()  # A stopped coordinator cannot restart silently.
        self.assertFalse(collector._thread.is_alive())

    def test_bounded_concurrency_fair_admission_and_no_overlap(self):
        sources = tuple(source(str(index)) for index in range(8))
        service = ReadService(sources)
        lock, release = threading.Lock(), threading.Event()
        active, seen, maximum, overlaps = set(), set(), [0], []
        class Transport:
            def fetch(self, item, cancel):
                with lock:
                    if item.source_id in active:
                        overlaps.append(item.source_id)
                    active.add(item.source_id)
                    seen.add(item.source_id)
                    maximum[0] = max(maximum[0], len(active))
                release.wait(3)
                with lock:
                    active.remove(item.source_id)
                return result(item.source_id, monotonic=time.monotonic())
        collector = PollingCoordinator(sources, service, transport=Transport())
        self.addCleanup(collector.stop)
        self.addCleanup(release.set)
        collector.start()
        collector.start()
        wait_for(lambda: len(seen) == 4)
        self.assertEqual(maximum[0], 4)
        release.set()
        wait_for(lambda: len(seen) == 8)
        collector.stop()
        self.assertLessEqual(maximum[0], 4)
        self.assertEqual(overlaps, [])
        self.assertEqual(active, set())

    def test_poll_interval_after_completion_no_retry_burst_and_recovery(self):
        sources = (source(),)
        service = ReadService(sources)
        starts = []
        class Transport:
            def fetch(self, item, cancel):
                starts.append(time.monotonic())
                if len(starts) == 1:
                    raise RuntimeError('secret-error-marker')
                return result(monotonic=time.monotonic())
        collector = PollingCoordinator(sources, service, transport=Transport())
        self.addCleanup(collector.stop)
        collector.start()
        wait_for(lambda: service.status()['sources'][0]['last_attempt'] is not None)
        self.assertEqual(service.status()['sources'][0]['last_attempt']['outcome'], 'collector_error')
        wait_for(lambda: service.status()['ready'])
        self.assertGreaterEqual(starts[1] - starts[0], 1.)
        self.assertNotIn('secret-error-marker', json.dumps(service.status()))

    def test_stop_before_start_performs_no_io(self):
        transport = Mock()
        collector = PollingCoordinator((source(),), ReadService((source(),)), transport=transport)
        collector.stop()
        collector.start()
        transport.fetch.assert_not_called()
