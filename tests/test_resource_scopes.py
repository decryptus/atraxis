"""Exact resource identity, catalog bounds, redaction and real HTTP scope checks."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import yaml

from atraxis.accounts import AccountError, Directory
from atraxis.account_store import AccountStore
from atraxis.client import validate_inventory, render_inventory as render_cli
from atraxis.parsing import InvalidPayload
from atraxis.inventory_access import InventoryAccess, load_profile
from atraxis.resource_scopes import ResourceScopes, MAX_TARGETS
from atraxis.service import ReadService
from atraxis.web import render_inventory
import test_inventory_access as fixtures
from test_collection import source, result
from test_observations import monit, certlord

CONTAINER = dict(kind='container', source='docker-lab', id='a'*64)
CERTIFICATE = dict(kind='certificate', source='cert-lab', id='12345678-1234-4234-8234-123456789abc')


def partial_profile():
    data=fixtures.profile_fixture()
    data['servers']=[dict(id='host-a',organization='default',sources=['docker-lab']),
                     dict(id='host-b',organization='default',sources=['extra-lab'])]
    data['resource_groups']=[dict(id='applications',organization='default',targets=[CONTAINER,CERTIFICATE]),
                             dict(id='fleet',organization='default',targets=[dict(kind='server',id='host-a'),dict(kind='server',id='host-b')])]
    data['grants'][0]['targets']=[CONTAINER]
    return json.loads(json.dumps(data))


def observation_service():
    service=ReadService((source('docker-lab'),source('extra-lab'),source('cert-lab',kind='certlord')),clock=lambda:100.)
    service.set_running(True)
    payload=monit();hidden=deepcopy(payload['containers'][0]);hidden.update(id='b'*64,name='private-container-marker')
    payload['containers'].append(hidden)
    service.record('docker-lab',result('docker-lab',payload))
    service.record('extra-lab',result('extra-lab',payload))
    certs=certlord();hidden_cert=deepcopy(certs['certificates'][0]);hidden_cert.update(
        certificate_id='22222222-2222-4222-8222-222222222222',domains=['private-domain-marker.test'])
    certs['certificates'].append(hidden_cert)
    service.record('cert-lab',result('cert-lab',certs))
    return service


class ResourceScopeTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup);self.root=Path(tmp.name)
        self.store=AccountStore(self.root/'directory.json');self.store.apply(Directory(fixtures.directory_fixture()),None)
        self.profile=self.root/'access.yml';self.data=partial_profile();self.write()
        self.access=InventoryAccess(self.profile);self.service=observation_service()
    def write(self):self.profile.write_text(yaml.safe_dump(self.data));self.profile.chmod(0o600)
    def read(self):return self.access.snapshot('alice','default',self.service)[1]

    def test_exact_source_kind_id_and_no_names_or_prefix_matching(self):
        value=self.read();self.assertEqual(len(value['sources']),1)
        rows=value['sources'][0]['last_valid_snapshot']['resources']
        self.assertEqual([(r['source_id'],r['kind'],r['native_id']) for r in rows],[('docker-lab','container','a'*64)])
        self.assertNotIn('private-container-marker',json.dumps(value))
        self.data['grants'][0]['targets']=[dict(CONTAINER,id='c'*64)];self.write()
        empty=self.read();self.assertEqual(empty['sources'][0]['last_valid_snapshot']['resources'],[])
        self.assertTrue(empty['ready']) # Fresh collection, not an assertion of existence/health.
        self.assertIn('No selected resources are present',render_inventory(empty))
        replacement=monit();replacement['containers'][0]['id']='c'*64
        self.data['grants'][0]['targets']=[CONTAINER];self.write()
        self.service.record('docker-lab',result('docker-lab',replacement))
        self.assertEqual(self.read()['sources'][0]['last_valid_snapshot']['resources'],[])

    def test_global_metadata_unknown_fields_and_counts_are_not_released(self):
        raw=self.service.status();raw['sources'][0]['surprise']='source-marker'
        raw['sources'][0]['last_attempt']['source_metadata']['actions']=dict(executed=91919)
        raw['sources'][0]['last_attempt']['surprise']='attempt-marker'
        raw['sources'][0]['last_valid_snapshot']['source_metadata']['cycles_total']=81818
        raw['sources'][0]['last_valid_snapshot']['surprise']='snapshot-marker'
        with patch.object(self.service,'status',return_value=raw):value=self.read()
        serialized=json.dumps(value)
        for marker in ('private-container-marker','91919','81818','source-marker','attempt-marker','snapshot-marker'):
            self.assertNotIn(marker,serialized)
        selected=value['sources'][0]
        self.assertIsNone(selected['last_attempt']['source_metadata']);self.assertIsNone(selected['last_valid_snapshot']['source_metadata'])
        self.assertIn('Visible resources: 1',render_inventory(value));self.assertIn('Selected resources only',render_cli(validate_inventory(value)))
        self.assertEqual(validate_inventory(value)['sources'][0]['visibility'],'resources')

    def test_certificates_and_named_resource_group_union(self):
        self.data['grants'][0]['targets']=[dict(kind='group',id='applications')];self.write()
        value=self.read();self.assertEqual(len(value['sources']),2)
        self.assertTrue(all(len(s['last_valid_snapshot']['resources'])==1 for s in value['sources']))
        self.assertNotIn('private-domain-marker',json.dumps(value));validate_inventory(value)

    def test_server_and_server_group_whole_sources_explicit_breadth(self):
        self.data['grants'][0]['targets']=[dict(kind='server',id='host-a')];self.write()
        value=self.read();self.assertEqual(len(value['sources']),1)
        self.assertEqual(len(value['sources'][0]['last_valid_snapshot']['resources']),2)
        self.assertIsNotNone(value['sources'][0]['last_valid_snapshot']['source_metadata'])
        self.data['grants'][0]['targets']=[dict(kind='group',id='fleet')];self.write()
        self.assertEqual({s['source_id'] for s in self.read()['sources']},{'docker-lab','extra-lab'})

    def test_broad_and_narrow_grants_union_without_duplicate_rows(self):
        grant=deepcopy(self.data['grants'][0]);grant['targets']=[dict(CONTAINER,id='b'*64)]
        self.data['grants'].append(grant);self.write()
        value=self.read();self.assertEqual(len(value['sources'][0]['last_valid_snapshot']['resources']),2)
        self.assertEqual(value['sources'][0]['visibility'],'resources')
        self.data['grants'].append(dict(grant,targets=[dict(kind='source',id='docker-lab')]));self.write()
        self.assertEqual(self.read()['sources'][0]['visibility'],'source')
        del self.data['grants'][-1];self.write()
        self.assertEqual(self.read()['sources'][0]['visibility'],'resources')

    def test_historical_unknown_empty_and_failed_source_keep_meaning(self):
        self.service.record('docker-lab',result('docker-lab',outcome='timeout'))
        value=self.read();self.assertFalse(value['ready']);self.assertEqual(len(value['sources'][0]['last_valid_snapshot']['resources']),1)
        self.assertNotIn('private-container-marker',render_inventory(value));validate_inventory(value)
        self.service=ReadService((source('docker-lab'),),clock=lambda:100.);self.service.set_running(True)
        self.assertIsNone(self.read()['sources'][0]['last_valid_snapshot'])
        payload=monit();payload['containers']=[];self.service.record('docker-lab',result('docker-lab',payload))
        self.assertEqual(self.read()['sources'][0]['last_valid_snapshot']['resources'],[])

    def test_catalog_change_and_membership_revocation_immediately_reduce_scope(self):
        self.data['grants'][0]['targets']=[dict(kind='group',id='applications')];self.write()
        self.data['resource_groups'][0]['targets']=[CERTIFICATE];self.write()
        self.assertEqual([s['source_id'] for s in self.read()['sources']],['cert-lab'])
        snap=self.store.snapshot();d=snap['directory'];d['groups'][0]['enabled']=False
        self.store.apply(Directory(d),snap['revision'])
        self.assertEqual(self.read()['sources'],[])

    def test_profile_version_invalid_targets_cross_org_and_group_recursion_denied(self):
        changes=[lambda d:d.update(version=1),
                 lambda d:d['grants'][0].update(targets=[dict(CONTAINER,id='a'*12)]),
                 lambda d:d['grants'][0].update(targets=[dict(CONTAINER,id='demo')]),
                 lambda d:d['grants'][0].update(targets=[dict(CERTIFICATE,id='invalid')]),
                 lambda d:d['grants'][0].update(targets=[dict(kind='group',id='missing')]),
                 lambda d:d['resource_groups'][0].update(targets=[dict(kind='group',id='fleet')]),
                 lambda d:d['resource_groups'][0].update(organization='second'),
                 lambda d:d['servers'][0].update(organization='second'),
                 lambda d:d['servers'].append(dict(id='other',organization='second',sources=['docker-lab'])),
                 lambda d:d['grants'][0].update(targets=[CONTAINER,CONTAINER]),
                 lambda d:d['grants'][0].update(targets=[dict(CONTAINER,extra=True)]),
                 lambda d:d['grants'][0].update(targets=[dict(CONTAINER,id=[])])]
        for change in changes:
            self.data=partial_profile();change(self.data);self.write()
            with self.assertRaises(AccountError):load_profile(self.profile)

    def test_expansion_limits_reject_before_projection(self):
        self.data['grants'][0]['targets']=[dict(kind='source',id=str(i)) for i in range(MAX_TARGETS+1)];self.write()
        with self.assertRaises(AccountError):load_profile(self.profile)
        catalog=ResourceScopes([],[])
        with self.assertRaises(AccountError):catalog.expand('default',[dict(kind='group',id='unknown')])

    def test_client_rejects_partial_aggregate_metadata_or_missing_context(self):
        value=self.read();value['sources'][0]['last_attempt']['source_metadata']=dict(cycles_total=5)
        with self.assertRaises(InvalidPayload):validate_inventory(value)
        value=self.read();del value['context']
        with self.assertRaises(InvalidPayload):validate_inventory(value)


class ResourceHTTPTests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixtures.InventoryWebTests();self.addCleanup(self.fixture.doCleanups);self.fixture.setUp()
        self.fixture.profile.write_text(yaml.safe_dump(partial_profile()))
        self.fixture.service.payload=observation_service().status()
    def test_html_json_readiness_head_and_hidden_targets(self):
        for path in ('/ui','/v1/status','/readyz'):
            code,headers,body=self.fixture.request(path+'?organization=default')
            self.assertEqual(code,200,body);self.assertNotIn('private-container-marker',body);self.assertNotIn('private-domain-marker',body)
            self.assertEqual(headers['Cache-Control'],'no-store')
            code,_,body=self.fixture.request(path+'?organization=default',method='HEAD');self.assertEqual((code,body),(200,''))
        code,_,body=self.fixture.request('/v1/status?organization=default');value=validate_inventory(json.loads(body))
        self.assertEqual(len(value['sources'][0]['last_valid_snapshot']['resources']),1)
        self.assertEqual(self.fixture.request('/v1/status?organization=default&resource=private-container-marker')[0],400)
    def test_no_widening_after_grant_removal_or_invalid_profile(self):
        data=partial_profile();data['grants']=[];self.fixture.profile.write_text(yaml.safe_dump(data))
        self.assertEqual(json.loads(self.fixture.request('/v1/status?organization=default')[2])['sources'],[])
        self.assertEqual(self.fixture.request('/readyz?organization=default')[0],503)
        data['version']=1;self.fixture.profile.write_text(yaml.safe_dump(data))
        self.assertEqual(self.fixture.request('/ui?organization=default')[0],503)
