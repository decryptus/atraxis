import asyncio
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
from textual.widgets import Input
from atraxis.inventory_textual import InventoryAdminApp
from atraxis.inventory_admin import InventoryAdmin, fingerprint
from atraxis.inventory_access import load_profile
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tests'))
import test_inventory_admin as fixtures


class InventoryTextualTests(unittest.IsolatedAsyncioTestCase):
    write = fixtures.GrantAdminTests.write

    async def asyncSetUp(self):
        asyncio.get_running_loop().set_debug(False)
        fixtures.GrantAdminTests.setUp(self)
        data=fixtures.admin_profile();data['identities'].append(dict(uid=os.getuid(),subject='alice'));self.write(data)
        self.app=InventoryAdminApp(InventoryAdmin(self.profile))

    async def settle(self, pilot):
        # A render pause does not await threaded storage writes or their refresh.
        async def ready():
            await pilot.pause()
            while self.app.busy:
                await self.app.workers.wait_for_complete()
                await pilot.pause()
        await asyncio.wait_for(ready(), timeout=10)

    async def fill(self,pilot):
        await pilot.press('a');await self.settle(pilot)
        for i,value in enumerate(('default','user','bob','container','docker-lab','b'*64)):
            self.app.screen.query_one('#field-'+str(i),Input).value=value
        await pilot.click('#form-submit');await self.settle(pilot)

    async def test_add_cancel_confirm_remove_and_no_repeat(self):
        async with self.app.run_test(size=(120,40)) as pilot:
            await self.settle(pilot);await self.fill(pilot)
            self.assertEqual(load_profile(self.profile)[0]['audit'],[])
            await pilot.press('escape');await self.settle(pilot)
            self.assertIsNone(self.app.pending)
            await self.fill(pilot);await pilot.click('#dw-confirm');await self.settle(pilot)
            self.app.confirm(True);await self.settle(pilot)
            data=load_profile(self.profile)[0];self.assertEqual(len(data['audit']),1)
            grant=next(g for g in data['grants'] if g['organization']=='default' and g['subject']=='bob')
            await pilot.press('d');await self.settle(pilot)
            self.app.screen.query_one('#field-0',Input).value=fingerprint(grant)
            await pilot.click('#form-submit');await self.settle(pilot)
            self.assertEqual(len(load_profile(self.profile)[0]['audit']),1)
            await pilot.click('#dw-confirm');await self.settle(pilot)
            self.assertEqual(len(load_profile(self.profile)[0]['audit']),2)
            self.assertEqual(load_profile(self.profile)[0]['audit'][-1]['operation'],'grant.remove')
            self.app.show_audit()

    async def test_small_terminal_revocation_clears_stale_view(self):
        async with self.app.run_test(size=(80,24)) as pilot:
            await self.settle(pilot);await self.fill(pilot)
            data=load_profile(self.profile)[0];data['administrators']=[];self.write(data)
            await pilot.click('#dw-confirm');await self.settle(pilot)
            self.assertIsNone(self.app.pending);self.assertIsNone(self.app.data)
            self.assertEqual(load_profile(self.profile)[0]['audit'],[])

    async def test_uncertain_write_does_not_repeat_and_refresh_recovers(self):
        async with self.app.run_test(size=(120,40)) as pilot:
            await self.settle(pilot);await self.fill(pilot)
            original=self.app.admin.commit
            def uncertain(plan):
                original(plan)
                raise OSError('private-marker')
            with patch.object(self.app.admin,'commit',side_effect=uncertain) as commit:
                await pilot.click('#dw-confirm');await self.settle(pilot)
                self.app.confirm(True);await self.settle(pilot)
                self.assertEqual(commit.call_count,1)
            self.assertIsNone(self.app.data)
            self.assertEqual(len(load_profile(self.profile)[0]['audit']),1)
            await pilot.press('r');await self.settle(pilot)
            self.assertIsNotNone(self.app.data)
