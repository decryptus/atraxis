"""HTTP lifecycle confirmations, concurrent changes and scoped audit boundaries."""
from copy import deepcopy
from dataclasses import replace
import re
import unittest
from urllib.parse import urlencode

from atraxis.config import load_config
from atraxis.server import build_context
from atraxis.web_rule_activation import RollbackClient, DeactivationClient, ACTIVATION_BYTES
from atraxis.web_rule_audit import AuditClient, events_checked
import test_web_rule_editor as fixtures
from web_editor_fixture import LifecycleFake, NEW_REVISION, REPORT, CASES
from test_web_rules import REVISION


class LifecycleTests(unittest.TestCase):
    fake_class = LifecycleFake
    request = fixtures.EditorTests.request
    write_mapping = fixtures.EditorTests.write_mapping
    stop = fixtures.EditorTests.stop

    def setUp(self):
        fixtures.EditorTests.setUp(self)
        self.active_state = dict(revision=NEW_REVISION, generation='e'*32)
        self.client = self.factory(self.root/'alice.yml', expected_actor=dict(subject='alice', organization='demo'))
        self.client.test('cpu', REVISION, CASES)
        self.calls.clear()

    def path(self, operation):
        values = dict(name='cpu')
        if operation == 'rollback':
            values.update(revision=REVISION, report=REPORT)
        return '/ui/rules/'+operation+'?'+urlencode(values)

    def token(self, operation, user='alice'):
        status, _, body = self.request(self.path(operation), user=user)
        self.assertEqual(status, 200, body)
        return re.search(r'name="token" value="([^"]+)"', body).group(1)

    def confirm(self, operation, token, **kwargs):
        return self.request('/ui/rules/'+operation, method='POST', body=dict(token=token, confirm=operation), **kwargs)

    def test_rollback_pins_evidence_and_generation_and_refresh_never_repeats(self):
        status, _, body = self.request(self.path('rollback'))
        self.assertEqual(status, 200)
        self.assertIn('previously active revision', body)
        self.assertIn(NEW_REVISION, body)
        self.assertNotIn(' checked', body)
        self.assertEqual(self.request(self.path('rollback'), method='HEAD')[2], '')
        self.assertFalse(any(c[0] == 'rollback' for c in self.calls))
        token = self.token('rollback')
        status, headers, _ = self.confirm('rollback', token)
        self.assertEqual(status, 303)
        call = next(c for c in self.calls if c[0] == 'rollback')
        self.assertEqual(call[1:], (dict(subject='alice', organization='demo'), 'cpu', REVISION,
                                  dict(expected_generation='e'*32, test_id=REPORT)))
        self.assertEqual(self.request(headers['Location'])[0], 200)
        self.assertEqual(self.request(headers['Location'])[0], 200)
        self.assertEqual(self.confirm('rollback', token)[0], 403)
        self.assertEqual(sum(c[0] == 'rollback' for c in self.calls), 1)
        self.assertEqual(self.events[0]['operation'], 'rollback')

    def test_deactivation_preserves_draft_and_redirects_with_no_repeat(self):
        status, _, body = self.request(self.path('deactivate'))
        self.assertEqual(status, 200)
        self.assertIn('Running actions are not cancelled', body)
        self.assertIn('server: <code>lab', body)
        self.assertEqual(self.request(self.path('deactivate'), method='HEAD')[2], '')
        self.assertFalse(any(c[0] == 'deactivate' for c in self.calls))
        token = self.token('deactivate')
        status, headers, _ = self.confirm('deactivate', token)
        self.assertEqual(status, 303)
        call = next(c for c in self.calls if c[0] == 'deactivate')
        self.assertEqual(call[4], dict(expected_generation='e'*32))
        self.assertIsNone(self.active_state['revision'])
        self.assertEqual(self.head, REVISION)
        self.assertIn('Not active', self.request(headers['Location'])[2])
        self.assertEqual(self.confirm('deactivate', token)[0], 403)
        body = self.request(self.path('deactivate'))[2]
        self.assertIn('already inactive', body)
        self.assertNotIn('name="token"', body)
        self.assertEqual(self.events[0]['operation'], 'deactivate')

    def test_reviews_conflict_across_operations_and_users(self):
        rollback = self.token('rollback')
        deactivate = self.token('deactivate', 'bob')
        self.assertEqual(self.confirm('rollback', rollback)[0], 303)
        status, _, body = self.confirm('deactivate', deactivate, user='bob')
        self.assertEqual(status, 409)
        self.assertIn('active version changed', body)
        self.assertEqual(self.confirm('deactivate', deactivate, user='bob')[0], 403)
        self.assertEqual(len(self.events), 1)

    def test_engine_denies_never_active_stale_tests_and_revoked_rights(self):
        token = self.token('rollback'); self.was_active = set()
        self.assertEqual(self.confirm('rollback', token)[0], 400)
        self.was_active = {REVISION}
        token = self.token('rollback'); self.report['id'] = 'f'*32
        self.assertEqual(self.confirm('rollback', token)[0], 400)
        self.report['id'] = REPORT
        for op in ('rollback', 'deactivate'):
            token = self.token(op); self.error = PermissionError('private-marker')
            status, _, body = self.confirm(op, token)
            self.assertEqual(status, 403)
            self.assertNotIn('private-marker', body)
            self.error = None
        self.assertEqual(self.active_state['revision'], NEW_REVISION)

    def test_cross_operation_and_account_tokens_origin_and_fields_rejected(self):
        for op, other in [('rollback', 'deactivate'), ('deactivate', 'rollback')]:
            self.assertEqual(self.confirm(other, self.token(op))[0], 403)
            self.assertEqual(self.confirm(op, self.token(op), user='bob')[0], 403)
            for extra in [{'Origin':'null'}, {'Origin':'https://evil.test'}, {'Sec-Fetch-Site':'cross-site'}]:
                self.assertEqual(self.confirm(op, self.token(op), extra=extra)[0], 403)
            token = self.token(op)
            for values in [dict(token=token), dict(token=token, confirm='activate'),
                           dict(token=token, confirm=op, expected_generation='e'*32)]:
                self.assertEqual(self.request('/ui/rules/'+op, method='POST', body=values)[0], 400)
            self.assertEqual(self.request('/ui/rules/'+op, method='POST',
                body=('token='+token+'&confirm='+op+'&confirm='+op).encode())[0],400)
        self.assertFalse(any(c[0] in ('rollback', 'deactivate') for c in self.calls))

    def test_mapping_change_and_expired_review_prevent_mutation(self):
        for op in ('rollback', 'deactivate'):
            self.write_mapping(self.identities)
            token = self.token(op)
            changed = deepcopy(self.identities); changed[0]['organization'] = 'another'
            self.write_mapping(changed)
            self.assertEqual(self.confirm(op, token)[0], 403)
            self.write_mapping(self.identities)
            first = self.token(op)
            for _ in range(16): self.token(op)
            self.assertEqual(self.confirm(op, first)[0], 403)
        self.assertFalse(any(c[0] in ('rollback', 'deactivate') for c in self.calls))

    def test_unknown_success_ack_and_post_commit_revocation_do_not_retry(self):
        for op in ('rollback', 'deactivate'):
            for scenario in ('network', 'malformed', 'revoked'):
                self.write_mapping(self.identities)
                self.active_state = dict(revision=NEW_REVISION, generation='e'*32)
                self.error = None; self.revoke = False; self.malformed_lifecycle = False
                token = self.token(op)
                self.error = RuntimeError('private-marker') if scenario == 'network' else None
                self.malformed_lifecycle = scenario == 'malformed'
                self.revoke = scenario == 'revoked'
                status, _, body = self.confirm(op, token)
                self.assertEqual(status, 403 if scenario == 'revoked' else 503)
                self.assertIn('may already have completed' if scenario == 'revoked' else 'outcome unconfirmed', body)
                self.assertNotIn('private-marker', body)
                self.assertEqual(self.confirm(op, token)[0], 403)
        self.assertEqual(sum(c[0] in ('rollback','deactivate') for c in self.calls), 6)

    def test_audit_is_read_only_bounded_and_retains_empty_window_navigation(self):
        self.confirm('rollback', self.token('rollback'))
        self.confirm('deactivate', self.token('deactivate'))
        before = len(self.calls)
        for _ in range(2):
            status, headers, body = self.request('/ui/rules/audit?name=cpu')
            self.assertEqual(status, 200)
            self.assertIn('Rollback</h3>', body); self.assertIn('Deactivation</h3>', body)
            self.assertIn('UTC', body); self.assertIn('alice', body)
            self.assertNotIn('<form class="activation"', body)
            self.assertEqual({k.lower():v for k,v in headers.items()}['cache-control'], 'no-store')
        self.assertTrue(all(c[0] == 'audit' for c in self.calls[before:]))
        self.assertEqual(self.calls[-1][2:], ('cpu',0,20))
        self.assertEqual(self.request('/ui/rules/audit?name=cpu', method='HEAD')[2], '')
        status, _, body = self.request('/ui/rules/audit?name=cpu&offset=20')
        self.assertEqual(status,200); self.assertIn('No accessible events',body)
        self.assertIn('Next window',body); self.assertIn('Previous window',body)
        self.assertNotEqual(self.request('/ui/rules/audit?name=cpu', method='POST', body={})[0], 200)

    def test_audit_denials_revocation_and_invalid_responses_hide_data(self):
        self.confirm('rollback', self.token('rollback'))
        original = deepcopy(self.events)
        for change in [dict(operation='<script>'), dict(author='<img src=x>'), dict(organization='another'),
                       dict(created_at=True), dict(revision='bad'), dict(scope_revisions=[]), dict(test_id=None)]:
            self.events = [dict(original[0], **change)]
            status, _, body = self.request('/ui/rules/audit?name=cpu')
            self.assertEqual(status,503); self.assertNotIn('<script>',body)
        with self.assertRaises(ValueError): events_checked(original*21,'demo')
        self.events = original
        self.error = PermissionError('private-marker')
        status, _, body = self.request('/ui/rules/audit?name=cpu')
        self.assertEqual(status,403); self.assertNotIn(REVISION,body)
        self.error = None; self.revoke_audit = True
        self.assertEqual(self.request('/ui/rules/audit?name=cpu')[0],403)

    def test_routes_queries_body_limits_and_narrow_client_methods(self):
        for path in ['/ui/rules/deactivate', '/ui/rules/deactivate?name=cpu&revision='+REVISION,
                     '/ui/rules/rollback?name=cpu', '/ui/rules/audit', '/ui/rules/audit?name=cpu&offset=-1',
                     '/ui/rules/audit?name=cpu&offset=1000001', '/ui/rules/audit?name=cpu&actor=admin',
                     '/ui/rules/audit?name=cpu&name=cpu']:
            self.assertEqual(self.request(path)[0],400,path)
        for op in ('rollback','deactivate'):
            self.assertEqual(self.request('/ui/rules/'+op,method='POST',body=b'',
                extra={'Content-Length':str(ACTIVATION_BYTES+1)})[0],413)
        context = build_context(replace(load_config(self.config_path),rules_web_origin=None),rule_client_factory=self.factory)
        self.assertIn('GET /ui/rules/audit',context.named_commands)
        for op in ('rollback','deactivate'):
            self.assertNotIn('POST /ui/rules/'+op,context.named_commands)
        for cls, denied in [(RollbackClient,('activate','deactivate','dispatch')), (DeactivationClient,('activate','rollback','dispatch')),
                            (AuditClient,('activate','rollback','deactivate','dispatch','read'))]:
            reader = cls(self.mapping,'alice',self.factory)
            for method in denied:
                with self.assertRaises(PermissionError):reader.call(method)

    def admission_events(self):
        common = dict(author='alice', organization='demo', created_at=1791385200.125,
                      revision=REVISION, scope_revisions=[REVISION])
        return [dict(common, operation='dispatch', generation='e'*32, binding='b'*64, run_id='d'*64),
                dict(common, operation='test_run', test_context={'test':True, 'id':'f'*64}, run_id='f'*64)]

    def test_mixed_audit_admissions_are_information_not_execution_success(self):
        self.confirm('rollback', self.token('rollback'))
        self.events.extend(self.admission_events())
        before = len(self.calls)
        status, _, body = self.request('/ui/rules/audit?name=cpu')
        self.assertEqual(status, 200, body)
        for label in ('Rollback</h3>', 'Intervention admitted</h3>', 'Test run admitted</h3>',
                      'Execution outcome is not recorded', 'Test mode', 'd'*64, 'f'*64):
            self.assertIn(label, body)
        self.assertEqual(body.count('class="info"'), 2)
        self.assertNotIn('class="success"', body)
        self.assertTrue(all(call[0] == 'audit' for call in self.calls[before:]))

    def test_admission_audit_rejects_invalid_shapes_markers_and_timestamps(self):
        dispatch, test = self.admission_events()
        malformed = [dict(dispatch, generation=None), dict(dispatch, binding='bad'),
                     dict(dispatch, run_id='bad'), dict(dispatch, unknown='secret'),
                     dict(test, test_context={'test':False, 'id':'f'*64}),
                     dict(test, test_context={'test':1, 'id':'f'*64}),
                     dict(test, test_context={'test':True, 'id':'e'*64}),
                     dict(test, scope_revisions=['b'*32]), dict(test, organization='other')]
        malformed += [dict(dispatch, created_at=value) for value in
                      (True, -1, float('nan'), float('inf'), 253402300800)]
        for event in malformed:
            with self.subTest(event=event), self.assertRaises(ValueError):
                events_checked([event], 'demo')
