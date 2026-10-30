import asyncio
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from textual.widgets import Input
from atraxis.inventory_search import SearchQuery,search_inventory
from atraxis.search_textual import InventorySearchApp


class InventorySearchTextualTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        asyncio.get_running_loop().set_debug(False)
        self.calls=[];self.denied=False
        resources=[dict(source_id='docker-lab',kind='container',native_id=format(i,'064x'),fields=dict(name='worker-'+str(i),status='running')) for i in range(105)]
        self.payload=dict(ready=True,reason=None,context=dict(subject='alice',organization='default',organizations=[dict(id='default',name='Default')]),sources=[dict(source_id='docker-lab',kind='monit_docker',current=True,last_valid_snapshot=dict(resources=resources))])
        def read(config,query):
            self.calls.append(query)
            return (None,'forbidden') if self.denied else (search_inventory(self.payload,query),None)
        self.mock=patch('atraxis.search_textual.read_search',side_effect=read);self.mock.start();self.addCleanup(self.mock.stop)
        self.app=InventorySearchApp(SimpleNamespace(organization='default'),SearchQuery(limit=50))

    async def test_server_search_beyond_loaded_page_native_id_and_reset(self):
        async with self.app.run_test(size=(120,40)) as pilot:
            await pilot.pause();self.assertEqual(len(self.app._rows),50)
            await pilot.press('slash');field=self.app.query_one(Input);field.value=format(104,'064x')
            await pilot.pause();self.assertEqual(len(self.calls),1)
            await pilot.press('enter');await pilot.pause()
            self.assertEqual(self.app.data['total'],1)
            self.assertEqual(len(self.app._visible_keys),1) # ID is not one of the displayed table cells.
            await pilot.press('escape');await pilot.pause()
            self.assertEqual(self.app.query.q,'');self.assertEqual(self.app.data['total'],105)

    async def test_pages_filters_and_revocation_clear_cached_rows(self):
        async with self.app.run_test(size=(120,40)) as pilot:
            await pilot.pause();await pilot.press('n');await pilot.pause()
            self.assertEqual(self.app.query.page,2)
            await pilot.press('p');await pilot.pause();self.assertEqual(self.app.query.page,1)
            await pilot.press('f');await pilot.pause()
            self.app.screen.query_one('#field-0',Input).value='worker-104'
            await pilot.click('#form-submit');await pilot.pause()
            self.assertEqual(self.app.data['total'],1)
            self.denied=True;await pilot.press('r');await pilot.pause()
            self.assertIsNone(self.app.data);self.assertEqual(self.app._rows,{})

    async def test_short_terminal_query_validation(self):
        async with self.app.run_test(size=(80,24)) as pilot:
            await pilot.pause();await pilot.press('f');await pilot.pause()
            self.app.screen.query_one('#field-1',Input).value='invalid'
            await pilot.click('#form-submit');await pilot.pause()
            self.assertIsNone(self.app.data);self.assertEqual(len(self.calls),1)

    async def test_certlord_metrics_without_domains_render_uuid_and_freshness_notice(self):
        uid='12345678-1234-4234-8234-123456789abc'
        self.payload['sources']=[dict(source_id='certificates',kind='prometheus_certlord',current=True,
            last_valid_snapshot=dict(resources=[dict(source_id='certificates',kind='certificate',native_id=uid,
                fields=dict(domains=None,projection='certlord_metrics',status='deployed'))]))]
        async with self.app.run_test(size=(100,30)) as pilot:
            await pilot.pause()
            self.assertEqual(self.app.data['total'],1)
            self.assertIn(uid,repr(self.app._rows))
            self.assertNotIn('None',str(next(iter(self.app._rows.values())).cells[1]))
