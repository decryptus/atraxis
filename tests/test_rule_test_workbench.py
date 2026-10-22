"""Test-mode presentation preserves reviewed inputs and recovery identity."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from atraxis.rule_workbench import RuleWorkbench

BINDING = 'a' * 64
CONTEXT = {'test': True, 'id': 'b' * 64}


class TestWorkbenchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.facts = Path(self.temp.name) / 'facts.json'
        self.facts.write_text('{"cpu_percent":99}')
        self.operator = Mock(); self.api = self.operator.application
        self.preview = {'simulation': True, 'binding': BINDING, 'revision': 'r1',
                        'target': ['container', 'web'], 'matches_binding': True}
        self.receipt = {'id': CONTEXT['id'], 'test_context': CONTEXT, 'status': 'queued',
                        'rule': 'cpu', 'revision': 'r1', 'results': {}, 'history': []}
        self.api.simulate_dispatch.return_value = self.preview
        self.api.test_dispatch.return_value = self.receipt
        self.api.test_dispatch_status.return_value = self.receipt
        self.model = RuleWorkbench(self.operator, 'cpu')

    def prepare(self):
        return self.model.prepare_test(('container', 'web'), BINDING, str(self.facts), 'event-1')

    def test_confirmation_uses_exact_reviewed_facts_and_never_real_dispatch(self):
        self.assertTrue(self.prepare()); self.api.test_dispatch.assert_not_called()
        self.facts.write_text('{"cpu_percent":1}')
        self.model.confirm(); self.model.confirm()
        self.api.test_dispatch.assert_called_once_with('cpu', ['container', 'web'],
            {'cpu_percent': 99}, BINDING, event_id='event-1')
        self.api.dispatch.assert_not_called(); self.api.activate.assert_not_called()
        self.assertIsNone(self.model.pending)
        self.assertEqual(self.model.test_notice, 'info')
        self.assertEqual(self.model.reports, {})

    def test_cancel_does_not_submit(self):
        self.prepare(); self.model.pending = None; self.model.confirm()
        self.api.test_dispatch.assert_not_called()

    def test_nonmatching_preview_does_not_offer_submission(self):
        self.preview['matches_binding'] = False
        self.assertFalse(self.prepare()); self.model.confirm()
        self.assertIsNone(self.model.pending)
        self.api.test_dispatch.assert_not_called()
        self.assertEqual(self.model.test_notice, 'warning')

    def test_unknown_outcome_is_not_retried_and_status_recovers_same_event(self):
        self.prepare(); self.api.test_dispatch.side_effect = TimeoutError('secret')
        with self.assertRaises(TimeoutError): self.model.confirm()
        self.model.confirm()
        self.assertEqual(self.model.test_event, 'event-1')
        self.model.test_status('event-1')
        self.api.test_dispatch.assert_called_once()
        self.api.test_dispatch_status.assert_called_once_with('cpu', 'event-1')
        self.assertEqual(self.model.test_context, CONTEXT)

    def test_markers_rule_revision_and_unknown_status_are_rejected(self):
        invalid = [{'test_context': {'test': False, 'id': CONTEXT['id']}},
                   {'test_context': {'test': True, 'id': 'invalid'}},
                   {'rule': 'another'}, {'revision': 'r2'}, {'status': 'accepted-as-success'},
                   {'id': 'c' * 64}]
        for change in invalid:
            with self.subTest(change=change):
                self.prepare()
                self.api.test_dispatch.return_value = dict(self.receipt, **change)
                with self.assertRaises(ValueError): self.model.confirm()
                self.assertIsNone(self.model.pending)
                self.assertEqual(self.model.test_notice, 'warning')

    def test_status_marker_cannot_change_for_same_event(self):
        self.prepare(); self.model.confirm()
        changed = copy.deepcopy(self.receipt)
        changed['test_context']['id'] = changed['id'] = 'c' * 64
        self.api.test_dispatch_status.return_value = changed
        with self.assertRaises(ValueError): self.model.test_status('event-1')

    def test_status_severity_distinguishes_acceptance_and_completion(self):
        expected = {'queued': 'info', 'running': 'running', 'retrying': 'running',
                    'succeeded': 'success', 'failed': 'failure', 'cancelled': 'failure',
                    'uncertain': 'warning'}
        for status, severity in expected.items():
            with self.subTest(status=status):
                self.api.test_dispatch_status.return_value = dict(self.receipt, status=status)
                self.model.test_status('event-1')
                self.assertEqual(self.model.test_notice, severity)
        self.api.test_dispatch.assert_not_called()

    def test_invalid_inputs_and_preview_never_submit(self):
        for content in ('[]', '{"x":1,"x":2}', '{"x":NaN}'):
            self.facts.write_text(content)
            with self.assertRaises(ValueError): self.prepare()
        self.facts.write_text('{}')
        self.preview['binding'] = 'different'
        with self.assertRaises(ValueError): self.prepare()
        self.assertIsNone(self.model.pending)
        self.api.test_dispatch.assert_not_called()

    def test_binding_list_is_read_only_and_validated(self):
        row={'id':BINDING,'rule':'cpu','revision':'r1','workflow':'demo',
             'actions':['core.echo'],'test_supported':True}
        self.api.test_bindings.return_value=[row]
        self.assertEqual(self.model.available_test_bindings(('container','web')),[row])
        self.api.test_bindings.assert_called_once_with('cpu',['container','web'])
        self.api.test_dispatch.assert_not_called()
        for result in ([row,row], [dict(row,rule='other')], [dict(row,test_supported=1)],
                       [dict(row,inputs={'secret':'hidden'})], [row]*65, None):
            self.api.test_bindings.return_value=result
            with self.subTest(result=result),self.assertRaises(ValueError):
                self.model.available_test_bindings(('container','web'))

    def test_queue_full_is_a_refusal_without_retry_or_success(self):
        self.prepare();self.api.test_dispatch.side_effect=ValueError('rule_queue_full')
        self.model.confirm();self.model.confirm()
        self.api.test_dispatch.assert_called_once()
        self.assertIsNone(self.model.pending)
        self.assertIsNone(self.model.test_context)
        self.assertEqual(self.model.test_notice,'warning')
        self.assertIn('not admitted',self.model.message)
