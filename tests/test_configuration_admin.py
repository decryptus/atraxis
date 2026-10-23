"""Explicit installation authority, review/CAS, private writes and HTTP forms."""
import base64
from copy import deepcopy
import hashlib
from html.parser import HTMLParser
import http.client
import json
import os
import socket
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

import yaml
from atraxis.accounts import default_directory, Directory, AccountError
from atraxis.account_store import AccountStore
from atraxis.config import load_config
from atraxis.configuration_admin import ConfigurationAdmin, MAX_HISTORY, fingerprint
from atraxis.doctor import diagnose
from atraxis.server import build_context, _Handler
from atraxis.web_configuration import WebConfiguration


class Tokens(HTMLParser):
    def __init__(self):super().__init__();self.values=[]
    def handle_starttag(self,tag,attrs):
        attrs=dict(attrs)
        if tag=='input' and attrs.get('name')=='token':self.values.append(attrs['value'])


class ConfigurationFixture(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup);self.root=Path(temp.name)
        directory=default_directory('alice').document()
        directory['users'].append(dict(id='bob',name='Bob',enabled=True))
        directory['memberships'].append(dict(user='bob',organization='default',group=None))
        self.store=AccountStore(self.root/'accounts.db');self.store.apply(Directory(directory),None)
        digest=base64.b64encode(hashlib.sha1(b'synthetic').digest()).decode()
        self.write('users','alice:{SHA}'+digest+'\nbob:{SHA}'+digest+'\n',raw=True)
        self.write('source.json',json.dumps(dict(username='reader',password='private-source-secret')),raw=True)
        self.profile=dict(version=1,store='accounts.db',identities=[dict(username=user,subject=user,organization='default') for user in ('alice','bob')],
            administrators=[dict(subject='alice',organization='default')],
            connections={'monitor':dict(kind='monit_docker',origin='https://monitor.invalid',credentials_file='source.json')})
        self.write('administration.yml',self.profile)
        self.document=dict(general=dict(auth_basic_file='users',configuration_web_file='administration.yml',configuration_web_origin='http://127.0.0.1:9810'),sources=[])
        self.path=self.root/'daemon.yml';self.save()
        self.admin=ConfigurationAdmin(self.root/'administration.yml',self.path,'alice')
        self.request=dict(operation='source.add',source_id='monitor-a',connection='monitor',timeout=2,poll_interval=15,max_age=45)

    def write(self,name,document,raw=False):
        path=self.root/name;path.write_text(document if raw else yaml.safe_dump(document));path.chmod(0o600)
    def save(self):self.write('daemon.yml',self.document)
    def prepare(self,request=None):return self.admin.prepare(request or self.request,self.admin.inspect()['revision'])


class ConfigurationAdminTests(ConfigurationFixture):
    def test_metric_selection_is_host_approved_and_cannot_be_overridden_in_request(self):
        metric = dict(name='orders_pending', match={'job': 'covenant-orders'}, labels=['job', 'instance'])
        self.profile['connections']['orders'] = dict(kind='prometheus', origin='https://prometheus.invalid',
                                                     credentials_file='source.json', metric=metric)
        self.write('administration.yml', self.profile)
        request = dict(self.request, connection='orders')
        with self.assertRaises(AccountError):
            self.prepare(dict(request, metric=dict(metric, name='unapproved_metric')))
        plan = self.prepare(request)
        self.admin.commit(plan)
        source = load_config(self.path).sources[0]
        self.assertEqual(source.kind, 'prometheus')
        self.assertEqual(source.metric, metric)

    def test_review_is_offline_and_commit_preserves_general_and_audits_atomically(self):
        before=self.path.read_bytes()
        with patch('socket.getaddrinfo',side_effect=AssertionError('No network during setup')):
            plan=self.prepare();self.assertEqual(self.path.read_bytes(),before)
            event=self.admin.commit(plan)
        result=yaml.safe_load(self.path.read_text())
        self.assertEqual(result['general'],self.document['general'])
        self.assertEqual(result['sources'][0]['source_id'],'monitor-a')
        self.assertEqual(result['configuration_history'][0]['id'],event)
        self.assertEqual(result['configuration_history'][0]['actor'],'alice')
        self.assertEqual(self.path.stat().st_mode&0o777,0o600)
        self.assertNotIn('private-source-secret',self.path.read_text())
        with self.assertRaises(AccountError):self.admin.commit(plan)

    def test_unmapped_or_non_admin_user_is_denied_even_with_membership(self):
        for user in ('bob','unknown'):
            with self.subTest(user=user),self.assertRaises(PermissionError):
                ConfigurationAdmin(self.root/'administration.yml',self.path,user).inspect()
        self.profile['administrators']=[];self.write('administration.yml',self.profile)
        with self.assertRaises(PermissionError):self.admin.inspect()

    def test_revision_profile_and_membership_changes_refuse_reviewed_write(self):
        plan=self.prepare()
        self.document['general']['max_workers']=3;self.save()
        with self.assertRaises(AccountError):self.admin.commit(plan)
        plan=self.prepare();self.profile['connections']['monitor']['origin']='https://changed.invalid'
        self.write('administration.yml',self.profile)
        with self.assertRaises(AccountError):self.admin.commit(plan)
        plan=self.prepare();snapshot=self.store.snapshot();directory=snapshot['directory'];directory['users'][0]['enabled']=False
        self.store.apply(Directory(directory),snapshot['revision'])
        with self.assertRaises(PermissionError):self.admin.commit(plan)
        self.assertEqual(yaml.safe_load(self.path.read_text())['sources'],[])

    def test_unknown_connection_urls_secrets_and_invalid_limits_are_rejected(self):
        for change in (dict(connection='not-approved'),dict(origin='https://override.invalid'),dict(credentials_file='/tmp/no'),
                       dict(timeout=True),dict(timeout=20,poll_interval=10),dict(max_age=0),dict(source_id='../x')):
            with self.subTest(change=change),self.assertRaises((AccountError,ValueError)):
                self.prepare(dict(self.request,**change))
        self.assertEqual(yaml.safe_load(self.path.read_text())['sources'],[])

    def test_update_keeps_other_limits_then_removal_retains_history(self):
        self.admin.commit(self.prepare())
        saved=yaml.safe_load(self.path.read_text());saved['sources'][0]['max_resources']=321
        self.write('daemon.yml',saved)
        request=dict(self.request,operation='source.update',max_age=90)
        self.admin.commit(self.prepare(request))
        self.assertEqual(yaml.safe_load(self.path.read_text())['sources'][0]['max_resources'],321)
        self.admin.commit(self.prepare(dict(operation='source.remove',source_id='monitor-a')))
        result=yaml.safe_load(self.path.read_text())
        self.assertEqual(result['sources'],[]);self.assertEqual(len(result['configuration_history']),3)

    def test_failed_replace_preserves_bytes_and_post_replace_fsync_is_uncertain(self):
        plan=self.prepare();before=self.path.read_bytes()
        with patch('atraxis.configuration_admin.os.replace',side_effect=OSError('synthetic')):
            with self.assertRaises(OSError):self.admin.commit(plan)
        self.assertEqual(self.path.read_bytes(),before)
        original=os.fsync;calls=[]
        def fsync(fd):
            calls.append(fd)
            if len(calls)==2:raise OSError('uncertain directory sync')
            return original(fd)
        with patch('atraxis.configuration_admin.os.fsync',side_effect=fsync):
            with self.assertRaises(OSError):self.admin.commit(plan)
        self.assertEqual(len(yaml.safe_load(self.path.read_text())['configuration_history']),1)
        with self.assertRaises(AccountError):self.admin.commit(plan)

    def test_profile_symlink_unsafe_directory_and_history_capacity_fail_closed(self):
        plan=self.prepare();self.root.chmod(0o755)
        with self.assertRaises(AccountError):self.admin.commit(plan)
        self.root.chmod(0o700)
        target=self.root/'real.yml';self.path.rename(target);self.path.symlink_to(target)
        with self.assertRaises(OSError):self.admin.inspect()
        self.path.unlink();target.rename(self.path)
        history=[dict(id=f'{i:032x}',actor='alice',organization='default',operation='source.add',source_id='old',previous='a'*64,time='2026-10-11T00:00:00+00:00') for i in range(MAX_HISTORY)]
        document=yaml.safe_load(self.path.read_text());document['configuration_history']=history;self.write('daemon.yml',document)
        with self.assertRaises(AccountError):self.admin.commit(self.prepare())
        self.assertEqual(len(yaml.safe_load(self.path.read_text())['configuration_history']),MAX_HISTORY)

    def test_competing_writers_publish_once_and_stale_read_is_rejected(self):
        plan=self.prepare();barrier=threading.Barrier(2);results=[]
        def write():
            barrier.wait()
            try:self.admin.commit(plan);results.append('saved')
            except AccountError:results.append('conflict')
        threads=[threading.Thread(target=write) for _ in range(2)]
        for thread in threads:thread.start()
        for thread in threads:thread.join(5);self.assertFalse(thread.is_alive())
        self.assertCountEqual(results,['saved','conflict'])
        self.assertEqual(len(yaml.safe_load(self.path.read_text())['configuration_history']),1)
        original=self.admin.state;calls=[]
        def state():
            if calls:
                document=yaml.safe_load(self.path.read_text());document['general']['max_workers']=3
                self.write('daemon.yml',document)
            calls.append(1);return original()
        with patch.object(self.admin,'state',side_effect=state):
            with self.assertRaises(AccountError):self.admin.inspect()

    def test_doctor_checks_configuration_authority_without_network_or_writes(self):
        before=self.path.read_bytes()
        with patch('socket.getaddrinfo',side_effect=AssertionError('No DNS')):
            report,code=diagnose(self.path)
        self.assertTrue(report['configuration_valid']);self.assertEqual(code,2)
        self.assertEqual(next(c['state'] for c in report['checks'] if c['component']=='configuration_admin'),'passed')
        self.assertEqual(self.path.read_bytes(),before)
        self.document['configuration_history']=[{}];self.save()
        self.assertFalse(diagnose(self.path)[0]['configuration_valid'])


class WebConfigurationTests(ConfigurationFixture):
    def setUp(self):
        super().setUp();self.web=WebConfiguration(self.root/'administration.yml',self.path,'http://127.0.0.1:9810',())
        self.headers={'origin':'http://127.0.0.1:9810','content-type':'application/x-www-form-urlencoded'}

    def get(self,path='/ui/configuration',user='alice'):
        return self.web.handle(path,user)
    def token(self,response):
        self.assertEqual(response[0],200,response[1]);parser=Tokens();parser.feed(response[1]);self.assertEqual(len(parser.values),1);return parser.values[0]
    def post(self,route,fields,*,user='alice',headers=None):
        return self.web.handle('/ui/configuration/'+route,user,method='POST',headers=headers or self.headers,raw=urlencode(fields).encode())
    def review(self):
        token=self.token(self.get('/ui/configuration/edit?operation=source.add'))
        return self.post('review',dict(token=token,source_id='monitor-a',connection='monitor',timeout='2',poll_interval='15',max_age='45'))

    def test_explicit_review_confirmation_pending_restart_and_replay_refusal(self):
        before=self.path.read_bytes();review=self.review();token=self.token(review)
        self.assertIn('Current saved source',review[1]);self.assertIn('Proposed source',review[1])
        self.assertNotIn('source.json',review[1]);self.assertNotIn('private-source-secret',review[1]);self.assertEqual(self.path.read_bytes(),before)
        response=self.post('commit',dict(token=token,confirm='yes'));self.assertEqual(response[0],303)
        self.assertIn('pending',self.get()[1]);self.assertEqual(self.post('commit',dict(token=token,confirm='yes'))[0],403)

    def test_cross_user_origin_missing_confirmation_and_duplicate_fields_refused(self):
        self.assertEqual(self.get(user='bob')[0],403)
        token=self.token(self.review())
        self.assertEqual(self.post('commit',dict(token=token,confirm='yes'),user='bob')[0],403)
        token=self.token(self.review())
        self.assertEqual(self.post('commit',dict(token=token,confirm='yes'),headers=dict(self.headers,origin='https://evil.invalid'))[0],403)
        self.assertEqual(self.post('commit',dict(token=token))[0],403)
        token=self.token(self.review())
        code,_,_=self.web.handle('/ui/configuration/commit','alice',method='POST',headers=self.headers,
            raw=urlencode(dict(token=token,confirm='yes')).encode()+b'&confirm=no')
        self.assertEqual(code,400);self.assertEqual(yaml.safe_load(self.path.read_text())['sources'],[])

    def test_revocation_and_concurrent_save_return_no_success(self):
        token=self.token(self.review());self.document['general']['max_workers']=2;self.save()
        self.assertEqual(self.post('commit',dict(token=token,confirm='yes'))[0],409)
        token=self.token(self.review());self.profile['administrators']=[dict(subject='bob',organization='default')];self.write('administration.yml',self.profile)
        self.assertEqual(self.post('commit',dict(token=token,confirm='yes'))[0],403)

    def test_real_authenticated_http_route_uses_existing_form_framing_guards(self):
        with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        self.document['general']['listen_port']=port;self.save()
        context=build_context(load_config(self.path));started=threading.Event()
        context.named_commands['GET /healthz'].at_start=lambda options:started.set()
        thread=threading.Thread(target=lambda:context.run(http_req_handler=_Handler),daemon=True);thread.start()
        self.addCleanup(lambda:(context.stop(),thread.join(7)))
        self.assertTrue(started.wait(5));server=context.server
        conn=http.client.HTTPConnection(*server.server_address,timeout=5)
        try:
            conn.request('GET','/ui/configuration');self.assertEqual(conn.getresponse().status,401)
        finally:conn.close()
        conn=http.client.HTTPConnection(*server.server_address,timeout=5)
        try:
            auth='Basic '+base64.b64encode(b'alice:synthetic').decode()
            conn.request('GET','/ui/configuration',headers={'Authorization':auth})
            response=conn.getresponse();self.assertEqual(response.status,200);self.assertIn(b'Source configuration',response.read())
        finally:conn.close()
