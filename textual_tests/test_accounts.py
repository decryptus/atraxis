import asyncio
import os
from pathlib import Path
import sys
import tempfile
import unittest
import yaml
from textual.widgets import Input
from atraxis.account_textual import AccountApp
from atraxis.account_admin import AccountAdmin
from atraxis.account_store import AccountStore
from atraxis.accounts import Directory
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tests'))
from test_account_delegation import fixture


class AccountTextualTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        asyncio.get_running_loop().set_debug(False)
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup);self.root=Path(tmp.name)
        self.store=AccountStore(self.root/'directory.json');self.rev=self.store.apply(Directory(fixture()),None)
        path=self.root/'admin.yml';path.write_text(yaml.safe_dump(dict(version=1,store='directory.json',
            identities=[dict(uid=os.getuid(),subject='alice')])));path.chmod(0o600)
        self.app=AccountApp(AccountAdmin(path))

    async def fill(self,pilot):
        await pilot.press('a');await pilot.pause()
        for i,value in enumerate(('default','charlie','ops')):self.app.screen.query_one('#field-'+str(i),Input).value=value
        await pilot.click('#form-submit');await pilot.pause()

    async def test_review_cancel_confirm_and_no_repeat(self):
        async with self.app.run_test(size=(120,40)) as pilot:
            await pilot.pause();await self.fill(pilot)
            self.assertEqual(self.store.snapshot()['revision'],self.rev)
            await pilot.press('escape');await pilot.pause()
            self.assertEqual(self.store.snapshot()['revision'],self.rev)
            await self.fill(pilot);await pilot.click('#dw-confirm');await pilot.pause()
            self.app.confirm(True);await pilot.pause()
            self.assertEqual(len(self.store.snapshot()['audit']),2)
            self.assertEqual(self.store.snapshot()['audit'][-1]['actor']['operation'],'membership.add')

    async def test_short_terminal_conflict_and_clear_stale_view(self):
        async with self.app.run_test(size=(80,24)) as pilot:
            await pilot.pause();await self.fill(pilot)
            d=fixture();d['delegations'][0]['enabled']=False;self.store.apply(Directory(d),self.rev)
            await pilot.click('#dw-confirm');await pilot.pause()
            self.assertIsNone(self.app.pending);self.assertIsNone(self.app.data)
            self.assertEqual(len(self.store.snapshot()['audit']),2)


class EntityTextualTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await AccountTextualTests.asyncSetUp(self)
        from test_account_entities import fixture as entity_fixture
        self.rev=self.store.apply(Directory(entity_fixture()),self.rev)

    async def test_entity_form_review_and_commit_in_short_terminal(self):
        async with self.app.run_test(size=(80,24)) as pilot:
            await pilot.pause();await pilot.press('e');await pilot.pause()
            self.app.screen.query_one('#field-0',Input).value='user.create'
            await pilot.click('#form-submit');await pilot.pause()
            for i,value in enumerate(('eve','Eve','yes')):
                self.app.screen.query_one('#field-'+str(i),Input).value=value
            await pilot.click('#form-submit');await pilot.pause()
            self.assertEqual(self.store.snapshot()['revision'],self.rev)
            await pilot.click('#dw-confirm');await pilot.pause()
            self.assertIn('eve',Directory(self.store.snapshot()['directory']).users)
