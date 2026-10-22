"""Native Chromium forms against separately installed services, not client mocks.

Run explicitly with Playwright/Chromium and ATROPOS_TEST_PYTHON. The initial
draft is fixture setup via the engine API; rule creation is not browser coverage.
"""
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import sys
from urllib.parse import parse_qs, urlencode, urlsplit

import yaml
from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'engine_tests'))
from test_web_engine import CASES, WebEngineFixture


class BrowserEngineFixture(WebEngineFixture):
    def write_engine(self):
        # Both are real product adapters. Redis is delivery only in this suite.
        self.engine_config['storage'] = dict(
            backend='sqlite', url='sqlite:///' + str(self.root / 'engine.db'))
        self.engine_config['queue'] = dict(backend='redis',
            url='redis://:' + self.storage_password + '@127.0.0.1:%s/1' % self.redis_port)
        super().write_engine()

    def setUp(self):
        super().setUp()
        launch_options = self.prepare_browser()
        playwright = sync_playwright().start()
        self.addCleanup(playwright.stop)
        browser = playwright.chromium.launch(**launch_options)
        self.addCleanup(browser.close)
        self.browser = browser
        self.session = browser.new_context(http_credentials=dict(
            username='alice', password=self.password))
        self.addCleanup(self.session.close)
        self.page = self.session.new_page()
        self.errors = []
        self.page.on('pageerror', lambda error: self.errors.append(str(error)))

    def prepare_browser(self):
        return {}

    def tearDown(self):
        self.assertEqual(self.errors, [])

    def open_rule(self, revision=None):
        values = dict(name='cpu', organization='demo')
        if revision is not None:
            values['revision'] = revision
        response = self.page.goto(self.origin + '/ui/rules?' + urlencode(values))
        self.assertEqual(response.status, 200)
        self.page.get_by_role('heading', name='Selected revision', exact=True).wait_for()

    def follow(self, label):
        with self.page.expect_navigation(wait_until='domcontentloaded') as response:
            self.page.get_by_role('link', name=label, exact=True).click()
        self.assertEqual(response.value.status, 200, self.page.locator('main').inner_text())

    def submit(self, button, path, status=303, page=None):
        page = page or self.page
        with page.expect_response(lambda response:
                response.request.method == 'POST'
                and urlsplit(response.url).path == path) as result:
            page.get_by_role('button', name=button, exact=True).click()
        self.assertEqual(result.value.status, status)
        self.assertEqual(result.value.request.headers.get('origin'), self.origin)
        return result.value

    def browser_scenarios(self, revision):
        self.open_rule(revision)
        self.follow('Test this revision')
        self.page.get_by_label('Scenarios JSON', exact=True).fill(json.dumps(CASES))
        self.submit('Run scenarios', '/ui/rules/test')
        self.page.get_by_text('All scenarios passed', exact=True).wait_for()
        report_id = parse_qs(urlsplit(self.page.url).query)['report'][0]
        report = self.clients['alice'].test_report('cpu', revision, report_id)
        self.assertTrue(report['passed'])
        self.assertEqual(report['revision'], revision)
        return report

    def browser_activate(self, revision):
        self.browser_scenarios(revision)
        self.follow('Review activation')
        confirmation = self.page.get_by_label(
            'I confirm activation of this revision for the targets shown above.', exact=True)
        self.assertFalse(confirmation.is_checked())
        self.assertIsNone(self.clients['alice'].active('cpu')['revision'])
        confirmation.check()
        self.submit('Confirm activation', '/ui/rules/activate')
        self.page.get_by_text('Active at page load', exact=True).wait_for()
        self.assertEqual(self.clients['alice'].active('cpu')['revision'], revision)

    def assert_sqlite_and_empty_delivery(self):
        uri = (self.root / 'engine.db').as_uri() + '?mode=ro'
        with sqlite3.connect(uri, uri=True) as connection:
            self.assertEqual(connection.execute('PRAGMA integrity_check').fetchall(), [('ok',)])
            self.assertEqual(connection.execute('PRAGMA foreign_key_check').fetchall(), [])
        # Only inspect the authenticated, disposable Redis started by this fixture.
        import subprocess
        raw = subprocess.check_output([self.engine_python, '-c',
            'import redis,sys; r=redis.Redis(host="127.0.0.1",port=int(sys.argv[1]),'
            'password=sys.stdin.read(),db=1); print(r.dbsize())', str(self.redis_port)],
            cwd=self.root, env=self.environment, input=self.storage_password,
            text=True, timeout=5)
        self.assertEqual(int(raw), 0)  # Lifecycle never submits an intervention.

class BrowserEngineTests(BrowserEngineFixture):
    def test_native_rollback_conflict_confirmation_replay_and_audit(self):
        client = self.clients['alice']
        first = self.first['revision']
        self.browser_activate(first)
        self.open_rule(first)
        self.follow('Edit current draft')
        document = yaml.safe_load(self.page.get_by_label('Rule YAML').input_value())
        document['checks']['cpu-high']['gte'] = 92
        self.page.get_by_label('Rule YAML').fill(yaml.safe_dump(document))
        self.submit('Save new draft', '/ui/rules/edit')
        self.page.get_by_text('Not the active revision', exact=True).wait_for()
        second = parse_qs(urlsplit(self.page.url).query)['revision'][0]
        self.browser_scenarios(second)
        self.follow('Review activation')
        self.page.get_by_label(
            'I confirm activation of this revision for the targets shown above.', exact=True).check()
        self.submit('Confirm activation', '/ui/rules/activate')
        self.page.get_by_text('Active at page load', exact=True).wait_for()
        self.assertEqual(client.active('cpu')['revision'], second)
        self.browser_scenarios(first)
        report_url = self.page.url
        self.follow('Review rollback')
        self.assertEqual(client.active('cpu')['revision'], second)
        stale = self.page
        other = self.session.new_page()
        self.page = other
        try:
            self.open_rule(second)
            self.follow('Review deactivation')
            self.page.get_by_label(
                'I confirm deactivation of this rule for the targets shown above.', exact=True).check()
            self.submit('Confirm deactivation', '/ui/rules/deactivate')
            self.page.get_by_text('Active revision: Not active', exact=True).wait_for()
        finally:
            self.page = stale
            other.close()
        state, audit = deepcopy(client.active('cpu')), deepcopy(client.audit('cpu'))
        confirmation = 'I confirm rollback of this revision for the targets shown above.'
        self.page.get_by_label(confirmation, exact=True).check()
        self.submit('Confirm rollback', '/ui/rules/rollback', status=409)
        self.assertEqual(client.active('cpu'), state)
        self.assertEqual(client.audit('cpu'), audit)
        self.assertEqual(self.page.goto(report_url).status, 200)
        self.follow('Review rollback')
        token = self.page.locator('input[name=token]').input_value()
        self.page.get_by_label(confirmation, exact=True).check()
        self.submit('Confirm rollback', '/ui/rules/rollback')
        self.page.get_by_text('Active at page load', exact=True).wait_for()
        rolled_back, audit = deepcopy(client.active('cpu')), deepcopy(client.audit('cpu'))
        self.assertEqual(rolled_back['revision'], first)
        self.assertEqual(client.draft('cpu')['revision'], second)
        self.assertEqual([event['operation'] for event in audit],
                         ['activate', 'activate', 'deactivate', 'rollback'])
        self.page.reload()
        self.assertEqual(client.audit('cpu'), audit)
        # Reuse the consumed token in a new native form for the other revision.
        self.browser_scenarios(second)
        self.follow('Review rollback')
        self.page.locator('input[name=token]').evaluate(
            '(element, value) => element.value = value', token)
        self.page.get_by_label(confirmation, exact=True).check()
        self.submit('Confirm rollback', '/ui/rules/rollback', status=403)
        self.assertEqual(client.active('cpu'), rolled_back)
        self.assertEqual(client.audit('cpu'), audit)
        self.assertEqual(self.page.goto(self.origin + '/ui/rules/audit?name=cpu&organization=demo').status, 200)
        self.page.get_by_text('Rollback', exact=True).wait_for()
        self.page.reload()
        self.assertEqual(client.audit('cpu'), audit)
        self.assert_sqlite_and_empty_delivery()

    def test_native_edit_test_activation_and_deactivation(self):
        client = self.clients['alice']
        initial = self.first['revision']
        self.open_rule(initial)
        self.follow('Edit current draft')
        document = yaml.safe_load(self.page.get_by_label('Rule YAML').input_value())
        document['checks']['cpu-high']['gte'] = 92
        self.page.get_by_label('Rule YAML').fill(yaml.safe_dump(document))
        self.page.get_by_label('Revision message', exact=True).fill('Native browser threshold')
        self.assertEqual(client.draft('cpu')['revision'], initial)
        self.submit('Save new draft', '/ui/rules/edit')
        self.page.get_by_text('Not the active revision', exact=True).wait_for()
        revision = parse_qs(urlsplit(self.page.url).query)['revision'][0]
        self.assertNotEqual(revision, initial)
        self.assertEqual(client.draft('cpu')['revision'], revision)
        self.assertIsNone(client.active('cpu')['revision'])
        self.browser_activate(revision)
        self.assertEqual(client.evaluate_active('cpu', ['server', 'lab'],
            dict(cpu_percent=95))['result']['decision'], 'review')
        before = deepcopy(client.audit('cpu'))
        for width, height in ((1440, 1100), (390, 844)):
            self.page.set_viewport_size(dict(width=width, height=height))
            self.page.reload()
            self.assertTrue(self.page.evaluate(
                'document.documentElement.scrollWidth <= window.innerWidth'))
        self.assertEqual(client.audit('cpu'), before)
        self.page.get_by_role('link', name='Review deactivation', exact=True).focus()
        self.page.keyboard.press('Enter')
        self.page.get_by_label(
            'I confirm deactivation of this rule for the targets shown above.', exact=True).check()
        self.submit('Confirm deactivation', '/ui/rules/deactivate')
        self.page.get_by_text('Active revision: Not active', exact=True).wait_for()
        self.page.reload()
        self.assertIsNone(client.active('cpu')['revision'])
        self.assertEqual([x['operation'] for x in client.audit('cpu')], ['activate', 'deactivate'])
        self.assert_sqlite_and_empty_delivery()

    def test_reader_cross_organization_revocation_and_confirmation_replay(self):
        client = self.clients['alice']
        for user, path in (('bob', '/ui/rules/edit?name=cpu&organization=demo'),
                           ('charlie', '/ui/rules?name=cpu&organization=demo')):
            context = self.browser.new_context(http_credentials=dict(
                username=user, password=self.password))
            try:
                page = context.new_page()
                response = page.goto(self.origin + path)
                if user == 'bob':
                    # Read rights permit displaying the draft, never saving it.
                    self.assertEqual(response.status, 200)
                    page.get_by_label('Revision message', exact=True).fill('Reader denied')
                    self.submit('Save new draft', '/ui/rules/edit', status=403, page=page)
                    self.assertEqual(client.draft('cpu')['revision'], self.first['revision'])
                else:
                    self.assertEqual(response.status, 403)
                    self.assertNotIn(self.first['revision'], page.content())
            finally:
                context.close()
        self.browser_scenarios(self.first['revision'])
        report_url = self.page.url
        self.follow('Review activation')
        token = self.page.locator('input[name=token]').input_value()
        original = deepcopy(self.engine_config['grants'])
        for grant in self.engine_config['grants']:
            if grant['subject'] == 'alice':
                grant['operations'] = [operation for operation in grant['operations']
                                       if operation != 'activate']
        self.write_engine()
        self.page.get_by_label(
            'I confirm activation of this revision for the targets shown above.', exact=True).check()
        self.submit('Confirm activation', '/ui/rules/activate', status=403)
        self.assertIsNone(client.active('cpu')['revision'])
        self.assertEqual(client.audit('cpu'), [])
        self.engine_config['grants'] = original
        self.write_engine()
        self.assertEqual(self.page.goto(report_url).status, 200)
        self.follow('Review activation')
        # Replay the consumed token through the native form, after rights return.
        self.page.locator('input[name=token]').evaluate('(element, value) => element.value = value', token)
        self.page.get_by_label(
            'I confirm activation of this revision for the targets shown above.', exact=True).check()
        self.submit('Confirm activation', '/ui/rules/activate', status=403)
        self.assertIsNone(client.active('cpu')['revision'])
        self.assertEqual(client.audit('cpu'), [])
        self.assert_sqlite_and_empty_delivery()

    def test_recreation_preserves_sqlite_history_and_expires_native_form(self):
        client = self.clients['alice']
        revision = self.first['revision']
        self.browser_activate(revision)
        active, audit = deepcopy(client.active('cpu')), deepcopy(client.audit('cpu'))
        self.open_rule(revision)
        self.follow('Edit current draft')
        self.page.get_by_label('Revision message', exact=True).fill('Expired browser form')
        self.stop('web'); self.stop('engine'); self.stop('storage')
        self.start_storage(); self.start_engine(); self.start_web()
        self.submit('Save new draft', '/ui/rules/edit', status=403)
        self.assertEqual(client.draft('cpu')['revision'], revision)
        self.assertEqual(client.active('cpu'), active)
        self.assertEqual(client.audit('cpu'), audit)
        self.open_rule(revision)
        self.stop('engine')
        self.assertEqual(self.page.reload().status, 503)
        self.assertNotIn(revision, self.page.content())
        self.start_engine()
        self.assertEqual(self.page.reload().status, 200)
        self.assertEqual(client.active('cpu'), active)
        self.assertEqual(client.audit('cpu'), audit)
        self.assert_sqlite_and_empty_delivery()

    def test_parallel_native_confirmation_rejects_changed_active_generation(self):
        client = self.clients['alice']
        revision = self.first['revision']
        self.browser_activate(revision)
        self.follow('Review deactivation')
        stale_page = self.page
        other = self.session.new_page()
        self.page = other
        try:
            self.open_rule(revision)
            self.follow('Review deactivation')
            other.get_by_label(
                'I confirm deactivation of this rule for the targets shown above.', exact=True).check()
            self.submit('Confirm deactivation', '/ui/rules/deactivate')
            other.get_by_text('Active revision: Not active', exact=True).wait_for()
            self.browser_activate(revision)
        finally:
            self.page = stale_page
            other.close()
        active, audit = deepcopy(client.active('cpu')), deepcopy(client.audit('cpu'))
        stale_page.get_by_label(
            'I confirm deactivation of this rule for the targets shown above.', exact=True).check()
        self.submit('Confirm deactivation', '/ui/rules/deactivate', status=409)
        self.assertEqual(client.active('cpu'), active)
        self.assertEqual(client.audit('cpu'), audit)
        self.assertEqual([x['operation'] for x in audit], ['activate', 'deactivate', 'activate'])
        self.assert_sqlite_and_empty_delivery()
