import asyncio
import unittest
from unittest.mock import Mock
from atraxis.rule_textual import RuleApp

class RuleUITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        asyncio.get_running_loop().set_debug(False)

    def app(self):
        operator = Mock()
        operator.application.history.return_value = [dict(revision='r1', author='demo')]
        operator.application.active.return_value = dict(revision='r1', generation='g1')
        operator.application.read.return_value = {'context': {'rule_kind': 'intervention', 'targets': [['container', 'demo']]}}
        return RuleApp(operator, 'demo')

    async def test_activation_exact_test_generation_and_cancel(self):
        app = self.app()
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            app.model.reports['r1'] = {'id': 'test1', 'passed': True}
            await pilot.press('a'); await pilot.pause()
            app.model.app.activate.assert_not_called()
            await pilot.press('escape'); await pilot.pause()
            app.model.app.activate.assert_not_called()
            await pilot.press('a'); await pilot.pause()
            await pilot.click('#dw-confirm'); await pilot.pause()
            app.model.app.activate.assert_called_once_with('demo', 'r1', test_id='test1', expected_generation='g1')

    async def test_conflict_consumes_confirmation_without_retry(self):
        app = self.app(); app.model.app.activate.side_effect = ValueError('conflict')
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            app.model.reports['r1'] = {'id': 'test1', 'passed': True}
            await pilot.press('a'); await pilot.pause()
            await pilot.click('#dw-confirm'); await pilot.pause()
            app.confirmed(True)
            app.model.app.activate.assert_called_once()
            self.assertIsNone(app.model.pending)

    async def test_untested_revision_cannot_activate(self):
        app = self.app()
        async with app.run_test() as pilot:
            await pilot.pause(); await pilot.press('a'); await pilot.pause()
            app.model.app.activate.assert_not_called()
            self.assertIsNone(app.model.pending)

    async def test_reading_revision_keeps_details_visible(self):
        app = self.app()
        async with app.run_test() as pilot:
            await pilot.pause(); await pilot.press('enter'); await pilot.pause()
            app.model.app.read.assert_called_with('demo', 'r1')
            self.assertIn('intervention', str(app.query_one('.dw-detail-text').render()))

    def configured_test_app(self):
        app = self.app()
        app.model.app.test_bindings.return_value = [dict(id='a'*64, rule='demo', revision='r1',
            workflow='demo-test', actions=['core.echo'], test_supported=True)]
        app.model.app.simulate_dispatch.return_value = {
            'simulation': True, 'binding': 'a'*64, 'revision': 'r1',
            'target': ['container', 'demo'], 'matches_binding': True}
        app.model.app.test_dispatch.return_value = {
            'id': 'b'*64, 'test_context': {'test': True, 'id': 'b'*64},
            'rule': 'demo', 'revision': 'r1', 'status': 'queued'}
        return app

    async def fill_test_form(self, pilot, app, path):
        from textual.widgets import Input
        await pilot.press('e'); await pilot.pause()
        app.screen.query_one('#field-0', Input).value = 'container:demo'
        await pilot.click('#form-submit'); await pilot.pause()
        await pilot.click('#binding-select'); await pilot.pause()
        for index, value in enumerate((path, 'ui-test')):
            app.screen.query_one('#field-%s' % index, Input).value = value
        await pilot.click('#form-submit'); await pilot.pause()

    async def test_end_to_end_form_reviews_cancels_and_confirms_once(self):
        import tempfile
        from pathlib import Path
        app = self.configured_test_app()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'facts.json'; path.write_text('{"cpu_percent":99}')
            async with app.run_test(size=(80, 24)) as pilot:
                await pilot.pause(); await self.fill_test_form(pilot, app, str(path))
                app.model.app.test_dispatch.assert_not_called()
                await pilot.press('escape'); await pilot.pause()
                app.model.app.test_dispatch.assert_not_called()
                await self.fill_test_form(pilot, app, str(path))
                await pilot.click('#dw-confirm'); await pilot.pause()
                app.confirmed(True)
                app.model.app.test_dispatch.assert_called_once()
                self.assertEqual(app.model.test_notice, 'info')
                app.model.app.dispatch.assert_not_called()

    async def test_unknown_test_submission_recovers_using_status_only(self):
        import tempfile
        from pathlib import Path
        from textual.widgets import Input
        app = self.configured_test_app()
        app.model.app.test_dispatch.side_effect = TimeoutError('private token')
        app.model.app.test_dispatch_status.return_value = {
            'id': 'b'*64, 'test_context': {'test': True, 'id': 'b'*64},
            'rule': 'demo', 'revision': 'r1', 'status': 'succeeded'}
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'facts.json'; path.write_text('{}')
            async with app.run_test(size=(160, 50)) as pilot:
                await pilot.pause(); await self.fill_test_form(pilot, app, str(path))
                await pilot.click('#dw-confirm'); await pilot.pause()
                self.assertIsNone(app.model.pending)
                await pilot.press('s'); await pilot.pause()
                self.assertEqual(app.screen.query_one('#field-0', Input).value, 'ui-test')
                await pilot.click('#form-submit'); await pilot.pause()
                app.model.app.test_dispatch.assert_called_once()
                app.model.app.test_dispatch_status.assert_called_once_with('demo', 'ui-test')
                self.assertEqual(app.model.test_notice, 'success')

    async def test_nonmatching_test_has_no_confirmation_or_submission(self):
        import tempfile
        from pathlib import Path
        app = self.configured_test_app(); app.model.app.simulate_dispatch.return_value['matches_binding'] = False
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'facts.json'; path.write_text('{}')
            async with app.run_test(size=(160, 50)) as pilot:
                await pilot.pause(); await self.fill_test_form(pilot, app, str(path))
                self.assertIsNone(app.model.pending)
                self.assertEqual(app.model.test_notice, 'warning')
                app.model.app.test_dispatch.assert_not_called()

    async def test_empty_binding_list_does_not_offer_manual_override(self):
        from textual.widgets import Input
        app=self.configured_test_app();app.model.app.test_bindings.return_value=[]
        async with app.run_test() as pilot:
            await pilot.pause();await pilot.press('e');await pilot.pause()
            app.screen.query_one('#field-0',Input).value='container:demo'
            await pilot.click('#form-submit');await pilot.pause()
            self.assertEqual(len(app.screen_stack),1)
            app.model.app.simulate_dispatch.assert_not_called()
            app.model.app.test_dispatch.assert_not_called()

    async def test_unsupported_binding_and_cancel_never_preview(self):
        from textual.widgets import Input, Static
        app=self.configured_test_app();app.model.app.test_bindings.return_value[0]['test_supported']=False
        async with app.run_test(size=(80,24)) as pilot:
            await pilot.pause();await pilot.press('e');await pilot.pause()
            app.screen.query_one('#field-0',Input).value='container:demo'
            await pilot.click('#form-submit');await pilot.pause()
            await pilot.click('#binding-select');await pilot.pause()
            self.assertIn('Simulator unavailable',str(app.screen.query_one('#binding-notice',Static).render()))
            await pilot.press('escape');await pilot.pause()
            app.model.app.simulate_dispatch.assert_not_called()
            app.model.app.test_dispatch.assert_not_called()

    async def test_queue_full_is_displayed_as_known_refusal_without_retry(self):
        import tempfile
        from pathlib import Path
        from dwho.tui.textual import StatusLine
        app=self.configured_test_app()
        app.model.app.test_dispatch.side_effect=ValueError('rule_queue_full')
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'facts.json';path.write_text('{}')
            async with app.run_test(size=(100,32)) as pilot:
                await pilot.pause();await self.fill_test_form(pilot,app,str(path))
                await pilot.click('#dw-confirm');await pilot.pause()
                self.assertIn('not admitted',app.model.message)
                self.assertEqual(app.query_one(StatusLine).state,'warning')
                self.assertIsNone(app.model.pending)
                app.confirmed(True)
                app.model.app.test_dispatch.assert_called_once()
