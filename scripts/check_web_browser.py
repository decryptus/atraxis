"""Synthetic, real HTTPdis/Chromium acceptance; no production credentials."""
import base64
from copy import deepcopy
from dataclasses import replace
import hashlib
import json
import socket
from pathlib import Path
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]

from playwright.sync_api import sync_playwright
from atraxis.config import load_config
from atraxis.server import _Handler, build_context
from atraxis.service import ReadService
from test_web import inventory
from test_web_rules import REVISION
from test_prometheus import response as metric_response, SPEC
from atraxis.source_config import SourceConfig
from atraxis.transport import TransportResult
from web_editor_fixture import RetirementLifecycleFake, NEW_REVISION, CASES


def rule_post(response, path):
    return (response.request.method == 'POST' and urlsplit(response.url).path == path
            and parse_qs(urlsplit(response.url).query).get('organization') == ['demo'])


class FixtureService(ReadService):
    def __init__(self):
        super().__init__()
        self.payload = inventory()
        certificate = deepcopy(self.payload['sources'][0])
        certificate.update(kind='certlord', source_id='certificates-lab')
        certificate['last_valid_snapshot']['resources'] = [dict(native_id='fixture-certificate',
            fields=dict(domains=['demo.example.test'], status='exists', material_state='missing',
                        not_after=None, renewal_owner='external'))]
        self.payload['sources'].append(certificate)
        metrics = ReadService([SourceConfig('covenant-orders', 'prometheus', 'https://prometheus.example',
            '/synthetic/unused.json', None, 60, 10, 180, 1048576, 10, 2, SPEC)], clock=lambda: 100.)
        metrics.set_running(True)
        sampled_at = time.time()
        metrics.record('covenant-orders', TransportResult('covenant-orders', 'success', sampled_at, sampled_at,
                                                        100., metric_response(sampled_at-20)))
        self.payload['sources'].extend(metrics.status()['sources'])

    def status(self):
        with self._lock:
            return deepcopy(self.payload)


def main():
    output = ROOT / 'web-browser-artifacts'
    output.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        digest = base64.b64encode(hashlib.sha1(b'fixture-password').digest()).decode()
        (root / 'users').write_text('reader:{SHA}' + digest + '\n')
        mapping = root / 'web-rules.yml'
        mapping.write_text(yaml.safe_dump(dict(version=1, identities=[dict(username='reader',
            subject='demo-operator', organization='demo', atropos_client='engine-client.yml')])))
        mapping.chmod(0o600)
        (root / 'config.yml').write_text('general:\n  auth_basic_file: users\n  rules_web_file: web-rules.yml\n')
        with socket.socket() as reservation:
            reservation.bind(('127.0.0.1', 0))
            port = reservation.getsockname()[1]
        origin = 'http://127.0.0.1:%s' % port
        from atraxis.accounts import Directory
        from atraxis.account_store import AccountStore
        from test_account_delegation import fixture as account_fixture
        accounts=AccountStore(root/'accounts.json');accounts.apply(Directory(account_fixture()),None)
        profile=root/'accounts-admin.yml'
        profile.write_text(yaml.safe_dump(dict(version=1,store='accounts.json',identities=[dict(username='reader',subject='alice')])))
        profile.chmod(0o600)
        service = FixtureService()
        owner = SimpleNamespace(destinations=[], calls=[], closed=0, error=None, revoke=False,
                                document_message='Review a high CPU observation', head=REVISION)
        context = build_context(replace(load_config(root / 'config.yml'), listen_port=port, rules_web_origin=origin, accounts_web_file=str(profile), accounts_web_origin=origin), service,
            rule_client_factory=lambda path, expected_actor: RetirementLifecycleFake(owner, path, expected_actor))
        started = threading.Event()
        context.named_commands['GET /healthz'].at_start = lambda options: started.set()
        thread = threading.Thread(target=lambda: context.run(http_req_handler=_Handler), daemon=True)
        thread.start()
        try:
            assert started.wait(5), 'server did not start'
            origin = 'http://127.0.0.1:%s' % context.server.server_address[1]
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch()
                anonymous = browser.new_context()
                assert anonymous.request.get(origin + '/ui').status == 401
                anonymous.close()
                authenticated = browser.new_context(http_credentials=dict(username='reader', password='fixture-password'))
                page = authenticated.new_page()
                errors = []
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.on('console', lambda message: errors.append(message.text)
                        if message.type == 'error' and 'content security policy' in message.text.lower() else None)
                for name, width, height in [('desktop', 1440, 1100), ('mobile', 390, 844)]:
                    page.set_viewport_size(dict(width=width, height=height))
                    response = page.goto(origin + '/ui')
                    assert response.status == 200
                    assert page.get_by_role('heading', name='Inventory overview').is_visible()
                    assert page.locator('.source').count() == 3
                    assert page.get_by_text('orders_pending', exact=True).is_visible()
                    assert page.get_by_text('Prometheus metrics', exact=True).is_visible()
                    assert page.locator('body').evaluate('(el) => getComputedStyle(el).backgroundColor') == 'rgb(9, 17, 31)', 'CSP blocked the stylesheet'
                    assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth'), 'page overflows viewport'
                    page.get_by_text('Details', exact=True).first.focus()
                    page.keyboard.press('Enter')
                    assert page.locator('details').first.get_attribute('open') is not None
                    page.get_by_role('link', name='Refresh snapshot').click()
                    assert page.locator('details[open]').count() == 0
                    page.screenshot(path=str(output / (name + '.png')), full_page=True)
                with service._lock:
                    service.payload.update(ready=False, reason='sources_not_current')
                    service.payload['sources'][0].update(current=False, availability='unavailable',
                        last_attempt=dict(outcome='timeout'))
                page.get_by_role('link', name='Refresh snapshot').click()
                assert page.get_by_text('Historical snapshot', exact=True).is_visible()
                assert page.get_by_text('Current at page load', exact=True).first.is_visible()
                page.set_viewport_size(dict(width=1440, height=1100))
                page.screenshot(path=str(output / 'historical.png'), full_page=True)
                page.get_by_role('link', name='Rules', exact=True).click()
                page.get_by_label('Rule name').fill('cpu')
                page.get_by_role('button', name='Open rule').click()
                assert page.get_by_role('heading', name='Revision history').is_visible()
                page.get_by_role('link', name=REVISION, exact=True).first.click()
                assert page.get_by_role('heading', name='Selected revision').is_visible()
                assert page.get_by_text('Active at page load', exact=True).is_visible()
                for name, width, height in [('rules-desktop',1440,1100),('rules-mobile',390,844)]:
                    page.set_viewport_size(dict(width=width,height=height))
                    assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
                    page.screenshot(path=str(output / (name+'.png')),full_page=True)
                page.set_viewport_size(dict(width=1440,height=1100))
                page.get_by_role('link',name='Edit current draft',exact=True).click()
                document = page.get_by_label('Rule YAML').input_value()
                page.get_by_label('Rule YAML').fill(document.replace('gt: 90','gt: 92'))
                page.get_by_label('Revision message').fill('Raise the CPU review threshold')
                for name,width,height in [('editor-desktop',1440,1100),('editor-mobile',390,844)]:
                    page.set_viewport_size(dict(width=width,height=height))
                    assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
                    page.screenshot(path=str(output/(name+'.png')),full_page=True)
                with page.expect_response(lambda response: rule_post(response, '/ui/rules/edit')) as saved:
                    page.get_by_role('button',name='Save new draft').click()
                assert saved.value.request.headers.get('origin') == origin
                assert saved.value.status == 303, (saved.value.status, saved.value.text())
                assert 'revision='+NEW_REVISION in page.url
                assert page.get_by_text('Not the active revision',exact=True).is_visible()
                page.get_by_role('link',name='Test this revision',exact=True).click()
                page.get_by_label('Scenarios JSON').fill(json.dumps(CASES,indent=2))
                with page.expect_response(lambda response: rule_post(response, '/ui/rules/test')) as tested:
                    page.get_by_role('button',name='Run scenarios').click()
                assert tested.value.status == 303, (tested.value.status, tested.value.text())
                assert page.get_by_text('All scenarios passed',exact=True).is_visible()
                page.reload()
                for name,width,height in [('test-report-desktop',1440,1100),('test-report-mobile',390,844)]:
                    page.set_viewport_size(dict(width=width,height=height))
                    assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
                    page.screenshot(path=str(output/(name+'.png')),full_page=True)
                assert sum(call[0]=='save_draft' for call in owner.calls)==1
                assert sum(call[0]=='test' for call in owner.calls)==1
                page.get_by_role('link',name='Review activation',exact=True).click()
                assert page.get_by_role('heading',name='Review activation',exact=True).is_visible()
                assert page.get_by_role('heading',name='Targets',exact=True).is_visible()
                confirmation = page.get_by_label('I confirm activation of this revision for the targets shown above.')
                assert not confirmation.is_checked()
                assert not any(call[0]=='activate' for call in owner.calls)
                for name,width,height in [('activation-desktop',1440,1100),('activation-mobile',390,844)]:
                    page.set_viewport_size(dict(width=width,height=height))
                    assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
                    page.screenshot(path=str(output/(name+'.png')),full_page=True)
                confirmation.check()
                with page.expect_response(lambda response: rule_post(response, '/ui/rules/activate')) as activated:
                    page.get_by_role('button',name='Confirm activation',exact=True).click()
                assert activated.value.status == 303, (activated.value.status, activated.value.text())
                assert page.get_by_text('Active at page load',exact=True).is_visible()
                page.reload()
                assert sum(call[0]=='activate' for call in owner.calls)==1
                page.goto(origin+'/ui/rules?name=cpu&revision='+REVISION)
                page.get_by_role('link',name='Test this revision',exact=True).click()
                page.get_by_label('Scenarios JSON').fill(json.dumps(CASES))
                page.get_by_role('button',name='Run scenarios',exact=True).click()
                assert page.get_by_text('All scenarios passed',exact=True).is_visible()
                page.get_by_role('link',name='Review rollback',exact=True).click()
                assert page.get_by_role('heading',name='Review rollback',exact=True).is_visible()
                confirmation=page.get_by_label('I confirm rollback of this revision for the targets shown above.')
                assert not confirmation.is_checked()
                for name,width,height in [('rollback-desktop',1440,1100),('rollback-mobile',390,844)]:
                    page.set_viewport_size(dict(width=width,height=height))
                    assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
                    page.screenshot(path=str(output/(name+'.png')),full_page=True)
                confirmation.check()
                with page.expect_response(lambda response: rule_post(response, '/ui/rules/rollback')) as restored:
                    page.get_by_role('button',name='Confirm rollback',exact=True).click()
                assert restored.value.status==303, (restored.value.status,restored.value.text())
                assert page.get_by_text('Active at page load',exact=True).is_visible()
                page.reload()
                assert sum(call[0]=='rollback' for call in owner.calls)==1
                page.get_by_role('link',name='Review deactivation',exact=True).click()
                confirmation=page.get_by_label('I confirm deactivation of this rule for the targets shown above.')
                assert not confirmation.is_checked()
                for name,width,height in [('deactivation-desktop',1440,1100),('deactivation-mobile',390,844)]:
                    page.set_viewport_size(dict(width=width,height=height))
                    assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
                    page.screenshot(path=str(output/(name+'.png')),full_page=True)
                confirmation.check()
                with page.expect_response(lambda response: rule_post(response, '/ui/rules/deactivate')) as stopped:
                    page.get_by_role('button',name='Confirm deactivation',exact=True).click()
                assert stopped.value.status==303, (stopped.value.status,stopped.value.text())
                assert page.get_by_text('Active revision: Not active',exact=True).is_visible()
                page.reload()
                assert sum(call[0]=='deactivate' for call in owner.calls)==1
                common=dict(author='demo-operator',organization='demo',created_at=1791385200.125,
                            revision=REVISION,scope_revisions=[REVISION])
                owner.events.extend([
                    dict(common,operation='dispatch',generation='e'*32,binding='b'*64,run_id='d'*64),
                    dict(common,operation='test_run',test_context={'test':True,'id':'f'*64},run_id='f'*64)])
                page.get_by_role('link',name='Lifecycle audit',exact=True).click()
                for heading in ('Activation','Rollback','Deactivation','Intervention admitted','Test run admitted'):
                    assert page.get_by_role('heading',name=heading,exact=True).is_visible()
                page.reload()
                assert page.locator('.info').count()==2
                assert page.locator('.info').first.evaluate('(el) => getComputedStyle(el).borderLeftColor')=='rgb(148, 211, 255)'
                assert page.get_by_text('Test mode · Test ID:',exact=False).is_visible()
                for name,width,height in [('audit-desktop',1440,1100),('audit-mobile',390,844)]:
                    page.set_viewport_size(dict(width=width,height=height))
                    assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
                    page.screenshot(path=str(output/(name+'.png')),full_page=True)
                page.get_by_role('link',name='Next window',exact=True).click()
                assert page.get_by_text('No accessible events in this audit window.',exact=False).is_visible()
                assert page.get_by_role('link',name='Next window',exact=True).is_visible()
                for operation in ('archive','unarchive','delete'):
                    page.goto(origin+'/ui/rules/'+operation+'?name=cpu')
                    assert page.get_by_role('heading',name='Review '+operation,exact=True).is_visible()
                    confirmation=page.get_by_label('I confirm '+operation+' for this rule.')
                    assert not confirmation.is_checked()
                    for size,width,height in [('desktop',1440,1100),('mobile',390,844)]:
                        page.set_viewport_size(dict(width=width,height=height))
                        assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
                        page.screenshot(path=str(output/(operation+'-'+size+'.png')),full_page=True)
                    if operation!='delete':
                        confirmation.check()
                        with page.expect_response(lambda response:rule_post(response, '/ui/rules/'+operation)) as retired:
                            page.get_by_role('button',name='Confirm '+operation,exact=True).click()
                        assert retired.value.status==303
                        page.reload()
                        assert sum(call[0]==operation for call in owner.calls)==1
                page.goto(origin+'/ui/rules?name=cpu&revision='+NEW_REVISION)
                mapping.write_text('version: 1\nidentities: []\n')
                assert page.reload().status == 403
                assert page.get_by_text('Organization access denied. Refresh to check your access.',exact=True).is_visible()
                assert not page.get_by_role('heading',name='Selected revision').count()
                assert all(call[0] in ('history','active','read','save_draft','test','test_report','activate','rollback','deactivate','audit','availability','archive','unarchive') for call in owner.calls)
                for size,width,height in [('desktop',1440,1100),('mobile',390,844)]:
                    page.set_viewport_size(dict(width=width,height=height))
                    assert page.goto(origin+'/ui/accounts').status==200
                    assert page.get_by_role('heading',name='People & delegations',exact=True).is_visible()
                    assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
                    page.screenshot(path=str(output/('accounts-'+size+'.png')),full_page=True)
                page.get_by_role('link',name='Add membership',exact=True).click()
                page.get_by_label('Organization ID',exact=True).fill('default')
                page.get_by_label('User ID',exact=True).fill('charlie')
                page.get_by_label('Group ID (empty for organization membership)',exact=True).fill('ops')
                with page.expect_response(lambda response:response.request.method=='POST' and response.url.endswith('/ui/accounts/review')) as reviewed:
                    page.get_by_role('button',name='Review change',exact=True).click()
                assert reviewed.value.status==200, page.locator('main').inner_text()
                page.get_by_role('heading',name='Review: Add membership',exact=True).wait_for(state='visible')
                for size,width,height in [('desktop',1440,1100),('mobile',390,844)]:
                    page.set_viewport_size(dict(width=width,height=height))
                    assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
                    page.screenshot(path=str(output/('accounts-review-'+size+'.png')),full_page=True)
                page.get_by_label('I confirm this account change.',exact=True).check()
                with page.expect_response(lambda response:response.request.method=='POST' and response.url.endswith('/ui/accounts/commit')) as committed:
                    page.get_by_role('button',name='Confirm change',exact=True).click()
                assert committed.value.status==303, page.locator('main').inner_text()
                assert len(accounts.snapshot()['audit'])==2
                page.reload();assert len(accounts.snapshot()['audit'])==2
                assert not errors, errors
                authenticated.close()
                browser.close()
            print('Browser acceptance passed: inventory and scoped rule views, desktop/mobile, keyboard, editing, scenarios, safe report refresh, explicit activation/rollback/deactivation, audit windows and revocation.')
        finally:
            context.stop()
            thread.join(7)
            assert not thread.is_alive(), 'server did not stop'


def scoped_inventory_acceptance():
    from test_inventory_access import InventoryWebTests
    from atraxis.accounts import Directory
    fixture = InventoryWebTests()
    try:
        fixture.setUp()
        origin = 'http://127.0.0.1:%s' % fixture.port
        output = ROOT / 'web-browser-artifacts'
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            session = browser.new_context(http_credentials=dict(username='alice', password='synthetic-password'))
            page = session.new_page()
            assert page.goto(origin+'/ui').status == 200
            assert page.get_by_text('Select an organization to view its inventory.', exact=True).is_visible()
            assert page.locator('.source-heading').count() == 0
            page.get_by_role('link', name='Default organization', exact=True).focus()
            page.keyboard.press('Enter')
            page.get_by_role('heading', name='docker-lab', exact=True).wait_for()
            assert 'hidden-source' not in page.content() and 'second-source' not in page.content()
            for name,width,height in [('desktop',1440,1100),('mobile',390,844)]:
                page.set_viewport_size(dict(width=width,height=height))
                assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
                page.screenshot(path=str(output/('inventory-context-'+name+'.png')), full_page=True)
            other = session.new_page()
            other.goto(origin+'/ui?organization=second')
            assert other.get_by_role('heading', name='second-source', exact=True).is_visible()
            page.get_by_role('link', name='Refresh snapshot', exact=True).click()
            assert page.get_by_role('heading', name='docker-lab', exact=True).is_visible()
            page.get_by_role('link', name='Second organization', exact=True).click()
            assert page.get_by_role('heading', name='second-source', exact=True).is_visible()
            assert session.request.get(origin+'/v1/status?organization=hidden').status == 403
            snap=fixture.store.snapshot();data=snap['directory']
            data['memberships']=[m for m in data['memberships'] if m['organization']!='second']
            fixture.store.apply(Directory(data),snap['revision'])
            assert page.reload().status == 403
            assert 'secret-marker' not in page.content()
            session.close();browser.close()
        print('Scoped inventory acceptance passed: selection, keyboard, desktop/mobile, independent tabs, API denial and live revocation.')
    finally:
        fixture.doCleanups()


def resource_inventory_acceptance():
    from test_resource_scopes import ResourceHTTPTests, partial_profile
    fixture = ResourceHTTPTests()
    try:
        fixture.setUp()
        server = fixture.fixture
        origin = 'http://127.0.0.1:%s' % server.port
        output = ROOT / 'web-browser-artifacts'
        profile = partial_profile()
        profile['grants'][0]['targets'] = [dict(kind='group', id='applications')]
        server.profile.write_text(yaml.safe_dump(profile))
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            session = browser.new_context(http_credentials=dict(username='alice', password='synthetic-password'))
            page = session.new_page()
            assert page.goto(origin+'/ui?organization=default').status == 200
            assert page.locator('.source-heading').count() == 2
            assert page.get_by_text('Selected resources only.', exact=False).count() == 2
            assert 'private-container-marker' not in page.content() and 'private-domain-marker' not in page.content()
            response = session.request.get(origin+'/v1/status?organization=default')
            assert response.status == 200
            assert all(s['visibility']=='resources' and s['last_valid_snapshot']['source_metadata'] is None
                       and len(s['last_valid_snapshot']['resources'])==1 for s in response.json()['sources'])
            for name,width,height in [('desktop',1440,1100),('mobile',390,844)]:
                page.set_viewport_size(dict(width=width,height=height))
                assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
                page.screenshot(path=str(output/('inventory-resources-'+name+'.png')), full_page=True)
            profile['grants'][0]['targets'] = [dict(kind='server',id='host-a')]
            server.profile.write_text(yaml.safe_dump(profile))
            page.reload()
            assert page.get_by_text('private-container-marker',exact=True).is_visible()
            profile['grants'] = []
            server.profile.write_text(yaml.safe_dump(profile))
            page.reload()
            assert page.get_by_role('heading',name='No sources available',exact=True).is_visible()
            assert 'private-container-marker' not in page.content()
            session.close();browser.close()
        print('Resource scope acceptance passed: group filtering, metadata redaction, server breadth, live revocation and desktop/mobile captures.')
    finally:
        fixture.doCleanups()


def inventory_admin_acceptance():
    from test_inventory_admin import GrantWebTests
    from atraxis.inventory_access import load_profile
    fixture=GrantWebTests()
    try:
        fixture.setUp()
        output=ROOT/'web-browser-artifacts'
        with sync_playwright() as playwright:
            browser=playwright.chromium.launch()
            session=browser.new_context(http_credentials=dict(username='alice',password='synthetic-password'))
            page=session.new_page()
            assert page.goto(fixture.origin+'/ui/inventory-access').status==200
            assert 'hidden-source' not in page.content() and 'second-source' not in page.content()
            for name,width,height in [('desktop',1440,1100),('mobile',390,844)]:
                page.set_viewport_size(dict(width=width,height=height))
                assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
                page.screenshot(path=str(output/('inventory-admin-'+name+'.png')),full_page=True)
            page.get_by_role('link',name='Add read grant',exact=True).click()
            page.get_by_label('Recipient ID',exact=True).fill('bob')
            page.get_by_label('Source ID (container, certificate or metric only)',exact=True).fill('docker-lab')
            page.get_by_label('Target ID',exact=True).fill('b'*64)
            with page.expect_response(lambda response:response.request.method=='POST' and response.url.endswith('/ui/inventory-access/review')) as reviewed:
                page.get_by_role('button',name='Review grant',exact=True).click()
            assert reviewed.value.status==200, reviewed.value.text()
            page.get_by_role('heading',name='Review: Add read grant',exact=True).wait_for()
            for name,width,height in [('desktop',1440,1100),('mobile',390,844)]:
                page.set_viewport_size(dict(width=width,height=height))
                assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
                page.screenshot(path=str(output/('inventory-admin-review-'+name+'.png')),full_page=True)
            assert load_profile(fixture.profile)[0]['audit']==[]
            page.get_by_label('I confirm this inventory access change.',exact=True).check()
            with page.expect_navigation(wait_until='domcontentloaded') as redirected, page.expect_response(lambda response:response.request.method=='POST' and response.url.endswith('/ui/inventory-access/commit')) as saved:
                page.get_by_role('button',name='Confirm access change',exact=True).click()
            assert saved.value.status==303,saved.value.text()
            assert redirected.value.status==200
            assert page.reload().status==200
            assert len(load_profile(fixture.profile)[0]['audit'])==1
            reader=browser.new_context(http_credentials=dict(username='bob',password='synthetic-password'))
            response=reader.request.get(fixture.origin+'/v1/status?organization=default')
            assert response.status==200 and response.json()['sources'][0]['last_valid_snapshot']['resources'][0]['native_id']=='b'*64
            page.get_by_role('link',name='Remove this grant',exact=True).last.click()
            page.get_by_label('I confirm this inventory access change.',exact=True).check()
            with page.expect_navigation(wait_until='domcontentloaded') as redirected, page.expect_response(lambda response:response.request.method=='POST' and response.url.endswith('/ui/inventory-access/commit')) as removed:
                page.get_by_role('button',name='Confirm access change',exact=True).click()
            assert removed.value.status==303
            assert redirected.value.status==200
            assert reader.request.get(fixture.origin+'/v1/status?organization=default').json()['sources']==[]
            assert len(load_profile(fixture.profile)[0]['audit'])==2
            reader.close();session.close();browser.close()
        print('Inventory grant administration passed: scoped listing, review/confirmation, reader effect, removal, no replay and desktop/mobile captures.')
    finally:
        fixture.doCleanups()


def inventory_search_acceptance():
    from test_inventory_search import InventorySearchHTTPTests
    fixture=InventorySearchHTTPTests();fixture.setUp();fixture.prepare()
    output=ROOT/'web-browser-artifacts';output.mkdir(exist_ok=True)
    try:
        data=yaml.safe_load(fixture.profile.read_text())
        data['grants'][0]['targets']=[dict(kind='source',id='docker-lab')]
        fixture.profile.write_text(yaml.safe_dump(data))
        snapshot=fixture.service.payload['sources'][0]['last_valid_snapshot']
        template=deepcopy(snapshot['resources'][0]);snapshot['resources']=[]
        for i in range(251):
            row=deepcopy(template);row['native_id']=format(i,'064x');row['fields']['name']='search-needle' if i==250 else 'worker-'+str(i)
            snapshot['resources'].append(row)
        origin='http://127.0.0.1:'+str(fixture.port)
        with sync_playwright() as p:
            browser=p.chromium.launch()
            session=browser.new_context(http_credentials=dict(username='alice',password='synthetic-password'))
            page=session.new_page()
            page.goto(origin+'/ui/search?organization=default&limit=1')
            assert '251 matching resources' in page.content()
            page.get_by_role('link',name='Next',exact=True).click()
            assert 'page=2' in page.url and 'organization=default' in page.url
            page.get_by_label('Name or identifier',exact=True).fill('SEARCH-NEEDLE')
            page.get_by_role('button',name='Search',exact=True).click()
            assert '1 matching resources' in page.content() and 'search-needle' in page.content()
            assert 'private-container-marker' not in page.content() and 'extra-lab' not in page.content()
            for name,width,height in [('desktop',1440,1000),('mobile',390,844)]:
                page.set_viewport_size(dict(width=width,height=height))
                assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
                page.screenshot(path=str(output/('inventory-search-'+name+'.png')),full_page=True)
            page.get_by_role('link',name='Reset filters',exact=True).click()
            assert '251 matching resources' in page.content()
            data['grants']=[];fixture.profile.write_text(yaml.safe_dump(data));page.reload()
            assert '0 matching resources' in page.content() and 'search-needle' not in page.content()
            session.close();browser.close()
        print('Inventory search passed: beyond-display-limit lookup, scoped paging, casefold, reset, revocation and desktop/mobile layouts.')
    finally:fixture.doCleanups()


def account_search_acceptance():
    from test_web_accounts import AccountWebTests
    fixture=AccountWebTests();fixture.setUp()
    output=ROOT/'web-browser-artifacts';output.mkdir(exist_ok=True)
    try:
        with sync_playwright() as p:
            browser=p.chromium.launch()
            session=browser.new_context(http_credentials=dict(username='alice-login',password='synthetic-password'))
            page=session.new_page();page.goto(fixture.origin+'/ui/accounts')
            page.get_by_role('link',name='Search accounts',exact=True).click()
            page.get_by_label('Type',exact=True).select_option('group')
            page.get_by_label('Text (user ID, group or organization ID/name)',exact=True).fill('OPS')
            page.get_by_role('button',name='Search',exact=True).click()
            assert '1 matches' in page.content() and 'secret' not in page.content()
            for name,width,height in [('desktop',1440,1000),('mobile',390,844)]:
                page.set_viewport_size(dict(width=width,height=height))
                assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
                page.screenshot(path=str(output/('account-search-'+name+'.png')),full_page=True)
            page.get_by_role('link',name='Reset filters',exact=True).click()
            assert '7 matches' in page.content()
            session.close();browser.close()
        print('Account search passed: form, casefold, reset, authorized groups and responsive layouts.')
    finally:fixture.doCleanups()


def intervention_search_acceptance():
    from unittest.mock import patch
    from test_web_rules import WebRulesTests,FakeClient
    from test_intervention_search import result
    calls=[]
    def search(client,name,**values):
        calls.append(values)
        data=result();data.update(subject=client.actor['subject'],organization=client.actor['organization'],query=values)
        data['items'][0]['target']=client.actor['organization']+'/container/web'
        return data
    fixture=WebRulesTests();fixture.setUp()
    output=ROOT/'web-browser-artifacts';output.mkdir(exist_ok=True)
    try:
        with patch.object(FakeClient,'search_interventions',search,create=True),sync_playwright() as p:
            browser=p.chromium.launch()
            session=browser.new_context(http_credentials=dict(username='alice',password='fixture-password'))
            page=session.new_page();page.goto('http://127.0.0.1:'+str(fixture.port)+'/ui/rules')
            page.get_by_role('link',name='Interventions',exact=True).click()
            page.get_by_label('Rule name',exact=True).fill('cpu')
            page.get_by_label('Text',exact=True).fill('web')
            page.get_by_label('Mode',exact=True).select_option('test')
            page.get_by_label('From (UTC)',exact=True).fill('2026-10-08T00:00')
            page.get_by_role('button',name='Search',exact=True).click()
            assert calls[-1]['mode']=='test' and calls[-1]['since']==1791417600
            assert '1 matches' in page.content()
            for name,width,height in [('desktop',1440,1100),('mobile',390,844)]:
                page.set_viewport_size(dict(width=width,height=height))
                assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
                page.screenshot(path=str(output/('intervention-search-'+name+'.png')),full_page=True)
            page.get_by_role('link',name='Reset filters',exact=True).click()
            assert calls[-1]['query']=='' and calls[-1]['mode']=='all' and calls[-1]['since'] is None
            fixture.write_mapping([]);page.reload()
            assert 'access denied' in page.content() and 'one/container/web' not in page.content()
            session.close();browser.close()
        print('Intervention search passed: forwarding, UTC filters, reset, revocation and responsive layouts.')
    finally:fixture.doCleanups()


if __name__ == '__main__':
    main()
    scoped_inventory_acceptance()
    resource_inventory_acceptance()
    inventory_admin_acceptance()
    inventory_search_acceptance()
    account_search_acceptance()
    intervention_search_acceptance()
