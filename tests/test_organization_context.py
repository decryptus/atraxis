from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from html.parser import HTMLParser
import re
import unittest
from urllib.parse import urlencode
import yaml
from atraxis.organization_context import split_path, scoped_url
from atraxis.web_rules import RuleReader, load_mapping
from atraxis.config import ConfigurationError
import test_web_rules as reads
import test_web_rule_editor as edits
from web_editor_fixture import EditorFake


class OrganizationReadTests(unittest.TestCase):
    stop=reads.WebRulesTests.stop
    request=reads.WebRulesTests.request
    write_mapping=reads.WebRulesTests.write_mapping
    def setUp(self):
        reads.WebRulesTests.setUp(self)
        self.identities.append(dict(self.identities[0],organization='three',atropos_client='three.yml'))
        self.write_mapping(self.identities)

    def test_explicit_choice_no_default_and_independent_parallel_tabs(self):
        self.assertEqual(self.request('/ui/rules')[0],409);self.assertEqual(self.calls,[])
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(lambda org:self.request('/ui/rules?'+urlencode(dict(name='cpu',organization=org))),('one','three')))
        self.assertEqual([r[0] for r in results],[200,200])
        self.assertEqual({c[1]['organization'] for c in self.calls},{'one','three'})
        for result,org in zip(results,('one','three')):
            self.assertIn('href="/ui?organization='+org+'"',result[2])
            self.assertIn('href="/ui/interventions?organization='+org+'"',result[2])
            self.assertIn('name="organization" value="'+org+'"',result[2])
        self.assertEqual(self.request('/ui/rules?name=cpu&organization=two')[0],403)
        self.assertEqual(self.request('/ui?organization=one')[0],400)
        self.assertEqual(self.request('/ui/rules?organization=one&organization=three')[0],400)

    def test_mapping_subject_consistency_identity_pin_and_live_revocation(self):
        reader=RuleReader(self.mapping,'alice',self.factory,organization='three')
        self.write_mapping(self.identities[:-1])
        with self.assertRaises(PermissionError):reader.call('history','cpu',0,20)
        bad=deepcopy(self.identities);bad[-1]['subject']='different-person';self.write_mapping(bad)
        with self.assertRaises(ConfigurationError):load_mapping(self.mapping)

    def test_selection_lists_only_current_memberships(self):
        from atraxis.accounts import Directory,default_directory
        from atraxis.account_store import AccountStore
        data=default_directory('alice-subject').document()
        data['organizations']=[dict(data['organizations'][0],id=org) for org in ('one','three')]
        data['memberships']=[dict(user='alice-subject',organization='one',group=None)]
        AccountStore(self.root/'accounts.db').apply(Directory(data),None)
        self.mapping.write_text(yaml.safe_dump(dict(version=1,accounts_file='accounts.db',identities=self.identities)))
        status,_,body=self.request('/ui/rules')
        self.assertEqual(status,200,body);self.assertNotIn('>three</a>',body)
        self.assertEqual(self.request('/ui/rules?organization=three')[0],403)


class OrganizationWriteTests(unittest.TestCase):
    fake_class=EditorFake
    stop=edits.EditorTests.stop
    request=edits.EditorTests.request
    write_mapping=edits.EditorTests.write_mapping
    document=edits.EditorTests.document
    def setUp(self):
        edits.EditorTests.setUp(self)
        self.identities.append(dict(self.identities[0],organization='other',atropos_client='other.yml'))
        self.write_mapping(self.identities)

    def form(self,organization):
        status,_,body=self.request('/ui/rules/edit?'+urlencode(dict(name='cpu',organization=organization)))
        self.assertEqual(status,200,body)
        self.assertIn('action="/ui/rules/edit?organization='+organization+'"',body)
        return re.search(r'name="token" value="([^"]+)"',body).group(1)

    def test_confirm_cannot_switch_organization_and_redirect_retains_context(self):
        token=self.form('demo')
        def save(org,token):
            return self.request('/ui/rules/edit?organization='+org,method='POST',body=dict(token=token,document=self.document(),message='Reviewed'))
        self.assertEqual(save('other',token)[0],403)
        self.assertFalse(any(c[0]=='save_draft' for c in self.calls))
        status,headers,_=save('other',self.form('other'))
        self.assertEqual(status,303);self.assertIn('organization=other',headers['Location'])
        self.assertEqual(next(c[1]['organization'] for c in self.calls if c[0]=='save_draft'),'other')


class ContextPathTests(unittest.TestCase):
    def test_only_internal_navigation_is_scoped_and_invalid_duplicates_fail(self):
        for url in ('https://example.test/ui','//example.test/ui','#rules','/ui/accounts','/ui/inventory-access'):
            self.assertEqual(scoped_url(url,'one'),url)
        self.assertEqual(split_path('/ui/rules?name=a&organization=one'),('/ui/rules?name=a','one'))
        self.assertEqual(scoped_url('/ui/interventions/detail?name=cpu', 'one'),
                         '/ui/interventions/detail?name=cpu&organization=one')
        self.assertEqual(scoped_url('/ui/interventions/detail?organization=two', 'one'),
                         '/ui/interventions/detail?organization=two')
        for path in ('/ui?organization=','/ui?organization=a/b','/ui?organization=one&organization=one'):
            with self.assertRaises(ValueError):split_path(path)
