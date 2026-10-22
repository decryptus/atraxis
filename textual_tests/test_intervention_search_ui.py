import asyncio
from types import SimpleNamespace
import unittest
from textual.widgets import Input
from atraxis.intervention_search import InterventionQuery
from atraxis.intervention_search_textual import InterventionSearchApp

class InterventionSearchTextualTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        asyncio.get_running_loop().set_debug(False);self.calls=[];self.denied=False
        def search(query):
            self.calls.append(query)
            if self.denied:raise PermissionError()
            row=dict(id='a'*64,revision='b'*32,rule='cpu',event_id='event-needle',mode='test',status='queued',target='org/container/web',workflow='inspect',created_at=1791480000,test_context=dict(test=True,id='a'*64))
            return dict(subject='alice',organization='org',rule='cpu',query=query.parameters(),total=1,items=[row],has_more=False)
        self.app=InterventionSearchApp(SimpleNamespace(name='cpu',search=search))

    async def test_submit_filters_reset_and_denial(self):
        async with self.app.run_test(size=(120,40)) as pilot:
            await pilot.pause();self.assertIn('runs for cpu',self.app.query_one(Input).placeholder);await pilot.press('slash');self.app.query_one(Input).value='EVENT-NEEDLE'
            await pilot.pause();self.assertEqual(len(self.calls),1)
            await pilot.press('enter');await pilot.pause();self.assertEqual(self.calls[-1].q,'EVENT-NEEDLE')
            self.assertEqual(len(self.app._visible_keys),1)
            await pilot.press('escape');await pilot.pause();self.assertEqual(self.app.query.q,'')
            self.denied=True;self.app.action_refresh();await pilot.pause()
            self.assertIsNone(self.app.data);self.assertEqual(self.app._rows,{})

    async def test_date_filters_in_short_terminal(self):
        async with self.app.run_test(size=(80,24)) as pilot:
            await pilot.pause();await pilot.press('f');await pilot.pause()
            self.app.screen.query_one('#field-5',Input).value='2026-10-08T00:00:00'
            await pilot.click('#form-submit');await pilot.pause()
            self.assertIsInstance(self.calls[-1].since,int)
            await pilot.press('f');await pilot.pause()
            self.app.screen.query_one('#field-5',Input).value='invalid'
            await pilot.click('#form-submit');await pilot.pause();self.assertIsNone(self.app.data)
