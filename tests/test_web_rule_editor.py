"""Authenticated browser writes with a synthetic independent client boundary."""
import base64
from copy import deepcopy
from dataclasses import replace
import hashlib
import http.client
import json
from pathlib import Path
import re
import tempfile
import threading
import unittest
from urllib.parse import urlencode

import yaml

from atraxis.config import load_config, ConfigurationError
from atraxis.server import build_context, _Handler
from atraxis.web_rule_editor import (WebEditor, FormTokens, EditorClient, validate_origin,
                                     MAX_USER_TOKENS, FORM_BYTES)
from test_web_rules import FakeClient, REVISION

from web_editor_fixture import EditorFake, NEW_REVISION, REPORT, ORIGIN, CASES


class EditorTests(unittest.TestCase):
    fake_class = EditorFake
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.mapping = self.root/'mapping.yml'
        self.identities = [dict(username=user, subject=user, organization='demo', atropos_client=user+'.yml') for user in ('alice','bob')]
        self.write_mapping(self.identities)
        digest = base64.b64encode(hashlib.sha1(b'fixture').digest()).decode()
        (self.root/'users').write_text(''.join(user+':{SHA}'+digest+'\n' for user in ('alice','bob')))
        self.config_path = self.root/'config.yml'
        self.config_path.write_text('general:\n  auth_basic_file: users\n  rules_web_file: mapping.yml\n  rules_web_origin: '+ORIGIN+'\n')
        self.calls, self.destinations, self.closed = [], [], 0
        self.head, self.error, self.revoke, self.document_message = REVISION, None, False, 'Synthetic CPU rule'
        self.factory = lambda path, expected_actor: self.fake_class(self, path, expected_actor)
        self.editor = WebEditor(self.mapping, ORIGIN, self.factory)
        config = replace(load_config(self.config_path), listen_port=0)
        self.context = build_context(config, rule_client_factory=self.factory)
        started = threading.Event()
        self.context.named_commands['GET /healthz'].at_start = lambda _:started.set()
        self.thread = threading.Thread(target=lambda:self.context.run(http_req_handler=_Handler), daemon=True)
        self.thread.start(); self.addCleanup(self.stop)
        self.assertTrue(started.wait(5))
        self.port = self.context.server.server_address[1]

    def stop(self):
        self.context.stop(); self.thread.join(7)
        self.assertFalse(self.thread.is_alive())

    def write_mapping(self, identities):
        self.mapping.write_text(yaml.safe_dump(dict(version=1, identities=identities)))
        self.mapping.chmod(0o600)

    def request(self, path, *, user='alice', method='GET', body=None, extra=None):
        headers = {'Authorization':'Basic '+base64.b64encode((user+':fixture').encode()).decode()}
        if body is not None:
            headers.update({'Content-Type':'application/x-www-form-urlencoded','Origin':ORIGIN})
            if isinstance(body, dict):body = urlencode(body).encode()
        headers.update(extra or {})
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=5)
        try:
            conn.request(method, path, body=body, headers=headers)
            response=conn.getresponse()
            return response.status, dict(response.getheaders()), response.read().decode()
        finally:conn.close()

    def token(self, action='edit', user='alice'):
        path='/ui/rules/'+action+'?name=cpu'+('&revision='+REVISION if action=='test' else '')
        status,_,body=self.request(path,user=user)
        self.assertEqual(status,200,body)
        return re.search(r'name="token" value="([^"]+)"',body).group(1)

    def document(self):
        return yaml.safe_dump(EditorFake(self,None,dict(subject='alice')).read('cpu',REVISION)['document'])

    def save(self, token, **kwargs):
        return self.request('/ui/rules/edit',method='POST',body=dict(token=token,document=self.document(),message='Reviewed in browser'),**kwargs)

    def test_save_preserves_scope_and_expected_revision_redirects_without_activation(self):
        token=self.token()
        status,headers,_=self.save(token)
        self.assertEqual(status,303)
        self.assertIn(NEW_REVISION,headers['Location'])
        call=next(c for c in self.calls if c[0]=='save_draft')
        self.assertEqual(call[1],dict(subject='alice',organization='demo'))
        self.assertEqual(call[3],dict(rule_kind='supervision',selections=[['server','lab']],expected_revision=REVISION,message='Reviewed in browser'))
        self.assertEqual(self.save(token)[0],403)
        self.assertEqual(sum(c[0]=='save_draft' for c in self.calls),1)
        self.assertTrue(all(c[0] in ('read','save_draft') for c in self.calls))

    def test_scenarios_pin_revision_and_report_get_refresh_is_read_only(self):
        token=self.token('test')
        status,headers,_=self.request('/ui/rules/test',method='POST',body=dict(token=token,cases=json.dumps(CASES)))
        self.assertEqual(status,303)
        for _ in range(2):
            code,_,body=self.request(headers['Location'])
            self.assertEqual(code,200)
            self.assertIn('All scenarios passed',body)
            self.assertIn(REPORT,body)
        self.assertEqual(sum(c[0]=='test' for c in self.calls),1)
        self.assertEqual(next(c for c in self.calls if c[0]=='test')[3],REVISION)
        self.assertEqual(self.request('/ui/rules/test',method='POST',body=dict(token=token,cases=json.dumps(CASES)))[0],403)

    def test_cross_origin_cross_account_and_tampered_fields_never_write(self):
        for kwargs in [dict(extra={'Origin':'null'}),dict(extra={'Origin':'https://evil.example'}),dict(extra={'Sec-Fetch-Site':'cross-site'}),dict(user='bob')]:
            self.assertEqual(self.save(self.token(),**kwargs)[0],403)
        token=self.token()
        body=urlencode(dict(token=token,document=self.document(),message=''))+'&subject=admin'
        self.assertEqual(self.request('/ui/rules/edit',method='POST',body=body.encode())[0],400)
        duplicate=urlencode(dict(token=token,document=self.document(),message=''))+'&message=other'
        self.assertEqual(self.request('/ui/rules/edit',method='POST',body=duplicate.encode())[0],400)
        self.assertFalse(any(c[0]=='save_draft' for c in self.calls))

    def test_conflict_denial_and_unknown_outcomes_are_not_retried(self):
        for error,status,notice in [(ValueError('rule_conflict'),409,'draft changed'),(PermissionError('secret-marker'),403,'Access denied'),(RuntimeError('secret-marker'),503,'Outcome unconfirmed')]:
            token=self.token();self.error=error
            code,_,body=self.save(token)
            self.assertEqual(code,status)
            self.assertIn(notice,body);self.assertNotIn('secret-marker',body)
            self.assertEqual(self.save(token)[0],403)
            self.error=None
        self.assertEqual(sum(c[0]=='save_draft' for c in self.calls),3)

    def test_mapping_change_and_revocation_block_old_forms(self):
        token=self.token()
        entries=deepcopy(self.identities);entries[0]['organization']='other'
        self.write_mapping(entries)
        self.assertEqual(self.save(token)[0],403)
        self.write_mapping(self.identities)
        token=self.token();self.write_mapping([])
        self.assertEqual(self.save(token)[0],403)
        self.assertFalse(any(c[0]=='save_draft' for c in self.calls))

    def test_invalid_yaml_alias_duplicate_name_change_and_non_json_types(self):
        for document in ['name: cpu\nname: other','name: other','name: cpu\nx: &a []\ny: *a','name: cpu\nx: 2026-10-07','name: cpu\nx: .nan']:
            token=self.token()
            code,_,_=self.request('/ui/rules/edit',method='POST',body=dict(token=token,document=document,message=''))
            self.assertEqual(code,400,document)
        self.assertFalse(any(c[0]=='save_draft' for c in self.calls))

    def test_payload_bounds_encoding_and_cases_reject_before_engine_call(self):
        self.assertEqual(self.request('/ui/rules/edit',method='POST',body=b'',extra={'Content-Length':str(FORM_BYTES+1)})[0],413)
        self.assertEqual(self.save(self.token(),extra={'Content-Type':'application/json'})[0],400)
        for cases in ['[]','[{"name":"a","name":"b"}]','[NaN]','[1e999]']:
            token=self.token('test')
            self.assertEqual(self.request('/ui/rules/test',method='POST',body=dict(token=token,cases=cases))[0],400)
        self.assertFalse(any(c[0]=='test' for c in self.calls))

    def test_expired_bounded_and_concurrently_consumed_tokens(self):
        from concurrent.futures import ThreadPoolExecutor
        now=[0]
        tokens=FormTokens(lambda:now[0]);reader=EditorClient(self.mapping,'alice',self.factory)
        first=tokens.issue(reader,'edit','cpu',REVISION)
        for _ in range(MAX_USER_TOKENS):tokens.issue(reader,'edit','cpu',REVISION)
        self.assertLessEqual(len(tokens.entries),MAX_USER_TOKENS)
        with self.assertRaises(PermissionError):tokens.consume(first,reader,'edit')
        token=tokens.issue(reader,'edit','cpu',REVISION)
        def take(_):
            try:tokens.consume(token,reader,'edit');return True
            except PermissionError:return False
        with ThreadPoolExecutor(max_workers=4) as pool:self.assertEqual(sum(pool.map(take,range(4))),1)
        token=tokens.issue(reader,'edit','cpu',REVISION);now[0]=901
        with self.assertRaises(PermissionError):tokens.consume(token,reader,'edit')

    def test_xss_head_and_method_allowlist(self):
        self.document_message='</textarea><script>alert(1)</script>'
        code,headers,body=self.request('/ui/rules/edit?name=cpu')
        self.assertEqual({key.lower():value for key,value in headers.items()}['referrer-policy'],'same-origin')
        self.assertEqual(code,200);self.assertNotIn('<script>',body);self.assertIn('&lt;script&gt;',body)
        self.assertEqual(self.request('/ui/rules/edit?name=cpu',method='HEAD')[2],'')
        reader=EditorClient(self.mapping,'alice',self.factory)
        for method in ('activate','dispatch','test_dispatch','import_draft'):
            with self.assertRaises(PermissionError):reader.call(method)

    def test_origin_configuration_is_explicit_and_restricts_cleartext_to_loopback(self):
        for origin in ['https://example.test','http://127.0.0.1:8080']:
            self.assertEqual(validate_origin(origin),origin)
        for origin in ['http://example.test','https://user@example.test','https://example.test/','https://example.test?x=1','https://example.test:99999','null']:
            with self.assertRaises(ValueError):validate_origin(origin)
        self.config_path.write_text('general:\n  auth_basic_file: users\n  rules_web_origin: '+ORIGIN+'\n')
        with self.assertRaises(ConfigurationError):load_config(self.config_path)


    def test_parallel_forms_conflict_and_routes_are_absent_when_disabled(self):
        first,second=self.token(),self.token()
        self.assertEqual(self.save(first)[0],303)
        self.assertEqual(self.save(second)[0],409)
        config=replace(load_config(self.config_path),rules_web_origin=None)
        context=build_context(config,rule_client_factory=self.factory)
        self.assertNotIn('POST /ui/rules/edit',context.named_commands)
        self.assertNotIn('GET /ui/rules/test',context.named_commands)

    def test_invalid_queries_and_mismatched_report_are_discarded(self):
        for path in ['/ui/rules/edit?name=cpu&subject=admin','/ui/rules/edit?name=cpu&revision='+REVISION,
                     '/ui/rules/test?name=cpu&revision='+REVISION+'&x=1&x=2']:
            self.assertEqual(self.request(path)[0],400)
        self.report=dict(id=REPORT,revision='e'*32,passed=True,results=[dict(name='private-marker',passed=True)])
        status,_,body=self.request('/ui/rules/test?name=cpu&revision='+REVISION+'&report='+REPORT)
        self.assertEqual(status,503)
        self.assertNotIn('private-marker',body)

    def test_revocation_during_save_hides_result_and_warns_about_uncertain_completion(self):
        token=self.token();self.revoke=True
        status,_,body=self.save(token)
        self.assertEqual(status,403)
        self.assertNotIn(NEW_REVISION,body)
        self.assertIn('may already have completed',body)
        self.assertEqual(sum(c[0]=='save_draft' for c in self.calls),1)
