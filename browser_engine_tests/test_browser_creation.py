"""Native new drafts with verified browser and independent-client HTTPS."""
from copy import deepcopy
import os
import sqlite3
import subprocess

import yaml
from playwright.sync_api import Error as BrowserError
from atropos_client.client import RuleClient
from test_browser_engine import BrowserEngineFixture
from tls_fixture import Authority, TLSIngress


class HTTPSBrowserFixture(BrowserEngineFixture):
    def rule_snapshot(self):
        with sqlite3.connect((self.root / 'engine.db').as_uri() + '?mode=ro', uri=True) as connection:
            return tuple(connection.iterdump())

    def prepare_browser(self):
        self.ca = Authority(self.root, 'browser-acceptance')
        self.web_tls = TLSIngress(self.origin, self.ca.leaf('web'))
        self.addCleanup(self.web_tls.close)
        self.engine_tls = TLSIngress('http://127.0.0.1:' + str(self.engine_port), self.ca.leaf('engine'))
        self.addCleanup(self.engine_tls.close)
        self.origin = self.web_tls.origin
        for user in self.tokens:
            self.clients[user].close()
            self.write_yaml(user + '.yml', dict(origin=self.engine_tls.origin,
                token_file=user + '.token', ca_file=str(self.ca.path), timeout=5))
            self.clients[user] = RuleClient(self.root / (user + '.yml'), expected_actor=dict(
                subject=user, organization='other' if user == 'charlie' else 'demo'))
            self.addCleanup(self.clients[user].close)
        config = yaml.safe_load((self.root / 'web.yml').read_text())
        config['general']['rules_web_origin'] = self.origin
        self.write_yaml('web.yml', config)
        self.stop('web'); self.start_web()
        # Trust only this disposable CA in this browser's isolated NSS directory.
        # Never use ignore_https_errors or certificate-error bypass arguments.
        home = self.root / 'browser-home'
        database = home / '.pki' / 'nssdb'
        database.mkdir(parents=True, mode=0o700)
        subprocess.run(['certutil', '-N', '--empty-password', '-d', 'sql:' + str(database)],
                       check=True, timeout=10, capture_output=True)
        subprocess.run(['certutil', '-A', '-d', 'sql:' + str(database), '-n', 'test-ca',
                        '-t', 'C,,', '-i', str(self.ca.path)], check=True, timeout=10, capture_output=True)
        return dict(env=dict(os.environ, HOME=str(home)))

    def creation_form(self, name, *, route='create', page=None, document=None):
        page = page or self.page
        self.assertEqual(page.goto(self.origin + '/ui/rules/' + route + '?organization=demo').status, 200)
        page.get_by_label('New rule name', exact=True).fill(name)
        page.get_by_label('Target ID', exact=True).fill('lab')
        if route == 'create':
            page.get_by_label('Template', exact=True).select_option('cpu-high')
        else:
            page.get_by_label('Rule YAML', exact=True).fill(yaml.safe_dump(document))
        return page.locator('input[name=token]').input_value()

    def create_submit(self, *, route='create', status=303, page=None):
        page = page or self.page
        self.submit('Create draft', '/ui/rules/' + route, status=status, page=page)
        if status == 303:
            page.get_by_role('heading', name='Selected revision', exact=True).wait_for()


class HTTPSCreationTests(HTTPSBrowserFixture):
    def test_native_catalog_and_import_create_only_new_drafts(self):
        client = self.clients['alice']
        baseline = deepcopy(client.history('cpu'))
        for route, name in (('create', 'browser-created'), ('import', 'browser-imported')):
            document = deepcopy(self.first['document']); document['name'] = name
            token = self.creation_form(name, route=route, document=document)
            for width, height in ((1440, 1100), (390, 844)):
                self.page.set_viewport_size(dict(width=width, height=height))
                self.assertTrue(self.page.evaluate('document.documentElement.scrollWidth <= window.innerWidth'))
            self.create_submit(route=route)
            draft = client.draft(name)
            self.assertEqual(draft['document']['name'], name)
            if route == 'import':
                self.assertEqual(draft['document'], document)
            self.assertEqual(draft['context'], dict(version=1, rule_kind='intervention', targets=[['server', 'lab']]))
            self.assertIsNone(client.active(name)['revision'])
            self.assertEqual(client.audit(name), [])
            history = deepcopy(client.history(name)); self.assertEqual(len(history), 1)
            self.page.reload(); self.assertEqual(client.history(name), history)
            self.creation_form(name, route=route, document=document)
            self.create_submit(route=route, status=409)
            self.assertEqual(client.history(name), history)
            self.creation_form(name, route=route, document=document)
            self.page.locator('input[name=token]').evaluate('(element, value) => element.value = value', token)
            self.create_submit(route=route, status=403)
            self.assertEqual(client.history(name), history)
        self.assertEqual(client.history('cpu'), baseline)
        self.assertTrue(self.engine_tls.calls); self.assertTrue(self.web_tls.calls)
        self.assert_sqlite_and_empty_delivery()

    def test_current_rights_scope_and_revoked_create_form(self):
        client = self.clients['alice']
        before = self.rule_snapshot()
        reader = self.browser.new_context(http_credentials=dict(username='bob', password=self.password))
        self.addCleanup(reader.close)
        page = reader.new_page()
        self.creation_form('reader-denied', page=page)
        self.create_submit(page=page, status=403)
        self.assertEqual(client.history('reader-denied'), [])
        outsider = self.browser.new_context(http_credentials=dict(username='charlie', password=self.password))
        self.addCleanup(outsider.close)
        self.assertEqual(outsider.new_page().goto(self.origin + '/ui/rules/create?organization=demo').status, 403)
        self.engine_config['resources'].append(dict(organization='demo', kind='server', id='ungranted'))
        self.write_engine()
        self.creation_form('scope-denied')
        self.page.get_by_label('Target ID', exact=True).fill('ungranted')
        self.create_submit(status=403)
        self.assertEqual(client.history('scope-denied'), [])
        token = self.creation_form('revoked-create')
        original = deepcopy(self.engine_config['grants'])
        for grant in self.engine_config['grants']:
            if grant['subject'] == 'alice':
                grant['operations'] = ['read']
        self.write_engine()
        self.create_submit(status=403)
        self.engine_config['grants'] = original; self.write_engine()
        self.creation_form('revoked-create')
        self.page.locator('input[name=token]').evaluate('(element, value) => element.value = value', token)
        self.create_submit(status=403)
        self.assertEqual(client.history('revoked-create'), [])
        self.assertEqual(self.rule_snapshot(), before)
        self.assert_sqlite_and_empty_delivery()

    def test_import_name_and_document_are_checked_before_mutation(self):
        client = self.clients['alice']
        before = self.rule_snapshot()
        document = deepcopy(self.first['document'])
        self.creation_form('wrong-name', route='import', document=document)
        self.create_submit(route='import', status=400)
        self.assertEqual(client.history('wrong-name'), [])
        self.creation_form('invalid-document', route='import', document={'name': 'invalid-document'})
        self.create_submit(route='import', status=400)
        self.assertEqual(client.history('invalid-document'), [])
        self.assertEqual(len(client.history('cpu')), 1)
        self.assertEqual(self.rule_snapshot(), before)
        self.assert_sqlite_and_empty_delivery()

    def test_browser_and_engine_reject_invalid_certificates_without_mutation(self):
        other = Authority(self.root, 'untrusted')
        certificates = [other.leaf('unknown-ca'), self.ca.leaf('wrong-host', wrong_host=True),
                        self.ca.leaf('expired', expired=True)]
        for certificate in certificates:
            with self.subTest(certificate=certificate[0].name):
                self.web_tls.certificate(certificate)
                before = list(self.web_tls.calls)
                session = self.browser.new_context(http_credentials=dict(username='alice', password=self.password))
                try:
                    with self.assertRaisesRegex(BrowserError, 'ERR_CERT_'):
                        session.new_page().goto(self.origin + '/ui/rules/create?organization=demo')
                    self.assertEqual(self.web_tls.calls, before)
                finally:
                    session.close()
        self.web_tls.certificate(self.ca.leaf('web-restored'))
        token = self.creation_form('bad-engine')
        self.engine_tls.certificate(other.leaf('engine-untrusted'))
        before = list(self.engine_tls.calls)
        self.create_submit(status=503)
        self.page.get_by_text('Outcome unconfirmed.', exact=False).wait_for()
        self.assertEqual(self.engine_tls.calls, before)
        self.engine_tls.certificate(self.ca.leaf('engine-restored'))
        self.assertEqual(self.clients['alice'].history('bad-engine'), [])
        self.creation_form('bad-engine')
        self.page.locator('input[name=token]').evaluate('(element, value) => element.value = value', token)
        self.create_submit(status=403)
        self.assertEqual(self.clients['alice'].history('bad-engine'), [])
        self.assert_sqlite_and_empty_delivery()
