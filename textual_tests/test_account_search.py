import asyncio
import unittest
from types import SimpleNamespace
from textual.widgets import Input
from atraxis.account_search import AccountQuery,search_accounts
from atraxis.account_search_textual import AccountSearchApp

class AccountSearchTextualTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        asyncio.get_running_loop().set_debug(False)
        self.denied=False;self.calls=[]
        data=dict(subject='alice',revision='demo',organizations=[dict(id='org',name='Organization',groups=[],
            memberships=[dict(user='member-'+str(i),group=None) for i in range(105)])])
        def search(query):
            self.calls.append(query)
            if self.denied:raise PermissionError()
            return search_accounts(data,query)
        self.app=AccountSearchApp(SimpleNamespace(search=search),AccountQuery(kind='user'))

    async def test_full_scope_query_pagination_and_revocation(self):
        async with self.app.run_test(size=(120,40)) as pilot:
            await pilot.pause();self.assertEqual(self.app.data['total'],105)
            await pilot.press('n');await pilot.pause();self.assertEqual(self.app.query.page,2)
            await pilot.press('slash');self.app.query_one(Input).value='MEMBER-104'
            await pilot.pause();self.assertEqual(len(self.calls),2)
            await pilot.press('enter');await pilot.pause()
            self.assertEqual(self.app.data['total'],1);self.assertEqual(len(self.app._visible_keys),1)
            await pilot.press('escape');await pilot.pause();self.assertEqual(self.app.data['total'],105)
            self.denied=True;self.app.action_refresh();await pilot.pause()
            self.assertIsNone(self.app.data);self.assertEqual(self.app._rows,{})

    async def test_filters_on_short_terminal(self):
        async with self.app.run_test(size=(80,24)) as pilot:
            await pilot.pause();await pilot.press('f');await pilot.pause()
            self.app.screen.query_one('#field-1',Input).value='group'
            await pilot.click('#form-submit');await pilot.pause()
            self.assertEqual(self.app.data['total'],0)
            await pilot.press('f');await pilot.pause()
            self.app.screen.query_one('#field-1',Input).value='invalid'
            await pilot.click('#form-submit');await pilot.pause()
            self.assertIsNone(self.app.data);self.assertEqual(len(self.calls),2)
