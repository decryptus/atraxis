from copy import deepcopy
from pathlib import Path
import re
import tempfile
import unittest
from urllib.parse import urlencode

import yaml

from atraxis.intervention_search import InterventionQuery
from atraxis.web_reconciliation import WebReconciliation
from atraxis.web_intervention_detail import receipt, render
from atraxis.web_rule_audit import events_checked
import test_intervention_search as search_fixture
import test_web_intervention_detail as detail_fixture

ORIGIN = 'https://console.example.test'


class WebReconciliationTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.mapping=Path(temp.name)/'mapping.yml'
        self.entries=[dict(username=u,subject=u,organization='org',atropos_client=u+'.yml') for u in ('alice','bob')]
        self.write_mapping(self.entries)
        self.row=search_fixture.result()['items'][0]
        self.row.update(mode='real',test_context=None,status='uncertain')
        self.token_hash='e'*64;self.calls=[];self.error=None;self.changed=False;self.revoke=False
        owner=self
        class Client:
            def search_interventions(self,name,**kwargs):
                values=dict(kwargs);values['q']=values.pop('query')
                return search_fixture.result(InterventionQuery(**values),items=[deepcopy(owner.row)])
            def plan_reconciliation(self,name,event_id,**kwargs):
                owner.calls.append(('plan',name,event_id,kwargs))
                return dict(mode=owner.row['mode'],expected_state=owner.token_hash,receipt=detail_fixture.state(owner.row))
            def reconcile(self,name,event_id,**kwargs):
                owner.calls.append(('reconcile',name,event_id,kwargs))
                if owner.error:raise owner.error
                if owner.revoke:owner.write_mapping([])
                data=detail_fixture.state(owner.row);data['status']=kwargs['outcome']
                data['reconciliation']=dict(author='alice',organization='org',created_at=1791480010,
                    expected_state=kwargs['expected_state'],outcome=kwargs['outcome'],evidence=kwargs['evidence'])
                return data
            def close(self):pass
        self.controller=WebReconciliation(self.mapping,ORIGIN,lambda *a,**kw:Client())

    def write_mapping(self,entries):
        self.mapping.write_text(yaml.safe_dump(dict(version=1,identities=entries)));self.mapping.chmod(0o600)
    def token(self,body):return re.search(r'name="token" value="([^"]+)"',body).group(1)
    def get(self):
        return self.controller.handle('reconcile','/ui/rules/reconcile?'+urlencode(dict(
            name='cpu',mode=self.row['mode'],id=self.row['id'])),'alice')
    def post(self,values,route='reconcile',user='alice',headers=None):
        return self.controller.handle(route,'/ui/rules/'+route+'?organization=org',user,
            method='POST',headers=headers or dict(origin=ORIGIN),raw=urlencode(values).encode())
    def review(self):
        code,body,_=self.get();self.assertEqual(code,200,body)
        code,body,_=self.post(dict(token=self.token(body),outcome='failed',evidence='ticket-42 <checked>'))
        self.assertEqual(code,200,body);self.assertIn('ticket-42 &lt;checked&gt;',body)
        return dict(token=self.token(body),confirm='reconcile-confirm')

    def test_separate_review_and_confirmation_pin_every_field(self):
        for mode in ('real','test'):
            self.row.update(mode=mode,test_context=dict(test=True,id=self.row['id']) if mode=='test' else None)
            self.calls.clear();values=self.review()
            self.assertEqual([c[0] for c in self.calls],['plan','plan'])
            code,_,headers=self.post(values,'reconcile-confirm');self.assertEqual(code,303)
            self.assertIn('organization=org',headers['Location']);self.assertIn('mode='+mode,headers['Location'])
            self.assertEqual(self.calls[-1],('reconcile','cpu','event-1',dict(mode=mode,
                expected_state='e'*64,outcome='failed',evidence='ticket-42 <checked>')))
            self.assertEqual(self.post(values,'reconcile-confirm')[0],403)

    def test_explicit_same_origin_and_actor_bound_confirmation(self):
        values=self.review()
        self.assertEqual(self.post(values,'reconcile-confirm',headers=dict(origin='https://evil.test'))[0],403)
        self.assertEqual(self.post(values,'reconcile-confirm',user='bob')[0],403)
        self.assertNotIn('reconcile',[c[0] for c in self.calls])

    def test_changed_state_invalid_evidence_and_extra_fields_do_not_confirm(self):
        code,body,_=self.get();self.assertEqual(code,200)
        values=dict(token=self.token(body),outcome='failed',evidence='checked')
        for change in (dict(evidence=''),dict(evidence='x'*2049),dict(outcome='retry'),dict(actor='bob')):
            self.assertEqual(self.post(dict(values,**change))[0],400)
        self.token_hash='f'*64
        self.assertEqual(self.post(values)[0],409)
        self.assertNotIn('reconcile',[c[0] for c in self.calls])

    def test_unknown_response_is_not_retried_and_keeps_followup_link(self):
        values=self.review();self.error=OSError('PRIVATE-REMOTE-ERROR')
        code,body,_=self.post(values,'reconcile-confirm')
        self.assertEqual(code,503);self.assertIn('outcome unconfirmed',body)
        self.assertIn('/ui/interventions/detail?',body);self.assertNotIn('PRIVATE-REMOTE-ERROR',body)
        self.assertEqual(self.post(values,'reconcile-confirm')[0],403)
        self.assertEqual([c[0] for c in self.calls].count('reconcile'),1)

    def test_revoked_mapping_after_call_never_claims_success(self):
        values=self.review();self.revoke=True
        code,body,_=self.post(values,'reconcile-confirm')
        self.assertNotEqual(code,303);self.assertNotIn('ticket-42',body)

    def test_receipt_and_audit_distinguish_attestation_from_execution(self):
        data=detail_fixture.state(self.row);data['status']='failed'
        annotation=dict(author='alice',organization='org',created_at=1791480010,
                        expected_state='e'*64,outcome='failed',evidence='<checked>')
        data['reconciliation']=annotation
        self.assertEqual(receipt(data,self.row),data)
        body=render(self.row,data)
        self.assertIn('Operator-confirmed outcome',body);self.assertIn('&lt;checked&gt;',body)
        self.assertIn('does not prove every workflow step ran',body)
        event=dict(operation='reconcile',mode='real',run_id=self.row['id'],revision=self.row['revision'],
                   scope_revisions=[self.row['revision']],**annotation)
        self.assertEqual(events_checked([event],'org'),[event])
        for change in (dict(organization='other'),dict(outcome='succeeded'),dict(expected_state='bad'),dict(evidence='')):
            bad=deepcopy(data);bad['reconciliation'].update(change)
            with self.assertRaises(ValueError):receipt(bad,self.row)
