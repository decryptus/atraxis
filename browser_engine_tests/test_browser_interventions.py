"""Native read-only follow-up of real Auton effects and synthetic alert contracts."""
from copy import deepcopy
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from urllib.parse import urlencode

from playwright.sync_api import sync_playwright
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'engine_tests'))
from test_https_journey import HTTPSJourneyFixture


class BrowserInterventionFixture(HTTPSJourneyFixture):
    def write_engine(self):
        self.engine_config['storage'] = dict(backend='sqlite', url='sqlite:///'+str(self.root/'engine.db'))
        self.engine_config['queue'] = dict(backend='redis',
            url='redis://:'+self.storage_password+'@127.0.0.1:%s/1' % self.redis_port)
        super().write_engine()

    def setUp(self):
        super().setUp()
        config = yaml.safe_load((self.root/'web.yml').read_text())
        config['general']['rules_web_file'] = 'mapping.yml'
        self.write_yaml('web.yml', config)
        self.stop('web'); self.start_web()
        home = self.root/'browser-home'
        database = home/'.pki'/'nssdb'
        database.mkdir(parents=True, mode=0o700)
        for args in (['-N', '--empty-password'], ['-A', '-n', 'test-ca', '-t', 'C,,', '-i', str(self.ca.path)]):
            subprocess.run(['certutil', '-d', 'sql:'+str(database), *args], check=True, capture_output=True, timeout=10)
        playwright = sync_playwright().start(); self.addCleanup(playwright.stop)
        self.browser = playwright.chromium.launch(env=dict(os.environ, HOME=str(home)))
        self.addCleanup(self.browser.close)
        self.session = self.browser.new_context(http_credentials=dict(username='alice', password=self.password))
        self.addCleanup(self.session.close)
        self.page = self.session.new_page()
        self.errors = []
        self.page.on('pageerror', lambda error: self.errors.append(str(error)))

    def tearDown(self):
        self.assertEqual(self.errors, [])

    def search(self, mode='real'):
        response = self.page.goto(self.https_origin+'/ui/interventions?organization=demo&name=observed')
        self.assertEqual(response.status, 200)
        self.page.get_by_label('Mode', exact=True).select_option(mode)
        with self.page.expect_navigation():
            self.page.get_by_role('button', name='Search', exact=True).click()

    def open_event(self, event):
        with self.page.expect_navigation() as response:
            self.page.get_by_role('link', name=event, exact=True).click()
        self.assertEqual(response.value.status, 200, self.page.locator('main').inner_text())
        self.page.get_by_role('heading', name='Intervention details', exact=True).wait_for()

    def assert_status(self, value):
        self.page.get_by_text('Status: '+value, exact=True).wait_for()

    def auton_history(self, count):
        with sqlite3.connect((self.auton.root/'jobs.db').as_uri()+'?mode=ro', uri=True) as connection:
            rows = connection.execute('SELECT uid,status,record FROM jobs ORDER BY uid').fetchall()
        self.assertEqual(len(rows), count)
        return rows

    def assert_integrity_and_delivery_drained(self):
        for path in (self.root/'engine.db', self.auton.root/'jobs.db'):
            with sqlite3.connect(path.as_uri()+'?mode=ro', uri=True) as connection:
                self.assertEqual(connection.execute('PRAGMA integrity_check').fetchall(), [('ok',)])
                self.assertEqual(connection.execute('PRAGMA foreign_key_check').fetchall(), [])
        raw = subprocess.check_output([self.engine_python, '-c',
            'import redis,sys,json; r=redis.Redis(host="127.0.0.1",port=int(sys.argv[1]),'
            'password=sys.stdin.read(),db=1); print(json.dumps([(r.xlen(k),'
            '[g["pending"] for g in r.xinfo_groups(k)]) for k in r.scan_iter() if r.type(k)==b"stream"]))',
            str(self.redis_port)], input=self.storage_password, text=True, timeout=5)
        for length, pending in json.loads(raw):
            self.assertEqual(length, 0); self.assertTrue(all(value == 0 for value in pending))


class BrowserInterventionTests(BrowserInterventionFixture):
    def test_signed_admission_real_auton_alert_and_read_only_browser_history(self):
        admitted = self.dispatch('browser-real')
        self.assertTrue(admitted['observation']['verified_at_admission'])
        self.search(); self.open_event('browser-real'); self.assert_status('queued')
        self.assertFalse((self.auton.root/'effect.txt').exists())
        self.assertEqual(self.receiver.requests, [])
        self.worker(); self.page.reload(); self.assert_status('queued')
        self.assertEqual((self.auton.root/'effect.txt').read_text(), 'done')
        self.assertEqual(self.receiver.requests, [])
        self.worker(); self.page.reload(); self.assert_status('succeeded')
        self.assertEqual(len(self.receiver.requests), 1)
        self.assertEqual(self.receiver.requests[0][0], '/api/v2/alerts')
        self.assertEqual(self.receiver.requests[0][1][0]['annotations']['atropos_run_id'], admitted['id'])
        state = deepcopy(self.clients['alice'].dispatch_status('observed', 'browser-real'))
        auton = self.auton_history(1)
        self.assertEqual([entry['step'] for entry in state['history']], ['run', 'run', 'notify', 'notify'])
        for width, height in ((1440, 1100), (390, 844)):
            self.page.set_viewport_size(dict(width=width, height=height)); self.page.reload()
            self.assertTrue(self.page.evaluate('document.documentElement.scrollWidth <= innerWidth'))
            self.assertEqual(self.page.get_by_role('button').count(), 0)
        self.stop('web'); self.stop('engine'); self.stop('storage')
        self.start_storage(); self.start_engine(); self.start_web()
        self.page.reload(); self.assert_status('succeeded')
        self.assertEqual(self.clients['alice'].dispatch_status('observed', 'browser-real'), state)
        self.assertEqual(self.auton_history(1), auton)
        self.assertEqual(len(self.receiver.requests), 1)
        self.assert_integrity_and_delivery_drained()

    def test_lost_alert_ack_stays_uncertain_after_refresh_restart_and_worker(self):
        self.dispatch('browser-unknown'); self.worker()
        self.receiver.mode = 'lost'; self.worker()
        self.search(); self.open_event('browser-unknown'); self.assert_status('uncertain')
        self.page.get_by_role('alert').get_by_text('Outcome unknown.', exact=False).wait_for()
        self.assertEqual(len(self.receiver.requests), 1)
        state = deepcopy(self.clients['alice'].dispatch_status('observed', 'browser-unknown'))
        auton = self.auton_history(1)
        self.receiver.mode = 'ok'
        self.stop('engine'); self.start_engine(); self.worker(); self.page.reload()
        self.assert_status('uncertain')
        self.assertEqual(self.clients['alice'].dispatch_status('observed', 'browser-unknown'), state)
        self.assertEqual(self.auton_history(1), auton)
        self.assertEqual(len(self.receiver.requests), 1)
        self.assertEqual((self.auton.root/'effect.txt').read_text(), 'done')
        self.assert_integrity_and_delivery_drained()

    def test_marked_simulation_has_no_auton_effect_and_preserves_test_history(self):
        (self.root/'executor.password').unlink()
        self.private('facts.json', json.dumps(dict(cpu_percent=95)))
        admitted = self.operator('test-dispatch', 'observed', '--target', 'container:web', '--binding', self.binding,
            '--input', str(self.root/'facts.json'), '--event-id', 'browser-test')
        self.worker(); self.assertEqual(self.receiver.requests, [])
        self.worker(test=True); self.worker(test=True)
        self.search(mode='test'); self.open_event('browser-test'); self.assert_status('succeeded')
        self.page.get_by_text('Test simulation', exact=True).wait_for()
        state = self.clients['alice'].test_dispatch_status('observed', 'browser-test')
        self.assertEqual(state['test_context'], admitted['test_context'])
        self.assertTrue(all(entry['test_context'] == admitted['test_context'] for entry in state['history']))
        self.assertEqual(state['results']['notify']['alert']['test_context'], admitted['test_context'])
        self.assertEqual(self.receiver.requests[0][0], '/v1/test-alerts')
        self.assertEqual(self.proxies['auton'].calls, [])
        self.assertFalse((self.auton.root/'effect.txt').exists())
        self.auton_history(0)
        self.page.reload(); self.assertEqual(len(self.receiver.requests), 1)
        self.assert_integrity_and_delivery_drained()

    def test_browser_own_run_cross_organization_and_live_rights_revocation(self):
        admitted = self.dispatch('browser-private')
        self.search(); self.open_event('browser-private')
        destination = self.page.url
        for user, organization in (('bob', 'demo'), ('charlie', 'other')):
            context = self.browser.new_context(http_credentials=dict(username=user, password=self.password))
            try:
                page = context.new_page()
                response = page.goto(self.https_origin+'/ui/interventions/detail?'+urlencode(dict(
                    name='observed', mode='real', id=admitted['id'], organization=organization)))
                self.assertIn(response.status, (403, 404))
                self.assertNotIn('browser-private', page.content()); self.assertNotIn(admitted['id'], page.content())
            finally: context.close()
        for grant in self.engine_config['grants']:
            if grant['subject'] == 'alice':
                grant['operations'] = [op for op in grant['operations'] if op not in ('read', 'execute')]
        self.write_engine()
        self.assertIn(self.page.goto(destination).status, (403, 404))
        self.assertNotIn('browser-private', self.page.content())
        self.worker()
        self.assertFalse((self.auton.root/'effect.txt').exists()); self.assertEqual(self.receiver.requests, [])
        self.auton_history(0)
        self.assert_integrity_and_delivery_drained()
