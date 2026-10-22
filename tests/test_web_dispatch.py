"""Real admission boundaries, pinned context, uncertain outcomes and default opt-in."""
from copy import deepcopy
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch, Mock
from urllib.parse import urlencode

import yaml

from atraxis.config import load_config, ConfigurationError
from atraxis.rule_observations import ObservationBinding
from atraxis.web_dispatch import WebDispatch, checked_plan
from atraxis.web_observations import WebObservationSession, load_profile
from atraxis.web_rules import RuleReader

ORIGIN = 'https://console.example.test'
REVISION, GENERATION, BINDING, RUN = 'a'*32, 'b'*32, 'c'*64, 'd'*64
SOURCE = ObservationBinding('demo', ('container', 'web'), 'monit', 'e'*64)


class WebDispatchTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name); self.mapping = self.root/'mapping.yml'
        self.entries = [dict(username=user, subject=user, organization='demo', atropos_client=user+'.yml') for user in ('alice', 'bob')]
        self.write_mapping(self.entries)
        self.profile = self.root/'observations.yml'
        self.document = dict(version=1, bindings=[dict(organization='demo', target=['container', 'web'], source_id='monit', native_id='e'*64)],
                             observation_signing=dict(key_id='collector', private_key_file='key.pem'))
        self.profile.write_text(yaml.safe_dump(self.document)); self.profile.chmod(0o600)
        self.calls = []; self.error = None; self.response = {}; self.revoke = False
        workflow = dict(version=1, name='approved', start='run', steps={'run':dict(action='ops.run', timeout=5, **{'with':{'secret':'do-not-render'}})})
        self.plan = dict(binding=BINDING, decision=dict(revision=REVISION, generation=GENERATION, target=['container','web'],
                        result=dict(decision='review', matched_rule='high')),
                        target='demo/container/web', workflow=workflow,
                        workflow_revision=hashlib.sha256(json.dumps(workflow, sort_keys=True, separators=(',',':')).encode()).hexdigest(),
                        input={'secret':'do-not-render'}, max_age_seconds=60, effects='none', facts={'cpu_percent':95},
                        observation=dict(source_id='monit', native_id='e'*64, filesystem=None, expires_at=1900000000))
        owner = self
        class Session:
            def __init__(self, reader, *args):
                self.reader = reader; self.revision = load_profile(owner.profile)[1]
                self.dispatcher = self; self.submitted = False
            def guard(self): self.reader.entry()
            def binding(self, target): return SOURCE
            def plan(self, *args):
                owner.calls.append(('plan', args)); self.guard()
                return deepcopy(owner.plan)
            def dispatch(self, *args, **kwargs):
                self.guard(); self.submitted = True
                owner.calls.append(('dispatch', args, kwargs))
                if owner.error: raise owner.error
                if owner.revoke: owner.write_mapping([])
                self.guard()
                result = dict(id=RUN, rule='cpu', revision=REVISION, generation=GENERATION, binding=BINDING,
                              event_id=kwargs['event_id'], target='demo/container/web', status='queued', history=[], error=None,
                              observation=dict(verified_at_admission=True, expires_at=1900000000))
                result.update(owner.response); return result
        patcher = patch('atraxis.web_dispatch.WebObservationSession', Session)
        patcher.start(); self.addCleanup(patcher.stop)
        self.controller = WebDispatch(self.mapping, ORIGIN, Mock(), self.profile, Mock(), Mock())

    def write_mapping(self, entries):
        self.mapping.write_text(yaml.safe_dump(dict(version=1, identities=entries))); self.mapping.chmod(0o600)

    def token(self, body): return re.search(r'name="token" value="([^"]+)"', body).group(1)

    def get(self, **kwargs):
        return self.controller.handle('dispatch', '/ui/rules/dispatch', 'alice', **kwargs)

    def form(self):
        code, body, _ = self.get(); self.assertEqual(code,200,body)
        return dict(token=self.token(body), name='cpu', kind='container', target='web', binding=BINDING)

    def post(self, fields, route='dispatch', user='alice', headers=None, organization='demo'):
        return self.controller.handle(route, '/ui/rules/'+route+'?organization='+organization, user,
            method='POST', headers=headers or {'origin':ORIGIN}, raw=urlencode(fields).encode())

    def review(self):
        code, body, _ = self.post(self.form()); self.assertEqual(code,200,body)
        self.assertNotIn('do-not-render',body)
        self.assertIn('has not queued work',body)
        return dict(token=self.token(body), confirm='dispatch-confirm'), re.search(r'Event: <code>([a-f0-9]{32})</code>', body).group(1)

    def test_review_and_confirmation_pin_generation_binding_target_and_event(self):
        fields,event=self.review(); self.assertEqual([c[0] for c in self.calls],['plan'])
        code,_,headers=self.post(fields,'dispatch-confirm'); self.assertEqual(code,303)
        self.assertIn('mode=real',headers['Location']); self.assertIn('organization=demo',headers['Location'])
        self.assertEqual(self.calls[-1],('dispatch',('cpu',['container','web'],BINDING),dict(expected_generation=GENERATION,event_id=event)))
        self.assertEqual(self.post(fields,'dispatch-confirm')[0],403)
        self.assertEqual(len(self.calls),2)

    def test_origin_account_organization_operation_and_manual_input_are_rejected(self):
        for headers in ({'origin':'null'},{'origin':ORIGIN,'sec-fetch-site':'cross-site'}):
            self.assertEqual(self.post(self.form(),headers=headers)[0],403)
        self.assertEqual(self.post(self.form(),user='bob')[0],403)
        for extra in ({'facts':'{}'},{'generation':GENERATION},{'actor':'alice'}):
            self.assertEqual(self.post(dict(self.form(),**extra))[0],400)
        fields,_=self.review()
        self.assertEqual(self.post(dict(fields,binding=BINDING),'dispatch-confirm')[0],400)
        self.assertEqual(self.post(fields,'dispatch-confirm',user='bob')[0],403)
        fields,_=self.review()
        self.write_mapping(self.entries+[dict(username='alice',subject='alice',organization='other',atropos_client='other.yml')])
        self.assertEqual(self.post(fields,'dispatch-confirm',organization='other')[0],403)
        self.assertFalse(any(c[0]=='dispatch' for c in self.calls))

    def test_profile_changes_and_revoked_mapping_invalidate_review(self):
        fields,_=self.review()
        self.profile.write_text(yaml.safe_dump(dict(self.document,bindings=[])))
        self.assertEqual(self.post(fields,'dispatch-confirm')[0],409)
        self.profile.write_text(yaml.safe_dump(self.document))
        fields,_=self.review(); self.write_mapping([])
        self.assertEqual(self.post(fields,'dispatch-confirm')[0],403)
        self.assertFalse(any(c[0]=='dispatch' for c in self.calls))

    def test_uncertain_or_mismatched_receipt_never_retries_and_keeps_event(self):
        for error,response in [(TimeoutError('secret'),{}),(PermissionError('secret'),{}),
                               (None,{'binding':'0'*64}),(None,{'generation':'0'*32}),
                               (None,{'observation':None}),(None,{'event_id':'wrong'}),(None,{'target':'other'})]:
            fields,event=self.review();self.error,self.response=error,response
            code,body,_=self.post(fields,'dispatch-confirm')
            self.assertIn(code,(403,503));self.assertIn(event,body);self.assertIn('Outcome unconfirmed',body)
            self.assertIn('mode=real',body);self.assertNotIn('secret',body)
            count=len(self.calls)
            self.assertEqual(self.post(fields,'dispatch-confirm')[0],403);self.assertEqual(len(self.calls),count)
            self.error,self.response=None,{}

    def test_safe_methods_make_no_engine_call_and_malformed_plans_are_hidden(self):
        self.assertEqual(self.get(method='HEAD')[0],200);self.assertEqual(self.calls,[])
        for changes in ({'target':'other/container/web'}, {'binding':'0'*64}, {'facts':{'cpu_percent':float('nan')}},
                        {'workflow_revision':'0'*64}, {'decision':{}}, {'observation':{}}):
            form=self.form(); original=self.plan;self.plan=dict(original,**changes)
            code,body,_=self.post(form);self.assertEqual(code,503);self.assertNotIn('name="confirm"',body)
            self.plan=original
        self.assertEqual(self.controller.handle('dispatch-confirm','/ui/rules/dispatch-confirm','alice')[0],400)

    def test_expiration_and_revocation_during_dispatch_hide_results(self):
        self.controller.tokens.clock=lambda:0
        fields,_=self.review();self.controller.tokens.clock=lambda:901
        self.assertEqual(self.post(fields,'dispatch-confirm')[0],403)
        fields,event=self.review();self.revoke=True
        code,body,_=self.post(fields,'dispatch-confirm');self.assertEqual(code,403)
        self.assertIn(event,body);self.assertIn('Outcome unconfirmed',body);self.assertNotIn(RUN,body)

    def test_observation_session_rechecks_inventory_identity_and_authority(self):
        reader=RuleReader(self.mapping,'alice',Mock())
        access=Mock(); scope={'subject':'alice','organization':'demo','revision':'v1'}
        access.resolve.return_value=scope
        session=WebObservationSession(reader,self.profile,access,Mock());session.scope=deepcopy(scope)
        session.guard()
        access.resolve.return_value=dict(scope,revision='v2')
        with self.assertRaises(PermissionError):session.guard()
        session.scope=None
        access.snapshot.return_value=(dict(scope,subject='bob'),{})
        with self.assertRaises(PermissionError):session.inventory()
        self.assertFalse(session.submitted)

    def test_profile_schema_rejects_ambient_identity_invalid_mapping_and_public_files(self):
        for changes in ({'uid':0},{'bindings':[dict(self.document['bindings'][0],native_id='wrong')]},
                        {'observation_signing':{'key_id':'collector'}},{'version':True}):
            self.profile.write_text(yaml.safe_dump(dict(self.document,**changes)))
            with self.assertRaises(ConfigurationError):load_profile(self.profile)
        self.profile.write_text(yaml.safe_dump(self.document));self.profile.chmod(0o644)
        with self.assertRaises(ConfigurationError):load_profile(self.profile)

    def test_routes_default_absent_and_configuration_requires_origin_inventory(self):
        from atraxis.server import build_context
        import base64
        (self.root/'users').write_text('alice:{SHA}'+base64.b64encode(hashlib.sha1(b'fixture').digest()).decode()+'\n')
        config=self.root/'server.yml'
        base=dict(auth_basic_file='users',rules_web_file='mapping.yml',rules_web_origin=ORIGIN)
        config.write_text(yaml.safe_dump(dict(general=base)))
        context=build_context(load_config(config),rule_client_factory=Mock())
        self.assertNotIn('POST /ui/rules/dispatch',context.named_commands)
        for general in (dict(base,rules_web_observations='observations.yml'),
                        dict(auth_basic_file='users',rules_web_observations='observations.yml')):
            config.write_text(yaml.safe_dump(dict(general=general)))
            with self.assertRaises(ConfigurationError):load_config(config)
