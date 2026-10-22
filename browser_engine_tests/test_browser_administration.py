"""Native administration with installed services and an authenticated HTTP source."""
import base64
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import hashlib
import http.client
import secrets
import ssl
import sys
import sqlite3
import threading
import time

import yaml
from atraxis.accounts import Directory, default_directory
from atraxis.account_store import AccountStore
from atraxis.account_entities import GRANT_OPERATIONS
from atraxis.inventory_access import load_profile
from test_browser_creation import HTTPSBrowserFixture
from atropos_client.client import RuleClient, RemoteRuleError
from playwright.sync_api import Error as BrowserError
from urllib.parse import urlsplit


class AdministrationTests(HTTPSBrowserFixture):
    def setUp(self):
        super().setUp()
        data = default_directory('alice').document()
        data['organizations'][0]['id'] = 'demo'
        data['memberships'][0]['organization'] = 'demo'
        data['organizations'].append(dict(id='other', name='Other organization',
                                         tenant='default', enabled=True))
        data['users'] += [dict(id=user, name=user, enabled=True) for user in ('bob', 'charlie')]
        data['memberships'] += [dict(user='bob', organization='demo', group=None),
                                dict(user='charlie', organization='other', group=None)]
        data['groups'] = [dict(id='ops', name='Operators', organization='demo',
                              parent=None, inherit_parent=False, enabled=True)]
        data['delegations'] = [dict(id='members', subject='alice', organization='demo',
            groups=['ops'], operations=['membership.add', 'membership.remove'],
            organization_membership=True, delegable=True, enabled=True, parent=None)]
        data['entity_delegations'] = [dict(id='people', subject='alice',
            scope=dict(kind='global', id=None), operations=['user.create', 'user.update'],
            parent=None, enabled=True, delegable=False)]
        self.accounts = AccountStore(self.root / 'accounts.db')
        self.accounts.apply(Directory(data), None)
        identities = [dict(username=user, subject=user) for user in ('alice', 'bob', 'charlie')]
        self.write_yaml('accounts-admin.yml', dict(version=1, store='accounts.db', identities=identities))
        self.access_path = self.root / 'access.yml'
        self.write_yaml('access.yml', dict(version=3, store='accounts.db', identities=identities,
            administrators=[dict(subject='alice', organization='demo',
                                 targets=[dict(kind='server', id='host')])],
            servers=[dict(id='host', organization='demo', sources=['monit'])],
            resource_groups=[], audit=[], grants=[]))
        self.engine_config['membership'] = dict(backend='sqlite', path='accounts.db')
        self.engine_config['grants'][1].update(subject='ops', subject_kind='group')
        self.write_engine()
        self.stop('engine'); self.start_engine()
        self.source_requests = []
        owner = self

        class Source(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                expected = 'Basic ' + base64.b64encode(('reader:' + owner.password).encode()).decode()
                owner.source_requests.append((self.command, self.path, self.headers.get('Authorization') == expected))
                if self.path != '/v1/status' or self.headers.get('Authorization') != expected:
                    self.send_error(403)
                    return
                now = time.time()
                raw = json.dumps(dict(api_version=1, ready=True, running=True,
                    last_cycle_success=True, cycles_total=2, errors_total=0, age_seconds=0,
                    last_success_at=now, last_cycle_finished_at=now,
                    containers=[dict(id='a'*64, name='visible-web', status='running', health='healthy', cpu_percent=12),
                                dict(id='b'*64, name='private-worker', status='running', health='healthy', cpu_percent=4)])).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        source = ThreadingHTTPServer(('127.0.0.1', 0), Source)
        thread = threading.Thread(target=source.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(lambda: (source.shutdown(), source.server_close(), thread.join(5)))
        self.private('source.json', json.dumps(dict(username='reader', password=self.password)))
        config = yaml.safe_load((self.root / 'web.yml').read_text())
        config['general'].update(accounts_web_file='accounts-admin.yml', accounts_web_origin=self.origin,
            inventory_access_file='access.yml', inventory_admin_origin=self.origin)
        config['sources'] = [dict(source_id='monit', kind='monit_docker',
            origin='http://127.0.0.1:' + str(source.server_port), credentials_file='source.json',
            allow_loopback_http=True, timeout=1, poll_interval=1, max_age=10)]
        self.stop('web'); self.write_yaml('web.yml', config); self.start_web()
        # Administration authority deliberately grants no inventory read access.
        self.ready('web', lambda: bool(self.source_requests))
        status, _, body = self.request('/readyz?organization=demo')
        self.assertEqual(status, 503)
        self.assertEqual(json.loads(body)['reason'], 'no_visible_sources')
        self.reader = self.browser.new_context(http_credentials=dict(username='bob', password=self.password))
        self.addCleanup(self.reader.close)
        self.reader_page = self.reader.new_page()

    def account_review(self, operation, fields, *, page=None, status=200):
        page = page or self.page
        response = page.goto(self.origin + '/ui/accounts/edit?operation=' + operation)
        self.assertEqual(response.status, 200)
        for label, value in fields.items():
            page.get_by_label(label, exact=True).fill(value)
        self.submit('Review change', '/ui/accounts/review', status=status, page=page)
        if status == 200:
            page.get_by_label('I confirm this account change.', exact=True).wait_for()

    def account_confirm(self, status=303, *, page=None):
        page = page or self.page
        page.get_by_label('I confirm this account change.', exact=True).check()
        self.submit('Confirm change', '/ui/accounts/commit', status=status, page=page)
        if status == 303:
            page.wait_for_url(self.origin + '/ui/accounts')

    def membership_review(self, operation):
        self.account_review(operation, {'Organization ID': 'demo', 'User ID': 'bob',
            'Group ID (empty for organization membership)': 'ops'})

    def grant_review(self):
        self.assertEqual(self.page.goto(self.origin + '/ui/inventory-access').status, 200)
        self.follow('Add read grant')
        self.page.get_by_label('Recipient ID', exact=True).fill('bob')
        self.page.get_by_label('Source ID (container or certificate only)', exact=True).fill('monit')
        self.page.get_by_label('Target ID', exact=True).fill('a'*64)
        self.submit('Review grant', '/ui/inventory-access/review', status=200)
        self.page.get_by_label('I confirm this inventory access change.', exact=True).wait_for()

    def grant_confirm(self, status=303):
        self.page.get_by_label('I confirm this inventory access change.', exact=True).check()
        self.submit('Confirm access change', '/ui/inventory-access/commit', status=status)
        if status == 303:
            self.page.wait_for_url(self.origin + '/ui/inventory-access')

    def assert_accounts_integrity(self):
        with sqlite3.connect((self.root / 'accounts.db').as_uri() + '?mode=ro', uri=True) as db:
            self.assertEqual(db.execute('PRAGMA integrity_check').fetchall(), [('ok',)])
            self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(), [])
        self.assertTrue(self.source_requests)
        self.assertTrue(all(request == ('GET', '/v1/status', True) for request in self.source_requests))
        self.assertEqual(self.clients['alice'].read('cpu', self.first['revision'])['revision'],
                         self.first['revision'])
        self.assertTrue(self.web_tls.calls)
        self.assertTrue(self.engine_tls.calls)
        self.assert_sqlite_and_empty_delivery()

    def test_account_creation_membership_and_live_engine_revocation(self):
        baseline = self.accounts.snapshot()
        self.account_review('user.create', {'User ID (immutable)': 'eve', 'Name': 'Browser Eve'})
        self.assertEqual(self.accounts.snapshot(), baseline)
        self.account_confirm()
        created = self.accounts.snapshot()
        self.assertEqual(len(created['audit']), len(baseline['audit']) + 1)
        self.assertIn('eve', Directory(created['directory']).users)
        self.page.reload()
        self.assertEqual(self.accounts.snapshot(), created)
        self.assertEqual(self.reader_page.goto(self.origin + '/ui/accounts').status, 403)
        with self.assertRaises(PermissionError):
            self.clients['bob'].read('cpu', self.first['revision'])
        self.membership_review('membership.add')
        self.assertEqual(self.accounts.snapshot(), created)
        self.account_confirm()
        self.assertEqual(Directory(self.accounts.snapshot()['directory']).effective_groups('bob', 'demo'), ('ops',))
        self.assertEqual(self.clients['bob'].read('cpu', self.first['revision'])['revision'], self.first['revision'])
        self.assertEqual(self.reader_page.goto(self.origin + '/ui/rules?name=cpu&organization=demo').status, 200)
        self.membership_review('membership.remove'); self.account_confirm()
        revoked = self.accounts.snapshot()
        self.assertEqual(len(revoked['audit']), len(baseline['audit']) + 3)
        with self.assertRaises(PermissionError):
            self.clients['bob'].read('cpu', self.first['revision'])
        self.assertEqual(self.reader_page.reload().status, 403)
        self.stop('web'); self.stop('engine'); self.start_engine(); self.start_web()
        self.assertEqual(self.accounts.snapshot(), revoked)
        self.assertEqual(self.reader_page.reload().status, 403)
        self.assert_accounts_integrity()

    def test_account_authority_revocation_between_review_and_confirmation(self):
        self.membership_review('membership.add')
        snapshot = self.accounts.snapshot()
        data = deepcopy(snapshot['directory'])
        data['delegations'] = []; data['entity_delegations'] = []
        self.accounts.apply(Directory(data), snapshot['revision'])
        revoked = self.accounts.snapshot()
        self.account_confirm(status=409)
        self.assertEqual(self.accounts.snapshot(), revoked)
        self.assertEqual(self.page.goto(self.origin + '/ui/accounts').status, 403)
        self.assertEqual(Directory(revoked['directory']).effective_groups('bob', 'demo'), ())
        self.assert_accounts_integrity()

    def test_inventory_grant_removal_and_revoked_administrator(self):
        self.assertEqual(self.reader_page.goto(self.origin + '/ui?organization=demo').status, 200)
        self.assertNotIn('visible-web', self.reader_page.content())
        before = self.access_path.read_bytes()
        self.grant_review()
        self.assertEqual(self.access_path.read_bytes(), before)
        self.grant_confirm()
        self.page.reload()
        self.assertEqual(len(load_profile(self.access_path)[0]['audit']), 1)
        self.assertEqual(self.reader_page.reload().status, 200)
        self.reader_page.get_by_text('visible-web', exact=True).wait_for()
        self.assertNotIn('private-worker', self.reader_page.content())
        for width, height in ((1440, 1100), (390, 844)):
            self.page.set_viewport_size(dict(width=width, height=height))
            self.assertTrue(self.page.evaluate('document.documentElement.scrollWidth <= window.innerWidth'))
        self.follow('Remove this grant'); self.grant_confirm()
        self.reader_page.reload()
        self.assertNotIn('visible-web', self.reader_page.content())
        self.assertEqual(len(load_profile(self.access_path)[0]['audit']), 2)
        self.grant_review()
        profile = load_profile(self.access_path)[0]
        profile['administrators'] = []
        self.write_yaml('access.yml', profile)
        revoked = self.access_path.read_bytes()
        self.grant_confirm(status=403)
        self.assertEqual(self.access_path.read_bytes(), revoked)
        self.assertEqual(self.reader_page.goto(self.origin + '/ui/inventory-access').status, 403)
        self.assertEqual(self.reader_page.goto(self.origin + '/ui?organization=other').status, 403)
        self.assert_accounts_integrity()

    def bootstrap_entity_authority(self):
        # Trusted fixture bootstrap only; every operation below uses native forms.
        snapshot = self.accounts.snapshot()
        data = deepcopy(snapshot['directory'])
        data['entity_delegations'] = [dict(id='root-entities', subject='alice',
            scope=dict(kind='global', id=None), operations=sorted(GRANT_OPERATIONS),
            parent=None, enabled=True, delegable=True)]
        self.accounts.apply(Directory(data), snapshot['revision'])

    def account_change(self, operation, fields, *, page=None, actor='alice'):
        before = self.accounts.snapshot()
        self.account_review(operation, fields, page=page)
        self.assertEqual(self.accounts.snapshot(), before)  # Review never writes.
        self.account_confirm(page=page)
        after = self.accounts.snapshot()
        self.assertNotEqual(after['revision'], before['revision'])
        self.assertEqual(len(after['audit']), len(before['audit']) + 1)
        self.assertEqual(after['audit'][-1]['actor']['subject'], actor)
        self.assertEqual(after['audit'][-1]['actor']['operation'], operation)
        return Directory(after['directory'])

    def test_native_tenant_organization_nested_groups_and_disable_hierarchy(self):
        self.bootstrap_entity_authority()
        self.account_change('tenant.create', {'Tenant ID (immutable)': 'company', 'Name': 'Browser company'})
        self.account_change('organization.create', {'Organization ID (immutable)': 'office',
            'Name': 'Browser office', 'Tenant ID (immutable ownership)': 'company'})
        team = {'Group ID (immutable)': 'team', 'Name': 'Browser team',
            'Organization ID (immutable ownership)': 'office', 'Parent group ID (optional)': '',
            'Inherit parent members (yes/no)': 'no'}
        self.account_change('group.create', team)
        self.account_change('group.create', {'Group ID (immutable)': 'night', 'Name': 'Browser night',
            'Organization ID (immutable ownership)': 'office', 'Parent group ID (optional)': 'team',
            'Inherit parent members (yes/no)': 'yes'})
        self.account_change('user.create', {'User ID (immutable)': 'eve', 'Name': 'Browser Eve'})
        for group in ('', 'team'):
            directory = self.account_change('membership.add', {'Organization ID': 'office',
                'User ID': 'eve', 'Group ID (empty for organization membership)': group})
        self.assertEqual(directory.contexts('eve'), ('office',))
        self.assertEqual(directory.effective_groups('eve', 'office'), ('night', 'team'))
        self.assertEqual(directory.effective_groups('eve', 'demo'), ())
        # Entity administration alone still grants no operational permission.
        with self.assertRaises(PermissionError):
            self.clients['bob'].read('cpu', self.first['revision'])
        before = self.accounts.snapshot()
        self.assertEqual(self.reader_page.goto(self.origin + '/ui/accounts').status, 403)
        self.assertEqual(self.accounts.snapshot(), before)
        directory = self.account_change('user.update', {'User ID (immutable)': 'eve',
            'Name': 'Renamed Eve', 'Enabled (yes/no)': 'no'})
        self.assertEqual(directory.contexts('eve'), ())
        self.account_change('user.update', {'User ID (immutable)': 'eve',
            'Name': 'Renamed Eve', 'Enabled (yes/no)': 'yes'})
        directory = self.account_change('group.update', dict(team, **{'Enabled (yes/no)': 'no'}))
        self.assertEqual(directory.effective_groups('eve', 'office'), ())
        self.account_change('group.update', dict(team, **{'Enabled (yes/no)': 'yes'}))
        directory = self.account_change('organization.update', {'Organization ID (immutable)': 'office',
            'Name': 'Browser office', 'Tenant ID (immutable ownership)': 'company',
            'Enabled (yes/no)': 'no'})
        self.assertEqual(directory.contexts('eve'), ())
        directory = self.account_change('tenant.update', {'Tenant ID (immutable)': 'company',
            'Name': 'Browser company', 'Enabled (yes/no)': 'no'})
        self.assertFalse(directory.tenants['company']['enabled'])
        saved = self.accounts.snapshot()
        self.stop('web'); self.stop('engine'); self.start_engine(); self.start_web()
        self.assertEqual(self.accounts.snapshot(), saved)
        self.assertEqual(Directory(saved['directory']).effective_groups('eve', 'office'), ())
        self.assert_accounts_integrity()

    def test_native_bounded_entity_delegation_and_revoked_pending_confirmation(self):
        self.bootstrap_entity_authority()
        self.account_change('entity_delegation.create', {'Your entity delegation ID': 'root-entities',
            'User ID': 'bob', 'Scope (global, tenant, organization, group, user)': 'group',
            'Scope ID (empty for global)': 'ops', 'Permissions (comma separated)': 'group.create,group.update',
            'Allow further delegation (yes/no)': 'yes'})
        grants = self.accounts.snapshot()['directory']['entity_delegations']
        grant = next(g['id'] for g in grants if g['subject'] == 'bob')
        fields = {'Group ID (immutable)': 'delegated', 'Name': 'Delegated subgroup',
            'Organization ID (immutable ownership)': 'demo', 'Parent group ID (optional)': 'ops',
            'Inherit parent members (yes/no)': 'no'}
        directory = self.account_change('group.create', fields, page=self.reader_page, actor='bob')
        self.assertEqual(directory.groups['delegated']['parent'], 'ops')
        with self.assertRaises(PermissionError):
            self.clients['bob'].read('cpu', self.first['revision'])
        before = self.accounts.snapshot()
        for operation, values in (
            ('user.create', {'User ID (immutable)': 'forbidden', 'Name': 'Forbidden'}),
            ('group.create', dict(fields, **{'Group ID (immutable)': 'foreign',
                'Organization ID (immutable ownership)': 'other', 'Parent group ID (optional)': ''})),
            ('entity_delegation.create', {'Your entity delegation ID': grant, 'User ID': 'charlie',
                'Scope (global, tenant, organization, group, user)': 'global',
                'Scope ID (empty for global)': '', 'Permissions (comma separated)': 'group.create'})):
            self.account_review(operation, values, page=self.reader_page, status=403)
            self.assertEqual(self.accounts.snapshot(), before)
        self.account_review('group.create', dict(fields, **{'Group ID (immutable)': 'pending'}),
                            page=self.reader_page)
        self.account_change('entity_delegation.revoke', {'Entity delegation ID to revoke': grant})
        revoked = self.accounts.snapshot()
        self.account_confirm(status=409, page=self.reader_page)
        self.assertEqual(self.accounts.snapshot(), revoked)
        self.assertNotIn('pending', Directory(revoked['directory']).groups)
        self.assertEqual(self.reader_page.goto(self.origin + '/ui/accounts').status, 403)
        self.assert_accounts_integrity()

    def test_native_cycle_ownership_conflict_and_confirmation_replay_refused(self):
        self.bootstrap_entity_authority()
        self.account_change('group.create', {'Group ID (immutable)': 'child', 'Name': 'Child',
            'Organization ID (immutable ownership)': 'demo', 'Parent group ID (optional)': 'ops'})
        before = self.accounts.snapshot()
        for fields, status in (
            ({'Group ID (immutable)': 'ops', 'Name': 'Operators',
              'Organization ID (immutable ownership)': 'demo', 'Parent group ID (optional)': 'child'}, 400),
            ({'Group ID (immutable)': 'ops', 'Name': 'Operators',
              'Organization ID (immutable ownership)': 'other', 'Parent group ID (optional)': ''}, 403)):
            self.account_review('group.update', fields, status=status)
            self.assertEqual(self.accounts.snapshot(), before)
        self.account_review('user.create', {'User ID (immutable)': 'stale', 'Name': 'Stale'})
        other = self.session.new_page()
        self.addCleanup(other.close)
        self.account_change('user.create', {'User ID (immutable)': 'fresh', 'Name': 'Fresh'}, page=other)
        current = self.accounts.snapshot()
        self.account_confirm(status=409)
        self.assertEqual(self.accounts.snapshot(), current)
        self.assertNotIn('stale', Directory(current['directory']).users)
        self.account_review('user.create', {'User ID (immutable)': 'confirmed', 'Name': 'Confirmed'})
        token = self.page.locator('input[name=token]').input_value()
        for width, height in ((1440, 1100), (390, 844)):
            self.page.set_viewport_size(dict(width=width, height=height))
            self.assertTrue(self.page.evaluate('document.documentElement.scrollWidth <= window.innerWidth'))
        self.account_confirm()
        confirmed = self.accounts.snapshot()
        self.account_review('user.create', {'User ID (immutable)': 'replayed', 'Name': 'Replayed'})
        self.page.locator('input[name=token]').evaluate('(element, value) => element.value = value', token)
        self.account_confirm(status=403)
        self.assertEqual(self.accounts.snapshot(), confirmed)
        self.assertNotIn('replayed', Directory(confirmed['directory']).users)
        self.assert_accounts_integrity()

    def replace_private(self, name, content):
        self.private(name + '.next', content)
        (self.root / (name + '.next')).replace(self.root / name)

    def https_login_status(self, username, password):
        target = urlsplit(self.origin)
        connection = http.client.HTTPSConnection(target.hostname, target.port,
            context=ssl.create_default_context(cafile=str(self.ca.path)), timeout=7)
        authorization = 'Basic ' + base64.b64encode((username + ':' + password).encode()).decode()
        try:
            connection.request('GET', '/healthz', headers={'Authorization': authorization})
            response = connection.getresponse()
            body = response.read().decode()
            self.assertNotIn(password, body)
            return response.status
        finally:
            connection.close()

    def test_http_password_rotation_requires_restart_and_expires_pending_forms(self):
        self.account_review('user.create', {'User ID (immutable)': 'rotated-user', 'Name': 'Rotated user'})
        old_form = self.page.locator('input[name=token]').input_value()
        before = self.accounts.snapshot()
        rules_before = self.rule_snapshot()
        replacement = secrets.token_urlsafe(32)
        # Synthetic SHA-1 fixture only; production operators must use a strong
        # HTTPdis-compatible hash. Preserve every other login and source secret.
        digest = base64.b64encode(hashlib.sha1(replacement.encode()).digest()).decode()
        users = (self.root / 'users').read_text().splitlines()
        users = [line if not line.startswith('alice:') else 'alice:{SHA}' + digest for line in users]
        self.replace_private('users', '\n'.join(users) + '\n')
        self.assertEqual(self.https_login_status('alice', self.password), 200)
        self.assertEqual(self.https_login_status('alice', replacement), 401)
        self.stop('web')
        self.spawn('web', [sys.executable, '-m', 'atraxis.cli', 'serve',
                          '--config', str(self.root / 'web.yml')])
        self.ready('web', lambda: self.https_login_status('alice', replacement) == 200)
        self.assertEqual(self.https_login_status('alice', self.password), 401)
        self.assertEqual(self.https_login_status('bob', self.password), 200)
        # Chromium may surface a rejected Basic challenge as a navigation error;
        # the independent CA-verified request above requires the actual HTTP 401.
        try:
            response = self.page.goto(self.origin + '/ui/accounts')
        except BrowserError as error:
            self.assertIn('net::ERR_INVALID_AUTH_CREDENTIALS', str(error))
        else:
            self.assertEqual(response.status, 401)
        current = self.browser.new_context(http_credentials=dict(username='alice', password=replacement))
        self.addCleanup(current.close)
        page = current.new_page()
        page.on('pageerror', lambda error: self.errors.append(str(error)))
        self.account_review('user.create', {'User ID (immutable)': 'rotated-user', 'Name': 'Rotated user'},
                            page=page)
        page.locator('input[name=token]').evaluate('(element, value) => element.value = value', old_form)
        self.account_confirm(status=403, page=page)
        self.assertEqual(self.accounts.snapshot(), before)
        self.account_change('user.create', {'User ID (immutable)': 'rotated-user', 'Name': 'Rotated user'},
                            page=page)
        saved = self.accounts.snapshot()
        page.reload()
        self.assertEqual(self.accounts.snapshot(), saved)
        self.assertEqual(self.rule_snapshot(), rules_before)
        self.assert_accounts_integrity()

    def test_engine_token_rotation_reloads_same_client_without_restart_or_replay(self):
        old_token = self.tokens['alice']
        self.private('old-alice.token', old_token)
        self.write_yaml('old-alice.yml', dict(origin=self.engine_tls.origin,
            token_file='old-alice.token', ca_file=str(self.ca.path), timeout=5))
        stale = RuleClient(self.root / 'old-alice.yml',
                           expected_actor=dict(subject='alice', organization='demo'))
        self.addCleanup(stale.close)
        self.assertEqual(stale.read('cpu', self.first['revision'])['revision'], self.first['revision'])
        account_before, rules_before = self.accounts.snapshot(), self.rule_snapshot()
        engine_pid, web_pid = self.processes['engine'].pid, self.processes['web'].pid
        grants = deepcopy(self.engine_config['grants'])
        replacement = secrets.token_urlsafe(32)
        identity = next(entry for entry in self.engine_config['identities'] if entry['subject'] == 'alice')
        identity['token_sha256'] = hashlib.sha256(replacement.encode()).hexdigest()
        self.write_engine()
        # Revoke the old server-side identity first. The intentional mismatch
        # window must deny access; no previous-token or administrator fallback.
        for client in (stale, self.clients['alice']):
            calls = len(self.engine_tls.calls)
            with self.assertRaises(PermissionError):
                client.read('cpu', self.first['revision'])
            self.assertEqual(len(self.engine_tls.calls) - calls, 1)
        calls = len(self.engine_tls.calls)
        with self.assertRaises(PermissionError):
            stale.create_from_templates(['cpu-high'], name='revoked-creation',
                rule_kind='intervention', selections=[['server', 'lab']])
        self.assertEqual(len(self.engine_tls.calls) - calls, 1)
        self.assertEqual(self.page.goto(self.origin + '/ui/rules?name=cpu&organization=demo').status, 403)
        self.assertEqual(self.rule_snapshot(), rules_before)
        self.replace_private('alice.token', replacement + '\n')
        self.assertEqual(self.clients['alice'].read('cpu', self.first['revision'])['revision'],
                         self.first['revision'])
        self.open_rule(self.first['revision'])
        with self.assertRaises(PermissionError):
            stale.read('cpu', self.first['revision'])
        self.assertEqual((self.processes['engine'].pid, self.processes['web'].pid), (engine_pid, web_pid))
        self.assertEqual(self.engine_config['grants'], grants)
        self.assertEqual(self.accounts.snapshot(), account_before)
        self.assertEqual(self.rule_snapshot(), rules_before)
        self.assert_accounts_integrity()

    def test_rotated_wrong_identity_and_missing_token_fail_closed_without_cached_access(self):
        # Give Bob an independently verified read grant before testing a
        # credential substitution; a refusal must not merely reflect his rights.
        self.membership_review('membership.add')
        self.account_confirm()
        self.assertEqual(self.clients['bob'].read('cpu', self.first['revision'])['revision'],
                         self.first['revision'])
        account_before, rules_before = self.accounts.snapshot(), self.rule_snapshot()
        original = (self.root / 'alice.token').read_text()
        for user in ('bob', 'charlie'):
            self.replace_private('alice.token', self.tokens[user] + '\n')
            calls = len(self.engine_tls.calls)
            with self.assertRaises(PermissionError):
                self.clients['alice'].read('cpu', self.first['revision'])
            self.assertEqual(len(self.engine_tls.calls) - calls, 1)
            self.assertEqual(self.page.goto(self.origin + '/ui/rules?name=cpu&organization=demo').status, 403)
            self.assertEqual(self.rule_snapshot(), rules_before)
            self.assertEqual(self.accounts.snapshot(), account_before)
        (self.root / 'alice.token').unlink()
        calls = len(self.engine_tls.calls)
        with self.assertRaises(RemoteRuleError) as failure:
            self.clients['alice'].read('cpu', self.first['revision'])
        self.assertEqual(str(failure.exception), 'rule_service_unavailable')
        self.assertEqual(len(self.engine_tls.calls), calls)
        self.assertEqual(self.page.goto(self.origin + '/ui/rules?name=cpu&organization=demo').status, 503)
        self.assertEqual(self.rule_snapshot(), rules_before)
        self.assertEqual(self.accounts.snapshot(), account_before)
        self.replace_private('alice.token', original)
        self.assertEqual(self.clients['alice'].read('cpu', self.first['revision'])['revision'],
                         self.first['revision'])
        self.open_rule(self.first['revision'])
        self.assert_accounts_integrity()
