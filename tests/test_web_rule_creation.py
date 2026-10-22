"""HTTP framing, actor-bound creation and unknown-outcome handling."""
from copy import deepcopy
import json
import re
import unittest
from urllib.parse import urlencode

import test_web_rule_editor as editor_tests
from web_editor_fixture import EditorFake, NEW_REVISION, ORIGIN


class CreationFake(EditorFake):
    def catalog(self):
        return dict(catalog_version=7, templates=[dict(id='cpu-high', description='CPU usage')])

    def create_from_templates(self, templates, **values):
        return self.save_created('create', dict(name=values['name']), dict(values, templates=templates))

    def import_draft(self, content, **values):
        return self.save_created('import', json.loads(content), values)

    def save_created(self, operation, document, values):
        self.owner.calls.append((operation, deepcopy(document), deepcopy(values), self.actor))
        if self.owner.error:
            raise self.owner.error
        if self.owner.revoke:
            self.owner.write_mapping([])
        return dict(revision=NEW_REVISION, document=document,
                    context=dict(version=1, rule_kind=values['rule_kind'], targets=values['selections']))


class CreationTests(unittest.TestCase):
    fake_class = CreationFake
    setUp = editor_tests.EditorTests.setUp
    stop = editor_tests.EditorTests.stop
    write_mapping = editor_tests.EditorTests.write_mapping
    request = editor_tests.EditorTests.request

    def fields(self, route='create', user='alice'):
        code, _, body = self.request('/ui/rules/' + route, user=user)
        self.assertEqual(code, 200, body)
        token = re.search(r'name="token" value="([^"]+)"', body).group(1)
        data = dict(token=token, name='new-rule', rule_kind='intervention', target_kind='server', target_id='lab')
        data.update(dict(template='cpu-high') if route == 'create' else dict(document='name: new-rule\n'))
        return data

    def post(self, data, route='create', **kwargs):
        return self.request('/ui/rules/' + route, method='POST', body=data, **kwargs)

    def test_new_drafts_pin_catalog_version_and_never_overwrite(self):
        for route in ('create', 'import'):
            with self.subTest(route=route):
                data = self.fields(route)
                before = len(self.calls)
                code, headers, _ = self.post(data, route)
                self.assertEqual(code, 303)
                self.assertIn('organization=demo', headers['Location'])
                call = self.calls[-1]
                self.assertEqual(call[0], route)
                self.assertEqual(call[3], dict(subject='alice', organization='demo'))
                self.assertEqual(call[2]['selections'], [['server', 'lab']])
                if route == 'create':
                    self.assertEqual(call[2]['catalog_version'], 7)
                else:
                    self.assertIsNone(call[2]['expected_revision'])
                    self.assertEqual(call[2]['format'], 'json')
                self.assertEqual(len(self.calls), before + 1)
                self.assertEqual(self.post(data, route)[0], 403)
                self.assertEqual(len(self.calls), before + 1)

    def test_cross_actor_operation_origin_and_extra_identity_are_rejected(self):
        data = self.fields()
        self.assertEqual(self.post(data, user='bob')[0], 403)
        data = self.fields()
        self.assertEqual(self.post(data, extra={'Origin': 'https://foreign.invalid'})[0], 403)
        self.assertEqual(self.post(dict(data, subject='alice'))[0], 400)
        imported = self.fields('import'); imported['token'] = data['token']
        self.assertEqual(self.post(imported, 'import')[0], 403)
        self.assertEqual(self.calls, [])

    def test_invalid_payloads_and_duplicate_framing_never_call_engine(self):
        for body in ('name: other\n', 'name: new-rule\nname: new-rule\n', '!!python/object:object {}'):
            data = self.fields('import'); data['document'] = body
            self.assertEqual(self.post(data, 'import')[0], 400)
        data = self.fields(); data['template'] = 'unknown'
        self.assertEqual(self.post(data)[0], 400)
        data = self.fields(); data['target_kind'] = 'server_group'
        self.assertEqual(self.post(data)[0], 400)
        data = self.fields()
        self.assertEqual(self.post((urlencode(data) + '&name=other').encode())[0], 400)
        self.assertEqual(self.post(data, extra={'Content-Type': 'application/json'})[0], 400)
        self.assertEqual(self.calls, [])

    def test_conflict_denial_and_unknown_outcome_are_not_retried(self):
        for error, status in ((ValueError('rule_conflict'), 409), (PermissionError(), 403),
                              (ValueError('rule_outcome_unknown'), 503)):
            with self.subTest(status=status):
                data = self.fields(); self.error = error
                before = len(self.calls)
                code, _, body = self.post(data)
                self.assertEqual(code, status)
                if status == 503:
                    self.assertIn('Outcome unconfirmed', body)
                self.assertEqual(len(self.calls), before + 1)
                self.error = None
                self.assertEqual(self.post(data)[0], 403)
                self.assertEqual(len(self.calls), before + 1)

    def test_mapping_revoked_before_or_during_request_discards_result(self):
        data = self.fields(); self.write_mapping([])
        self.assertEqual(self.post(data)[0], 403)
        self.assertEqual(self.calls, [])
        self.write_mapping(self.identities)
        data = self.fields(); self.revoke = True
        code, _, body = self.post(data)
        self.assertEqual(code, 403)
        self.assertIn('may already have completed', body)
        self.assertEqual(len(self.calls), 1)

    def test_navigation_only_when_writes_configured_and_form_queries_rejected(self):
        from atraxis.web_rules import handle
        code, body = handle('/ui/rules', 'alice', self.mapping, factory=self.factory)
        self.assertEqual(code, 200)
        self.assertNotIn('Create from template', body)
        self.assertNotIn('Import a new rule', body)
        code, _, body = self.request('/ui/rules')
        self.assertEqual(code, 200)
        self.assertIn('Create from template', body)
        self.assertIn('/ui/rules/create?organization=demo', body)
        self.assertEqual(self.request('/ui/rules/create?name=existing')[0], 400)
