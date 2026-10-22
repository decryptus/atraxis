"""Native explicit test admission through HTTPS browser, client and engine."""
from copy import deepcopy
import re

import yaml
from test_browser_interventions import BrowserInterventionFixture


class BrowserSimulationTests(BrowserInterventionFixture):
    def setUp(self):
        super().setUp()
        config = yaml.safe_load((self.root/'web.yml').read_text())
        config['general']['rules_web_origin'] = self.https_origin
        self.write_yaml('web.yml', config)
        self.stop('web'); self.start_web()

    def submit(self, button, route, status=200):
        with self.page.expect_response(lambda r: r.request.method == 'POST' and
                                        '/ui/rules/'+route+'?' in r.url) as response:
            with self.page.expect_navigation(wait_until='domcontentloaded') as navigation:
                self.page.get_by_role('button', name=button, exact=True).click()
        self.assertEqual(response.value.status, status, self.page.locator('main').inner_text())
        self.assertEqual(response.value.request.headers['origin'], self.https_origin)
        self.assertEqual(navigation.value.status, 200 if status == 303 else status)

    def review(self, cpu=95):
        self.assertEqual(self.page.goto(self.https_origin+'/ui/rules?organization=demo').status, 200)
        with self.page.expect_navigation():
            self.page.get_by_role('link', name='Launch a test simulation', exact=True).click()
        self.page.get_by_label('Rule name', exact=True).fill('observed')
        self.page.get_by_label('Target kind', exact=True).select_option('container')
        self.page.get_by_label('Target ID', exact=True).fill('web')
        with self.page.expect_navigation():
            self.page.get_by_role('button', name='Find test workflows', exact=True).click()
        self.page.get_by_label('Approved test workflow', exact=True).select_option(self.binding)
        self.page.get_by_label('Facts JSON', exact=True).fill('{"cpu_percent":%d}' % cpu)
        self.submit('Review simulation', 'simulate')
        event = re.search(r'Event: ([0-9a-f]{32})', self.page.locator('main').inner_text()).group(1)
        self.page.get_by_label('I confirm this test simulation', exact=False).check()
        return event, self.page.locator('input[name=token]').input_value()

    def test_explicit_browser_test_runs_only_simulators_and_survives_restart(self):
        (self.root/'executor.password').unlink()
        active = deepcopy(self.clients['alice'].active('observed'))
        event, token = self.review()
        self.assertEqual(self.receiver.requests, [])
        self.assertEqual(self.clients['alice'].search_interventions('observed', mode='test')['total'], 0)
        for width, height in ((1440, 1100), (390, 844)):
            self.page.set_viewport_size(dict(width=width, height=height))
            self.assertTrue(self.page.evaluate('document.documentElement.scrollWidth <= innerWidth'))
        self.submit('Confirm test simulation', 'simulate-confirm', 303)
        self.assertIn('organization=demo', self.page.url); self.assert_status('queued')
        self.worker(); self.assertEqual(self.receiver.requests, [])
        self.worker(test=True); self.worker(test=True)
        self.page.reload(); self.assert_status('succeeded')
        state = self.clients['alice'].test_dispatch_status('observed', event)
        marker = state['test_context']
        self.assertTrue(all(row['test_context'] == marker for row in state['history']))
        self.assertEqual(state['results']['notify']['alert']['test_context'], marker)
        self.assertEqual(self.receiver.requests[0][0], '/v1/test-alerts')
        self.assertEqual(len(self.receiver.requests), 1)
        self.assertEqual(self.proxies['auton'].calls, [])
        self.assertFalse((self.auton.root/'effect.txt').exists()); self.auton_history(0)
        self.assertEqual(self.clients['alice'].active('observed'), active)
        destination = self.page.url
        self.review()
        self.page.locator('input[name=token]').evaluate('(element, value) => element.value = value', token)
        self.submit('Confirm test simulation', 'simulate-confirm', 403)
        self.stop('web'); self.stop('engine'); self.stop('storage')
        self.start_storage(); self.start_engine(); self.start_web()
        self.page.goto(destination); self.assert_status('succeeded')
        self.assertEqual(self.clients['alice'].test_dispatch_status('observed', event), state)
        self.assertEqual(len(self.receiver.requests), 1)
        self.assert_integrity_and_delivery_drained()

    def test_unmatched_facts_do_not_create_a_run(self):
        event, _ = self.review(cpu=1)
        self.submit('Confirm test simulation', 'simulate-confirm')
        self.page.get_by_role('heading', name='No matching test decision', exact=True).wait_for()
        self.assertIn(event, self.page.locator('main').inner_text())
        self.assertEqual(self.clients['alice'].search_interventions('observed', mode='test')['total'], 0)
        self.worker(test=True); self.worker()
        self.assertEqual(self.receiver.requests, []); self.auton_history(0)
        self.assert_integrity_and_delivery_drained()

    def test_current_test_rights_revoked_after_review_prevent_admission(self):
        self.review()
        for grant in self.engine_config['grants']:
            if grant['subject'] == 'alice':
                grant['operations'] = [op for op in grant['operations'] if op != 'test']
        self.write_engine()
        self.submit('Confirm test simulation', 'simulate-confirm', 409)
        self.worker(test=True); self.worker()
        self.assertEqual(self.receiver.requests, []); self.auton_history(0)
        self.assert_integrity_and_delivery_drained()
