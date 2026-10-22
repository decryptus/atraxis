"""Web reader through the real independent client against a synthetic API."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import secrets
import re
from urllib.parse import urlencode
import tempfile
import threading
import unittest
import yaml

from atraxis.web_rules import handle
from atraxis.web_rule_editor import WebEditor
from atraxis.web_rule_activation import WebActivation
from atraxis.web_rule_audit import handle as audit_handle
from atraxis.web_rules import client_factory


class WebClientTests(unittest.TestCase):
    def test_server_profile_supplies_token_and_expected_actor_without_browser_secrets(self):
        revision='a'*32
        calls=[]
        active=dict(revision=revision,generation='b'*32)
        token=secrets.token_urlsafe(32)
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_POST(self):
                body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                calls.append(body)
                authorized=(self.headers.get('Authorization')=='Bearer '+token
                            and body.get('expected_actor')==dict(subject='alice',organization='one'))
                if not authorized:
                    status,result=403,{'error':'rule_access_denied'}
                elif body['method']=='history':
                    status,result=200,{'result':[dict(revision=revision,author='alice',message='Fixture revision')]}
                elif body['method']=='active':
                    status,result=200,{'result':dict(active)}
                elif body['method'] in ('read','draft'):
                    status,result=200,{'result':dict(revision=revision,document=dict(name='cpu',condition='cpu_percent > 90'),
                        context=dict(version=1,rule_kind='supervision',targets=[['server','lab']]),
                        content_sha256='1'*64,context_sha256='2'*64)}
                elif body['method']=='save_draft':
                    status,result=200,{'result':dict(revision='c'*32,document=body['arguments']['document'])}
                elif body['method'] in ('test','test_report'):
                    status,result=200,{'result':dict(id='d'*32,revision=revision,passed=True,content_sha256='1'*64,context_sha256='2'*64,
                        results=[dict(name='CPU',passed=True,actual=dict(decision='review',matched_rule='high_cpu'))])}
                elif body['method'] in ('activate','rollback'):
                    active.update(revision=body['arguments']['revision'],generation=('f'*32 if body['method']=='activate' else 'e'*32),test_id=body['arguments']['test_id'])
                    status,result=200,{'result':dict(active)}
                elif body['method']=='deactivate':
                    active.update(revision=None,generation='9'*32,test_id=None)
                    status,result=200,{'result':dict(active)}
                elif body['method']=='audit':
                    status,result=200,{'result':[]}
                else:
                    status,result=400,{'error':'invalid_rule_request'}
                raw=json.dumps(result).encode()
                self.send_response(status)
                self.send_header('Content-Type','application/json')
                self.send_header('X-Rule-Service','atropos')
                self.send_header('Content-Length',str(len(raw)))
                self.end_headers();self.wfile.write(raw)
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        self.addCleanup(lambda:(server.shutdown(),server.server_close(),thread.join()))
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            (root/'token').write_text(token);(root/'token').chmod(0o600)
            profile=root/'engine.yml'
            profile.write_text(yaml.safe_dump(dict(origin='http://127.0.0.1:%s'%server.server_port,
                allow_loopback_http=True,token_file='token',timeout=3)));profile.chmod(0o600)
            mapping=root/'web.yml'
            identities=[dict(username='reader',subject='alice',organization='one',atropos_client='engine.yml')]
            mapping.write_text(yaml.safe_dump(dict(version=1,identities=identities)));mapping.chmod(0o600)
            status,page=handle('/ui/rules?name=cpu&revision='+revision,'reader',mapping)
            self.assertEqual(status,200)
            self.assertIn('Selected revision',page)
            self.assertEqual([call['method'] for call in calls],['history','active','read'])
            self.assertTrue(all(call['expected_actor']==dict(subject='alice',organization='one') for call in calls))
            self.assertNotIn(token,page)
            self.assertNotIn(str(profile),page)
            editor=WebEditor(mapping,'https://console.example.test',client_factory)
            for operation in ('edit','test'):
                path='/ui/rules/'+operation+'?name=cpu'+('&revision='+revision if operation=='test' else '')
                code,form,_=editor.handle(operation,path,'reader')
                self.assertEqual(code,200)
                nonce=re.search(r'name="token" value="([^"]+)"',form).group(1)
                fields=dict(token=nonce,document='name: cpu\ncondition: cpu_percent > 95',message='Browser edit') if operation=='edit' else dict(token=nonce,cases='[{"name":"CPU","input":{"cpu_percent":95},"expected":{"decision":"review","matched_rule":"high_cpu"}}]')
                code,body,headers=editor.handle(operation,'/ui/rules/'+operation,'reader',method='POST',
                    headers={'origin':'https://console.example.test'},raw=urlencode(fields).encode())
                self.assertEqual(code,303,body)
                self.assertNotIn(token,body)
            mutations=[call for call in calls if call['method'] in ('save_draft','test')]
            self.assertEqual(len(mutations),2)
            self.assertEqual(mutations[0]['arguments']['expected_revision'],revision)
            self.assertEqual(mutations[0]['arguments']['selections'],[['server','lab']])
            self.assertEqual(mutations[1]['arguments']['revision'],revision)
            self.assertTrue(all(call['expected_actor']==dict(subject='alice',organization='one') for call in mutations))
            active['revision']=None
            activation=WebActivation(mapping,'https://console.example.test',client_factory)
            code,form,_=activation.handle('activate','/ui/rules/activate?'+urlencode(dict(name='cpu',revision=revision,report='d'*32)),'reader')
            self.assertEqual(code,200,form)
            nonce=re.search(r'name="token" value="([^"]+)"',form).group(1)
            code,body,headers=activation.handle('activate','/ui/rules/activate','reader',method='POST',
                headers={'origin':'https://console.example.test'},raw=urlencode(dict(token=nonce,confirm='activate')).encode())
            self.assertEqual(code,303,body)
            self.assertEqual(calls[-1]['method'],'activate')
            self.assertEqual(calls[-1]['arguments'],dict(name='cpu',revision=revision,test_id='d'*32,expected_generation='b'*32))
            self.assertEqual(calls[-1]['expected_actor'],dict(subject='alice',organization='one'))
            self.assertNotIn(token,form)
            active['revision']='c'*32
            for operation in ('rollback','deactivate'):
                values=dict(name='cpu')
                if operation=='rollback':values.update(revision=revision,report='d'*32)
                code,form,_=activation.handle(operation,'/ui/rules/'+operation+'?'+urlencode(values),'reader')
                self.assertEqual(code,200,form)
                nonce=re.search(r'name="token" value="([^"]+)"',form).group(1)
                generation=active['generation']
                code,body,_=activation.handle(operation,'/ui/rules/'+operation,'reader',method='POST',
                    headers={'origin':'https://console.example.test'},raw=urlencode(dict(token=nonce,confirm=operation)).encode())
                self.assertEqual(code,303,body)
                expected=dict(name='cpu',expected_generation=generation)
                if operation=='rollback':expected.update(revision=revision,test_id='d'*32)
                self.assertEqual(calls[-1]['method'],operation)
                self.assertEqual(calls[-1]['arguments'],expected)
                self.assertEqual(calls[-1]['expected_actor'],dict(subject='alice',organization='one'))
                self.assertNotIn(token,form)
            code,body=audit_handle('/ui/rules/audit?name=cpu&offset=20','reader',mapping)
            self.assertEqual(code,200,body)
            self.assertEqual(calls[-1]['method'],'audit')
            self.assertEqual(calls[-1]['arguments'],dict(name='cpu',offset=20,limit=20))
            self.assertNotIn(token,body)
            before=len(calls)
            identities[0]['organization']='two'
            mapping.write_text(yaml.safe_dump(dict(version=1,identities=identities)))
            status,page=handle('/ui/rules?name=cpu','reader',mapping)
            self.assertEqual(status,403)
            self.assertNotIn('Fixture revision',page)
            self.assertEqual(len(calls),before+1)
