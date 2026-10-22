"""Synthetic guarded-worker acceptance through the real operator Compose file."""
import json
import os
import subprocess
import time

import yaml

WORKERS = 4
CAPACITY = 24
DEADLINE_SECONDS = 60
CLIENT = '''from atropos_client.client import RuleClient
import json, sys
c = RuleClient('/etc/atraxis/engine-client.yml', expected_actor={'subject':'alice','organization':'lab'})
try:
    operation, args = sys.argv[1], json.loads(sys.argv[2])
    if operation == 'activate':
        report = c.test('compose-rule', args['revision'], [dict(name='High CPU',
            input=dict(cpu_percent=95), expected=dict(decision='review', matched_rule='combined'))])
        assert report['passed']
        result = c.activate('compose-rule', args['revision'], test_id=report['id'], expected_generation=None)
    elif operation == 'submit':
        result = c.dispatch('compose-rule', ['server','host'], {'cpu_percent':95},
            args['binding'], expected_generation=args['generation'], event_id=args['event'])
    elif operation == 'test-submit':
        result = c.test_dispatch('compose-rule', ['server','host'], {'cpu_percent':95},
            args['binding'], event_id=args['event'])
    elif operation == 'status':
        result = c.dispatch_status('compose-rule', args['event'])
    elif operation == 'test-status':
        result = c.test_dispatch_status('compose-rule', args['event'])
    else:
        raise ValueError('unknown_worker_acceptance_operation')
    print(json.dumps(result, sort_keys=True))
finally:
    c.close()
'''
INSPECT = '''from atropos.rule_execution import configured_runtime
import json, sys
with configured_runtime('/etc/atropos/engine.yml') as runtime:
    if sys.argv[1] == 'capacity':
        for store in (runtime.store, runtime.test_store):
            store.initialize()
            store.configure_queue(int(sys.argv[2]))
    if sys.argv[1] == 'run':
        print(json.dumps(runtime.store.get(sys.argv[2])))
    else:
        print(json.dumps(dict(real=runtime.store.queue_status(), test=runtime.test_store.queue_status())))
'''


def exercise_workers(case, compose, site, revision):
    """Never contacts Auton: the only approved fixture action is core.echo."""
    config = site / 'engine/engine.yml'
    document = yaml.safe_load(config.read_text())
    grant = document['grants'][0]
    grant['operations'] += ['test', 'activate', 'evaluate', 'execute']
    grant['actions'] = ['core.echo']
    document['dispatch_bindings'] = 'bindings.yml'

    def private(path, value):
        pending = path.with_suffix('.next')
        pending.write_text(yaml.safe_dump(value))
        pending.chmod(0o600)
        os.chown(pending, 10001, 10001)
        pending.replace(path)

    private(config, document)
    private(site / 'engine/bindings.yml', [dict(organization='lab', rule='compose-rule',
        revision=revision, result=dict(decision='review', matched_rule='combined'),
        input={}, max_age_seconds=300, workflow=dict(version=1, name='compose-echo',
        start='echo', steps={'echo': dict(action='core.echo', **{'with': {'value': 'synthetic'}})}))])
    binding = json.loads(compose('exec', '-T', 'engine', 'atropos', 'rules-bindings',
        '--config', '/etc/atropos/engine.yml').stdout)[0]['id']
    capacity = json.loads(compose('exec', '-T', 'engine', 'python', '-c', INSPECT, 'capacity', str(CAPACITY)).stdout)
    case.assertEqual(capacity['real']['max_outstanding'], CAPACITY)
    case.assertEqual(capacity['test']['max_outstanding'], CAPACITY)

    def client(operation, **arguments):
        return json.loads(compose('exec', '-T', 'web', 'python', '-c', CLIENT,
            operation, json.dumps(arguments)).stdout)

    def real_state(run_id):
        return json.loads(compose('exec', '-T', 'engine', 'python', '-c', INSPECT, 'run', run_id).stdout)

    def wait_status(event, expected, test=False):
        deadline = time.monotonic() + DEADLINE_SECONDS
        operation = 'test-status' if test else 'status'
        while True:
            # Read-only transport probes may repeat; admissions never do.
            result = compose('exec', '-T', 'web', 'python', '-c', CLIENT,
                operation, json.dumps(dict(event=event)), check=False)
            if result.returncode == 0:
                state = json.loads(result.stdout)
                if state['status'] == expected:
                    return state
                case.assertIn(state['status'], ('queued', 'running'))
            if time.monotonic() >= deadline:
                case.fail('compose_worker_status_deadline: ' + expected)
            time.sleep(0.5)

    active = client('activate', revision=revision)
    arguments = dict(binding=binding, generation=active['generation'])
    real = client('submit', event='before-workers', **arguments)
    simulation = client('test-submit', event='simulation', binding=binding)
    case.assertEqual(real['status'], 'queued')
    case.assertEqual(simulation['status'], 'queued')
    case.assertEqual(len(compose('ps', '-q').stdout.split()), 4)
    compose('--profile', 'execution', 'up', '-d', '--no-build', '--no-deps',
        '--scale', 'worker=' + str(WORKERS), 'worker')
    done = wait_status('before-workers', 'succeeded')
    case.assertEqual(done['id'], real['id'])
    case.assertEqual(real_state(done['id'])['results']['echo'], {'value': 'synthetic'})
    case.assertEqual(client('test-status', event='simulation')['status'], 'queued')
    duplicate = client('submit', event='before-workers', **arguments)
    case.assertEqual(duplicate['id'], real['id'])
    case.assertEqual(client('status', event='before-workers'), done)
    ids = compose('ps', '-q', 'worker').stdout.split()
    case.assertEqual(len(ids), WORKERS)
    inspected = json.loads(subprocess.run(['docker', 'inspect', *ids],
        capture_output=True, text=True, check=True, timeout=30).stdout)
    for container in inspected:
        case.assertFalse(container['State']['OOMKilled'])
        case.assertTrue(container['HostConfig']['ReadonlyRootfs'])
        case.assertEqual(container['HostConfig']['Memory'], 512 * 1024 * 1024)
        case.assertEqual(container['HostConfig']['NanoCpus'], 2 * 1000000000)
        case.assertEqual(container['HostConfig']['PidsLimit'], 128)
        case.assertEqual(container['Config']['User'], '10001:10001')
        case.assertEqual(container['HostConfig']['RestartPolicy']['Name'], 'no')
    compose('stop', 'worker')

    compose('--profile', 'simulation', 'up', '-d', '--no-build', '--no-deps', 'test-worker')
    simulated = wait_status('simulation', 'succeeded', test=True)
    case.assertEqual(simulated['test_context'], simulation['test_context'])
    case.assertTrue(simulated['test_context']['test'])
    for result in simulated['results'].values():
        case.assertEqual(result['_test_context'], simulation['test_context'])
    for event in simulated['history']:
        case.assertEqual(event['test_context'], simulation['test_context'])
    case.assertEqual(client('status', event='before-workers'), done)
    compose('stop', 'test-worker')

    client('submit', event='revoked', **arguments)
    client('test-submit', event='revoked-simulation', binding=binding)
    grant['actions'] = []
    private(config, document)
    compose('--profile', 'execution', '--profile', 'simulation', 'up', '-d',
        '--no-build', '--no-deps', '--scale', 'worker=' + str(WORKERS), 'worker', 'test-worker')
    for event, test in (('revoked', False), ('revoked-simulation', True)):
        refused = wait_status(event, 'cancelled', test=test)
        case.assertEqual((refused if test else real_state(refused['id']))['results'], {})
    compose('stop', 'worker', 'test-worker')
    grant['actions'] = ['core.echo']
    private(config, document)

    queued = client('submit', event='recreated', **arguments)
    case.assertEqual(queued['status'], 'queued')
    compose('--profile', 'execution', '--profile', 'simulation', 'up', '-d',
        '--no-build', '--force-recreate', '--scale', 'worker=' + str(WORKERS))
    recovered = wait_status('recreated', 'succeeded')
    case.assertEqual(recovered['id'], queued['id'])
    case.assertEqual(real_state(recovered['id'])['results']['echo'], {'value': 'synthetic'})
    case.assertEqual(client('submit', event='recreated', **arguments)['id'], queued['id'])
    case.assertEqual(client('status', event='recreated'), recovered)
    status = json.loads(compose('exec', '-T', 'engine', 'python', '-c', INSPECT, 'status').stdout)
    for queue in status.values():
        case.assertEqual(queue['outstanding'], 0)
        case.assertEqual(queue['max_outstanding'], CAPACITY)
    # Keep the original four-service cold-restore drill independent of profiles.
    compose('--profile', '*', 'stop')
    compose('rm', '-f', 'worker', 'test-worker')
    compose('up', '-d', '--no-build')
    wait_status('recreated', 'succeeded')
    print('COMPOSE_WORKER_REPORT workers=4 capacity=24 default_workers=0 real_completed=2 '
          'simulations_completed=1 revoked_cancelled=2 recovery_verified=true '
          'deduplication_verified=true test_isolation=true synthetic_only=true', flush=True)
    return {event: client(operation, event=event) for event, operation in (
        ('before-workers', 'status'), ('simulation', 'test-status'), ('revoked', 'status'),
        ('revoked-simulation', 'test-status'), ('recreated', 'status'))}


def assert_restored_workers(case, compose, evidence):
    for event, operation, status in (('before-workers', 'status', 'succeeded'),
            ('simulation', 'test-status', 'succeeded'), ('revoked', 'status', 'cancelled'),
            ('revoked-simulation', 'test-status', 'cancelled'), ('recreated', 'status', 'succeeded')):
        restored = json.loads(compose('exec', '-T', 'web', 'python', '-c', CLIENT,
            operation, json.dumps(dict(event=event))).stdout)
        case.assertEqual(restored['status'], status)
        case.assertEqual(restored, evidence[event])
