"""Real CLI/worker/server scopes and trusted local inventory identity."""
from contextlib import redirect_stdout, redirect_stderr
from copy import deepcopy
from dataclasses import replace
import io
import json
import os
from unittest.mock import patch
import unittest

from atraxis.cli import main
from atraxis.client import read_inventory
from atraxis.client_config import load_client_config
from atraxis.inventory_admin import InventoryAdmin, request_from_fields
from atraxis.inventory_access import load_profile, InventoryAccess
from atraxis.accounts import AccountError, Directory
from atraxis.transport import TransportResult
import test_inventory_access as access_fixture
import test_inventory_admin as admin_fixture
from test_client import inventory


class InventoryCLIContextTests(unittest.TestCase):
    setUp = access_fixture.InventoryWebTests.setUp
    stop = access_fixture.InventoryWebTests.stop

    def invoke(self, *flags):
        credentials = self.root/'reader.json'
        credentials.write_text(json.dumps(dict(username='alice', password='synthetic-password')))
        config = self.root/'client.yml'
        config.write_text(json.dumps(dict(origin='http://127.0.0.1:'+str(self.port),
             credentials_file='reader.json', allow_loopback_http=True)))
        # Use a fully validated observation envelope across all synthetic sources.
        base = inventory()['sources'][0]
        sources = []
        for source_id in ('docker-lab', 'second-source', 'hidden-source'):
            item = deepcopy(base);item['source_id'] = source_id
            for row in item['last_valid_snapshot']['resources']:
                row['source_id'] = source_id
            sources.append(item)
        self.service.payload = dict(inventory(), sources=sources)
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(['inventory', '--config', str(config), '--json', *flags])
        return code, out.getvalue(), err.getvalue()

    def test_short_long_selection_missing_context_and_revocation(self):
        code, out, err = self.invoke()
        self.assertEqual((code, out), (1, ''));self.assertIn('--organization', err)
        for flag, org, source in (('-o', 'default', 'docker-lab'), ('--organization', 'second', 'second-source')):
            code, out, err = self.invoke(flag, org)
            self.assertEqual(code, 0, err)
            data = json.loads(out)
            self.assertEqual(data['context']['organization'], org)
            self.assertEqual([s['source_id'] for s in data['sources']], [source])
            self.assertNotIn('hidden-source', out)
        self.assertEqual(self.invoke('-o', 'hidden')[:2], (1, ''))
        snap = self.store.snapshot();data = snap['directory']
        data['memberships'] = [m for m in data['memberships'] if not (m['user']=='alice' and m['organization']=='second')]
        self.store.apply(Directory(data),snap['revision'])
        self.assertEqual(self.invoke('-o','second')[:2],(1,''))

    def test_invalid_selector_never_calls_transport_and_wrong_response_rejected(self):
        with patch('atraxis.client.HTTPTransport.fetch', side_effect=AssertionError('no request')):
            self.assertEqual(self.invoke('-o', 'default&subject=bob')[:2], (1, ''))
        self.invoke('-o','default')
        config = replace(load_client_config(self.root/'client.yml'),organization='second')
        payload = inventory()
        for context in (None, dict(subject='alice',organization='default',organizations=[dict(id='default',name='Default')])):
            data = dict(payload)
            if context is not None:data['context'] = context
            with patch('atraxis.client.HTTPTransport.fetch',return_value=TransportResult('atraxis-api','success',1,2,100,data)):
                self.assertEqual(read_inventory(config), (None,'invalid_payload'))


class InventoryLocalIdentityTests(unittest.TestCase):
    setUp = admin_fixture.GrantAdminTests.setUp
    write = admin_fixture.GrantAdminTests.write

    def test_uid_mapping_is_separate_from_web_and_environment(self):
        with self.assertRaises(PermissionError):InventoryAdmin(self.profile)
        data=admin_fixture.admin_profile();data['identities'].append(dict(uid=os.getuid(),subject='alice'));self.write(data)
        with patch.dict(os.environ, {'USER':'bob','LOGNAME':'bob'}):
            admin=InventoryAdmin(self.profile)
            self.assertEqual(admin.inspect()['subject'],'alice')
        self.assertEqual(InventoryAccess(self.profile).resolve('bob','default')['subject'],'bob')
        plan=admin.prepare(admin_fixture.candidate(),admin.inspect()['revision'])
        data['identities'][-1]['subject']='bob';self.write(data)
        with self.assertRaises(PermissionError):admin.commit(plan)
        self.assertEqual(load_profile(self.profile)[0]['audit'],[])

    def test_uid_validation_and_shared_form_rejects_overrides(self):
        for uid in (True,-1,'0'):
            data=admin_fixture.admin_profile();data['identities'].append(dict(uid=uid,subject='alice'));self.write(data)
            with self.assertRaises(AccountError):load_profile(self.profile)
        data=admin_fixture.admin_profile();data['identities'] += [dict(uid=123,subject='alice')]*2;self.write(data)
        with self.assertRaises(AccountError):load_profile(self.profile)
        fields=dict(organization='default',subject_kind='user',subject='bob',kind='container',source='docker-lab',id='b'*64)
        self.assertEqual(request_from_fields(fields),admin_fixture.candidate())
        for changes in ({'actor':'alice'},{'kind':'server'}, {'id':'x'*129}):
            with self.assertRaises(AccountError):request_from_fields(dict(fields,**changes))
