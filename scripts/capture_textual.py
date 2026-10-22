"""Capture actual Textual widgets with synthetic data; no production service."""
import argparse
import asyncio
import hashlib
import importlib.metadata
import dwho.tui.textual
import json
import os
os.environ.pop("NO_COLOR", None)
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from types import SimpleNamespace
from atraxis.rule_textual import RuleApp, BindingPicker

class DemoRules:
    def history(self, *args):
        return [dict(revision='demo-revision-03', author='demo-operator'), dict(revision='demo-revision-02', author='demo-operator')]
    def active(self, *args):
        return dict(revision='demo-revision-02', generation='demo-generation-04')
    def read(self, *args):
        return dict(context=dict(rule_kind='supervision', targets=[['container_group', 'demo-services']]),
                    rule={'name': 'high-cpu', 'condition': 'cpu_percent > 85', 'decision': 'review'})
    def test_bindings(self, *args):
        return [dict(id='a'*64, rule='high-cpu', revision='demo-revision-03',
                     workflow='inspect-and-alert', actions=['core.echo', 'core.alert'], test_supported=True),
                dict(id='c'*64, rule='high-cpu', revision='demo-revision-02',
                     workflow='external-job', actions=['auton.job'], test_supported=False)]
    def simulate_dispatch(self, name, target, facts, binding):
        return dict(simulation=True, binding=binding, revision='demo-revision-03',
                    target=list(target), matches_binding=True)
    def test_dispatch(self, *args, **kwargs):
        return dict(id='b'*64, rule='high-cpu', revision='demo-revision-03',
                    test_context=dict(test=True, id='b'*64), status='queued')
    def test_dispatch_status(self, *args):
        return dict(self.test_dispatch(), status='succeeded', results={
            'intervene': {'status': 'simulated', '_test_context': {'test': True, 'id': 'b'*64}},
            'notify': {'alert': {'subject': '[TEST] Simulated intervention completed', 'delivery': 'simulated'}}},
            history=[{'step':'intervene','status':'queued'}, {'step':'notify','status':'succeeded'}])
    def audit(self, *args):
        return [{'event': 'rule.tested', 'revision': 'demo-revision-03', 'passed': True, 'synthetic': True}]
    def catalog(self):
        return [{'name': 'high-cpu', 'description': 'Review sustained CPU observations'}]


async def capture(output, png=False):
    output.mkdir(parents=True, exist_ok=True)
    artifacts = []
    app = RuleApp(SimpleNamespace(application=DemoRules()), 'high-cpu', demo=True)
    async with app.run_test(size=(140, 38)) as pilot:
        await pilot.pause(); await pilot.press('enter'); await pilot.pause()
        save(app, output, 'textual-rules', artifacts, png)
        app.model.reports['demo-revision-03'] = {'id': 'demo-test-03', 'passed': True}
        await pilot.press('a'); await pilot.pause()
        save(app, output, 'textual-review', artifacts, png)
        await pilot.press('escape'); await pilot.pause()
        app.push_screen(BindingPicker(app.model.available_test_bindings(('container', 'demo')), 'container:demo'))
        await pilot.pause()
        save(app, output, 'textual-test-bindings', artifacts, png)
        await pilot.press('escape'); await pilot.pause()
        with tempfile.TemporaryDirectory() as folder:
            facts = Path(folder)/'synthetic.json'; facts.write_text('{"cpu_percent":95}')
            app.prepare_test_run(dict(target='container_group:demo-services', binding='a'*64,
                                      facts=str(facts), event='demo-e2e-test'))
            await pilot.pause()
            save(app, output, 'textual-test-review', artifacts, png)
            await pilot.click('#dw-confirm'); await pilot.pause()
            save(app, output, 'textual-test-queued', artifacts, png)
            app.model.test_status('demo-e2e-test'); app.render_test_result()
            await pilot.pause()
            save(app, output, 'textual-test-result', artifacts, png)

    from atraxis.account_textual import AccountApp
    from atraxis.account_admin import AccountAdmin
    from atraxis.account_store import AccountStore
    from atraxis.accounts import Directory
    import yaml
    sys.path.insert(0,str(ROOT/'tests'))
    from test_account_delegation import fixture as account_fixture
    with tempfile.TemporaryDirectory() as folder:
        root=Path(folder);AccountStore(root/'directory.json').apply(Directory(account_fixture()),None)
        profile=root/'admin.yml'
        profile.write_text(yaml.safe_dump(dict(version=1,store='directory.json',identities=[dict(uid=os.getuid(),subject='alice')])))
        profile.chmod(0o600)
        admin=AccountAdmin(profile);accounts=AccountApp(admin)
        async with accounts.run_test(size=(140,38)) as pilot:
            await pilot.pause();save(accounts,output,'textual-accounts',artifacts,png)
            plan=admin.prepare(dict(operation='membership.add',organization='default',subject='charlie',group='ops'),admin.inspect()['revision'])
            accounts.review(plan);await pilot.pause();save(accounts,output,'textual-accounts-review',artifacts,png)

        from atraxis.account_search_textual import AccountSearchApp
        search=AccountSearchApp(admin)
        async with search.run_test(size=(140,38)) as pilot:
            await pilot.pause();save(search,output,'textual-account-search',artifacts,png)

    from atraxis.inventory_textual import InventoryAdminApp
    from atraxis.inventory_admin import InventoryAdmin
    from test_inventory_admin import admin_profile, candidate
    from test_inventory_access import directory_fixture
    with tempfile.TemporaryDirectory() as folder:
        root=Path(folder);directory=directory_fixture()
        directory['memberships'].append(dict(user='bob',organization='default',group=None))
        AccountStore(root/'directory.json').apply(Directory(directory),None)
        data=admin_profile();data['identities'].append(dict(uid=os.getuid(),subject='alice'))
        profile=root/'inventory.yml';profile.write_text(yaml.safe_dump(data));profile.chmod(0o600)
        admin=InventoryAdmin(profile);inventory=InventoryAdminApp(admin)
        async with inventory.run_test(size=(140,38)) as pilot:
            await pilot.pause();save(inventory,output,'textual-inventory-admin',artifacts,png)
            inventory.review(admin.prepare(candidate(),admin.inspect()['revision']))
            await pilot.pause();save(inventory,output,'textual-inventory-admin-review',artifacts,png)

    from atraxis.search_textual import InventorySearchApp
    from atraxis.inventory_search import SearchQuery, search_inventory
    from unittest.mock import patch
    from test_resource_scopes import observation_service
    payload=observation_service().status()
    payload['context']=dict(subject='demo-operator',organization='demo',organizations=[dict(id='demo',name='Demo')])
    for source in payload['sources']:
        for i,row in enumerate(source['last_valid_snapshot']['resources']):
            if row['kind']=='container':row['fields']['name']='api-worker-'+str(i+1)
    query=SearchQuery(q='docker')
    with patch('atraxis.search_textual.read_search', side_effect=lambda config, q:(search_inventory(payload,q),None)):
        search=InventorySearchApp(SimpleNamespace(organization=None),query)
        async with search.run_test(size=(140,38)) as pilot:
            await pilot.pause();save(search,output,'textual-inventory-search',artifacts,png)

    from atraxis.intervention_search_textual import InterventionSearchApp
    from test_intervention_search import result as intervention_result
    service=SimpleNamespace(name='cpu',search=lambda query:intervention_result(query))
    interventions=InterventionSearchApp(service)
    async with interventions.run_test(size=(140,38)) as pilot:
        await pilot.pause();save(interventions,output,'textual-intervention-search',artifacts,png)

    sources = {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
               for path in (ROOT / 'atraxis').rglob('*.py')}
    try:
        revision = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, stderr=subprocess.DEVNULL, text=True).strip()
        dirty = bool(subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT, text=True))
    except (OSError, subprocess.CalledProcessError):
        revision, dirty = None, True
    (output / 'textual-manifest.json').write_text(json.dumps(dict(synthetic=True,
        renderer='Textual', textual_version=importlib.metadata.version('textual'),
        dwho_version=importlib.metadata.version('dwho'),
        shared_source_sha256={p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                              for p in Path(dwho.tui.textual.__file__).parent.glob('*.py')}, source_revision=revision, dirty=dirty, source_sha256=sources, captures=artifacts), indent=2)+'\n')


def save(app, output, name, artifacts, png):
    path = output / (name + '.svg')
    path.write_text(app.export_screenshot(title='Atraxis · SYNTHETIC DEMO'))
    artifacts.append(dict(file=path.name, sha256=hashlib.sha256(path.read_bytes()).hexdigest()))
    if png:
        import resvg_py
        rendered = path.with_suffix('.png')
        rendered.write_bytes(resvg_py.svg_to_bytes(svg_path=str(path), monospace_family='DejaVu Sans Mono'))
        artifacts.append(dict(file=rendered.name, sha256=hashlib.sha256(rendered.read_bytes()).hexdigest()))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--png', action='store_true')
    args = parser.parse_args()
    asyncio.run(capture(args.output, args.png))
