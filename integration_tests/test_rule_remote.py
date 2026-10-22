"""Real HTTPdis + Redis + ZEN, synthetic identities only; no silent skips."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout, redirect_stderr
import copy
import hashlib
from http.server import ThreadingHTTPServer
import http.client
import io
import json
import os
from pathlib import Path
import secrets
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import unittest

import yaml

from atropos.authorization import OPERATIONS
from atraxis.cli import main
from atraxis.rule_remote import RemoteRuleOperator, RemoteRuleError
from atraxis.rule_server import RuleHandler, build_context, load_service_config
from atraxis.rule_tui import RuleWorkbench
from test_rules_atropos import fixture_module


class RemoteRuleIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = fixture_module.RedisFixture()

    @classmethod
    def tearDownClass(cls):
        cls.fixture.close()

    def setUp(self):
        self.fixture.client.flushdb()
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = self.root / 'server.yml'
        self.engine_config = self.root / 'engine.yml'
        self.gateway_doc = None
        self.tokens = {user: secrets.token_urlsafe(32) for user in ('alice', 'bob', 'outsider')}
        self.engine_tokens = {user: secrets.token_urlsafe(32) for user in self.tokens}
        self.doc = {'version': 1, 'namespace': 'remote',
            'storage': {'backend': 'redis', 'url': 'redis://127.0.0.1:%s/0' % self.fixture.client.connection_pool.connection_kwargs['port']},
            'identities': [{'token_sha256': hashlib.sha256(token.encode()).hexdigest(),
                            'subject': user, 'organization': 'other' if user == 'outsider' else 'org'}
                           for user, token in self.engine_tokens.items()],
            'resources': [{'organization': org, 'kind': 'server', 'id': 'host'} for org in ('org', 'other')] +
                [{'organization': org, 'kind': 'container', 'id': name, 'server_id': 'host', 'groups': [name]}
                 for org in ('org', 'other') for name in ('web', 'db')],
            'grants': [{'subject': user, 'organization': 'other' if user == 'outsider' else 'org',
                        'rule_kind': 'intervention', 'operations': sorted(OPERATIONS),
                        'scopes': [['container_group', 'db' if user == 'bob' else 'web']],
                        'providers': ['internal', 'upload', 'download']} for user in self.tokens]}
        self.write_config()
        from atropos.rule_server import build_context as engine_context, RuleHandler as EngineHandler
        engine = ThreadingHTTPServer(('127.0.0.1', 0), engine_context(self.engine_config).bind_handler(EngineHandler))
        self.engine = engine
        engine_thread = threading.Thread(target=engine.serve_forever, daemon=True); engine_thread.start()
        self.addCleanup(lambda: (engine.shutdown(), engine.server_close(), engine_thread.join()))
        self.gateway_doc = {'version':2,'identities':[]}
        for user, token in self.tokens.items():
            secret = self.root/(user+'-engine.token'); secret.write_text(self.engine_tokens[user]); secret.chmod(0o600)
            profile = self.root/(user+'-engine.yml')
            profile.write_text(yaml.safe_dump({'server':{'url':'http://127.0.0.1:%d'%engine.server_port},
                        'allow_loopback_http':True,'token_file':secret.name,'timeout':5}));profile.chmod(0o600)
            self.gateway_doc['identities'].append({'token_sha256':hashlib.sha256(token.encode()).hexdigest(),
                    'subject':user,'organization':'other' if user=='outsider' else 'org','atropos_client':profile.name})
        self.write_config()
        context = build_context(self.config)
        self.assertEqual(set(context.named_commands), {'POST /v1/rules/command'})
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), context.bind_handler(RuleHandler))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(lambda: (self.server.shutdown(), self.server.server_close(), self.thread.join()))
        self.profiles = {}
        for user, token in self.tokens.items():
            tokenfile = self.root / (user + '.token'); tokenfile.write_text(token + '\n'); tokenfile.chmod(0o600)
            profile = self.root / (user + '.yml')
            profile.write_text(yaml.safe_dump({'origin': 'http://127.0.0.1:%d' % self.server.server_port,
                      'allow_loopback_http': True, 'token_file': tokenfile.name, 'timeout': 5}))
            profile.chmod(0o600); self.profiles[user] = profile
        self.client = RemoteRuleOperator(self.profiles['alice'])
        self.cases = [{'name': 'pressure', 'input': {'cpu_percent': 95},
                       'expected': {'decision': 'review', 'matched_rule': 'combined'}}]

    def write_config(self):
        engine_doc = {k:v for k,v in self.doc.items() if k not in ('bindings','inventory_client')}
        for path, document in [(self.engine_config, engine_doc)]:
            temporary = path.with_suffix('.new')
            temporary.write_text(yaml.safe_dump(document));temporary.chmod(0o600);temporary.replace(path)
        if self.gateway_doc is not None:
            gateway_doc = dict(self.gateway_doc)
            for key in ('bindings','inventory_client'):
                if key in self.doc: gateway_doc[key] = self.doc[key]
            temporary = self.config.with_suffix('.new')
            temporary.write_text(yaml.safe_dump(gateway_doc));temporary.chmod(0o600);temporary.replace(self.config)

    def create(self, client=None, name='cpu', target='web'):
        return (client or self.client).create_from_templates(['cpu-high'], name=name,
                           rule_kind='intervention', selections=[['container_group', target]])

    def activate(self):
        first = self.create()
        report = self.client.test('cpu', first['revision'], self.cases)
        active = self.client.activate('cpu', first['revision'], test_id=report['id'], expected_generation=None)
        return first, report, active

    def raw(self, body, *, token=None, headers=None, method='POST', path='/v1/rules/command'):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        all_headers = {'Authorization': 'Bearer ' + (token or self.tokens['alice']), 'Content-Type': 'application/json'}
        all_headers.update(headers or {})
        connection.request(method, path, body=body, headers=all_headers)
        response = connection.getresponse(); data = response.read(); status = response.status
        connection.close()
        return status, data

    def test_remote_lifecycle_draft_evidence_active_evaluation_and_rollback(self):
        first, report, active = self.activate()
        self.assertTrue(self.client.test_report('cpu', first['revision'], report['id'])['passed'])
        self.assertEqual(self.client.draft('cpu')['revision'], first['revision'])
        self.assertEqual(self.client.evaluate_active('cpu', ['container', 'web'], {'cpu_percent': 95})['result']['decision'], 'review')
        second = self.client.import_draft(self.client.export_yaml('cpu', first['revision']), rule_kind='intervention',
                   selections=[['container', 'web']], expected_revision=first['revision'])
        self.assertEqual(self.client.active('cpu')['revision'], first['revision'])
        report2 = self.client.test('cpu', second['revision'], self.cases)
        active2 = self.client.activate('cpu', second['revision'], test_id=report2['id'], expected_generation=active['generation'])
        restored = self.client.rollback('cpu', first['revision'], test_id=report['id'], expected_generation=active2['generation'])
        self.client.deactivate('cpu', expected_generation=restored['generation'])
        self.assertIsNone(self.client.active('cpu')['revision'])
        self.assertEqual(len(self.client.history('cpu')), 2)
        self.assertEqual([item['operation'] for item in self.client.audit('cpu')], ['activate', 'activate', 'rollback', 'deactivate'])
        self.assertFalse(self.fixture.client.exists('remote:queue'))

    def test_user_scope_and_organization_isolation(self):
        first = self.create()
        bob = RemoteRuleOperator(self.profiles['bob'])
        with self.assertRaises(PermissionError): bob.read('cpu', first['revision'])
        with self.assertRaises(PermissionError): self.create(bob)
        with self.assertRaises(PermissionError): self.create(name='db', target='db')
        outsider = RemoteRuleOperator(self.profiles['outsider'])
        with self.assertRaisesRegex(RemoteRuleError, 'rule_not_found'): outsider.read('cpu', first['revision'])
        other = self.create(outsider)
        self.assertEqual(other['author'], 'outsider')
        self.assertEqual(self.client.draft('cpu')['revision'], first['revision'])

    def test_revocation_reload_and_server_owned_actor(self):
        first = self.create()
        self.doc['grants'] = [g for g in self.doc['grants'] if g['subject'] != 'alice']; self.write_config()
        with self.assertRaises(PermissionError): self.client.read('cpu', first['revision'])
        self.doc['identities'] = [i for i in self.doc['identities'] if i['subject'] != 'alice']; self.write_config()
        with self.assertRaises(PermissionError): self.client.catalog()

    def test_injected_actor_and_unknown_operations_never_mutate(self):
        for value in ({'method': 'catalog', 'arguments': {'actor': 'bob'}},
                      {'method': '_session', 'arguments': {}},
                      {'method': 'catalog', 'arguments': {}, 'organization': 'other'},
                      {'method': 'save_draft', 'arguments': {'document': {}, 'policy': {}}}):
            status, _ = self.raw(json.dumps(value))
            self.assertEqual(status, 400)
        self.assertEqual(self.fixture.client.dbsize(), 0)

    def test_authentication_browser_headers_and_method_restrictions(self):
        body = '{"method":"catalog","arguments":{}}'
        self.assertEqual(self.raw(body, token='x'*43)[0], 401)
        for headers in ({'Origin': 'https://foreign.invalid'}, {'Cookie': 'session=unknown'},
                        {'Sec-Fetch-Site': 'same-origin'}):
            self.assertEqual(self.raw(body, headers=headers)[0], 401)
        self.assertEqual(self.raw(body, method='OPTIONS')[0], 405)
        self.assertNotEqual(self.raw('', method='GET')[0], 200)
        self.assertEqual(self.raw(body, path='/v1/rules/command?actor=bob')[0], 400)
        self.assertEqual(self.raw(body, headers={'Content-Type':'text/plain'})[0], 400)

    def test_duplicate_json_nonfinite_and_framing_are_rejected(self):
        for body in ('{"method":"catalog","method":"active","arguments":{}}',
                     '{"method":"evaluate_active","arguments":{"name":"cpu","target":[],"facts":{"x":NaN}}}',
                     '[' * 1200):
            self.assertEqual(self.raw(body)[0], 400)
        body = b'{"method":"catalog","arguments":{}}'
        for extra in (b'Content-Length: 1\r\n', b'Transfer-Encoding: identity\r\n',
                      b'Authorization: Bearer '+b'x'*43+b'\r\n'):
            with socket.create_connection(('127.0.0.1', self.server.server_port), timeout=5) as stream:
                stream.sendall(b'POST /v1/rules/command HTTP/1.0\r\nContent-Type: application/json\r\n'
                    + b'Authorization: Bearer '+self.tokens['alice'].encode()+b'\r\n'
                    + b'Content-Length: '+str(len(body)).encode()+b'\r\n'+extra+b'\r\n'+body)
                status = stream.recv(4096).split(b'\r\n',1)[0]
            self.assertTrue(b'400' in status or b'401' in status, status)
        self.assertEqual(self.raw('x', headers={'Content-Length':'524289'})[0], 413)

    def test_concurrent_activation_has_one_winner_and_one_audit_entry(self):
        first = self.create(); report = self.client.test('cpu', first['revision'], self.cases)
        def activate():
            try:
                self.client.activate('cpu', first['revision'], test_id=report['id'], expected_generation=None)
                return 'ok'
            except RemoteRuleError as error: return str(error)
        with ThreadPoolExecutor(max_workers=2) as pool:
            result = list(pool.map(lambda _: activate(), range(2)))
        self.assertEqual(sorted(result), ['ok', 'rule_conflict'])
        self.assertEqual(len(self.client.audit('cpu')), 1)

    def test_concurrent_users_keep_request_identity(self):
        def create(user):
            client = RemoteRuleOperator(self.profiles[user])
            return self.create(client, name=user, target='db' if user == 'bob' else 'web')['author']
        with ThreadPoolExecutor(max_workers=3) as pool:
            self.assertEqual(list(pool.map(create, self.tokens)), list(self.tokens))

    def test_cli_and_tui_use_remote_without_local_administrator_profile(self):
        first = self.create()
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(['rules','--remote-config',str(self.profiles['alice']),'draft','cpu'])
        self.assertEqual((code, stderr.getvalue()), (0, ''))
        self.assertEqual(json.loads(stdout.getvalue())['revision'], first['revision'])
        casesfile = self.root/'cases.json'; casesfile.write_text(json.dumps(self.cases))
        model = RuleWorkbench(self.client, 'cpu', str(casesfile))
        model.refresh(); model.test_selected(); model.prepare('activate')
        self.assertIsNone(self.client.active('cpu')['revision'])
        model.confirm()
        self.assertEqual(self.client.active('cpu')['revision'], first['revision'])

    def test_config_corruption_and_destination_change_fail_closed(self):
        self.doc['namespace'] = 'changed'; self.write_config()
        with self.assertRaises(RemoteRuleError): self.client.catalog()
        self.assertEqual(self.fixture.client.dbsize(), 0)
        self.config.write_text('secret_password: DO_NOT_REPORT\n')
        with self.assertRaises(RemoteRuleError) as caught: self.client.catalog()
        self.assertNotIn('DO_NOT_REPORT', str(caught.exception))

    def test_actual_daemon_subprocess_and_shutdown(self):
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',0)); port = sock.getsockname()[1]
        process = subprocess.Popen([sys.executable, '-m', 'atraxis', 'rules-serve', '--config',
                     str(self.config), '--port', str(port)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            for _ in range(100):
                try:
                    with socket.create_connection(('127.0.0.1', port), timeout=.05): break
                except OSError: time.sleep(.02)
            else: self.fail('rule server did not start')
            profile = yaml.safe_load(self.profiles['alice'].read_text())
            profile['origin'] = 'http://127.0.0.1:%d' % port
            self.profiles['alice'].write_text(yaml.safe_dump(profile))
            self.assertEqual(len(RemoteRuleOperator(self.profiles['alice']).catalog()['templates']),8)
            process.terminate(); out, err = process.communicate(timeout=10)
            self.assertEqual(process.returncode, 0, err.decode())
            self.assertNotIn(self.tokens['alice'].encode(), out+err)
        finally:
            if process.poll() is None: process.kill(); process.communicate()

    def test_lost_response_after_committed_activation_is_unknown_without_retry(self):
        from http.server import BaseHTTPRequestHandler
        first = self.create(); report = self.client.test('cpu', first['revision'], self.cases)
        upstream_port = self.server.server_port
        forwarded = []
        class DropResponse(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers['Content-Length']))
                upstream = http.client.HTTPConnection('127.0.0.1',upstream_port,timeout=5)
                upstream.request('POST',self.path,body=body,headers={'Content-Type':'application/json',
                                    'Authorization':self.headers['Authorization']})
                response=upstream.getresponse();forwarded.append(response.status);response.read();upstream.close()
                self.close_connection=True
                self.connection.shutdown(socket.SHUT_RDWR)
            def log_message(self,*args):pass
        proxy=ThreadingHTTPServer(('127.0.0.1',0),DropResponse)
        thread=threading.Thread(target=proxy.serve_forever,daemon=True);thread.start()
        self.addCleanup(lambda:(proxy.shutdown(),proxy.server_close(),thread.join()))
        profile=self.root/'loss.yml'
        profile.write_text(yaml.safe_dump({'origin':'http://127.0.0.1:%d'%proxy.server_port,
                       'allow_loopback_http':True,'token_file':'alice.token','timeout':5}));profile.chmod(0o600)
        lossy=RemoteRuleOperator(profile)
        with self.assertRaisesRegex(RemoteRuleError,'rule_outcome_unknown'):
            lossy.activate('cpu',first['revision'],test_id=report['id'],expected_generation=None)
        self.assertEqual(forwarded,[200])
        self.assertEqual(self.client.active('cpu')['revision'],first['revision'])
        self.assertEqual(len(self.client.audit('cpu')),1)

    def test_remote_observe_uses_server_binding_and_rejects_stale_inventory(self):
        import base64
        from types import SimpleNamespace
        from http.server import BaseHTTPRequestHandler
        from atraxis.service import ReadService
        self.activate()
        source=SimpleNamespace(source_id='monit',kind='monit_docker',max_age=60,max_resources=10)
        service=ReadService((source,));service.set_running(True)
        now=time.time()
        payload=dict(api_version=1,ready=True,running=True,last_cycle_success=True,cycles_total=1,
            errors_total=0,age_seconds=0,last_success_at=now,last_cycle_finished_at=now,
            containers=[dict(id='a'*64,name='demo',status='running',health='healthy',cpu_percent=95)])
        service.record('monit',SimpleNamespace(source_id='monit',outcome='success',payload=payload,
            received_at=now,received_monotonic=time.monotonic(),duration_seconds=0,attempted_at=now))
        class Inventory(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path != '/v1/status' or self.headers.get('Authorization') != 'Basic '+base64.b64encode(b'reader:synthetic').decode():
                    self.send_error(403);return
                content=json.dumps(service.status()).encode()
                self.send_response(200);self.send_header('Content-Type','application/json')
                self.send_header('Content-Length',str(len(content)));self.end_headers();self.wfile.write(content)
            def log_message(self,*args):pass
        inventory=ThreadingHTTPServer(('127.0.0.1',0),Inventory)
        thread=threading.Thread(target=inventory.serve_forever,daemon=True);thread.start()
        self.addCleanup(lambda:(inventory.shutdown(),inventory.server_close(),thread.join()))
        (self.root/'inventory-secret.json').write_text('{"username":"reader","password":"synthetic"}')
        (self.root/'inventory.yml').write_text(yaml.safe_dump({'origin':'http://127.0.0.1:%d'%inventory.server_port,
            'allow_loopback_http':True,'credentials_file':'inventory-secret.json'}))
        self.doc['inventory_client']='inventory.yml'
        self.doc['bindings']=[{'organization':'org','target':['container','web'],'source_id':'monit','native_id':'a'*64}]
        self.write_config()
        result=self.client.live.evaluate('cpu',['container','web'])
        self.assertEqual(result['result']['decision'],'review')
        self.assertEqual(result['observation']['native_id'],'a'*64)
        service.record('monit',SimpleNamespace(source_id='monit',outcome='timeout',attempted_at=now,received_at=now))
        with self.assertRaises(RemoteRuleError):self.client.live.evaluate('cpu',['container','web'])

    def test_delegated_credential_cannot_change_verified_actor(self):
        # Alice's gateway entry accidentally points to Bob's Atropos credential.
        self.gateway_doc['identities'][0]['atropos_client']='bob-engine.yml';self.write_config()
        with self.assertRaises(PermissionError):self.create(target='db')
        self.assertEqual(self.fixture.client.dbsize(),0)
        # Client requests cannot supply the gateway's expected-actor precondition.
        status,_=self.raw(json.dumps({'method':'catalog','arguments':{},
                         'expected_actor':{'subject':'bob','organization':'org'}}))
        self.assertEqual(status,400)

    def test_gateway_revocation_and_engine_unavailability_never_fall_back(self):
        self.gateway_doc['identities']=[i for i in self.gateway_doc['identities'] if i['subject']!='alice']
        self.write_config()
        with self.assertRaises(PermissionError):self.client.catalog()
        profile=self.root/'bob-engine.yml';config=yaml.safe_load(profile.read_text())
        config['server']['url']='http://127.0.0.1:1';profile.write_text(yaml.safe_dump(config))
        with self.assertRaisesRegex(RemoteRuleError,'rule_outcome_unknown'):
            self.create(RemoteRuleOperator(self.profiles['bob']),name='db',target='db')
        self.assertEqual(self.fixture.client.dbsize(),0)

    def test_two_real_processes_with_atraxis_engine_imports_blocked(self):
        # A fresh Atraxis process explicitly cannot import any engine/storage module.
        guard=self.root/'guard';guard.mkdir()
        (guard/'sitecustomize.py').write_text('''import sys
class BlockEngine:
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in ('atropos', 'redis', 'zen'):
            raise ImportError('engine dependency forbidden in Atraxis')
sys.meta_path.insert(0, BlockEngine())
''')
        def port():
            with socket.socket() as sock:
                sock.bind(('127.0.0.1',0));return sock.getsockname()[1]
        engine_port,gateway_port=port(),port()
        processes=[]
        def start(module,config,listen_port,env=None):
            process=subprocess.Popen([sys.executable,'-m',module,'rules-serve','--config',str(config),
                '--port',str(listen_port)],stdout=subprocess.PIPE,stderr=subprocess.PIPE,env=env)
            processes.append(process)
            for _ in range(100):
                if process.poll() is not None:self.fail(process.communicate()[1].decode())
                try:
                    with socket.create_connection(('127.0.0.1',listen_port),timeout=.05):return
                except OSError:time.sleep(.02)
            self.fail('service did not start')
        try:
            start('atropos',self.engine_config,engine_port)
            for user in self.tokens:
                profile=self.root/(user+'-engine.yml');config=yaml.safe_load(profile.read_text())
                config['server']['url']='http://127.0.0.1:%d'%engine_port
                profile.write_text(yaml.safe_dump(config))
            env=dict(os.environ)
            env['PYTHONPATH']=str(guard)+os.pathsep+os.getcwd()+os.pathsep+env.get('PYTHONPATH','')
            start('atraxis',self.config,gateway_port,env)
            profile=self.profiles['alice'];config=yaml.safe_load(profile.read_text())
            config['origin']='http://127.0.0.1:%d'%gateway_port;profile.write_text(yaml.safe_dump(config))
            self.client=RemoteRuleOperator(profile)
            first,report,active=self.activate()
            self.assertEqual(self.client.evaluate_active('cpu',['container','web'],{'cpu_percent':95})['result']['decision'],'review')
            # Stop Atraxis. Atropos retains and serves the active version directly.
            processes[1].terminate();processes[1].communicate(timeout=10)
            from atropos_client.client import RuleClient
            engine_client=RuleClient(self.root/'alice-engine.yml',expected_actor={'subject':'alice','organization':'org'})
            self.assertEqual(engine_client.active('cpu')['generation'],active['generation'])
        finally:
            for process in processes:
                if process.poll() is None:process.terminate()
                try:out,err=process.communicate(timeout=10)
                except subprocess.TimeoutExpired:process.kill();out,err=process.communicate()
                self.assertNotIn(self.tokens['alice'].encode(),out+err)

    def prepare_dispatch(self):
        from atropos.dispatch import ApprovedBindings
        from atropos.actions import builtin_actions
        first,report,active=self.activate()
        for grant in self.doc['grants']:grant['actions']=['core.echo']
        binding={'organization':'org','rule':'cpu','revision':first['revision'],
                 'result':self.cases[0]['expected'],'input':{'approved':'fixed'},'max_age_seconds':60,
                 'workflow':{'version':1,'name':'inspect','start':'echo','steps':{
                     'echo':{'action':'core.echo','with':{'value':{'$ref':'/input/approved'}}}}}}
        path=self.root/'dispatch.yml';path.write_text(yaml.safe_dump([binding]));path.chmod(0o600)
        self.doc['dispatch_bindings']=path.name;self.write_config()
        return next(iter(ApprovedBindings([binding],builtin_actions()).records)),active

    def run_guarded_worker(self):
        process=subprocess.run([sys.executable,'-m','atropos','rules-worker','--config',str(self.engine_config),'--once'],
                    capture_output=True,text=True,timeout=10)
        self.assertEqual(process.returncode,0,process.stderr)
        self.assertNotIn(self.engine_tokens['alice'],process.stdout+process.stderr)

    def test_remote_cli_review_dispatch_guarded_worker_and_status(self):
        binding,active=self.prepare_dispatch()
        listing=subprocess.run([sys.executable,'-m','atropos','rules-bindings','--config',str(self.engine_config)],
                               capture_output=True,text=True,timeout=10)
        self.assertEqual(listing.returncode,0,listing.stderr)
        self.assertEqual(json.loads(listing.stdout)[0]['id'],binding)
        facts=self.root/'facts.json';facts.write_text(json.dumps({'cpu_percent':95}))
        base=['rules','--remote-config',str(self.profiles['alice'])]
        def command(args):
            out,err=io.StringIO(),io.StringIO()
            with redirect_stdout(out),redirect_stderr(err):code=main(base+args)
            self.assertEqual(code,0,err.getvalue());return json.loads(out.getvalue())
        plan=command(['dispatch-plan','cpu','--target','container:web','--input',str(facts),'--binding',binding])
        self.assertEqual(plan['input'],{'approved':'fixed'})
        self.assertFalse(self.fixture.client.exists('remote:queue'))
        receipt=command(['dispatch','cpu','--target','container:web','--input',str(facts),'--binding',binding,
                         '--expected-generation',active['generation'],'--event-id','event-cli','--confirm'])
        self.assertEqual(receipt['status'],'queued')
        self.run_guarded_worker()
        status=command(['dispatch-status','cpu','--event-id','event-cli'])
        self.assertEqual(status['id'],receipt['id']);self.assertEqual(status['status'],'succeeded')
        self.assertNotIn('credential_id',json.dumps(status))
        self.assertEqual(self.client.audit('cpu')[-1]['operation'],'dispatch')

    def test_dispatch_cannot_override_workflow_and_engine_token_revocation_blocks_queue(self):
        binding,active=self.prepare_dispatch()
        values={'name':'cpu','target':['container','web'],'facts':{'cpu_percent':95},'binding':binding,
                'expected_generation':active['generation'],'event_id':'event-revoke'}
        status,_=self.raw(json.dumps({'method':'dispatch','arguments':dict(values,workflow={'shell':'injected'})}))
        self.assertEqual(status,400)
        receipt=self.client.dispatch(**values)
        self.doc['identities']=[i for i in self.doc['identities'] if i['subject']!='alice'];self.write_config()
        self.run_guarded_worker()
        from atropos.redis_store import RedisStore
        state=RedisStore(self.fixture.client,'remote').get(receipt['id'])
        self.assertEqual(state['status'],'cancelled');self.assertEqual(state['results'],{})

    def test_lost_dispatch_response_is_recovered_by_event_without_repeating_effects(self):
        from atropos.service import WorkflowService
        from unittest.mock import patch
        binding,active=self.prepare_dispatch()
        submit=WorkflowService.submit
        def lost(service,*args,**kwargs):
            result=submit(service,*args,**kwargs)
            if kwargs.get('execution') is not None:raise RuntimeError('synthetic lost receipt')
            return result
        with patch.object(WorkflowService,'submit',lost):
            with self.assertRaisesRegex(RemoteRuleError,'rule_outcome_unknown'):
                self.client.dispatch('cpu',['container','web'],{'cpu_percent':95},binding,
                                     expected_generation=active['generation'],event_id='lost-event')
        receipt=self.client.dispatch_status('cpu','lost-event')
        self.assertEqual(receipt['status'],'queued')
        self.assertEqual(self.fixture.client.xlen('remote:queue'),1)
        self.run_guarded_worker()
        self.assertEqual(self.client.dispatch_status('cpu','lost-event')['status'],'succeeded')

    def start_live_source(self):
        import base64
        from types import SimpleNamespace
        from http.server import BaseHTTPRequestHandler
        from atraxis.service import ReadService
        source=SimpleNamespace(source_id='monit',kind='monit_docker',max_age=60,max_resources=10)
        service=ReadService((source,));service.set_running(True);now=time.time()
        payload=dict(api_version=1,ready=True,running=True,last_cycle_success=True,cycles_total=1,
            errors_total=0,age_seconds=0,last_success_at=now,last_cycle_finished_at=now,
            containers=[dict(id='a'*64,name='demo',status='running',health='healthy',cpu_percent=95)])
        service.record('monit',SimpleNamespace(source_id='monit',outcome='success',payload=payload,
            received_at=now,received_monotonic=time.monotonic(),duration_seconds=0,attempted_at=now))
        class Inventory(BaseHTTPRequestHandler):
            def do_GET(inner):
                if inner.path!='/v1/status' or inner.headers.get('Authorization')!='Basic '+base64.b64encode(b'reader:synthetic').decode():
                    inner.send_error(403);return
                content=json.dumps(service.status()).encode();inner.send_response(200)
                inner.send_header('Content-Type','application/json');inner.send_header('Content-Length',str(len(content)))
                inner.end_headers();inner.wfile.write(content)
            def log_message(self,*args):pass
        server=ThreadingHTTPServer(('127.0.0.1',0),Inventory)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        self.addCleanup(lambda:(server.shutdown(),server.server_close(),thread.join()))
        (self.root/'inventory-secret.json').write_text('{"username":"reader","password":"synthetic"}')
        (self.root/'inventory.yml').write_text(yaml.safe_dump({'origin':'http://127.0.0.1:%d'%server.server_port,
            'allow_loopback_http':True,'credentials_file':'inventory-secret.json'}))
        self.doc['inventory_client']='inventory.yml'
        self.doc['bindings']=[{'organization':'org','target':['container','web'],'source_id':'monit','native_id':'a'*64}]
        self.write_config();return service

    def prepare_observed_dispatch(self):
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from atropos.dispatch import digest
        _,active=self.prepare_dispatch();self.live_source=self.start_live_source()
        private=Ed25519PrivateKey.generate()
        for filename,content in [('observation-private.pem',private.private_bytes(serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,serialization.NoEncryption())),
                ('observation-public.pem',private.public_key().public_bytes(serialization.Encoding.PEM,
                serialization.PublicFormat.SubjectPublicKeyInfo))]:
            path=self.root/filename;path.write_bytes(content);path.chmod(0o600)
        self.gateway_doc['observation_signing']={'key_id':'gateway','private_key_file':'observation-private.pem'}
        self.doc['observation_keys']={'gateway':'observation-public.pem'}
        path=self.root/'dispatch.yml';bindings=yaml.safe_load(path.read_text())
        bindings[0]['observation']={'key_id':'gateway','source_id':'monit','native_id':'a'*64,
                                    'filesystem':None,'max_age_seconds':60}
        path.write_text(yaml.safe_dump(bindings));self.write_config()
        return digest(bindings[0]),active

    def test_observed_cli_plan_confirm_recollect_and_guarded_execution(self):
        binding,active=self.prepare_observed_dispatch()
        base=['rules','--remote-config',str(self.profiles['alice'])]
        def command(args):
            out,err=io.StringIO(),io.StringIO()
            with redirect_stdout(out),redirect_stderr(err):code=main(base+args)
            self.assertEqual(code,0,err.getvalue());return json.loads(out.getvalue())
        plan=command(['observe-dispatch-plan','cpu','--target','container:web','--binding',binding])
        self.assertEqual(plan['facts'],{'cpu_percent':95});self.assertEqual(plan['observation']['source_id'],'monit')
        self.assertFalse(self.fixture.client.exists('remote:queue'))
        receipt=command(['observe-dispatch','cpu','--target','container:web','--binding',binding,
             '--expected-generation',active['generation'],'--event-id','live-event','--confirm'])
        self.run_guarded_worker()
        self.assertEqual(self.client.dispatch_status('cpu','live-event')['status'],'succeeded')
        self.assertTrue(receipt['observation']['verified_at_admission'])
        self.assertNotIn('token',str(receipt))

    def test_observed_confirmation_rejects_new_unavailable_snapshot_and_manual_override(self):
        from types import SimpleNamespace
        binding,active=self.prepare_observed_dispatch()
        self.client.observe_dispatch_plan('cpu',['container','web'],binding)
        self.live_source.record('monit',SimpleNamespace(source_id='monit',outcome='timeout',attempted_at=time.time(),received_at=time.time()))
        with self.assertRaises(RemoteRuleError):
            self.client.observe_dispatch('cpu',['container','web'],binding,expected_generation=active['generation'],event_id='bad-event')
        with self.assertRaises(PermissionError):
            self.client.dispatch('cpu',['container','web'],{'cpu_percent':95},binding,
                                 expected_generation=active['generation'],event_id='bad-manual')
        self.assertFalse(self.fixture.client.exists('remote:queue'))

    def test_gateway_cannot_sign_supplied_facts_or_forward_supplied_proof(self):
        binding,active=self.prepare_observed_dispatch()
        values={'name':'cpu','target':['container','web'],'binding':binding,
                'expected_generation':active['generation'],'event_id':'injected','facts':{'cpu_percent':95}}
        status,_=self.raw(json.dumps({'method':'observe_dispatch','arguments':values}));self.assertEqual(status,400)
        status,_=self.raw(json.dumps({'method':'dispatch_observed','arguments':{'proof':'forged',
                       'expected_generation':active['generation'],'event_id':'injected'}}));self.assertEqual(status,403)
        self.doc['observation_keys']={};self.write_config()
        with self.assertRaises((PermissionError,RemoteRuleError)):
            self.client.observe_dispatch_plan('cpu',['container','web'],binding)
        self.assertFalse(self.fixture.client.exists('remote:queue'))

    def test_end_to_end_test_flag_survives_api_queue_worker_and_alert_without_execute_right(self):
        from atropos.dispatch import digest
        from atropos.test_execution import test_namespace
        _,active=self.prepare_dispatch()
        path=self.root/'dispatch.yml';bindings=yaml.safe_load(path.read_text())
        bindings[0]['workflow']={'version':1,'name':'test-and-alert','start':'intervene','steps':{
            'intervene':{'action':'core.echo','with':{'status':'completed'},'next':'notify'},
            'notify':{'action':'core.alert','with':{'subject':'Intervention done','status':{'$ref':'/results/intervene/status'}}}}}
        path.write_text(yaml.safe_dump(bindings));binding=digest(bindings[0])
        for grant in self.doc['grants']:
            if grant['subject']=='alice':grant['operations']=['read','test'];grant['actions']=['core.echo','core.alert']
        self.write_config()
        with self.assertRaises(PermissionError):
            self.client.dispatch('cpu',['container','web'],{'cpu_percent':95},binding,
                    expected_generation=active['generation'],event_id='forbidden-real')
        preview=self.client.simulate_dispatch('cpu',['container','web'],{'cpu_percent':95},binding)
        self.assertTrue(preview['simulation'])
        self.assertFalse(self.fixture.client.exists(test_namespace('remote')+':queue'))
        from atraxis.rule_workbench import RuleWorkbench as TextualWorkbench
        facts=self.root/'textual-facts.json';facts.write_text('{"cpu_percent":95}')
        model=TextualWorkbench(self.client,'cpu')
        rows=model.available_test_bindings(('container','web'))
        self.assertEqual([row['id'] for row in rows],[binding])
        self.assertTrue(rows[0]['test_supported'])
        self.assertNotIn('input',rows[0])
        self.assertEqual(model.available_test_bindings(('server','host')),[])
        self.assertTrue(model.prepare_test(('container','web'),binding,str(facts),'e2e-test'))
        self.assertFalse(self.fixture.client.exists(test_namespace('remote')+':queue'))
        model.confirm()
        self.assertEqual(model.test_notice,'info')
        receipt={'test_context':model.test_context}
        for _ in range(2):
            process=subprocess.run([sys.executable,'-m','atropos','rules-worker','--config',str(self.engine_config),'--test','--once'],capture_output=True,text=True,timeout=10)
            self.assertEqual(process.returncode,0,process.stderr)
        model.test_status('e2e-test')
        self.assertEqual(model.test_notice,'success')
        status=self.client.test_dispatch_status('cpu','e2e-test')
        self.assertEqual(status['status'],'succeeded')
        alert=status['results']['notify']['alert']
        self.assertEqual(alert['test_context'],receipt['test_context']);self.assertEqual(alert['delivery'],'simulated')
        self.assertTrue(alert['subject'].startswith('[TEST]'))
        self.assertFalse(self.fixture.client.exists('remote:queue'))
        for event in status['history']:self.assertEqual(event['test_context'],receipt['test_context'])

    def test_end_to_end_test_delivery_reaches_receiver_with_same_marker(self):
        self.check_test_receiver_delivery(valid_ack=True)

    def test_missing_receiver_marker_quarantines_test_without_retry(self):
        self.check_test_receiver_delivery(valid_ack=False)

    def check_test_receiver_delivery(self, valid_ack):
        from http.server import BaseHTTPRequestHandler
        from atropos.dispatch import digest
        from atropos.redis_store import RedisStore
        from atropos.test_execution import test_namespace
        received=[];token=secrets.token_urlsafe(32)
        class Receiver(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_POST(self):
                body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                received.append((dict(self.headers),body))
                ack={'version':1,'accepted':True,'delivery_id':body['delivery_id']}
                if valid_ack:ack['test_context']=body['test_context']
                raw=json.dumps(ack).encode()
                self.send_response(202);self.send_header('Content-Type','application/json')
                self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
        receiver=ThreadingHTTPServer(('127.0.0.1',0),Receiver)
        thread=threading.Thread(target=receiver.serve_forever,daemon=True);thread.start()
        self.addCleanup(lambda:(receiver.shutdown(),receiver.server_close(),thread.join()))
        self.prepare_dispatch()
        path=self.root/'dispatch.yml';bindings=yaml.safe_load(path.read_text())
        bindings[0]['workflow']={'version':1,'name':'test-delivery','start':'intervene','steps':{
            'intervene':{'action':'core.echo','with':{'status':'completed'},'next':'notify'},
            'notify':{'action':'core.alert','with':{'subject':'Test intervention complete'},
                      'on_error':'must-not-run'},
            'must-not-run':{'action':'core.echo','with':{'unexpected':True}}}}
        path.write_text(yaml.safe_dump(bindings));binding=digest(bindings[0])
        secret=self.root/'receiver.token';secret.write_text(token);secret.chmod(0o600)
        self.doc['test_alert_delivery']={'backend':'webhook','url':'http://127.0.0.1:%d'%receiver.server_port,
            'allow_loopback_http':True,'token_file':secret.name,'timeout':3}
        for grant in self.doc['grants']:
            if grant['subject']=='alice':grant['operations']=['read','test'];grant['actions']=['core.echo','core.alert']
        self.write_config()
        receipt=self.client.test_dispatch('cpu',['container','web'],{'cpu_percent':95},binding,event_id='receiver-test')
        for _ in range(3):
            process=subprocess.run([sys.executable,'-m','atropos','rules-worker','--config',str(self.engine_config),
                '--test','--once'],capture_output=True,text=True,timeout=10)
            self.assertEqual(process.returncode,0,process.stderr)
            self.assertNotIn(token,process.stdout+process.stderr)
        status=self.client.test_dispatch_status('cpu','receiver-test')
        self.assertEqual(len(received),1)
        headers,event=received[0]
        self.assertEqual(headers['Authorization'],'Bearer '+token)
        self.assertEqual(headers['X-Atropos-Test-ID'],receipt['test_context']['id'])
        self.assertEqual(event['test_context'],receipt['test_context'])
        self.assertEqual(event['alert']['test_context'],receipt['test_context'])
        self.assertNotIn('must-not-run',status['results'])
        self.assertFalse(self.fixture.client.exists('remote:queue'))
        store=RedisStore(self.fixture.client,test_namespace('remote'))
        state=store.get(receipt['id'])
        if valid_ack:
            self.assertEqual(status['status'],'succeeded')
            self.assertEqual(status['results']['notify']['alert']['delivery'],'test_receiver_acknowledged')
            self.assertEqual(status['results']['notify']['alert']['test_context'],receipt['test_context'])
            self.assertFalse(self.fixture.client.exists(store.target_key(state['target'])))
        else:
            self.assertEqual(status['status'],'uncertain')
            self.assertEqual(self.fixture.client.get(store.target_key(state['target'])),receipt['id'])
            self.assertNotIn('notify',status['results'])

    def test_authorized_binding_discovery_cli_and_revocation(self):
        binding,_=self.prepare_dispatch()
        for grant in self.doc['grants']:
            if grant['subject']=='alice':
                grant['operations']=['read','test'];grant['actions']=['core.echo']
        self.write_config()
        out,err=io.StringIO(),io.StringIO()
        with redirect_stdout(out),redirect_stderr(err):
            code=main(['rules','--remote-config',str(self.profiles['alice']),
                       'test-bindings','cpu','--target','container:web'])
        self.assertEqual(code,0,err.getvalue())
        rows=json.loads(out.getvalue());self.assertEqual([r['id'] for r in rows],[binding])
        self.assertNotIn('input',rows[0])
        self.assertFalse(self.fixture.client.exists('remote:queue'))
        for grant in self.doc['grants']:
            if grant['subject']=='alice':grant['actions']=[]
        self.write_config()
        self.assertEqual(self.client.test_bindings('cpu',['container','web']),[])
        with self.assertRaises(PermissionError):
            self.client.test_dispatch('cpu',['container','web'],{'cpu_percent':95},binding,event_id='revoked-selection')

    def test_queue_capacity_refusal_crosses_both_services_without_admission(self):
        from atropos.redis_store import RedisStore
        from atropos.test_execution import test_namespace
        binding,active=self.prepare_dispatch()
        store=RedisStore(self.fixture.client,'remote');store.configure_queue(0)
        before=self.client.audit('cpu')
        with self.assertRaisesRegex(RemoteRuleError,'rule_queue_full'):
            self.client.dispatch('cpu',['container','web'],{'cpu_percent':95},binding,
                                 expected_generation=active['generation'],event_id='full-real')
        self.assertEqual(self.client.audit('cpu'),before)
        self.assertEqual(store.queue_status()['outstanding'],0)
        # The test namespace is independent of the paused real namespace.
        receipt=self.client.test_dispatch('cpu',['container','web'],{'cpu_percent':95},binding,event_id='test-ok')
        self.assertEqual(receipt['status'],'queued')
        tests=RedisStore(self.fixture.client,test_namespace('remote'));tests.configure_queue(1)
        with self.assertRaisesRegex(RemoteRuleError,'rule_queue_full'):
            self.client.test_dispatch('cpu',['container','web'],{'cpu_percent':95},binding,event_id='test-full')
        self.assertEqual(tests.queue_status()['outstanding'],1)
        duplicate=self.client.test_dispatch('cpu',['container','web'],{'cpu_percent':95},binding,event_id='test-ok')
        self.assertEqual(duplicate['id'],receipt['id'])
