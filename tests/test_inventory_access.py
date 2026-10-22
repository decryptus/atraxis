"""Synthetic HTTP authorization, independent of sources and engine credentials."""
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
import yaml

from atraxis.account_store import AccountStore
from atraxis.accounts import Directory, default_directory, AccountError
from atraxis.config import load_config, ConfigurationError
from atraxis.inventory_access import InventoryAccess, load_profile, requested_organization
from atraxis.server import build_context, _Handler
from atraxis.service import ReadService
from test_web import inventory


def directory_fixture():
    data = default_directory('alice').document()
    data['organizations'] += [dict(id='second', name='Second organization', tenant='default', enabled=True),
                              dict(id='hidden', name='Hidden organization', tenant='default', enabled=True)]
    data['users'].append(dict(id='bob', name='Bob', enabled=True))
    data['memberships'] += [dict(user='alice', organization='second', group=None),
                            dict(user='bob', organization='hidden', group=None)]
    data['groups'] = [dict(id='ops', name='Operators', organization='default', parent=None, inherit_parent=False, enabled=True),
                      dict(id='night', name='Night', organization='default', parent='ops', inherit_parent=True, enabled=True)]
    data['memberships'].append(dict(user='alice', organization='default', group='ops'))
    return data


def profile_fixture():
    return dict(version=3, administrators=[], audit=[], servers=[], resource_groups=[], store='directory.json', identities=[dict(username='alice', subject='alice'), dict(username='bob', subject='bob')], grants=[
        dict(organization='default', subject_kind='group', subject='night', targets=[dict(kind='source',id='docker-lab')]),
        dict(organization='second', subject_kind='user', subject='alice', targets=[dict(kind='source',id='second-source')]),
        dict(organization='hidden', subject_kind='user', subject='bob', targets=[dict(kind='source',id='hidden-source')])])


class FixtureService(ReadService):
    def __init__(self):
        super().__init__()
        self.payload = inventory()
        self.payload.update(schema_version=1, collecting=True)
        for source_id in ('second-source','hidden-source'):
            source = deepcopy(self.payload['sources'][0]);source['source_id'] = source_id
            source['last_valid_snapshot']['resources'][0]['fields']['name'] = source_id+'-secret-marker'
            source['last_valid_snapshot']['source_metadata'] = dict(marker=source_id+'-metadata')
            self.payload['sources'].append(source)
    def status(self):
        return deepcopy(self.payload)


class InventoryAccessTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup); self.root=Path(tmp.name)
        self.store=AccountStore(self.root/'directory.json'); self.store.apply(Directory(directory_fixture()),None)
        self.profile=self.root/'access.yml'; self.write_profile(profile_fixture())
        self.access=InventoryAccess(self.profile); self.service=FixtureService()
    def write_profile(self,data):
        self.profile.write_text(yaml.safe_dump(data));self.profile.chmod(0o600)
    def change(self, mutate):
        snap=self.store.snapshot();data=snap['directory'];mutate(data)
        self.store.apply(Directory(data),snap['revision'])

    def test_explicit_context_and_no_membership_implied_read_grant(self):
        scope,payload=self.access.snapshot('alice',None,self.service)
        self.assertIsNone(payload);self.assertEqual([c['id'] for c in scope['organizations']],['default','second'])
        self.write_profile(dict(profile_fixture(),grants=[]))
        scope,payload=self.access.snapshot('alice','default',self.service)
        self.assertEqual(payload['sources'],[]);self.assertEqual(payload['reason'],'no_visible_sources')
        for user,org in [('alice','hidden'),('unknown','default'),('bob','default')]:
            with self.assertRaises(PermissionError):self.access.snapshot(user,org,self.service)

    def test_projection_hides_metadata_counts_and_unrelated_failure(self):
        self.service.payload['sources'][2]['current']=False
        self.service.payload.update(ready=False,reason='sources_not_current',global_secret='marker')
        _,result=self.access.snapshot('alice','default',self.service)
        self.assertTrue(result['ready']);self.assertEqual(len(result['sources']),1)
        self.assertNotIn('hidden',json.dumps(result));self.assertNotIn('second-source',json.dumps(result));self.assertNotIn('global_secret',result)
        _,bob=self.access.snapshot('bob',None,self.service)
        self.assertEqual(bob['context']['organization'],'hidden');self.assertFalse(bob['ready'])

    def test_revoked_group_and_disabled_ancestors_fail_closed(self):
        self.change(lambda d:d['groups'][0].update(enabled=False))
        self.assertEqual(self.access.snapshot('alice','default',self.service)[1]['sources'],[])
        self.change(lambda d:d['tenants'][0].update(enabled=False))
        with self.assertRaises(PermissionError):self.access.snapshot('alice','default',self.service)

    def test_membership_revoke_and_profile_change_during_read(self):
        original=self.service.status
        def revoke():
            self.change(lambda d:d.update(memberships=[m for m in d['memberships'] if m['organization']!='second']))
            return original()
        with patch.object(self.service,'status',side_effect=revoke):
            with self.assertRaises(PermissionError):self.access.snapshot('alice','second',self.service)
        def modify():
            self.write_profile(dict(profile_fixture(),grants=[]));return original()
        with patch.object(self.service,'status',side_effect=modify):
            with self.assertRaises(PermissionError):self.access.snapshot('alice','default',self.service)

    def test_historical_unknown_empty_and_stopped_preserved(self):
        source=self.service.payload['sources'][0];source.update(current=False,availability='unavailable')
        _,result=self.access.snapshot('alice','default',self.service)
        self.assertFalse(result['ready']);self.assertIsNotNone(result['sources'][0]['last_valid_snapshot'])
        source['last_valid_snapshot']=None
        self.assertIsNone(self.access.snapshot('alice','default',self.service)[1]['sources'][0]['last_valid_snapshot'])
        self.service.payload['collecting']=False
        self.assertEqual(self.access.snapshot('alice','default',self.service)[1]['reason'],'collector_stopped')

    def test_malformed_private_profiles_and_missing_authority(self):
        for mutation in (lambda d:d.update(version=True), lambda d:d['grants'][0].update(subject_kind=[]),
                         lambda d:d['identities'].append(d['identities'][0]),lambda d:d.update(override=True)):
            data=profile_fixture();mutation(data);self.write_profile(data)
            with self.assertRaises(AccountError):load_profile(self.profile)
        self.write_profile(profile_fixture());self.profile.chmod(0o644)
        with self.assertRaises(AccountError):self.access.resolve('alice','default')
        self.profile.chmod(0o600);self.store.path.unlink()
        with self.assertRaises(AccountError):self.access.resolve('alice','default')

    def test_context_query_strictness(self):
        for query in ('organization=','organization=default&organization=second','subject=alice','organization=%FF','organization=x/y','organization=default&ignored=x'):
            with self.assertRaises((ValueError,TypeError)):requested_organization('/ui?'+query)
        self.assertEqual(requested_organization('/ui?organization=default'),'default')


class InventoryWebTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup);self.root=Path(tmp.name)
        self.store=AccountStore(self.root/'directory.json');self.store.apply(Directory(directory_fixture()),None)
        self.profile=self.root/'access.yml';self.profile.write_text(yaml.safe_dump(profile_fixture()));self.profile.chmod(0o600)
        digest=base64.b64encode(hashlib.sha1(b'synthetic-password').digest()).decode()
        (self.root/'users').write_text('alice:{SHA}'+digest+'\nbob:{SHA}'+digest+'\nunmapped:{SHA}'+digest+'\n')
        with socket.socket() as sock:sock.bind(('127.0.0.1',0));self.port=sock.getsockname()[1]
        config=self.root/'web.yml';config.write_text(yaml.safe_dump(dict(general=dict(auth_basic_file='users',listen_port=self.port,inventory_access_file='access.yml'))))
        self.service=FixtureService();self.context=build_context(load_config(config),self.service)
        started=threading.Event();self.context.named_commands['GET /healthz'].at_start=lambda options:started.set()
        self.thread=threading.Thread(target=lambda:self.context.run(http_req_handler=_Handler),daemon=True)
        self.thread.start();self.addCleanup(self.stop);self.assertTrue(started.wait(5))
    def stop(self):self.context.stop();self.thread.join(7);self.assertFalse(self.thread.is_alive())
    def request(self,path,user='alice',method='GET'):
        headers={} if user is None else {'Authorization':'Basic '+base64.b64encode((user+':synthetic-password').encode()).decode()}
        conn=http.client.HTTPConnection('127.0.0.1',self.port,timeout=5)
        try:
            conn.request(method,path,headers=headers);r=conn.getresponse();return r.status,dict(r.getheaders()),r.read().decode()
        finally:conn.close()

    def test_selection_scope_refresh_and_api_do_not_leak(self):
        code,headers,body=self.request('/ui');self.assertEqual(code,200)
        self.assertIn('Select an organization',body);self.assertNotIn('worker',body);self.assertNotIn('Hidden organization',body);self.assertNotIn('hidden-source',body)
        for path in ('/ui','/v1/status','/readyz'):
            code,headers,body=self.request(path+'?organization=default');self.assertEqual(code,200,body)
            self.assertNotIn('second-source',body);self.assertNotIn('Hidden organization',body);self.assertNotIn('hidden-source',body);self.assertEqual(headers['Cache-Control'],'no-store')
            self.assertEqual(self.request(path+'?organization=hidden')[0],403)
            self.assertEqual(self.request(path,user='unmapped')[0],403)
            self.assertEqual(self.request(path,user=None)[0],401)
            self.assertEqual(self.request(path+'?organization=hidden',method='HEAD')[0],403)
        self.assertIn('href="/ui?organization=default">Refresh',self.request('/ui?organization=default')[2])
        self.assertEqual(self.request('/v1/status')[0],409)
        self.assertEqual(self.request('/readyz')[0],409)
        self.assertEqual(self.request('/v1/status',user='bob')[0],200)

    def test_live_revocation_unavailable_config_and_bad_query(self):
        snap=self.store.snapshot();d=snap['directory'];d['users'][0]['enabled']=False
        self.store.apply(Directory(d),snap['revision'])
        self.assertEqual(self.request('/ui?organization=default')[0],403)
        self.profile.unlink()
        self.assertEqual(self.request('/v1/status?organization=default')[0],503)
        self.assertEqual(self.request('/ui?organization=default&organization=second')[0],400)
        self.assertEqual(self.request('/healthz')[0],200)
