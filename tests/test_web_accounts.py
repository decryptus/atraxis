import base64
import hashlib
import http.client
from html.parser import HTMLParser
from pathlib import Path
import socket
import tempfile
import threading
import unittest
from urllib.parse import urlencode
import yaml
from atraxis.accounts import Directory
from atraxis.account_store import AccountStore
from atraxis.config import load_config
from atraxis.server import _Handler,build_context
from test_account_delegation import fixture


class Tokens(HTMLParser):
    def __init__(self):super().__init__();self.tokens=[]
    def handle_starttag(self,tag,attrs):
        attrs=dict(attrs)
        if tag=='input' and attrs.get('name')=='token':self.tokens.append(attrs['value'])


class AccountWebTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup);self.root=Path(tmp.name)
        self.store=AccountStore(self.root/'directory.json');self.rev=self.store.apply(Directory(fixture()),None)
        self.profile=self.root/'admin.yml';self.write('admin.yml',dict(version=1,store='directory.json',identities=[
            dict(username='alice-login',subject='alice'),dict(username='bob-login',subject='bob')]))
        digest=base64.b64encode(hashlib.sha1(b'synthetic-password').digest()).decode()
        (self.root/'users').write_text('alice-login:{SHA}'+digest+'\nbob-login:{SHA}'+digest+'\n')
        with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        self.origin='http://127.0.0.1:'+str(port);self.port=port
        self.write('web.yml',dict(general=dict(auth_basic_file='users',listen_port=port,
            accounts_web_file='admin.yml',accounts_web_origin=self.origin)))
        self.context=build_context(load_config(self.root/'web.yml'));started=threading.Event()
        self.context.named_commands['GET /healthz'].at_start=lambda options:started.set()
        self.thread=threading.Thread(target=lambda:self.context.run(http_req_handler=_Handler),daemon=True)
        self.thread.start();self.addCleanup(self.stop);self.assertTrue(started.wait(5))

    def stop(self):self.context.stop();self.thread.join(7);self.assertFalse(self.thread.is_alive())
    def write(self,name,data):
        path=self.root/name;path.write_text(yaml.safe_dump(data));path.chmod(0o600)
    def request(self,path='/ui/accounts',*,user='alice-login',fields=None,origin=None):
        headers={}
        if user is not None:headers['Authorization']='Basic '+base64.b64encode((user+':synthetic-password').encode()).decode()
        if fields is not None:headers.update({'Content-Type':'application/x-www-form-urlencoded','Origin':origin or self.origin})
        conn=http.client.HTTPConnection('127.0.0.1',self.port,timeout=5)
        try:
            conn.request('POST' if fields is not None else 'GET',path,body=None if fields is None else urlencode(fields),headers=headers)
            response=conn.getresponse();return response.status,dict(response.getheaders()),response.read().decode()
        finally:conn.close()
    def token(self,body):
        parser=Tokens();parser.feed(body);self.assertEqual(len(parser.tokens),1);return parser.tokens[0]
    def review(self,*,operation='membership.add',user='alice-login',**kwargs):
        status,_,body=self.request('/ui/accounts/edit?'+urlencode({'operation':operation}),user=user)
        self.assertEqual(status,200,body)
        values=dict(token=self.token(body),operation=operation,organization='default',subject='charlie',group='ops')
        values.update(kwargs)
        return self.request('/ui/accounts/review',user=user,fields=values)

    def test_authentication_scoped_view_and_inventory_link(self):
        self.assertEqual(self.request(user=None)[0],401)
        self.assertEqual(self.request(user='bob-login')[0],403)
        status,headers,body=self.request();self.assertEqual(status,200)
        self.assertNotIn('secret',body);self.assertIn('Company group',body)
        self.assertEqual(headers['Cache-Control'],'no-store')
        self.assertIn('/ui/accounts',self.request('/ui')[2])

    def test_review_confirm_single_use_and_actor_audit(self):
        status,_,body=self.review();self.assertEqual(status,200,body)
        self.assertEqual(self.store.snapshot()['revision'],self.rev)
        token=self.token(body)
        self.assertEqual(self.request('/ui/accounts/commit',fields=dict(token=token,confirm='yes'))[0],303)
        self.assertEqual(self.request('/ui/accounts/commit',fields=dict(token=token,confirm='yes'))[0],403)
        self.assertEqual(self.store.snapshot()['audit'][-1]['actor']['subject'],'alice')

    def test_child_delegation_can_be_created_and_revoked_through_forms(self):
        _,_,body=self.request('/ui/accounts/edit?operation=delegation.create')
        values=dict(token=self.token(body),operation='delegation.create',parent='root',subject='bob',
            groups='night',operations='membership.add',organization_membership='no',delegable='no')
        status,_,body=self.request('/ui/accounts/review',fields=values);self.assertEqual(status,200,body)
        self.assertEqual(self.request('/ui/accounts/commit',fields=dict(token=self.token(body),confirm='yes'))[0],303)
        self.assertEqual(self.request(user='bob-login')[0],200)
        grant=self.store.snapshot()['directory']['delegations'][-1]['id']
        _,_,body=self.request('/ui/accounts/edit?operation=delegation.revoke')
        status,_,body=self.request('/ui/accounts/review',fields=dict(token=self.token(body),operation='delegation.revoke',grant=grant))
        self.assertEqual(status,200,body)
        self.assertEqual(self.request('/ui/accounts/commit',fields=dict(token=self.token(body),confirm='yes'))[0],303)
        self.assertEqual(self.request(user='bob-login')[0],403)

    def test_wrong_origin_cross_account_missing_confirmation_and_self_change_denied(self):
        status,_,body=self.review();self.assertEqual(status,200)
        token=self.token(body)
        self.assertEqual(self.request('/ui/accounts/commit',fields=dict(token=token,confirm='yes'),origin='https://foreign.invalid')[0],403)
        self.assertEqual(self.request('/ui/accounts/commit',fields=dict(token=token,confirm='yes'),user='bob-login')[0],403)
        self.assertEqual(self.review(subject='alice')[0],403)
        status,_,body=self.review()
        self.assertEqual(self.request('/ui/accounts/commit',fields=dict(token=self.token(body)))[0],403)
        self.assertEqual(self.store.snapshot()['revision'],self.rev)

    def test_revision_conflict_and_revocation_after_review(self):
        status,_,body=self.review();self.assertEqual(status,200)
        token=self.token(body);d=fixture();d['delegations'][0]['enabled']=False
        self.store.apply(Directory(d),self.rev)
        self.assertEqual(self.request('/ui/accounts/commit',fields=dict(token=token,confirm='yes'))[0],409)
        self.assertEqual(self.request()[0],403)
        self.assertEqual(Directory(self.store.snapshot()['directory']).effective_groups('charlie','default'),())

    def test_injected_identity_duplicate_fields_and_outside_scope_denied(self):
        self.assertEqual(self.review(group='secret')[0],403)
        self.assertEqual(self.review(subject_override='alice')[0],400)
        self.assertEqual(self.request('/ui/accounts?subject=alice')[0],400)
        self.assertEqual(self.store.snapshot()['revision'],self.rev)

class EntityWebTests(unittest.TestCase):
    stop=AccountWebTests.stop
    write=AccountWebTests.write
    request=AccountWebTests.request
    token=AccountWebTests.token
    # Reuse the real authenticated HTTP fixture without inheriting its test cases.
    def setUp(self):
        AccountWebTests.setUp(self)
        from test_account_entities import fixture
        self.rev=self.store.apply(Directory(fixture()),self.rev)

    def test_entity_review_cancel_commit_and_cross_account_token(self):
        _,_,body=self.request('/ui/accounts/edit?operation=user.create')
        fields=dict(token=self.token(body),operation='user.create',id='eve',name='Eve',enabled='yes')
        status,_,body=self.request('/ui/accounts/review',fields=fields)
        self.assertEqual(status,200,body);self.assertNotIn('eve',Directory(self.store.snapshot()['directory']).users)
        token=self.token(body)
        self.assertEqual(self.request('/ui/accounts/commit',user='bob-login',fields=dict(token=token,confirm='yes'))[0],403)
        _,_,body=self.request('/ui/accounts/edit?operation=user.create')
        fields['token']=self.token(body)
        status,_,body=self.request('/ui/accounts/review',fields=fields);self.assertEqual(status,200)
        token=self.token(body)
        self.assertEqual(self.request('/ui/accounts/commit',fields=dict(token=token,confirm='yes'))[0],303)
        self.assertEqual(self.request('/ui/accounts/commit',fields=dict(token=token,confirm='yes'))[0],403)
        self.assertEqual(self.request('/ui/accounts/search?q=eve')[0],200)
        self.assertEqual(self.store.snapshot()['audit'][-1]['actor']['operation'],'user.create')

    def test_entity_revocation_after_review_blocks_commit(self):
        _,_,body=self.request('/ui/accounts/edit?operation=tenant.create')
        status,_,body=self.request('/ui/accounts/review',fields=dict(token=self.token(body),operation='tenant.create',id='new',name='New',enabled='yes'))
        self.assertEqual(status,200,body)
        token=self.token(body);data=self.store.snapshot()['directory'];data['entity_delegations'][0]['enabled']=False
        self.store.apply(Directory(data),self.rev)
        self.assertEqual(self.request('/ui/accounts/commit',fields=dict(token=token,confirm='yes'))[0],409)
        self.assertEqual(self.request()[0],403)
