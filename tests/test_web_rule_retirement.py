"""HTTP confirmation boundaries; engine eligibility is verified in engine_tests."""
from copy import deepcopy
import re
import unittest
import uuid

import test_web_rule_editor as fixtures
from web_editor_fixture import EditorFake
from test_web_rules import REVISION
from atraxis.web_rule_audit import events_checked


class RetirementFake(EditorFake):
    def availability(self,name):return deepcopy(self.owner.availability)
    def _retire(self,operation,name,**values):
        self.owner.calls.append((operation,self.actor,name,values))
        if self.owner.error:raise self.owner.error
        if (values['expected_revision']!=self.owner.head or values['expected_generation']!=self.owner.availability['generation']):
            raise ValueError('rule_conflict')
        result=dict(status={'archive':'archived','unarchive':'available','delete':'deleted'}[operation],
                    revision=self.owner.head,generation=uuid.uuid4().hex)
        self.owner.availability=result
        return deepcopy(result)
    def archive(self,name,**values):return self._retire('archive',name,**values)
    def unarchive(self,name,**values):return self._retire('unarchive',name,**values)
    def delete(self,name,**values):return self._retire('delete',name,**values)


class RetirementHTTPTests(unittest.TestCase):
    fake_class=RetirementFake
    request=fixtures.EditorTests.request
    write_mapping=fixtures.EditorTests.write_mapping
    stop=fixtures.EditorTests.stop
    def setUp(self):
        self.availability=dict(status='available',generation=None)
        fixtures.EditorTests.setUp(self)
    def token(self,operation,user='alice'):
        status,_,body=self.request('/ui/rules/'+operation+'?name=cpu',user=user)
        self.assertEqual(status,200,body)
        return re.search(r'name="token" value="([^"]+)"',body).group(1)
    def confirm(self,operation,token,**kwargs):
        return self.request('/ui/rules/'+operation,method='POST',body=dict(token=token,confirm=operation),**kwargs)

    def test_each_operation_requires_exact_single_use_review(self):
        for operation in ('archive','unarchive','delete'):
            token=self.token(operation)
            self.assertFalse(any(c[0]==operation for c in self.calls))
            status,headers,body=self.confirm(operation,token)
            self.assertEqual(status,303,body)
            self.assertEqual(headers['Location'],'/ui/rules/audit?name=cpu&organization=demo')
            self.assertEqual(self.confirm(operation,token)[0],403)
            call=next(c for c in self.calls if c[0]==operation)
            self.assertEqual(call[3]['expected_revision'],REVISION)
        self.assertEqual(self.availability['status'],'deleted')

    def test_cross_user_origin_operation_and_stale_generation_are_refused(self):
        self.assertEqual(self.confirm('archive',self.token('archive'),user='bob')[0],403)
        self.assertEqual(self.confirm('archive',self.token('archive'),extra={'Origin':'https://wrong.example'})[0],403)
        self.assertEqual(self.confirm('delete',self.token('archive'))[0],403)
        token=self.token('archive');self.availability['generation']='a'*32
        self.assertEqual(self.confirm('archive',token)[0],409)
        self.assertEqual(self.availability['status'],'available')

    def test_lost_response_cannot_be_retried_with_same_confirmation(self):
        token=self.token('delete');self.error=ValueError('rule_outcome_unknown')
        status,_,body=self.confirm('delete',token)
        self.assertEqual(status,503);self.assertIn('Outcome unconfirmed',body)
        self.assertEqual(self.confirm('delete',token)[0],403)
        self.assertEqual(sum(c[0]=='delete' for c in self.calls),1)

    def test_retirement_audit_schema_accepts_multiple_scope_revisions(self):
        event=dict(operation='delete',generation='a'*32,previous_generation=None,
            author='alice',organization='demo',created_at=1,revision=REVISION,
            scope_revisions=[REVISION,'b'*32,'c'*32])
        self.assertEqual(events_checked([event],'demo'),[event])
        with self.assertRaises(ValueError):events_checked([event],'other')
