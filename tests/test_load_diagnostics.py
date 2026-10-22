"""Failure evidence must not weaken readiness or disclose source payloads."""
import importlib.util
import io
import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr
from pathlib import Path
import subprocess
import sys
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch


def helper():
    path = Path(__file__).resolve().parents[1] / 'scripts/chain/load.py'
    spec = importlib.util.spec_from_file_location('load_diagnostics_fixture', path)
    module = importlib.util.module_from_spec(spec)
    original_path = sys.path[:]
    try:
        with patch.dict(sys.modules, {'node': SimpleNamespace(client=None, inventory=None)}):
            spec.loader.exec_module(module)
    finally:
        sys.path[:] = original_path
    return module


def harness():
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location('load_harness_fixture', root/'container_tests/test_load.py')
    module = importlib.util.module_from_spec(spec)
    dependencies = SimpleNamespace(ChainFixture=unittest.TestCase, IMAGES={}, ROOT=root,
                                   docker=Mock(return_value=SimpleNamespace(stdout='{}')), network=None)
    with patch.dict(sys.modules, {'test_chain': dependencies}):
        spec.loader.exec_module(module)
    case = module.LoadTests()
    case.name = lambda role: 'fixture-' + role
    return module, case


class LoadDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.load = helper()

    def evidence(self, value):
        with self.assertRaises(AssertionError) as caught:
            self.load.require_ready(value)
        prefix, data = str(caught.exception).split(' ', 1)
        self.assertEqual(prefix, 'load_inventory_not_ready')
        return json.loads(data)

    def test_only_explicit_true_passes(self):
        self.load.require_ready({'ready': True})
        for value in (False, None, 0, 1, 'true'):
            with self.subTest(value=value):
                self.evidence({'ready': value})
        for value in (None, [], 'secret', {}):
            self.evidence(value)

    def test_original_failure_fields_are_retained(self):
        result = self.evidence(dict(ready=False, collecting=True, reason='sources_not_current',
            sources=[dict(current=False, last_attempt=dict(outcome='timeout', attempted_at=10,
                completed_at=12), last_valid_snapshot=dict(received_at=9, age_seconds=4,
                max_age_seconds=15))]))
        self.assertEqual(result['reason'], 'sources_not_current')
        self.assertEqual(result['sources'], [dict(current=False, outcome='timeout',
            attempted_at=10, completed_at=12, received_at=9, age_seconds=4, max_age_seconds=15)])

    def test_secrets_and_unbounded_payloads_are_excluded(self):
        source = dict(source_id='secret', current='secret', last_attempt=dict(outcome='secret',
            attempted_at=float('inf'), completed_at=-1, source_metadata={'secret': 'secret'}),
            last_valid_snapshot=dict(received_at=True, age_seconds=float('nan'),
                max_age_seconds=10**30, resources=['secret'] * 100))
        result = self.evidence(dict(ready=False, collecting='secret', reason='secret',
                                    sources=[source] * 100, token='secret'))
        self.assertNotIn('secret', json.dumps(result))
        self.assertEqual(len(result['sources']), self.load.DIAGNOSTIC_SOURCE_LIMIT)
        self.assertTrue(all(value is None for value in result['sources'][0].values()))

    def test_malformed_diagnostic_fields_preserve_assertion_failure(self):
        self.evidence(dict(reason=[], sources=[None, {}, dict(last_attempt=[], last_valid_snapshot=[])]))

    def test_failed_read_is_not_retried_or_allowed_to_continue(self):
        client = Mock()
        inventory = Mock(return_value={'ready': False, 'reason': 'collector_stopped'})
        with patch.object(self.load, 'READS', 1), patch.object(self.load, 'CLIENTS', 1), \
                patch.object(self.load, 'client', return_value=client), \
                patch.object(self.load, 'inventory', inventory), \
                patch.dict(sys.modules, {'atropos_client': SimpleNamespace(
                    client=SimpleNamespace(subprocess=subprocess))}):
            with self.assertRaisesRegex(AssertionError, 'load_inventory_not_ready'):
                self.load.read_burst()
        inventory.assert_called_once_with()
        client.active.assert_not_called()
        client.search_interventions.assert_not_called()
        client.close.assert_called_once_with()

    def test_real_subprocess_deadline_is_identified_without_disclosing_child_output(self):
        transport = Mock(wraps=subprocess)
        diagnostic = self.load.ReadDiagnostics(transport)
        output = io.StringIO()
        with redirect_stderr(output):
            with self.assertRaises(subprocess.TimeoutExpired):
                diagnostic.call('search_interventions', lambda:
                    diagnostic.run([sys.executable, '-c',
                        'import time; print("PRIVATE_CHILD_OUTPUT", flush=True); time.sleep(10)'],
                        capture_output=True, timeout=1))
        self.assertEqual(transport.run.call_count, 1)
        self.assertEqual(transport.run.call_args.kwargs['timeout'], 1)
        record = json.loads(output.getvalue().split(' ', 1)[1])
        self.assertEqual(record['stage'], 'search_interventions')
        self.assertEqual(record['worker']['outcome'], 'deadline_exceeded')
        self.assertNotIn('PRIVATE_CHILD_OUTPUT', output.getvalue())

    def test_diagnostic_preserves_original_failure_and_excludes_exception_details(self):
        error = OSError('PRIVATE_EXCEPTION')
        transport = Mock()
        transport.run.side_effect = error
        diagnostic = self.load.ReadDiagnostics(transport)
        output = io.StringIO()
        with redirect_stderr(output), self.assertRaises(OSError) as caught:
            diagnostic.call('active', lambda: diagnostic.run(['PRIVATE_ARGUMENT'], input=b'PRIVATE_INPUT'))
        self.assertIs(caught.exception, error)
        transport.run.assert_called_once_with(['PRIVATE_ARGUMENT'], input=b'PRIVATE_INPUT')
        self.assertNotIn('PRIVATE', output.getvalue())
        self.assertEqual(json.loads(output.getvalue().split(' ', 1)[1])['worker']['outcome'], 'spawn_error')

    def test_success_does_not_emit_diagnostics_or_change_subprocess_result(self):
        result = subprocess.CompletedProcess(['private'], 0, b'private', b'private')
        transport = Mock(run=Mock(return_value=result))
        diagnostic = self.load.ReadDiagnostics(transport)
        output = io.StringIO()
        with redirect_stderr(output):
            self.assertIs(diagnostic.call('active', lambda: diagnostic.run(['command'], timeout=5)), result)
        transport.run.assert_called_once_with(['command'], timeout=5)
        self.assertEqual(output.getvalue(), '')

    def test_concurrent_success_cannot_replace_failed_thread_evidence(self):
        ready, completed = threading.Event(), threading.Event()
        error = subprocess.TimeoutExpired(['PRIVATE'], 5, output=b'PRIVATE')
        def run(command):
            if command == 'fail':
                ready.set()
                if not completed.wait(5): raise AssertionError('success_thread_not_completed')
                raise error
            if not ready.wait(5): raise AssertionError('failure_thread_not_started')
            completed.set()
            return subprocess.CompletedProcess([], 0, b'', b'')
        diagnostic = self.load.ReadDiagnostics(SimpleNamespace(run=run))
        output = io.StringIO()
        with redirect_stderr(output), ThreadPoolExecutor(2) as pool:
            failed = pool.submit(diagnostic.call, 'active', lambda: diagnostic.run('fail'))
            passed = pool.submit(diagnostic.call, 'active', lambda: diagnostic.run('pass'))
            self.assertEqual(passed.result().returncode, 0)
            with self.assertRaises(subprocess.TimeoutExpired) as caught: failed.result()
        self.assertIs(caught.exception, error)
        lines = output.getvalue().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0].split(' ', 1)[1])['worker']['outcome'], 'deadline_exceeded')

    def test_unrecognized_stage_cannot_be_logged(self):
        operation = Mock()
        diagnostic = self.load.ReadDiagnostics(subprocess)
        with self.assertRaisesRegex(ValueError, 'unsupported_read_diagnostic_stage'):
            diagnostic.call('PRIVATE_STAGE', operation)
        operation.assert_not_called()

    def test_source_metrics_use_stdin_without_filesystem_copy(self):
        module, case = harness()
        self.assertEqual(case.load('source', 'metrics'), {})
        module.docker.assert_called_once_with('exec', '-i', 'fixture-source', 'python', '-', 'metrics',
            input=(module.ROOT/'scripts/chain/load.py').read_text(), timeout=90)

    def test_source_probe_rejects_other_commands_and_inputs(self):
        module, case = harness()
        for command, arguments, value in (('reads', (), None), ('metrics', ('extra',), None),
                                           ('metrics', (), 'extra')):
            with self.subTest(command=command, arguments=arguments, value=value):
                with self.assertRaisesRegex(ValueError, 'unsupported_source_load_probe'):
                    case.load('source', command, *arguments, input=value)
        module.docker.assert_not_called()

    def test_copy_targets_remain_the_six_named_state_roles(self):
        module, _ = harness()
        self.assertEqual(module.STATE_HELPER_ROLES,
                         ('web', 'operator', 'engine', 'redis', 'auton', 'alert'))
        self.assertNotIn('source', module.STATE_HELPER_ROLES)

    def test_other_probes_keep_the_existing_command_and_input(self):
        module, case = harness()
        case.load('operator', 'burst', 'binding', input='[]', timeout=30)
        module.docker.assert_called_once_with('exec', '-i', 'fixture-operator',
            'python', '/state/load.py', 'burst', 'binding', input='[]', timeout=30)

    def test_existing_load_modes_keep_their_original_limits(self):
        module, _ = harness()
        self.assertEqual(module.load_settings('0'), dict(duration=0, max_rounds=240,
            worker_seconds=600, certificate_hours=2, result_ttl=None, quiescent_seconds=0))
        self.assertEqual(module.load_settings('7200'), dict(duration=7200, max_rounds=240,
            worker_seconds=9600, certificate_hours=4, result_ttl=14400, quiescent_seconds=0))

    def test_extended_mode_is_explicit_and_exceeds_the_four_hour_run(self):
        module, _ = harness()
        self.assertEqual(module.load_settings('14400'), dict(duration=14400, max_rounds=960,
            worker_seconds=18000, certificate_hours=8, result_ttl=21600, quiescent_seconds=600))
        for value in (14400, True, '14401', '3600', '-1', None):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'unsupported_endurance_duration'):
                module.load_settings(value)

    def quiescent_harness(self):
        module, case = harness()
        case.quiescent_seconds = module.QUIESCENT_SECONDS
        now = [0.0]
        def sleep(seconds):
            self.assertEqual(seconds, 60)
            now[0] += seconds
        module.time = SimpleNamespace(monotonic=lambda: now[0], sleep=sleep)
        case.sample = Mock(return_value=dict(values=dict(engine=dict(redis=dict(pending=0, stream_entries=0)))))
        case.queue = Mock(return_value={'outstanding': 0})
        case.effects = Mock(return_value=1)
        case.auton_records = Mock(return_value=['original'])
        case.load = Mock(return_value={'runs': ['original']})
        case.checkpoint = Mock()
        return module, case

    def test_quiescence_observes_ten_minutes_without_admission_or_purge(self):
        _, case = self.quiescent_harness()
        result = case.observe_quiescence({'runs': ['original']}, ['original'])
        self.assertEqual(result, dict(minimum_seconds=600, observed_seconds=600, samples=10))
        case.load.assert_called_once_with('engine', 'evidence')
        self.assertEqual(case.checkpoint.call_count, 10)
        self.assertTrue(all(c.args[0]['phase']=='quiescent' for c in case.checkpoint.call_args_list))
        case.sample.assert_called_with('quiescent')

    def test_quiescence_fails_if_effects_or_retained_evidence_change(self):
        for attribute, value in (('effects', 2), ('auton_records', []),
                                 ('load', {'runs': []}), ('queue', {'outstanding': 1})):
            with self.subTest(attribute=attribute):
                _, case = self.quiescent_harness()
                getattr(case, attribute).return_value = value
                with self.assertRaises(AssertionError):
                    case.observe_quiescence({'runs': ['original']}, ['original'])
                self.assertTrue(all(c.args[0]['phase']!='complete' for c in case.checkpoint.call_args_list))


if __name__ == '__main__':
    unittest.main()
