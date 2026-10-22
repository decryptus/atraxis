"""Real, separately installed Atraxis/Atropos services and disposable Redis.

Set ATROPOS_TEST_PYTHON to the engine venv interpreter. Run this suite using
Atraxis's venv. No mocks, engine imports, ambient destinations or silent skips.
"""
import base64
from copy import deepcopy
import hashlib
from html.parser import HTMLParser
import http.client
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from urllib.parse import parse_qs, urlencode, urlsplit

import yaml
from atropos_client.client import RuleClient

OPERATIONS = ['read','create','update','validate','test','import','export','save',
              'activate','deactivate','rollback','evaluate','archive','unarchive','delete']
CASES = [dict(name='High CPU',input=dict(cpu_percent=95),
              expected=dict(decision='review',matched_rule='combined'))]


class FormParser(HTMLParser):
    def __init__(self):
        super().__init__(); self.tokens=[]
    def handle_starttag(self, tag, attrs):
        attrs=dict(attrs)
        if tag=='input' and attrs.get('name')=='token':self.tokens.append(attrs['value'])


def free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1',0));return s.getsockname()[1]


class WebEngineFixture(unittest.TestCase):
    def setUp(self):
        engine=os.environ.get('ATROPOS_TEST_PYTHON')
        if not engine or not Path(engine).is_absolute() or not Path(engine).is_file():
            raise RuntimeError('Set ATROPOS_TEST_PYTHON to the installed engine venv interpreter')
        self.engine_python=engine
        self.environment={key:os.environ[key] for key in ('PATH','LANG','LC_ALL') if key in os.environ}
        self.environment.update(PYTHONNOUSERSITE='1',PYTHONUNBUFFERED='1')
        temporary=tempfile.TemporaryDirectory(prefix='atraxis-engine-')
        self.addCleanup(temporary.cleanup);self.root=Path(temporary.name)
        self.processes={};self.logs={};self.addCleanup(self.stop_all)
        self.redis_port,self.engine_port,self.web_port=free_port(),free_port(),free_port()
        # Reserve distinct values even on hosts that immediately reuse ephemeral ports.
        while len({self.redis_port,self.engine_port,self.web_port})!=3:
            self.redis_port,self.engine_port,self.web_port=free_port(),free_port(),free_port()
        self.origin='http://127.0.0.1:'+str(self.web_port)
        self.password=secrets.token_urlsafe(24)
        self.storage_password=secrets.token_urlsafe(32)
        self.tokens={user:secrets.token_urlsafe(32) for user in ('alice','bob','charlie')}
        self.identities=[dict(username=user,subject=user,organization='other' if user=='charlie' else 'demo',
            atropos_client=user+'.yml') for user in self.tokens]
        self.engine_config=dict(version=1,namespace='webintegration',
            storage=dict(backend='redis',url='redis://:'+self.storage_password+'@127.0.0.1:%s/0'%self.redis_port),
            identities=[dict(token_sha256=hashlib.sha256(token.encode()).hexdigest(),subject=user,
                organization='other' if user=='charlie' else 'demo') for user,token in self.tokens.items()],
            resources=[dict(organization=org,kind='server',id='lab') for org in ('demo','other')],
            grants=[dict(subject=user,organization='other' if user=='charlie' else 'demo',rule_kind='intervention',
                operations=['read'] if user=='bob' else OPERATIONS,scopes=[['server','lab']],providers=['internal','upload'])
                for user in self.tokens])
        self.write_engine()
        self.write_yaml('mapping.yml',dict(version=1,identities=self.identities))
        digest=base64.b64encode(hashlib.sha1(self.password.encode()).digest()).decode()
        self.private('users',''.join(user+':{SHA}'+digest+'\n' for user in self.tokens))
        self.write_yaml('web.yml',dict(general=dict(listen_addr='127.0.0.1',listen_port=self.web_port,
            auth_basic_file='users',rules_web_file='mapping.yml',rules_web_origin=self.origin)))
        for user, token in self.tokens.items():
            self.private(user+'.token',token)
            self.write_yaml(user+'.yml',dict(origin='http://127.0.0.1:'+str(self.engine_port),
                allow_loopback_http=True,token_file=user+'.token',timeout=5))
        self.clients={user:RuleClient(self.root/(user+'.yml'),expected_actor=dict(subject=user,
            organization='other' if user=='charlie' else 'demo')) for user in self.tokens}
        for client in self.clients.values():self.addCleanup(client.close)
        self.redis_binary=os.environ.get('ATROPOS_TEST_REDIS_SERVER') or shutil.which('redis-server')
        if not self.redis_binary:
            self.redis_binary=subprocess.check_output([self.engine_python,'-c',
                'from pathlib import Path; import redislite; print(Path(redislite.__file__).parent/"bin"/"redis-server")'],
                cwd=self.root,env=self.environment,text=True,timeout=10).strip()
        self.start_storage();self.start_engine();self.start_web()
        self.first=self.clients['alice'].create_from_templates(['cpu-high'],name='cpu',
            rule_kind='intervention',selections=[['server','lab']])

    def private(self, name, content):
        path=self.root/name;path.write_text(content);path.chmod(0o600)

    def write_yaml(self, name, document):
        self.private(name,yaml.safe_dump(json.loads(json.dumps(document))))

    def write_engine(self):
        # Atomic replacement preserves the private-file permission during policy reload.
        self.write_yaml('engine.next',self.engine_config)
        (self.root/'engine.next').replace(self.root/'engine.yml')

    def spawn(self, name, args):
        log=(self.root/(name+'.log')).open('ab');self.logs[name]=log
        self.processes[name]=subprocess.Popen(args,cwd=self.root,env=self.environment,stdout=log,stderr=log)

    def stop(self, name):
        process=self.processes.pop(name,None)
        if process is not None:
            if process.poll() is None:
                process.terminate()
                try:process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill();process.wait(timeout=5)
                    raise AssertionError(name+' did not stop gracefully')
            self.assertEqual(process.returncode,0,name+' stopped with nonzero status')
        log=self.logs.pop(name,None)
        if log:log.close()

    def stop_all(self):
        errors=[]
        for name in ('web','engine','storage'):
            try:self.stop(name)
            except Exception as error:errors.append(error)
        if errors:raise errors[0]

    def ready(self, name, probe):
        deadline=time.monotonic()+15
        while time.monotonic()<deadline:
            if self.processes[name].poll() is not None:
                # Synthetic service logs contain no credential values; do not dump config files.
                raise AssertionError(name+' stopped at startup: '+(self.root/(name+'.log')).read_text()[-2000:])
            try:
                if probe():return
            except (OSError,ValueError,http.client.HTTPException):pass
            time.sleep(.05)
        raise AssertionError(name+' did not become ready')

    def start_storage(self):
        directory=self.root/'storage';directory.mkdir(exist_ok=True)
        self.private('redis.conf','bind 127.0.0.1\nport '+str(self.redis_port)+'\ndir '+str(directory)+'\nsave \"\"\nappendonly yes\nappendfsync always\nmaxmemory-policy noeviction\nrequirepass '+self.storage_password+'\n')
        self.spawn('storage',[self.redis_binary,str(self.root/'redis.conf')])
        def probe():
            with socket.create_connection(('127.0.0.1',self.redis_port),timeout=1) as conn:
                conn.sendall(('AUTH '+self.storage_password+'\r\n').encode())
                if conn.recv(64)!=b'+OK\r\n':return False
                conn.sendall(b'*1\r\n$4\r\nPING\r\n');return conn.recv(64)==b'+PONG\r\n'
        self.ready('storage',probe)

    def start_engine(self):
        self.spawn('engine',[self.engine_python,'-m','atropos.cli','rules-serve',
            '--config',str(self.root/'engine.yml'),'--port',str(self.engine_port)])
        self.ready('engine',lambda:bool(self.clients['alice'].catalog()))

    def start_web(self):
        self.spawn('web',[sys.executable,'-m','atraxis.cli','serve','--config',str(self.root/'web.yml')])
        self.ready('web',lambda:self.request('/healthz')[0]==200)

    def request(self, path, *, user='alice', fields=None):
        headers={'Authorization':'Basic '+base64.b64encode((user+':'+self.password).encode()).decode()}
        raw=None
        if fields is not None:
            raw=urlencode(fields).encode();headers.update({'Origin':self.origin,'Content-Type':'application/x-www-form-urlencoded'})
        conn=http.client.HTTPConnection('127.0.0.1',self.web_port,timeout=7)
        try:
            conn.request('POST' if fields is not None else 'GET',path,body=raw,headers=headers)
            response=conn.getresponse();body=response.read().decode()
            for token in self.tokens.values():self.assertNotIn(token,body)
            return response.status,dict(response.getheaders()),body
        finally:conn.close()

    def form(self, path, *, user='alice'):
        status,_,body=self.request(path,user=user);self.assertEqual(status,200,body)
        parser=FormParser();parser.feed(body);self.assertEqual(len(parser.tokens),1,body)
        return parser.tokens[0]

    def scenarios(self, revision, cases=CASES):
        token=self.form('/ui/rules/test?'+urlencode(dict(name='cpu',revision=revision)))
        status,headers,body=self.request('/ui/rules/test',fields=dict(token=token,cases=json.dumps(cases)))
        self.assertEqual(status,303,body)
        values=parse_qs(urlsplit(headers['Location']).query)
        report=self.clients['alice'].test_report('cpu',revision,values['report'][0])
        self.assertTrue(report['passed'],report)
        self.assertIn('All scenarios passed',self.request(headers['Location'])[2])
        return report

    def review(self, operation, revision=None, report=None, *, user='alice'):
        values=dict(name='cpu')
        if operation!='deactivate':values.update(revision=revision,report=report)
        return self.form('/ui/rules/'+operation+'?'+urlencode(values),user=user)

    def confirm(self, operation, token, *, status=303, user='alice'):
        result=self.request('/ui/rules/'+operation,user=user,fields=dict(token=token,confirm=operation))
        self.assertEqual(result[0],status,result[2])
        if status==303:self.assertEqual(self.request(result[1]['Location'],user=user)[0],200)
        return result

    def edit(self, threshold=99):
        client=self.clients['alice'];document=deepcopy(client.draft('cpu')['document'])
        token=self.form('/ui/rules/edit?name=cpu');document['checks']['cpu-high']['gte']=threshold
        status,headers,body=self.request('/ui/rules/edit',fields=dict(token=token,document=yaml.safe_dump(document),message='Integration edit'))
        self.assertEqual(status,303,body)
        return client.read('cpu',parse_qs(urlsplit(headers['Location']).query)['revision'][0])

    def assert_rules_only(self):
        # Read keys only from the disposable server started by this test.
        raw=subprocess.check_output([self.engine_python,'-c',
            'import json,redis,sys; r=redis.Redis(host="127.0.0.1",port=int(sys.argv[1]),password=sys.stdin.read(),decode_responses=True); print(json.dumps(sorted(r.scan_iter())))',
            str(self.redis_port)],cwd=self.root,env=self.environment,input=self.storage_password,text=True,timeout=5)
        keys=json.loads(raw);self.assertTrue(keys)
        self.assertTrue(all(key.startswith('webintegration:rules:') for key in keys),keys)

class WebEngineTests(WebEngineFixture):
    def test_web_lifecycle_uses_real_zen_and_audited_storage(self):
        client=self.clients['alice'];first=self.first['revision']
        report=self.scenarios(first)
        self.confirm('activate',self.review('activate',first,report['id']))
        self.assertEqual(client.evaluate_active('cpu',['server','lab'],dict(cpu_percent=95))['result']['decision'],'review')
        second=self.edit()['revision'];self.assertEqual(client.active('cpu')['revision'],first)
        report2=self.scenarios(second,[dict(name='Below new threshold',input=dict(cpu_percent=95),
            expected=dict(decision='ignore',matched_rule='__default__'))])
        self.confirm('activate',self.review('activate',second,report2['id']))
        self.assertEqual(client.evaluate_active('cpu',['server','lab'],dict(cpu_percent=95))['result']['decision'],'ignore')
        rollback=self.review('rollback',first,report['id']);self.confirm('rollback',rollback)
        self.confirm('rollback',rollback,status=403)
        self.confirm('deactivate',self.review('deactivate'))
        self.assertIsNone(client.active('cpu')['revision'])
        self.assertEqual(client.draft('cpu')['revision'],second)
        self.assertEqual([event['operation'] for event in client.audit('cpu')],['activate','activate','rollback','deactivate'])
        status,_,body=self.request('/ui/rules/audit?name=cpu');self.assertEqual(status,200,body)
        for label in ('Activation</h3>','Rollback</h3>','Deactivation</h3>'):self.assertIn(label,body)
        self.assertIn('No accessible events',self.request('/ui/rules/audit?name=cpu&offset=20')[2])
        self.assert_rules_only()

    def test_current_permissions_identity_and_organization_are_enforced(self):
        client=self.clients['alice'];revision=self.first['revision'];report=self.scenarios(revision)
        self.assertEqual(self.request('/ui/rules?name=cpu',user='bob')[0],200)
        token=self.review('activate',revision,report['id'],user='bob')
        self.confirm('activate',token,user='bob',status=403)
        self.assertIsNone(client.active('cpu')['revision'])
        self.assertEqual(self.request('/ui/rules?name=cpu',user='charlie')[0],404)
        self.assertEqual(self.request('/ui/rules?name=cpu&organization=demo',user='charlie')[0],403)
        token=self.review('activate',revision,report['id'])
        self.engine_config['grants'][0]['operations']=['read'];self.write_engine()
        self.confirm('activate',token,status=403)
        self.assertIsNone(client.active('cpu')['revision'])
        self.engine_config['grants'][0]['operations']=OPERATIONS;self.write_engine()
        self.confirm('activate',self.review('activate',revision,report['id']))
        grant=self.engine_config['grants'].pop(0);self.write_engine()
        self.assertEqual(self.request('/ui/rules/audit?name=cpu')[0],200)  # filtered, never leaks the event
        self.assertIn('No accessible events',self.request('/ui/rules/audit?name=cpu')[2])
        self.assertEqual(self.request('/ui/rules?name=cpu')[0],403)
        self.engine_config['grants'].insert(0,grant);self.write_engine()
        changed=deepcopy(self.identities);changed[0]['subject']='bob'
        self.write_yaml('mapping.yml',dict(version=1,identities=changed))
        self.assertEqual(self.request('/ui/rules?name=cpu')[0],403)
        self.assert_rules_only()

    def test_engine_rejects_never_active_rollback_stale_evidence_and_generation(self):
        client=self.clients['alice'];first=self.first['revision'];report=self.scenarios(first)
        self.confirm('activate',self.review('activate',first,report['id']))
        stale_deactivation=self.review('deactivate')
        second=self.edit()['revision']
        cases=[dict(name='Below threshold',input=dict(cpu_percent=95),expected=dict(decision='ignore',matched_rule='__default__'))]
        evidence=self.scenarios(second,cases)
        self.confirm('rollback',self.review('rollback',second,evidence['id']),status=400)
        token=self.review('activate',second,evidence['id'])
        failed=client.test('cpu',second,CASES);self.assertFalse(failed['passed'])
        self.confirm('activate',token,status=400)
        self.assertEqual(client.active('cpu')['revision'],first)
        evidence=self.scenarios(second,cases)
        self.confirm('activate',self.review('activate',second,evidence['id']))
        self.confirm('deactivate',stale_deactivation,status=409)
        self.confirm('deactivate',stale_deactivation,status=403)
        self.assertEqual([event['operation'] for event in client.audit('cpu')],['activate','activate'])
        self.assert_rules_only()

    def test_graceful_service_and_storage_restart_preserves_state_and_expires_forms(self):
        client=self.clients['alice'];first=self.first['revision'];report=self.scenarios(first)
        self.confirm('activate',self.review('activate',first,report['id']))
        state=client.active('cpu');second=self.edit()['revision'];audit=client.audit('cpu')
        token=self.review('deactivate')
        self.stop('engine')
        self.assertEqual(self.request('/ui/rules?name=cpu')[0],503)
        self.stop('web');self.stop('storage')
        self.start_storage();self.start_engine();self.start_web()
        self.assertEqual(client.active('cpu'),state)
        self.assertEqual(client.draft('cpu')['revision'],second)
        self.assertEqual(client.test_report('cpu',first,report['id']),report)
        self.assertEqual(client.audit('cpu'),audit)
        self.assertIn('Active at page load',self.request('/ui/rules?'+urlencode(dict(name='cpu',revision=first)))[2])
        self.confirm('deactivate',token,status=403)
        self.assertEqual(client.active('cpu'),state)
        self.assert_rules_only()

    def test_real_dispatch_and_test_admissions_remain_readable_in_web_audit(self):
        client=self.clients['alice'];revision=self.first['revision']
        report=self.scenarios(revision)
        self.confirm('activate',self.review('activate',revision,report['id']))
        binding=dict(organization='demo',rule='cpu',revision=revision,
            result=dict(decision='review',matched_rule='combined'),input={},max_age_seconds=300,
            workflow=dict(version=1,name='audit-example',start='echo',
                steps={'echo':dict(action='core.echo',timeout=5)}))
        self.write_yaml('bindings.yml',[binding])
        self.engine_config['dispatch_bindings']='bindings.yml'
        self.engine_config['grants'][0]['actions']=['core.echo']
        self.engine_config['grants'][0]['operations']=OPERATIONS+['execute']
        self.write_engine()
        binding_id=hashlib.sha256(json.dumps(binding,sort_keys=True,separators=(',',':')).encode()).hexdigest()
        admitted=client.dispatch('cpu',['server','lab'],{'cpu_percent':95},binding_id,
            expected_generation=client.active('cpu')['generation'],event_id='audit-live')
        simulated=client.test_dispatch('cpu',['server','lab'],{'cpu_percent':95},binding_id,event_id='audit-test')
        self.assertEqual(admitted['status'],'queued')
        self.assertEqual(simulated['status'],'queued')
        self.assertEqual([event['operation'] for event in client.audit('cpu')],['activate','dispatch','test_run'])
        status,_,body=self.request('/ui/rules/audit?name=cpu')
        self.assertEqual(status,200,body)
        for value in ('Intervention admitted</h3>','Test run admitted</h3>',admitted['id'],simulated['id'],
                      'Execution outcome is not recorded', 'Test mode'):
            self.assertIn(value,body)
        self.assertEqual(body.count('class="info"'),2)
        # No worker was started: admission must never appear as completed work.
        self.assertEqual(client.dispatch_status('cpu','audit-live')['status'],'queued')
        self.assertEqual(client.test_dispatch_status('cpu','audit-test')['status'],'queued')

    def account_workflow(self):
        from atraxis.accounts import Directory, default_directory
        from atraxis.account_store import AccountStore
        data=default_directory('alice').document()
        data['organizations'][0]['id']='demo'
        data['memberships'][0]['organization']='demo'
        data['groups']=[dict(id='operators',name='Operators',organization='demo',parent=None,
                            inherit_parent=False,enabled=True)]
        data['memberships'].append(dict(user='alice',organization='demo',group='operators'))
        store=AccountStore(self.root/'accounts.sqlite3');store.apply(Directory(data),None)
        self.engine_config['membership']=dict(backend='sqlite',path='accounts.sqlite3')
        self.engine_config['grants'][0].update(subject='operators',subject_kind='group',
            operations=OPERATIONS+['execute'],actions=['core.echo'])
        self.write_engine()
        client=self.clients['alice'];revision=self.first['revision']
        report=client.test('cpu',revision,CASES)
        active=client.activate('cpu',revision,test_id=report['id'],expected_generation=None)
        binding=dict(organization='demo',rule='cpu',revision=revision,
            result=dict(decision='review',matched_rule='combined'),input={},max_age_seconds=300,
            workflow=dict(version=1,name='membership-example',start='first',steps={
                'first':dict(action='core.echo',timeout=5,next='last'),
                'last':dict(action='core.echo',timeout=5)}))
        self.write_yaml('bindings.yml',[binding]);self.engine_config['dispatch_bindings']='bindings.yml'
        self.write_engine()
        binding_id=hashlib.sha256(json.dumps(binding,sort_keys=True,separators=(',',':')).encode()).hexdigest()
        return store,data,binding_id,active

    def account_worker_once(self, test=False):
        args=[self.engine_python,'-m','atropos','rules-worker','--config',str(self.root/'engine.yml'),'--once']
        if test:args.append('--test')
        completed=subprocess.run(args,cwd=self.root,env=self.environment,capture_output=True,timeout=20)
        self.assertEqual(completed.returncode,0,'guarded worker failed')

    def test_account_revocation_cancels_live_and_test_queues_without_gateway_bypass(self):
        from atraxis.accounts import Directory
        store,original,binding,active=self.account_workflow();client=self.clients['alice']
        for index,kind in enumerate(('user','organization','tenant','group','membership','missing','corrupt')):
            with self.subTest(kind=kind):
                event='revoked-'+str(index)
                client.dispatch('cpu',['server','lab'],{'cpu_percent':95},binding,
                    expected_generation=active['generation'],event_id=event)
                client.test_dispatch('cpu',['server','lab'],{'cpu_percent':95},binding,event_id=event)
                data=deepcopy(original);snapshot=store.snapshot();saved=(self.root/'accounts.sqlite3').read_bytes()
                if kind in ('user','organization','tenant','group'):data[kind+'s'][0]['enabled']=False
                elif kind=='membership':data['memberships']=[]
                if kind=='missing':(self.root/'accounts.sqlite3').unlink()
                elif kind=='corrupt':self.private('accounts.sqlite3','{}')
                else:store.apply(Directory(data),snapshot['revision'])
                # Uses the engine credential directly, bypassing Atraxis entirely.
                with self.assertRaises(PermissionError):client.read('cpu',self.first['revision'])
                self.account_worker_once();self.account_worker_once(test=True)
                if kind in ('missing','corrupt'):
                    (self.root/'accounts.next').write_bytes(saved);(self.root/'accounts.next').chmod(0o600)
                    (self.root/'accounts.next').replace(self.root/'accounts.sqlite3')
                else:store.apply(Directory(original),store.snapshot()['revision'])
                for receipt in (client.dispatch_status('cpu',event),client.test_dispatch_status('cpu',event)):
                    self.assertEqual(receipt['status'],'cancelled')
                    self.assertEqual(receipt['error'],'execution_authorization_denied')
                    self.assertEqual([entry['status'] for entry in receipt['history']],['running','cancelled'])
                # Re-enabling membership does not revive cancelled work.
                self.account_worker_once();self.account_worker_once(test=True)
                self.assertEqual(client.dispatch_status('cpu',event)['status'],'cancelled')

    def test_account_group_revocation_between_steps_preserves_completed_effects(self):
        from atraxis.accounts import Directory
        store,original,binding,active=self.account_workflow();client=self.clients['alice']
        client.dispatch('cpu',['server','lab'],{'cpu_percent':95},binding,
            expected_generation=active['generation'],event_id='between-steps')
        self.account_worker_once()
        before=client.dispatch_status('cpu','between-steps')
        self.assertEqual(before['status'],'queued')
        data=deepcopy(original);data['groups'][0]['enabled']=False
        store.apply(Directory(data),store.snapshot()['revision']);self.account_worker_once()
        store.apply(Directory(original),store.snapshot()['revision'])
        after=client.dispatch_status('cpu','between-steps')
        self.assertEqual(after['status'],'cancelled')
        self.assertEqual(after['history'][:-2],before['history'])
        self.assertEqual([entry['status'] for entry in after['history'][-2:]],['running','cancelled'])

    def test_web_archive_unarchive_and_delete_preserve_scoped_audit(self):
        client=self.clients['alice']
        archive=self.form('/ui/rules/archive?name=cpu')
        # A reader can review but cannot commit a lifecycle mutation.
        denied=self.form('/ui/rules/archive?name=cpu',user='bob')
        self.confirm('archive',denied,status=403,user='bob')
        self.confirm('archive',archive)
        self.assertEqual(client.availability('cpu')['status'],'archived')
        self.assertEqual(self.request('/ui/rules/archive?name=cpu',user='charlie')[0],404)
        token=self.form('/ui/rules/unarchive?name=cpu');self.confirm('unarchive',token)
        self.assertIsNone(client.active('cpu')['revision'])
        deleted=self.form('/ui/rules/delete?name=cpu');self.confirm('delete',deleted)
        self.assertEqual(client.availability('cpu')['status'],'deleted')
        self.assertEqual([e['operation'] for e in client.audit('cpu')],['archive','unarchive','delete'])
        self.assertEqual(self.request('/ui/rules?name=cpu')[0],404)
        status,_,body=self.request('/ui/rules/audit?name=cpu')
        self.assertEqual(status,200,body);self.assertIn('Draft contents deleted',body)
        self.stop('engine');self.stop('storage');self.start_storage();self.start_engine()
        self.assertEqual(client.availability('cpu')['status'],'deleted')
        self.assertEqual(len(client.audit('cpu')),3)

    def test_intervention_search_real_engine_test_mode_scope_and_revocation(self):
        client=self.clients['alice'];revision=self.first['revision']
        for grant in self.engine_config['grants']:
            if grant['subject']=='alice':grant['actions']=['core.echo']
        workflow=dict(version=1,name='inspect',start='inspect',steps=dict(inspect=dict(action='core.echo')))
        binding=dict(organization='demo',rule='cpu',revision=revision,
            result=dict(decision='review',matched_rule='combined'),workflow=workflow,input={},max_age_seconds=60)
        self.write_yaml('bindings.yml',[binding]);self.engine_config['dispatch_bindings']='bindings.yml';self.write_engine()
        binding_id=client.test_bindings('cpu',['server','lab'])[0]['id']
        receipt=client.test_dispatch('cpu',['server','lab'],dict(cpu_percent=95),binding_id,event_id='search-test')
        data=client.search_interventions('cpu',query='SEARCH-TEST',mode='test')
        self.assertEqual(data['total'],1);self.assertEqual(data['items'][0]['id'],receipt['id'])
        self.assertEqual(client.search_interventions('cpu',mode='real')['total'],0)
        self.assertEqual(self.clients['bob'].search_interventions('cpu')['total'],0)
        status,_,body=self.request('/ui/interventions?name=cpu&mode=test&q=SEARCH-TEST')
        self.assertEqual(status,200,body);self.assertIn('1 matches',body);self.assertIn('demo/server/lab',body)
        self.assertEqual(client.test_dispatch_status('cpu','search-test')['status'],'queued')
        self.engine_config['grants']=[];self.write_engine()
        status,_,body=self.request('/ui/interventions?name=cpu&mode=test')
        self.assertEqual(status,403,body);self.assertNotIn('demo/server/lab',body)

    def test_sqlite_accounts_engine_and_redis_delivery_restore_rechecks_revocation(self):
        from atraxis.account_backup import backup as account_backup, restore as account_restore
        from atraxis.account_store import AccountStore
        from atraxis.accounts import Directory
        self.stop('engine')
        delivery_url = self.engine_config['storage']['url'][:-1] + '1'
        self.engine_config['storage'] = dict(backend='sqlite', url='sqlite:///' + str(self.root/'engine.db'))
        self.engine_config['queue'] = dict(backend='redis', url=delivery_url)
        self.write_engine(); self.start_engine()
        client = self.clients['alice']
        self.first = client.create_from_templates(['cpu-high'], name='cpu', rule_kind='intervention', selections=[['server','lab']])
        accounts, original, binding, active = self.account_workflow()
        real = client.dispatch('cpu', ['server','lab'], {'cpu_percent':95}, binding,
                               expected_generation=active['generation'], event_id='restore-live')
        simulated = client.test_dispatch('cpu', ['server','lab'], {'cpu_percent':95}, binding, event_id='restore-test')
        self.account_worker_once()  # The first real step is durably checkpointed.
        before = client.dispatch_status('cpu', 'restore-live')
        self.assertEqual(before['status'], 'queued')

        def storage_command(command, *arguments):
            result = subprocess.run([self.engine_python, '-m', 'atropos.cli', command,
                '--config', str(self.root/'engine.yml'), *map(str, arguments)],
                cwd=self.root, env=self.environment, capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(result.stdout)

        storage_command('storage-backup', '--output', self.root/'engine-backup.json')
        account_backup(accounts.path, self.root/'accounts-backup.json')
        self.stop('web'); self.stop('engine')
        account_restore(self.root/'restored-accounts.db', self.root/'accounts-backup.json', services_stopped=True)
        restored_accounts = AccountStore(self.root/'restored-accounts.db')
        self.assertEqual(restored_accounts.snapshot(), accounts.snapshot())
        self.engine_config['membership']['path'] = 'restored-accounts.db'
        self.engine_config['storage']['url'] = 'sqlite:///' + str(self.root/'restored-engine.db')
        self.engine_config['queue']['url'] = delivery_url[:-1] + '2'
        self.write_engine()
        restored = storage_command('storage-restore', '--input', self.root/'engine-backup.json', '--confirm-source-stopped')
        self.assertEqual(restored['execution'], 'paused')
        self.stop('storage'); self.start_storage(); self.start_engine(); self.start_web()
        self.assertEqual(client.dispatch_status('cpu', 'restore-live'), before)
        self.assertEqual(client.search_interventions('cpu')['total'], 2)
        self.assertEqual(client.search_interventions('cpu', mode='test')['total'], 1)
        self.assertEqual(client.test_dispatch_status('cpu', 'restore-test')['id'], simulated['id'])
        self.assertEqual(client.dispatch('cpu', ['server','lab'], {'cpu_percent':95}, binding,
            expected_generation=active['generation'], event_id='restore-live')['id'], real['id'])
        self.assertEqual([x['operation'] for x in client.audit('cpu')], ['activate', 'dispatch', 'test_run'])
        self.assertEqual(self.request('/ui/rules/audit?name=cpu')[0], 200)
        self.account_worker_once(); self.account_worker_once(test=True)
        self.assertEqual(client.dispatch_status('cpu', 'restore-live'), before)
        revoked = deepcopy(original); revoked['groups'][0]['enabled'] = False
        restored_accounts.apply(Directory(revoked), restored_accounts.revision())
        with self.assertRaises(PermissionError): client.read('cpu', self.first['revision'])
        storage_command('storage-resume', '--confirm-reconciled')
        self.account_worker_once(); self.account_worker_once(test=True)
        restored_accounts.apply(Directory(original), restored_accounts.revision())
        for receipt in (client.dispatch_status('cpu', 'restore-live'), client.test_dispatch_status('cpu', 'restore-test')):
            self.assertEqual(receipt['status'], 'cancelled')
            self.assertEqual(receipt['error'], 'execution_authorization_denied')
        self.assertEqual(client.dispatch_status('cpu', 'restore-live')['history'][:-2], before['history'])
        self.account_worker_once(); self.account_worker_once(test=True)
        self.assertEqual(client.dispatch_status('cpu', 'restore-live')['status'], 'cancelled')


    def test_parallel_organization_context_and_cross_context_form_with_real_engine(self):
        token=secrets.token_urlsafe(32)
        self.private('alice-other.token',token)
        self.write_yaml('alice-other.yml',dict(origin='http://127.0.0.1:'+str(self.engine_port),
            allow_loopback_http=True,token_file='alice-other.token',timeout=5))
        self.engine_config['identities'].append(dict(token_sha256=hashlib.sha256(token.encode()).hexdigest(),subject='alice',organization='other'))
        self.engine_config['grants'].append(dict(subject='alice',organization='other',rule_kind='intervention',
            operations=OPERATIONS,scopes=[['server','lab']],providers=['internal','upload']))
        self.write_engine()
        other=RuleClient(self.root/'alice-other.yml',expected_actor=dict(subject='alice',organization='other'))
        self.addCleanup(other.close)
        second=other.create_from_templates(['cpu-high'],name='cpu',rule_kind='intervention',selections=[['server','lab']])
        self.identities.append(dict(username='alice',subject='alice',organization='other',atropos_client='alice-other.yml'))
        self.write_yaml('mapping.yml',dict(version=1,identities=self.identities))
        self.assertEqual(self.request('/ui/rules?name=cpu')[0],409)
        for organization,revision in (('demo',self.first['revision']),('other',second['revision'])):
            status,_,body=self.request('/ui/rules?'+urlencode(dict(name='cpu',organization=organization,revision=revision)))
            self.assertEqual(status,200,body);self.assertIn(revision,body)
        form=self.form('/ui/rules/edit?name=cpu&organization=demo')
        document=self.clients['alice'].draft('cpu')['document']
        status,_,body=self.request('/ui/rules/edit?organization=other',fields=dict(token=form,document=yaml.safe_dump(document),message='Wrong context'))
        self.assertEqual(status,403,body)
        self.assertEqual(other.draft('cpu')['revision'],second['revision'])
        self.assertEqual(self.clients['alice'].draft('cpu')['revision'],self.first['revision'])
        self.identities.pop();self.write_yaml('mapping.yml',dict(version=1,identities=self.identities))
        self.assertEqual(self.request('/ui/rules?name=cpu&organization=other')[0],403)
