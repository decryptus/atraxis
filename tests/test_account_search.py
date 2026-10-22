import io
import json
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch
from atraxis.account_search import AccountQuery,parse_query,search_accounts
from atraxis.account_admin import AccountAdmin
from atraxis.account_store import AccountStore
from atraxis.accounts import Directory
from atraxis.cli import main
import test_account_delegation as delegation
from test_account_delegation import fixture
import test_web_accounts as webtests

class AccountSearchTests(unittest.TestCase):
    setUp=delegation.AdminTests.setUp

    def test_full_scope_pagination_literal_query_and_no_hidden_group(self):
        data=fixture()
        for i in range(251):
            user='member-'+str(i).zfill(3)
            data['users'].append(dict(id=user,name=user,enabled=True))
            data['memberships'].append(dict(user=user,organization='default',group=None))
        data['memberships'].append(dict(user='bob',organization='default',group='ops'))
        self.store.apply(Directory(data),self.rev)
        result=self.admin.search(AccountQuery(kind='user',limit=200,page=2))
        self.assertEqual(result['total'],255);self.assertEqual(len(result['items']),55)
        result=self.admin.search(AccountQuery(q='MEMBER-250'))
        self.assertEqual([r['id'] for r in result['items']],['member-250'])
        self.assertEqual(self.admin.search(AccountQuery(q='secret'))['total'],0)
        self.assertEqual(self.admin.search(AccountQuery(organization='hidden'))['total'],0)
        self.assertEqual(self.admin.search(AccountQuery(q='.*'))['total'],0)
        self.assertEqual(self.admin.search(AccountQuery(kind='group'))['total'],2)

    def test_group_only_authority_hides_other_memberships_and_users(self):
        data=fixture();data['delegations'][0]['organization_membership']=False
        data['memberships'].append(dict(user='bob',organization='default',group='ops'))
        self.store.apply(Directory(data),self.rev)
        result=self.admin.search(AccountQuery(kind='user'))
        self.assertEqual([r['id'] for r in result['items']],['bob'])
        self.assertNotIn('charlie',json.dumps(result))

    def test_revision_and_profile_rechecked_after_search(self):
        original=AccountStore.search
        def revoked(store,subject,query):
            result=original(store,subject,query)
            doc=fixture();doc['delegations'][0]['enabled']=False
            self.store.apply(Directory(doc),self.rev)
            return result
        with patch.object(AccountStore,'search',revoked):
            with self.assertRaises(PermissionError):self.admin.search(AccountQuery())
        with self.assertRaises(PermissionError):self.admin.search(AccountQuery())

    def test_query_validation_and_cli_no_mutation(self):
        for fields in ({'page':True},{'limit':201},{'q':'a\n'},{'kind':'unknown'},{'organization':'a/b'}):
            with self.subTest(fields=fields),self.assertRaises(ValueError):AccountQuery(**fields)
        for path in ('?q=a&q=b','?actor=alice','?page=-1','?limit=0','?q=%0A'):
            with self.subTest(path=path),self.assertRaises(ValueError):parse_query(path)
        out=io.StringIO()
        with redirect_stdout(out):
            code=main(['accounts-admin','--config',str(self.profile),'search','--kind','group','-q','OPS','-o','default'])
        self.assertEqual(code,0);self.assertEqual(json.loads(out.getvalue())['total'],1)
        self.assertEqual(self.store.snapshot()['revision'],self.rev)

    def test_sql_search_uses_scoped_index_without_loading_directory_or_audit(self):
        from atraxis.account_sqlite import SQLiteTransaction
        import sqlite3
        with patch.object(SQLiteTransaction,'read',side_effect=AssertionError('whole directory read')):
            result=self.admin.search(AccountQuery(kind='group',q='ops'))
        self.assertEqual(result['total'],1)
        with sqlite3.connect(self.store.path) as c:
            plan=list(c.execute("EXPLAIN QUERY PLAN SELECT id FROM account_search_scope WHERE subject=? AND kind=?",('alice','group')))
        self.assertTrue(any('account_search_kind' in row[3] for row in plan))

class AccountSearchWebTests(unittest.TestCase):
    setUp=webtests.AccountWebTests.setUp
    stop=webtests.AccountWebTests.stop
    write=webtests.AccountWebTests.write
    request=webtests.AccountWebTests.request

    def test_authenticated_search_escape_filters_and_revocation(self):
        self.assertEqual(self.request('/ui/accounts/search',user=None)[0],401)
        self.assertEqual(self.request('/ui/accounts/search',user='bob-login')[0],403)
        status,headers,body=self.request('/ui/accounts/search?kind=group&q=OPS')
        self.assertEqual(status,200);self.assertIn('1 matches',body);self.assertNotIn('secret',body)
        self.assertEqual(headers['Cache-Control'],'no-store')
        self.assertIn('Reset filters',body)
        self.assertEqual(self.request('/ui/accounts/search?kind=wrong')[0],400)
        status,_,body=self.request('/ui/accounts/search?q=%3Cscript%3E')
        self.assertEqual(status,200);self.assertIn('&lt;script&gt;',body);self.assertNotIn('<script>',body)
        doc=fixture();doc['delegations'][0]['enabled']=False
        self.store.apply(Directory(doc),self.rev)
        self.assertEqual(self.request('/ui/accounts/search')[0],403)
