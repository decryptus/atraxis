"""Installed CLI → authenticated source → signed engine admission → Auton → alert.

Requires three independent installed environments; all destinations are disposable
loopback services. Alert receivers implement contracts, not downstream delivery.
"""
import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import subprocess
import sys
import threading
import time

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from atraxis.accounts import default_directory, Directory
from atraxis.account_store import AccountStore
from journey_fixture import AutonDaemon, AlertReceiver
from test_web_engine import WebEngineFixture, CASES


class SourceJourneyFixture(WebEngineFixture):
    def setUp(self):
        super().setUp()
        self.auton=AutonDaemon();self.addCleanup(self.auton.close)
        self.receiver=AlertReceiver();self.addCleanup(self.receiver.close)
        self.source_calls=[];self.cpu=95;self.source_ready=True
        owner=self
        class Source(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_GET(self):
                owner.source_calls.append((self.command,self.path))
                expected='Basic '+base64.b64encode(('reader:'+owner.password).encode()).decode()
                if self.path!='/v1/status' or self.headers.get('Authorization')!=expected:
                    self.send_error(403);return
                now=time.time()
                body=dict(api_version=1,ready=owner.source_ready,running=True,last_cycle_success=owner.source_ready,
                    cycles_total=2,errors_total=0,age_seconds=0,last_success_at=now,last_cycle_finished_at=now,
                    containers=[dict(id='a'*64,name='web',status='running',health='healthy',cpu_percent=owner.cpu)])
                raw=json.dumps(body).encode();self.send_response(200)
                self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(raw)))
                self.end_headers();self.wfile.write(raw)
        source=ThreadingHTTPServer(('127.0.0.1',0),Source)
        thread=threading.Thread(target=source.serve_forever,daemon=True);thread.start()
        self.addCleanup(lambda:(source.shutdown(),source.server_close(),thread.join()))
        directory=default_directory('alice').document()
        directory['organizations'][0]['id']='demo';directory['memberships'][0]['organization']='demo'
        AccountStore(self.root/'accounts.db').apply(Directory(directory),None)
        self.write_yaml('access.yml',dict(version=3,store='accounts.db',administrators=[],audit=[],servers=[],
            resource_groups=[],identities=[dict(username='alice',subject='alice')],
            grants=[dict(organization='demo',subject_kind='user',subject='alice',targets=[dict(kind='source',id='monit')])]))
        self.private('source.json',json.dumps(dict(username='reader',password=self.password)))
        self.private('inventory.json',json.dumps(dict(username='alice',password=self.password)))
        self.write_yaml('inventory.yml',dict(origin=self.origin,credentials_file='inventory.json',allow_loopback_http=True))
        self.stop('web')
        self.write_yaml('web.yml',dict(general=dict(listen_addr='127.0.0.1',listen_port=self.web_port,
            auth_basic_file='users',inventory_access_file='access.yml'),
            sources=[dict(source_id='monit',kind='monit_docker',origin='http://127.0.0.1:'+str(source.server_port),
                credentials_file='source.json',allow_loopback_http=True,timeout=1,poll_interval=1,max_age=10)]))
        self.start_web()
        key=Ed25519PrivateKey.generate()
        self.private('observation.pem',key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()).decode())
        self.private('observation.pub',key.public_key().public_bytes(serialization.Encoding.PEM,serialization.PublicFormat.SubjectPublicKeyInfo).decode())
        self.write_yaml('operator.yml',dict(version=2,identities=[dict(uid=os.getuid(),subject='alice',organization='demo',atropos_client='alice.yml')],
            accounts_file='accounts.db',inventory_client='inventory.yml',
            observation_signing=dict(key_id='collector',private_key_file='observation.pem'),
            bindings=[dict(organization='demo',target=['container','web'],source_id='monit',native_id='a'*64)]))
        self.private('executor.password',self.auton.password);self.private('alert.token',self.receiver.token)
        adapters={'ops.run':dict(backend='auton',target='demo/container/web',origin=self.auton.origin,endpoint='approved',
                    username='operator',password_file='executor.password',allow_loopback_http=True,simulation='noop'),
                  'ops.notify':dict(backend='alertmanager',target='demo/container/web',origin=self.receiver.origin,
                    token_file='alert.token',allow_loopback_http=True,labels={'alertname':'InterventionComplete'},simulation='test_receiver')}
        self.engine_config['resources'].append(dict(organization='demo',kind='container',id='web',server_id='lab'))
        self.engine_config['grants'][0].update(scopes=[['server','lab'],['container','web']],actions=list(adapters),operations=self.engine_config['grants'][0]['operations']+['execute'])
        self.engine_config.update(execution_adapters=adapters,dispatch_bindings='bindings.yml',observation_keys={'collector':'observation.pub'},
            test_alert_delivery=dict(backend='webhook',url=self.receiver.origin,token_file='alert.token',allow_loopback_http=True))
        self.write_yaml('bindings.yml',[]);self.write_engine();self.stop('engine');self.start_engine()
        client=self.clients['alice']
        rule=client.create_from_templates(['cpu-high'],name='observed',rule_kind='intervention',selections=[['container','web']])
        report=client.test('observed',rule['revision'],CASES)
        self.active=client.activate('observed',rule['revision'],test_id=report['id'],expected_generation=None)
        code="from pathlib import Path; p=Path(%r); p.write_text('done')" % str(self.auton.root/'effect.txt')
        revisions=self.engine('execution-adapters','--config',str(self.root/'engine.yml'))
        binding=dict(organization='demo',rule='observed',revision=rule['revision'],result=CASES[0]['expected'],input={},max_age_seconds=300,
            adapter_revisions={name:value['revision'] for name,value in revisions.items()},
            observation=dict(key_id='collector',source_id='monit',native_id='a'*64,filesystem=None,max_age_seconds=60),
            workflow=dict(version=1,name='source-journey',start='run',steps={
                'run':{'action':'ops.run','timeout':5,'with':{'args':['-c',code]},'next':'notify'},
                'notify':{'action':'ops.notify','timeout':5,'with':{'subject':'Intervention completed'}}}))
        self.write_yaml('bindings.yml',[binding])
        self.binding=self.engine('rules-bindings','--config',str(self.root/'engine.yml'))[0]['id']
        self.ready('web',lambda:self.request('/readyz')[0]==200)

    def command(self,python,module,args,expected=0):
        result=subprocess.run([python,'-m',module,*args],cwd=self.root,env=self.environment,capture_output=True,text=True,timeout=15)
        for secret in (*self.tokens.values(),self.password,self.auton.password,self.receiver.token):
            self.assertNotIn(secret,result.stdout+result.stderr)
        self.assertEqual(result.returncode,expected,result.stdout+result.stderr)
        return json.loads(result.stdout) if expected==0 and result.stdout.strip() else None

    def engine(self,*args):return self.command(self.engine_python,'atropos.cli',args)

    def operator(self,*args,expected=0):
        return self.command(sys.executable,'atraxis.cli',['rules','--operator-config',str(self.root/'operator.yml'),*args],expected)

    def dispatch(self,event='observed'):
        return self.operator('observe-dispatch','observed','--target','container:web','--binding',self.binding,
            '--expected-generation',self.active['generation'],'--event-id',event,'--confirm')

    def worker(self,test=False):
        return self.engine('rules-worker','--config',str(self.root/'engine.yml'),'--once',*(['--test'] if test else []))


class SourceJourneyTests(SourceJourneyFixture):
    def test_source_preview_confirmation_execution_and_alert(self):
        preview=self.operator('observe-dispatch-plan','observed','--target','container:web','--binding',self.binding)
        self.assertEqual(preview['facts'],{'cpu_percent':95})
        self.assertFalse((self.auton.root/'effect.txt').exists());self.assertEqual(self.receiver.requests,[])
        receipt=self.dispatch();self.assertTrue(receipt['observation']['verified_at_admission'])
        self.worker();self.assertEqual((self.auton.root/'effect.txt').read_text(),'done');self.assertEqual(self.receiver.requests,[])
        self.worker();state=self.clients['alice'].dispatch_status('observed','observed')
        self.assertEqual(state['status'],'succeeded',state)
        self.assertEqual(self.receiver.requests[0][0],'/api/v2/alerts')
        self.assertEqual(self.receiver.requests[0][1][0]['annotations']['atropos_run_id'],receipt['id'])
        self.assertTrue(self.source_calls);self.assertEqual(set(self.source_calls),{('GET','/v1/status')})

    def test_manual_facts_are_denied_and_key_revocation_stops_queued_work(self):
        with self.assertRaises(PermissionError):
            self.clients['alice'].dispatch('observed',['container','web'],{'cpu_percent':95},self.binding,
                expected_generation=self.active['generation'],event_id='manual')
        self.dispatch();self.engine_config.pop('observation_keys');self.write_engine();self.worker()
        self.assertEqual(self.clients['alice'].dispatch_status('observed','observed')['status'],'cancelled')
        self.assertFalse((self.auton.root/'effect.txt').exists());self.assertEqual(self.receiver.requests,[])

    def test_cli_test_chain_keeps_marker_and_never_contacts_auton(self):
        (self.root/'executor.password').unlink()
        self.private('facts.json',json.dumps(dict(cpu_percent=95)))
        receipt=self.operator('test-dispatch','observed','--target','container:web','--binding',self.binding,
            '--input',str(self.root/'facts.json'),'--event-id','simulation')
        self.worker();self.assertEqual(self.receiver.requests,[])
        self.worker(test=True);self.worker(test=True)
        state=self.operator('test-dispatch-status','observed','--event-id','simulation')
        self.assertEqual(state['status'],'succeeded',state)
        self.assertEqual(state['results']['run']['test_context'],receipt['test_context'])
        self.assertEqual(state['results']['notify']['alert']['test_context'],receipt['test_context'])
        self.assertEqual(self.receiver.requests[0][0],'/v1/test-alerts')
        self.assertFalse((self.auton.root/'effect.txt').exists())

    def test_source_unavailable_after_preview_refuses_confirmation(self):
        self.operator('observe-dispatch-plan','observed','--target','container:web','--binding',self.binding)
        self.source_ready=False
        self.ready('web',lambda:self.request('/readyz')[0]==503)
        self.operator('observe-dispatch','observed','--target','container:web','--binding',self.binding,
            '--expected-generation',self.active['generation'],'--event-id','stale','--confirm',expected=2)
        self.worker();self.assertFalse((self.auton.root/'effect.txt').exists());self.assertEqual(self.receiver.requests,[])
        self.assertEqual([event['operation'] for event in self.clients['alice'].audit('observed')],['activate'])
