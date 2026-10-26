"""Cross-host uncertainty assertions through installed clients and guarded workers."""
import hashlib
import json
from pathlib import Path
import secrets
import time

import yaml

from operator_access import private
from uncertain_target import CONFIG_PATH, ORIGIN

ACTION = 'recovery.alert'
RULE = 'recovery-alert'
EVENT = 'lost-receipt'
TARGET = ['server', 'receipt-fixture']
DEADLINE_SECONDS = 60
CLIENT_CONFIG = '/etc/atraxis/engine-client.yml'
SCRIPT = Path(__file__).with_name('uncertain_target.py')
CLIENT = '''from atropos_client.client import RuleClient, RemoteRuleError
import json, sys
configuration, method, args, kwargs = json.loads(sys.argv[1])
with_client = RuleClient(configuration, expected_actor={'subject':'alice','organization':'lab'})
try:
    try:
        result = getattr(with_client, method)(*args, **kwargs)
    except PermissionError:
        result = {'denied': True}
    except RemoteRuleError as error:
        result = {'error': str(error)}
    print(json.dumps(result, sort_keys=True))
finally:
    with_client.close()
'''


def client(compose, method, *args, **kwargs):
    return json.loads(compose('exec', '-T', 'web', 'python', '-c', CLIENT,
        json.dumps([CLIENT_CONFIG, method, args, kwargs])).stdout)


def snapshot(compose, run_id, probe=False):
    return json.loads(compose('exec', '-T', 'engine', 'python', '-c',
        SCRIPT.read_text(), 'probe' if probe else 'snapshot', run_id).stdout)


def submit(compose, evidence):
    return client(compose, 'dispatch', RULE, TARGET, {'cpu_percent':95},
        evidence['binding'], expected_generation=evidence['generation'], event_id=EVENT)


def prepare(case, compose, site):
    config = site / 'engine/engine.yml'
    document = yaml.safe_load(config.read_text())
    document['resources'].append(dict(organization='lab', kind=TARGET[0], id=TARGET[1]))
    grant = document['grants'][0]
    grant['scopes'].append(TARGET)
    grant['actions'].append(ACTION)
    document['execution_adapters'] = {ACTION: dict(backend='alertmanager',
        target='lab/' + '/'.join(TARGET), origin=ORIGIN, allow_loopback_http=True,
        token_file='recovery-alert.token', timeout=5,
        labels=dict(alertname='SyntheticRecoveryReceipt'))}
    private(site / 'engine/recovery-alert.token', secrets.token_urlsafe(32))
    private(config, yaml.safe_dump(document))
    compose('up', '-d', '--no-build', '--pull', 'never', '--no-deps', '--force-recreate', 'engine')
    deadline = time.monotonic() + DEADLINE_SECONDS
    while 'error' in client(compose, 'catalog'):
        if time.monotonic() > deadline:
            case.fail('uncertain_fixture_engine_deadline')
        time.sleep(.5)
    created = client(compose, 'create_from_templates', ['cpu-high'], name=RULE,
        rule_kind='intervention', selections=[TARGET])
    revision = created['revision']
    tested = client(compose, 'test', RULE, revision, [dict(name='Synthetic alert',
        input=dict(cpu_percent=95), expected=dict(decision='review', matched_rule='combined'))])
    case.assertTrue(tested['passed'])
    active = client(compose, 'activate', RULE, revision, test_id=tested['id'], expected_generation=None)
    bindings_file = site / 'engine/bindings.yml'
    bindings = yaml.safe_load(bindings_file.read_text())
    bindings.append(dict(organization='lab', rule=RULE, revision=revision,
        result=dict(decision='review', matched_rule='combined'), input={}, max_age_seconds=300,
        workflow=dict(version=1, name='lost-receipt', start='send', steps={
            'send': dict(action=ACTION, timeout=10, next='later'),
            'later': dict(action=ACTION, timeout=10)})))
    private(bindings_file, yaml.safe_dump(bindings))
    approved = json.loads(compose('exec', '-T', 'engine', 'atropos', 'rules-bindings',
        '--config', CONFIG_PATH).stdout)
    binding, = [b['id'] for b in approved if b['rule'] == RULE]
    evidence = dict(binding=binding, generation=active['generation'])
    admitted = submit(compose, evidence)
    case.assertEqual(admitted['status'], 'queued')
    observed = snapshot(compose, admitted['id'], probe=True)
    state = observed['state']
    case.assertEqual(state['status'], 'uncertain')
    case.assertEqual(state['error'], 'action_outcome_unknown')
    case.assertEqual(state['results'], {})
    case.assertEqual(state['attempts'], {'send': 1})
    case.assertEqual(observed['lock'], state['id'])
    case.assertEqual(observed['uncertain'], 1)
    case.assertEqual(len(observed['receipts']), 1)
    receipt = observed['receipts'][0]
    case.assertEqual((receipt['run'], receipt['step']), (state['id'], 'send'))
    case.assertEqual(receipt['delivery'], hashlib.sha256((state['id'] + ':send').encode()).hexdigest())
    case.assertEqual(submit(compose, evidence)['id'], state['id'])
    case.assertEqual(snapshot(compose, state['id'], probe=True), observed)
    return dict(evidence, snapshot=observed, audit=client(compose, 'audit', RULE))


def preserved(case, compose, evidence, probe=False):
    observed = snapshot(compose, evidence['snapshot']['state']['id'], probe=probe)
    case.assertEqual(observed, evidence['snapshot'])
    case.assertEqual(client(compose, 'audit', RULE), evidence['audit'])


def reconcile(case, compose, site, evidence):
    before = evidence['snapshot']
    run_id = before['state']['id']
    preserved(case, compose, evidence, probe=True)
    case.assertEqual(client(compose, 'plan_reconciliation', RULE, EVENT), {'denied': True})
    config = site / 'engine/engine.yml'
    document = yaml.safe_load(config.read_text())
    grant = document['grants'][0]
    grant['actions'] = [ACTION]
    grant['operations'].append('reconcile')
    private(config, yaml.safe_dump(document))
    plan = client(compose, 'plan_reconciliation', RULE, EVENT)
    case.assertEqual(plan['receipt']['status'], 'uncertain')
    arguments = dict(expected_state=plan['expected_state'], outcome='succeeded',
        evidence='synthetic-receipt-sha256:' + before['receipts'][0]['sha256'])
    stale = client(compose, 'reconcile', RULE, EVENT, **dict(arguments, expected_state='0' * 64))
    case.assertEqual(stale, {'error': 'rule_conflict'})
    grant['operations'].remove('reconcile')
    private(config, yaml.safe_dump(document))
    case.assertEqual(client(compose, 'reconcile', RULE, EVENT, **arguments), {'denied': True})
    preserved(case, compose, evidence, probe=True)
    grant['operations'].append('reconcile')
    private(config, yaml.safe_dump(document))
    # Explicit attestation of the independently recorded synthetic receipt only.
    result = client(compose, 'reconcile', RULE, EVENT, **arguments)
    case.assertEqual((result['id'], result['status']), (run_id, 'succeeded'))
    after = snapshot(compose, run_id, probe=True)
    case.assertIsNone(after['lock'])
    case.assertEqual(after['uncertain'], 0)
    case.assertEqual(after['receipts'], before['receipts'])
    for key, value in before['state'].items():
        if key not in ('status', 'updated_at'):
            case.assertEqual(after['state'][key], value, key)
    annotation = after['state']['reconciliation']
    for key in ('expected_state', 'outcome', 'evidence'):
        case.assertEqual(annotation[key], arguments[key])
    case.assertEqual((annotation['author'], annotation['organization']), ('alice', 'lab'))
    audit = client(compose, 'audit', RULE)
    case.assertEqual(audit[:-1], evidence['audit'])
    case.assertEqual((audit[-1]['operation'], audit[-1]['run_id']), ('reconcile', run_id))
    case.assertEqual(submit(compose, evidence)['id'], run_id)
    case.assertEqual(snapshot(compose, run_id, probe=True), after)
    return dict(accepted_requests=1, later_step_executed=False, duplicate_replayed=False,
        uncertain_lock_preserved=True, current_reconciliation_rights_checked=True,
        stale_confirmation_refused=True, attestation_audited=True)
