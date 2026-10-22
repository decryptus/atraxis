"""Bounded inventory grant edits, atomic audit and real authenticated web forms."""
import base64
from copy import deepcopy
import hashlib
import http.client
import json
from pathlib import Path
import socket
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import urlencode
import yaml

from atraxis.accounts import AccountError,Directory
from atraxis.account_store import AccountStore
from atraxis.inventory_access import InventoryAccess,load_profile
from atraxis.inventory_admin import InventoryAdmin,fingerprint
from atraxis.config import load_config
from atraxis.server import build_context,_Handler
from test_resource_scopes import partial_profile,observation_service,CONTAINER
from test_inventory_access import directory_fixture
from test_web_accounts import Tokens


def admin_profile():
    data=partial_profile()
    data['administrators']=[dict(subject='alice',organization='default',targets=[dict(kind='server',id='host-a')])]
    return data


def candidate():
    return dict(operation='grant.add',organization='default',subject_kind='user',subject='bob',
                targets=[dict(CONTAINER,id='b'*64)])


class GrantAdminTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup);self.root=Path(tmp.name)
        directory=directory_fixture();directory['memberships'].append(dict(user='bob',organization='default',group=None))
        self.store=AccountStore(self.root/'directory.json');self.store.apply(Directory(directory),None)
        self.profile=self.root/'access.yml';self.write(admin_profile());self.admin=InventoryAdmin(self.profile,'alice')
    def write(self,data):self.profile.write_text(yaml.safe_dump(data));self.profile.chmod(0o600)
    def prepare(self,request=None):return self.admin.prepare(request or candidate(),load_profile(self.profile)[2])

    def test_explicit_admin_bound_and_scoped_listing(self):
        with self.assertRaises(PermissionError):InventoryAdmin(self.profile,'bob').inspect()
        data=self.admin.inspect();self.assertEqual([o['id'] for o in data['organizations']],['default'])
        self.assertNotIn('hidden-source',json.dumps(data));self.assertNotIn('second-source',json.dumps(data))
        for request in (dict(candidate(),organization='second'),dict(candidate(),targets=[dict(kind='source',id='extra-lab')]),
                        dict(candidate(),targets=[dict(kind='group',id='applications')]),dict(candidate(),subject='unknown')):
            with self.assertRaises(PermissionError):self.prepare(request)
        data=admin_profile();data['administrators'][0]['targets']=[CONTAINER];self.write(data)
        with self.assertRaises(PermissionError):self.prepare(dict(candidate(),targets=[dict(kind='server',id='host-a')]))

    def test_atomic_grant_and_audit_are_immediately_used_by_reader(self):
        old=self.profile.read_bytes();plan=self.prepare();self.assertEqual(self.profile.read_bytes(),old)
        event=self.admin.commit(plan);profile,_,_=load_profile(self.profile)
        self.assertEqual(len(profile['audit']),1);self.assertEqual(profile['audit'][0]['id'],event)
        self.assertEqual(profile['audit'][0]['actor'],'alice');self.assertEqual(profile['audit'][0]['grant'],plan['grant'])
        _,view=InventoryAccess(self.profile).snapshot('bob','default',observation_service())
        self.assertEqual(view['sources'][0]['last_valid_snapshot']['resources'][0]['native_id'],'b'*64)
        self.assertEqual(self.profile.stat().st_mode&0o777,0o600)
        with self.assertRaises((AccountError,PermissionError)):self.admin.commit(plan)
        self.assertEqual(len(load_profile(self.profile)[0]['audit']),1)

    def test_removal_keeps_other_grants_and_records_audit(self):
        self.admin.commit(self.prepare());profile=load_profile(self.profile)[0]
        grant=next(g for g in profile['grants'] if g['organization']=='default' and g['subject']=='bob')
        self.admin.commit(self.prepare(dict(operation='grant.remove',grant=fingerprint(grant))))
        profile=load_profile(self.profile)[0];self.assertEqual(len(profile['audit']),2)
        self.assertEqual(profile['audit'][-1]['operation'],'grant.remove')
        self.assertTrue(any(g['subject']=='night' for g in profile['grants']))
        self.assertEqual(InventoryAccess(self.profile).snapshot('bob','default',observation_service())[1]['sources'],[])

    def test_concurrent_reviews_exact_profile_and_directory_revision(self):
        first=self.prepare();second=self.prepare(dict(candidate(),targets=[dict(CONTAINER,id='c'*64)]))
        self.admin.commit(first)
        with self.assertRaises(AccountError):self.admin.commit(second)
        plan=self.prepare(dict(candidate(),targets=[dict(CONTAINER,id='d'*64)]))
        snap=self.store.snapshot();self.store.apply(Directory(snap['directory']),snap['revision'])
        with self.assertRaises(AccountError):self.admin.commit(plan)

    def test_simultaneous_writers_only_one_commits(self):
        from concurrent.futures import ThreadPoolExecutor
        plans=[self.prepare(),self.prepare(dict(candidate(),targets=[dict(CONTAINER,id='c'*64)]))]
        barrier=threading.Barrier(2)
        def commit(plan):
            barrier.wait(5)
            try:self.admin.commit(plan);return 'saved'
            except AccountError:return 'conflict'
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures=[pool.submit(commit,p) for p in plans]
            self.assertEqual(sorted(f.result(timeout=5) for f in futures),['conflict','saved'])
        self.assertEqual(len(load_profile(self.profile)[0]['audit']),1)

    def test_revoked_admin_membership_and_mapping_rechecked(self):
        plan=self.prepare();data=admin_profile();data['administrators']=[];self.write(data)
        with self.assertRaises(PermissionError):self.admin.commit(plan)
        self.write(admin_profile());plan=self.prepare();snap=self.store.snapshot();data=snap['directory'];data['users'][0]['enabled']=False
        self.store.apply(Directory(data),snap['revision'])
        with self.assertRaises(PermissionError):self.admin.commit(plan)
        self.assertEqual(load_profile(self.profile)[0]['audit'],[])

    def test_recipient_group_and_no_catalog_or_identity_overrides(self):
        self.admin.commit(self.prepare(dict(candidate(),subject_kind='group',subject='ops')))
        with self.assertRaises(PermissionError):self.prepare(dict(candidate(),subject_kind='group',subject='missing'))
        with self.assertRaises(AccountError):self.prepare(dict(candidate(),administrators=[]))
        with self.assertRaises(AccountError):self.prepare(dict(candidate(),store='other.json'))

    def test_write_failure_before_replace_preserves_whole_profile(self):
        plan=self.prepare();original=self.profile.read_bytes()
        with patch('atraxis.inventory_admin.os.replace',side_effect=OSError('synthetic')):
            with self.assertRaises(OSError):self.admin.commit(plan)
        self.assertEqual(self.profile.read_bytes(),original)
        self.assertEqual(list(self.root.glob('.inventory-*')),[])

    def test_audit_capacity_and_invalid_v2_fail_closed(self):
        plan=self.prepare()
        with patch('atraxis.inventory_admin.MAX_GRANT_AUDIT',0):
            with self.assertRaises(AccountError):self.admin.commit(plan)
        data=admin_profile();data['version']=2;self.write(data)
        with self.assertRaises(AccountError):load_profile(self.profile)

    def test_scoped_audit_does_not_reveal_unknown_or_broader_catalog(self):
        self.admin.commit(self.prepare());data=load_profile(self.profile)[0]
        data['administrators'][0]['targets']=[CONTAINER];self.write(data)
        self.assertEqual(self.admin.inspect()['audit'],[])


class GrantWebTests(unittest.TestCase):
    def setUp(self):
        self.domain=GrantAdminTests();self.domain.setUp();self.addCleanup(self.domain.doCleanups)
        self.root=self.domain.root;self.profile=self.domain.profile;self.store=self.domain.store
        digest=base64.b64encode(hashlib.sha1(b'synthetic-password').digest()).decode()
        (self.root/'users').write_text('alice:{SHA}'+digest+'\nbob:{SHA}'+digest+'\n')
        with socket.socket() as sock:sock.bind(('127.0.0.1',0));self.port=sock.getsockname()[1]
        self.origin='http://127.0.0.1:'+str(self.port)
        config=self.root/'web.yml';config.write_text(yaml.safe_dump(dict(general=dict(auth_basic_file='users',listen_port=self.port,
            inventory_access_file='access.yml',inventory_admin_origin=self.origin))))
        self.context=build_context(load_config(config),observation_service());started=threading.Event()
        self.context.named_commands['GET /healthz'].at_start=lambda options:started.set()
        self.thread=threading.Thread(target=lambda:self.context.run(http_req_handler=_Handler),daemon=True)
        self.thread.start();self.addCleanup(self.stop);self.assertTrue(started.wait(5))
    def stop(self):self.context.stop();self.thread.join(7);self.assertFalse(self.thread.is_alive())
    def request(self,path='/ui/inventory-access',user='alice',fields=None,origin=None,method=None):
        headers={} if user is None else {'Authorization':'Basic '+base64.b64encode((user+':synthetic-password').encode()).decode()}
        if fields is not None:headers.update({'Content-Type':'application/x-www-form-urlencoded','Origin':origin or self.origin})
        conn=http.client.HTTPConnection('127.0.0.1',self.port,timeout=5)
        try:
            conn.request(method or ('POST' if fields is not None else 'GET'),path,body=urlencode(fields) if fields is not None else None,headers=headers)
            r=conn.getresponse();return r.status,dict(r.getheaders()),r.read().decode()
        finally:conn.close()
    def token(self,body):
        parser=Tokens();parser.feed(body);self.assertEqual(len(parser.tokens),1);return parser.tokens[0]
    def review(self,**overrides):
        code,_,body=self.request('/ui/inventory-access/edit?organization=default');self.assertEqual(code,200,body)
        values=dict(token=self.token(body),subject_kind='user',subject='bob',kind='container',source='docker-lab',id='b'*64)
        values.update(overrides)
        return self.request('/ui/inventory-access/review',fields=values)

    def test_authentication_and_scoped_page(self):
        self.assertEqual(self.request(user=None)[0],401);self.assertEqual(self.request(user='bob')[0],403)
        code,headers,body=self.request();self.assertEqual(code,200);self.assertEqual(headers['Cache-Control'],'no-store')
        self.assertNotIn('hidden-source',body);self.assertNotIn('second-source',body)
        self.assertIn('/ui/inventory-access',self.request('/ui?organization=default')[2])
        self.assertEqual(self.request('/ui/inventory-access/edit?organization=second')[0],403)

    def test_review_confirm_no_replay_and_remove(self):
        code,_,body=self.review();self.assertEqual(code,200,body);token=self.token(body)
        self.assertEqual(load_profile(self.profile)[0]['audit'],[])
        self.assertEqual(self.request('/ui/inventory-access/commit',fields=dict(token=token,confirm='yes'))[0],303)
        self.assertEqual(self.request('/ui/inventory-access/commit',fields=dict(token=token,confirm='yes'))[0],403)
        profile=load_profile(self.profile)[0];grant=profile['grants'][-1]
        code,_,body=self.request('/ui/inventory-access/remove?grant='+fingerprint(grant));self.assertEqual(code,200)
        self.assertEqual(len(load_profile(self.profile)[0]['audit']),1)
        self.assertEqual(self.request('/ui/inventory-access/commit',fields=dict(token=self.token(body),confirm='yes'))[0],303)
        self.assertEqual(len(load_profile(self.profile)[0]['audit']),2)

    def test_wrong_origin_cross_account_confirmation_and_injection_denied(self):
        _,_,body=self.review();token=self.token(body)
        self.assertEqual(self.request('/ui/inventory-access/commit',fields=dict(token=token,confirm='yes'),origin='https://other.test')[0],403)
        self.assertEqual(self.request('/ui/inventory-access/commit',user='bob',fields=dict(token=token,confirm='yes'))[0],403)
        _,_,body=self.review()
        self.assertEqual(self.request('/ui/inventory-access/commit',fields=dict(token=self.token(body)))[0],403)
        self.assertEqual(self.review(subject='alice',kind='source',source='',id='extra-lab')[0],403)
        self.assertEqual(self.review(actor='bob')[0],400)
        self.assertEqual(load_profile(self.profile)[0]['audit'],[])

    def test_stale_profile_and_revoked_admin_after_review(self):
        _,_,body=self.review();token=self.token(body)
        data=load_profile(self.profile)[0];data['grants'].pop();self.domain.write(data)
        self.assertEqual(self.request('/ui/inventory-access/commit',fields=dict(token=token,confirm='yes'))[0],409)
        _,_,body=self.review();data=load_profile(self.profile)[0];data['administrators']=[];self.domain.write(data)
        self.assertEqual(self.request('/ui/inventory-access/commit',fields=dict(token=self.token(body),confirm='yes'))[0],403)

    def test_unconfirmed_after_atomic_replace_is_not_retried(self):
        _,_,body=self.review();token=self.token(body)
        real_fsync=__import__('os').fsync;calls=[]
        def fail_directory(descriptor):
            calls.append(descriptor)
            if len(calls)==2:raise OSError('synthetic')
            return real_fsync(descriptor)
        with patch('atraxis.inventory_admin.os.fsync',side_effect=fail_directory):
            code,_,body=self.request('/ui/inventory-access/commit',fields=dict(token=token,confirm='yes'))
        self.assertEqual(code,503);self.assertIn('Outcome unconfirmed',body)
        self.assertEqual(len(load_profile(self.profile)[0]['audit']),1)
        self.assertEqual(self.request('/ui/inventory-access/commit',fields=dict(token=token,confirm='yes'))[0],403)
