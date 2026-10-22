"""Simulation confirmation, strict HTTP forms and engine authority boundaries."""
import re
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

import test_web_rule_editor as editor_tests
from web_editor_fixture import REVISION
from web_simulation_fixture import SimulationFake, BINDING, RUN


class SimulationTests(unittest.TestCase):
    fake_class = SimulationFake
    setUp = editor_tests.EditorTests.setUp
    stop = editor_tests.EditorTests.stop
    write_mapping = editor_tests.EditorTests.write_mapping
    request = editor_tests.EditorTests.request

    def token(self, body):
        return re.search(r'name="token" value="([^"]+)"', body).group(1)

    def form(self):
        status, headers, body = self.request('/ui/rules/simulate?name=cpu&kind=server&target=lab')
        self.assertEqual(status, 200, body)
        self.assertEqual(headers['Cache-Control'], 'no-store')
        return dict(token=self.token(body), binding=BINDING, facts='{"cpu_percent":95}')

    def post(self, fields, route='simulate', **kwargs):
        return self.request('/ui/rules/'+route, method='POST', body=fields, **kwargs)

    def review(self):
        status, _, body = self.post(self.form())
        self.assertEqual(status, 200, body)
        self.assertIn('This review has not queued work', body)
        event = re.search(r'Event: <code>([0-9a-f]{32})</code>', body).group(1)
        return dict(token=self.token(body), confirm='simulate-confirm'), event

    def test_review_is_read_only_confirm_pins_context_and_replay_is_rejected(self):
        fields, event = self.review()
        self.assertEqual(self.calls, [])
        status, headers, _ = self.post(fields, 'simulate-confirm')
        self.assertEqual(status, 303)
        self.assertIn('/ui/interventions/detail?', headers['Location'])
        self.assertIn('mode=test', headers['Location']); self.assertIn('organization=demo', headers['Location'])
        self.assertEqual(self.calls, [('test_dispatch', dict(subject='alice', organization='demo'),
                                     'cpu', ['server', 'lab'], dict(cpu_percent=95), BINDING, event)])
        self.assertEqual(self.post(fields, 'simulate-confirm')[0], 403)
        self.assertEqual(len(self.calls), 1)

    def test_origin_actor_operation_identity_and_framing_are_checked(self):
        for extra in ({'Origin': 'null'}, {'Sec-Fetch-Site': 'cross-site'}, {'Content-Type': 'application/json'}):
            self.assertIn(self.post(self.form(), extra=extra)[0], (400, 403))
        self.assertEqual(self.post(self.form(), user='bob')[0], 403)
        fields, _ = self.review()
        self.assertEqual(self.post(fields, 'simulate-confirm', user='bob')[0], 403)
        for value in ({'actor': 'alice'}, {'generation': 'a'*32}, {'facts': '{}'}):
            fields, _ = self.review()
            self.assertEqual(self.post(dict(fields, **value), 'simulate-confirm')[0], 400)
        form = self.form()
        self.assertEqual(self.post(dict(token=form['token'], confirm='simulate-confirm'), 'simulate-confirm')[0], 403)
        self.assertEqual(self.post((urlencode(self.form())+'&binding='+BINDING).encode())[0], 400)
        self.assertEqual(self.calls, [])

    def test_invalid_and_unbounded_facts_and_bindings_cannot_be_submitted(self):
        for facts in ('[]', '{"a":1,"a":2}', '{"v":NaN}', '{', '{"v":"'+'x'*65536+'"}'):
            form = self.form(); form['facts'] = facts
            self.assertEqual(self.post(form)[0], 400)
        form = self.form(); form['binding'] = '0'*64
        self.assertEqual(self.post(form)[0], 400)
        for change in (dict(test_supported=False), dict(revision='invalid'), dict(id='bad')):
            self.bindings = [dict(id=BINDING, rule='cpu', revision=REVISION, workflow='notify', actions=['ops.notify'], test_supported=True)]
            self.bindings[0].update(change)
            status, _, body = self.request('/ui/rules/simulate?name=cpu&kind=server&target=lab')
            self.assertEqual(status, 200 if change == dict(test_supported=False) else 503)
            self.assertNotIn('name="token"', body)
        self.assertEqual(self.calls, [])

    def test_current_rights_and_binding_checked_again_on_confirmation(self):
        fields, _ = self.review(); self.denied = True
        self.assertEqual(self.post(fields, 'simulate-confirm')[0], 403)
        self.denied = False
        self.assertEqual(self.post(fields, 'simulate-confirm')[0], 403)
        fields, _ = self.review(); self.bindings = []
        self.assertEqual(self.post(fields, 'simulate-confirm')[0], 409)
        self.assertEqual(self.calls, [])

    def test_confirmation_is_bound_to_selected_organization(self):
        fields, _ = self.review()
        self.write_mapping(self.identities + [dict(username='alice', subject='alice',
            organization='other', atropos_client='other.yml')])
        status, _, _ = self.request('/ui/rules/simulate-confirm?organization=other', method='POST', body=fields)
        self.assertEqual(status, 403)
        self.assertEqual(self.calls, [])

    def test_safe_methods_never_submit_and_strict_query_rejects_override(self):
        for query in ('name=cpu&kind=server&target=lab&subject=alice', 'name=cpu&kind=group&target=lab',
                      'name=cpu&name=cpu', 'name=cpu&kind=server&target='):
            self.assertEqual(self.request('/ui/rules/simulate?'+query)[0], 400)
        code, headers, body = self.request('/ui/rules/simulate?name=cpu&kind=server&target=lab', method='HEAD')
        self.assertEqual(code, 200); self.assertEqual(body, '')
        self.assertEqual(headers['Cache-Control'], 'no-store'); self.assertEqual(self.calls, [])

    def test_unknown_or_malformed_result_keeps_event_and_never_retries(self):
        for error, response in ((TimeoutError('secret'), {}), (None, dict(test_context=None)),
                                (None, dict(revision='0'*32)), (PermissionError('secret'), {})):
            fields, event = self.review(); self.error, self.response = error, response
            status, _, body = self.post(fields, 'simulate-confirm')
            self.assertEqual(status, 403 if isinstance(error, PermissionError) else 503)
            self.assertIn('Outcome unconfirmed', body); self.assertIn(event, body)
            self.assertIn('mode=test', body); self.assertIn('organization=demo', body)
            self.assertNotIn('secret', body)
            count = len(self.calls)
            self.error = None; self.response = {}
            self.assertEqual(self.post(fields, 'simulate-confirm')[0], 403)
            self.assertEqual(len(self.calls), count)

    def test_nonmatching_decision_is_not_reported_as_an_admitted_run(self):
        fields, event = self.review()
        self.response = dict(status='not_matched', simulation=True, matches_binding=False,
                             binding=BINDING, target=['server', 'lab'])
        status, headers, body = self.post(fields, 'simulate-confirm')
        self.assertEqual(status, 200, body); self.assertNotIn('Location', headers)
        self.assertIn('No test run was admitted', body); self.assertIn(event, body)

    def test_live_mapping_revocation_discards_results_and_default_routes_absent(self):
        fields, _ = self.review(); self.revoke = True
        status, _, body = self.post(fields, 'simulate-confirm')
        self.assertEqual(status, 403); self.assertIn('Outcome unconfirmed', body)
        self.write_mapping(self.identities)
        from dataclasses import replace
        from atraxis.config import load_config
        from atraxis.server import build_context
        # Core CI deliberately has no private optional client installed.
        with patch.dict('sys.modules', {'atropos_client': None, 'atropos_client.client': None}):
            readonly = build_context(replace(load_config(self.config_path), rules_web_origin=None),
                                     rule_client_factory=self.factory)
        self.assertFalse(any('simulate' in key for key in readonly.named_commands))
        self.assertEqual(self.request('/ui/rules/simulate-confirm')[0], 400)
