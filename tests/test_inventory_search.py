"""Authorized full-dataset search, deterministic paging and real CLI/API reads."""
from copy import deepcopy
from contextlib import redirect_stdout, redirect_stderr
from dataclasses import replace
import io
import json
from urllib.parse import urlencode
import unittest
from unittest.mock import patch
import yaml

from atraxis.cli import main
from atraxis.inventory_search import SearchQuery, parse_query, search_inventory
from atraxis.inventory_access import InventoryAccess
from atraxis.search_client import validate_result
from atraxis.accounts import Directory
from atraxis.web_inventory_search import render_search
import test_resource_scopes as scopes
import test_inventory_access as access


class InventorySearchTests(unittest.TestCase):
    setUp=scopes.ResourceScopeTests.setUp
    write=scopes.ResourceScopeTests.write

    def search(self,**fields):
        return self.access.search('alice','default',SearchQuery(**fields),self.service)[1]

    def test_hidden_rows_counts_metadata_and_mixed_group(self):
        data=self.search(q='private')
        self.assertEqual(data['total'],0);self.assertEqual(data['items'],[])
        self.assertNotIn('private-container-marker',json.dumps(data))
        data=self.search(group='applications')
        self.assertEqual(data['total'],1)
        self.assertNotIn('cert-lab',json.dumps(data['items']))
        self.assertNotIn('source_metadata',json.dumps(data))
        self.assertEqual(self.search(server='host-b')['total'],0)
        self.assertEqual(self.search(group='missing')['total'],0)
        self.assertEqual(self.search(server='host-a')['total'],1)

    def test_name_native_id_casefold_and_combined_filters(self):
        row=self.search()['items'][0]
        self.assertEqual(self.search(q=row['fields']['name'].upper())['total'],1)
        self.assertEqual(self.search(q='A'*64)['total'],1)
        self.assertEqual(self.search(q='docker',kind='certificate')['total'],0)
        self.assertEqual(self.search(q='docker',state='current',source='docker-lab',server='host-a',group='applications')['total'],1)
        self.assertEqual(self.search(status='not-a-state')['total'],0)
        self.assertEqual(self.search(status=row['fields']['status'].upper())['total'],1)

    def test_scan_after_display_limit_paging_and_readiness_independence(self):
        self.data['grants'][0]['targets']=[dict(kind='source',id='docker-lab')];self.write()
        raw=self.service.status();snapshot=raw['sources'][0]['last_valid_snapshot']
        template=deepcopy(snapshot['resources'][0]);snapshot['resources']=[]
        for i in range(251):
            row=deepcopy(template);row['native_id']=format(i,'064x');row['fields']['name']='needle' if i==250 else 'ordinary'
            snapshot['resources'].append(row)
        with patch.object(self.service,'status',return_value=raw):
            found=self.search(q='needle');self.assertEqual(found['total'],1)
            self.assertEqual(found['items'][0]['native_id'],format(250,'064x'))
            first=self.search(limit=200);second=self.search(limit=200,page=2)
            self.assertEqual((len(first['items']),len(second['items'])),(200,51))
            self.assertTrue(first['has_more']);self.assertFalse(second['has_more'])
            self.assertEqual(len({r['native_id'] for r in first['items']+second['items']}),251)
            empty=self.search(q='none');self.assertTrue(empty['ready'])
            self.assertEqual(self.search(page=999)['items'],[])

    def test_unknown_historical_and_revocation_after_search(self):
        self.service.set_running(False)
        data=self.search(q='missing',state='current')
        self.assertEqual(data['total'],0);self.assertEqual(data['unavailable'][0]['state'],'historical')
        self.assertFalse(data['ready'])
        original=search_inventory
        def revoke(*args,**kwargs):
            value=original(*args,**kwargs)
            self.data['grants']=[];self.write()
            return value
        with patch('atraxis.inventory_search.search_inventory',side_effect=revoke):
            with self.assertRaises(PermissionError):self.search()
        self.assertEqual(self.search()['total'],0)

    def test_query_contract_client_schema_and_html_escaping(self):
        for query in ('q=a&q=b','q=%0A','organization=','page=-1','limit=201','limit=true','unknown=x','group=x/y','q='+'x'*129):
            with self.subTest(query=query),self.assertRaises((ValueError,TypeError)):parse_query('/ui/search?'+query)
        data=self.search(q='<script>')
        rendered=render_search(data)
        self.assertNotIn('<script>',rendered);self.assertIn('&lt;script&gt;',rendered)
        validate_result(data,SearchQuery(q='<script>'),'default')
        for change in ({'total':True},{'has_more':True},{'context':None},{'items':[{}]}):
            with self.assertRaises((ValueError,TypeError,KeyError)):validate_result(dict(data,**change),SearchQuery(q='<script>'),'default')


class InventorySearchHTTPTests(unittest.TestCase):
    setUp=access.InventoryWebTests.setUp
    stop=access.InventoryWebTests.stop
    request=access.InventoryWebTests.request

    def prepare(self):
        profile=scopes.partial_profile()
        self.profile.write_text(yaml.safe_dump(profile));self.profile.chmod(0o600)
        self.service.payload=scopes.observation_service().status()

    def test_get_head_context_no_leak_and_strict_queries(self):
        self.prepare()
        self.assertEqual(self.request('/v1/inventory/search')[0],409)
        code,_,body=self.request('/ui/search?q=worker')
        self.assertEqual(code,200);self.assertIn('Select an organization',body)
        for route in ('/v1/inventory/search','/ui/search'):
            code,headers,body=self.request(route+'?organization=default&q=private')
            self.assertEqual(code,200);self.assertNotIn('private-container-marker',body)
            self.assertNotIn('extra-lab',body);self.assertEqual(headers['Cache-Control'],'no-store')
            self.assertEqual(self.request(route+'?organization=hidden')[0],403)
            self.assertEqual(self.request(route+'?organization=default',user=None)[0],401)
            self.assertEqual(self.request(route+'?organization=default',method='HEAD')[2],'')
            self.assertEqual(self.request(route+'?organization=default&q=x&q=y')[0],400)

    def test_real_cli_worker_search_and_membership_revocation(self):
        self.prepare();config=self.root/'client.yml'
        (self.root/'reader.json').write_text(json.dumps(dict(username='alice',password='synthetic-password')))
        config.write_text(yaml.safe_dump(dict(origin='http://127.0.0.1:'+str(self.port),credentials_file='reader.json',allow_loopback_http=True)))
        def invoke(*flags):
            out,err=io.StringIO(),io.StringIO()
            with redirect_stdout(out),redirect_stderr(err):
                code=main(['search','inventory','--config',str(config),'-o','default','--json',*flags])
            return code,out.getvalue(),err.getvalue()
        code,out,err=invoke('-q','A'*64,'--limit','1')
        self.assertEqual(code,0,err);self.assertEqual(json.loads(out)['total'],1)
        self.assertEqual(json.loads(invoke('-q','private')[1])['total'],0)
        snap=self.store.snapshot();directory=snap['directory'];directory['users'][0]['enabled']=False
        self.store.apply(Directory(directory),snap['revision'])
        self.assertEqual(invoke()[:2],(1,''))
