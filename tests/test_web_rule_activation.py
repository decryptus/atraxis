"""Activation uses reviewed evidence and explicit, one-use HTTP confirmation."""
from copy import deepcopy
from dataclasses import replace
import re
import unittest
from urllib.parse import urlencode

from atraxis.config import load_config
from atraxis.server import build_context
from atraxis.web_rule_activation import ActivationClient, ACTIVATION_BYTES
import test_web_rule_editor as fixtures
from web_editor_fixture import ActivationFake, NEW_REVISION, REPORT, ORIGIN, CASES
from test_web_rules import REVISION


class ActivationTests(unittest.TestCase):
    fake_class = ActivationFake
    request = fixtures.EditorTests.request
    write_mapping = fixtures.EditorTests.write_mapping
    stop = fixtures.EditorTests.stop

    def setUp(self):
        fixtures.EditorTests.setUp(self)
        client=self.factory(self.root/'alice.yml',expected_actor=dict(subject='alice',organization='demo'))
        client.test('cpu',NEW_REVISION,CASES)
        self.calls.clear()

    def review(self, user='alice', revision=NEW_REVISION, report=REPORT):
        return self.request('/ui/rules/activate?'+urlencode(dict(name='cpu',revision=revision,report=report)),user=user)

    def token(self, user='alice'):
        status,_,body=self.review(user)
        self.assertEqual(status,200,body)
        return re.search(r'name="token" value="([^"]+)"',body).group(1)

    def confirm(self, token, **kwargs):
        return self.request('/ui/rules/activate',method='POST',body=dict(token=token,confirm='activate'),**kwargs)

    def test_review_and_head_are_read_only_then_exact_confirmation_redirects(self):
        status,headers,body=self.review()
        self.assertEqual(status,200)
        self.assertIn('alice',body);self.assertIn('server: <code>lab',body)
        self.assertIn(NEW_REVISION,body);self.assertIn(REPORT,body)
        self.assertIn('Back to test report',body)
        path='/ui/rules/activate?'+urlencode(dict(name='cpu',revision=NEW_REVISION,report=REPORT))
        self.assertEqual(self.request(path,method='HEAD')[2],'')
        self.assertFalse(any(c[0]=='activate' for c in self.calls))
        token=self.token()
        code,response,_=self.confirm(token)
        self.assertEqual(code,303)
        call=next(c for c in self.calls if c[0]=='activate')
        self.assertEqual(call[1],dict(subject='alice',organization='demo'))
        self.assertEqual(call[2:],('cpu',NEW_REVISION,dict(test_id=REPORT,expected_generation='b'*32)))
        for _ in range(2):
            status,_,page=self.request(response['Location'])
            self.assertEqual(status,200)
            self.assertIn('Active at page load',page)
        self.assertEqual(self.confirm(token)[0],403)
        self.assertEqual(sum(c[0]=='activate' for c in self.calls),1)

    def test_failed_or_wrong_evidence_never_issues_confirmation(self):
        original=deepcopy(self.report)
        for changes in [dict(passed=False,results=[dict(name='High CPU',passed=False)]),
                        dict(content_sha256='0'*64),dict(context_sha256='1'*64)]:
            self.report=dict(original,**changes)
            status,_,body=self.review()
            self.assertEqual(status,422)
            self.assertNotIn('Confirm activation',body)
        self.report=dict(original,revision=REVISION)
        self.assertEqual(self.review()[0],503)
        self.assertFalse(any(c[0]=='activate' for c in self.calls))

    def test_failed_report_has_no_review_link_and_active_revision_has_no_form(self):
        self.report.update(passed=False,results=[dict(name='CPU',passed=False)])
        path='/ui/rules/test?'+urlencode(dict(name='cpu',revision=NEW_REVISION,report=REPORT))
        self.assertNotIn('Review activation',self.request(path)[2])
        self.report.update(passed=True,results=[dict(name='CPU',passed=True)])
        self.active_state=dict(revision=NEW_REVISION,generation='e'*32)
        status,_,body=self.review()
        self.assertEqual(status,200);self.assertIn('already active',body)
        self.assertNotIn('name="token"',body)

    def test_first_activation_pins_none_generation(self):
        self.active_state=dict(revision=None,generation=None)
        self.assertEqual(self.confirm(self.token())[0],303)
        self.assertIsNone(next(c for c in self.calls if c[0]=='activate')[4]['expected_generation'])

    def test_concurrent_reviews_conflict_without_automatic_retry(self):
        first,second=self.token(),self.token('bob')
        self.assertEqual(self.confirm(first)[0],303)
        status,_,body=self.confirm(second,user='bob')
        self.assertEqual(status,409);self.assertIn('active version changed',body)
        self.assertEqual(self.confirm(second,user='bob')[0],403)
        self.assertEqual(sum(c[0]=='activate' for c in self.calls),2)

    def test_latest_report_and_engine_permission_are_rechecked_at_confirmation(self):
        token=self.token();self.report['id']='e'*32
        self.assertEqual(self.confirm(token)[0],400)
        self.report['id']=REPORT
        token=self.token();self.error=PermissionError('private-marker')
        status,_,body=self.confirm(token)
        self.assertEqual(status,403);self.assertNotIn('private-marker',body)
        self.assertEqual(sum(c[0]=='activate' for c in self.calls),2)

    def test_identity_change_revocation_origin_and_confirmation_fields(self):
        self.assertEqual(self.confirm(self.token(),user='bob')[0],403)
        for headers in [{'Origin':'null'},{'Origin':'https://evil.example'},{'Sec-Fetch-Site':'cross-site'}]:
            self.assertEqual(self.confirm(self.token(),extra=headers)[0],403)
        token=self.token()
        for body in [dict(token=token),dict(token=token,confirm='no'),dict(token=token,confirm='activate',revision=REVISION)]:
            self.assertEqual(self.request('/ui/rules/activate',method='POST',body=body)[0],400)
        self.assertEqual(self.request('/ui/rules/activate',method='POST',body=('token='+token+'&confirm=activate&confirm=activate').encode())[0],400)
        changed=deepcopy(self.identities);changed[0]['organization']='another'
        self.write_mapping(changed)
        self.assertEqual(self.confirm(token)[0],403)
        self.write_mapping(self.identities);token=self.token();self.write_mapping([])
        self.assertEqual(self.confirm(token)[0],403)
        self.assertFalse(any(c[0]=='activate' for c in self.calls))

    def test_unknown_or_malformed_success_never_retries_or_claims_success(self):
        for malformed in (False,True):
            token=self.token()
            self.malformed_activation=malformed
            self.error=None if malformed else RuntimeError('private-marker')
            status,_,body=self.confirm(token)
            self.assertEqual(status,503);self.assertIn('outcome unconfirmed',body)
            self.assertNotIn('private-marker',body)
            self.assertEqual(self.confirm(token)[0],403)
            self.error=None
        self.assertEqual(sum(c[0]=='activate' for c in self.calls),2)

    def test_revocation_after_commit_hides_success_and_warns(self):
        token=self.token();self.revoke=True
        status,_,body=self.confirm(token)
        self.assertEqual(status,403);self.assertIn('may already have completed',body)
        self.assertNotIn(NEW_REVISION,body)
        self.assertEqual(self.active_state['revision'],NEW_REVISION)

    def test_query_bounds_disabled_route_and_method_boundary(self):
        for path in ['/ui/rules/activate','/ui/rules/activate?name=cpu&revision='+NEW_REVISION+'&report='+REPORT+'&actor=admin',
                     '/ui/rules/activate?name=cpu&revision='+NEW_REVISION+'&report=bad']:
            self.assertEqual(self.request(path)[0],400)
        self.assertEqual(self.request('/ui/rules/activate',method='POST',body=b'',extra={'Content-Length':str(ACTIVATION_BYTES+1)})[0],413)
        context=build_context(replace(load_config(self.config_path),rules_web_origin=None),rule_client_factory=self.factory)
        self.assertNotIn('POST /ui/rules/activate',context.named_commands)
        reader=ActivationClient(self.mapping,'alice',self.factory)
        for method in ('dispatch','test_dispatch','rollback','deactivate','save_draft'):
            with self.assertRaises(PermissionError):reader.call(method)
