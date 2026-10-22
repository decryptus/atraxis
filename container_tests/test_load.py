"""Bounded repeated saturation, read pressure and recovery on the real chain."""
import json
import os
import time

from test_chain import ChainFixture, IMAGES, ROOT, docker, network

ROUNDS = 6
ENDURANCE_SECONDS = 7200
ENDURANCE_MAX_ROUNDS = 240
ENDURANCE_WORKER_SECONDS = 9600
ENDURANCE_RESULT_TTL = 14400
MEMORY_SECONDS = 14400
MEMORY_MAX_ROUNDS = 960
MEMORY_WORKER_SECONDS = 18000
MEMORY_RESULT_TTL = 21600
QUIESCENT_SECONDS = 600
QUIESCENT_INTERVAL_SECONDS = 60
CAPACITY = 24
ATTEMPTS = 48
WORKERS = 4
MEMORY_LIMIT = 512 * 1024 * 1024
STATE_HELPER_ROLES = ('web', 'operator', 'engine', 'redis', 'auton', 'alert')


def load_settings(mode):
    if type(mode) is not str or mode not in ('0', str(ENDURANCE_SECONDS), str(MEMORY_SECONDS)):
        raise ValueError('unsupported_endurance_duration')
    duration = int(mode)
    extended = duration == MEMORY_SECONDS
    return dict(duration=duration,
                max_rounds=MEMORY_MAX_ROUNDS if extended else ENDURANCE_MAX_ROUNDS,
                worker_seconds=MEMORY_WORKER_SECONDS if extended else ENDURANCE_WORKER_SECONDS if duration else 600,
                certificate_hours=8 if extended else 4 if duration else 2,
                result_ttl=MEMORY_RESULT_TTL if extended else ENDURANCE_RESULT_TTL if duration else None,
                quiescent_seconds=QUIESCENT_SECONDS if extended else 0)


class LoadTests(ChainFixture):
    def setUp(self):
        settings = load_settings(os.environ.get('CHAIN_ENDURANCE_SECONDS', '0'))
        self.duration = settings['duration']
        self.max_rounds = settings['max_rounds']
        self.worker_seconds = settings['worker_seconds']
        self.quiescent_seconds = settings['quiescent_seconds']
        self.certificate_hours = settings['certificate_hours']
        # Retain the first real execution across the entire bounded run and
        # recreation. Auton's retention sweeper remains enabled.
        self.auton_result_ttl = settings['result_ttl']
        super().setUp()

    def checkpoint(self, record):
        # Synthetic metrics only, no credentials or full run payloads. Retain
        # completed cycles even if a later assertion or job deadline fails.
        directory = ROOT / 'acceptance-artifacts'
        directory.mkdir(exist_ok=True)
        path = directory / ('endurance.jsonl' if self.duration else 'load.jsonl')
        line = json.dumps(record, sort_keys=True)
        with path.open('a') as stream:
            stream.write(line+'\n')
            stream.flush()
        print('LOAD_PROGRESS '+line, flush=True)

    def load(self, role, command, *arguments, input=None, timeout=90):
        if role == 'source':
            if command != 'metrics' or arguments or input is not None:
                raise ValueError('unsupported_source_load_probe')
            # Docker archive copy is not usable for this read-only-root fixture.
            # Execute the read-only probe from stdin, without changing mounts,
            # filesystem permissions, credentials or container protections.
            result = docker('exec', '-i', self.name(role), 'python', '-', 'metrics',
                            input=(ROOT/'scripts/chain/load.py').read_text(), timeout=timeout)
            return json.loads(result.stdout)
        result = docker('exec', *(['-i'] if input is not None else []), self.name(role),
                        'python', '/state/load.py', command, *arguments, input=input, timeout=timeout)
        return json.loads(result.stdout)

    def queue(self):
        return self.admin('queue-status', '--test')

    def sample(self, phase):
        roles = ('source', 'web', 'operator', 'engine', 'redis', 'auton', 'alert',
                 *(f'load-worker-{n}' for n in range(WORKERS)))
        values = {}
        for role in roles:
            state = json.loads(docker('inspect', '--format', '{{json .State}}', self.name(role)).stdout)
            self.assertTrue(state['Running'], role)
            self.assertFalse(state['OOMKilled'], role)
            data = self.load(role, 'metrics')
            self.assertLess(data['cgroup']['memory.peak'], MEMORY_LIMIT, role)
            self.assertEqual(int(data['cgroup']['events']['oom_kill']), 0, role)
            values[role] = data
        return dict(phase=phase, values=values)

    def drain(self):
        start = time.monotonic()
        self.wait(lambda: self.queue()['outstanding'] == 0, timeout=90)
        return time.monotonic()-start

    def observe_quiescence(self, evidence, original_auton_records):
        # No new admissions, search bursts, GC, checkpoint, trim or cache reset.
        # Keep services/workers alive and execution paused at the last drain.
        started = time.monotonic()
        samples = 0
        for _ in range(self.quiescent_seconds // QUIESCENT_INTERVAL_SECONDS):
            time.sleep(QUIESCENT_INTERVAL_SECONDS)
            snapshot = self.sample('quiescent')
            self.assertEqual(self.queue()['outstanding'], 0)
            self.assertEqual(snapshot['values']['engine']['redis']['pending'], 0)
            self.assertEqual(snapshot['values']['engine']['redis']['stream_entries'], 0)
            self.assertEqual(self.effects(), 1)
            self.assertEqual(self.auton_records(), original_auton_records)
            samples += 1
            self.checkpoint(dict(phase='quiescent', elapsed_seconds=time.monotonic()-started,
                                 sample=snapshot))
        elapsed = time.monotonic()-started
        self.assertGreaterEqual(elapsed, self.quiescent_seconds)
        self.assertEqual(self.load('engine', 'evidence'), evidence)
        return dict(minimum_seconds=self.quiescent_seconds, observed_seconds=elapsed, samples=samples)

    def test_repeated_concurrent_capacity_storage_and_recovery(self):
        # Exercise an actual external execution before and after the simulated
        # load, without making real observation freshness artificially longer.
        self.dispatch('load-before')
        self.worker(); self.worker()
        self.assertEqual(self.status('load-before')['status'], 'succeeded')
        self.assertEqual(self.effects(), 1)
        original_auton_records = self.auton_records()
        self.assertEqual(len(original_auton_records), 1)
        self.engine('storage-pause')
        self.admin('queue-configure', '--test', '--max-outstanding', str(CAPACITY))
        # Copy only into existing named state volumes. Source metrics execute
        # from stdin instead; its read-only root and tmpfs remain unchanged.
        for role in STATE_HELPER_ROLES:
            docker('cp', str(ROOT/'scripts/chain/load.py'), self.name(role)+':/state/load.py')
        # Existing services use /state/load.py; the workers use the same file.
        for n in range(WORKERS):
            role = f'load-worker-{n}'
            self.containers.append(self.name(role))
            docker('run', '-d', '--name', self.name(role), *network.HARDENING,
                '--network', 'container:'+self.name('engine'),
                '-v', self.state_volumes['engine']+':/state',
                '-v', str(ROOT/'scripts/chain/node.py')+':/node.py:ro',
                '--entrypoint', 'python', IMAGES['engine'], '/state/load.py', 'worker',
                str(self.worker_seconds))
        report = dict(rounds=[], samples=[], clients=8, workers=WORKERS, capacity=CAPACITY,
                      attempts_per_round=ATTEMPTS, limits=dict(memory_bytes=MEMORY_LIMIT, cpus=2, pids=128))
        accepted_ids = set()
        baseline = self.load('engine', 'evidence')
        report['samples'].append(self.sample('baseline'))
        started = time.monotonic()
        self.checkpoint(dict(phase='start', minimum_seconds=self.duration, sample=report['samples'][0]))
        cycle = 0
        while cycle < ROUNDS or time.monotonic()-started < self.duration:
            self.assertLess(cycle, self.max_rounds, 'endurance_round_limit_before_duration')
            self.engine('storage-pause')
            before = self.load('engine', 'evidence')
            burst = self.load('operator', 'burst', self.binding,
                              input=json.dumps([f'load-{cycle}-{n}' for n in range(ATTEMPTS)]))
            accepted = [r for r in burst['results'] if 'receipt' in r]
            refused = [r for r in burst['results'] if 'error' in r]
            self.assertEqual(len(accepted), CAPACITY)
            self.assertEqual(len(refused), ATTEMPTS-CAPACITY)
            self.assertEqual({r['error'] for r in refused}, {'rule_queue_full'})
            self.assertEqual(self.queue()['outstanding'], CAPACITY)
            self.assertTrue(self.queue()['full'])
            after = self.load('engine', 'evidence')
            self.assertEqual(after['admissions']-before['admissions'], CAPACITY)
            self.assertEqual(after['journal']-before['journal'], CAPACITY)
            self.assertEqual(len(after['runs'])-len(before['runs']), CAPACITY)
            # Reusing eight accepted event IDs must recover receipts even while
            # full; no extra run or audit record is allowed.
            duplicated = self.load('operator', 'burst', self.binding,
                                   input=json.dumps([r['event'] for r in accepted[:8]]))
            self.assertEqual([r['receipt'] for r in duplicated['results']], [r['receipt'] for r in accepted[:8]])
            self.assertEqual(self.load('engine', 'evidence'), after)
            accepted_ids.update(r['receipt']['id'] for r in accepted)
            self.engine('storage-resume', '--confirm-reconciled')
            try:
                reads = self.load('operator', 'reads')  # concurrent with guarded workers
            except Exception:
                # Preserve failure-time resource evidence before cleanup. The
                # original failing response is in require_ready's bounded error;
                # do not fetch a newer response, retry or mark this cycle complete.
                failure = dict(phase='failed', stage='concurrent_reads', cycle=cycle+1,
                               elapsed_seconds=time.monotonic()-started)
                try:
                    failure['sample'] = self.sample('read-failure')
                except Exception:
                    failure['sample_error'] = 'failure_metrics_unavailable'
                self.checkpoint(failure)
                raise
            drain_seconds = self.drain()
            # One known refusal is explicitly resubmitted with its original ID
            # after space is available. Unknown outcomes are never retried.
            recovered = self.load('operator', 'burst', self.binding,
                                  input=json.dumps([refused[0]['event']]))['results'][0]
            self.assertIn('receipt', recovered)
            accepted_ids.add(recovered['receipt']['id'])
            drain_seconds += self.drain()
            self.engine('storage-pause')
            snapshot = self.sample(f'round-{cycle+1}')
            storage = snapshot['values']['engine']
            self.assertEqual(storage['redis']['stream_entries'], 0)
            self.assertEqual(storage['redis']['pending'], 0)
            self.assertEqual(self.queue()['outstanding'], 0)
            self.assertEqual(storage['sqlite']['rows']['runs'], 1+len(accepted_ids))
            self.assertEqual(storage['sqlite']['rows']['admissions'], 1+len(accepted_ids))
            self.assertEqual(self.effects(), 1)
            self.assertEqual(self.auton_records(), original_auton_records)
            report['samples'].append(snapshot)
            report['rounds'].append(dict(cycle=cycle+1, admitted=len(accepted), refused=len(refused),
                duplicate_receipts=8, recovered=1, admission_seconds=burst['seconds'],
                admission_latency=burst['latency'], concurrent_read_latency=reads, drain_wait_seconds=drain_seconds))
            self.checkpoint(dict(phase='cycle', elapsed_seconds=time.monotonic()-started,
                round=report['rounds'][-1], sample=snapshot))
            if self.duration:
                # Every sample is persisted above; keep the terminal report compact.
                report['samples'] = [report['samples'][0], snapshot]
            cycle += 1
        report['load_seconds'] = time.monotonic()-started
        report['minimum_seconds'] = self.duration
        self.assertGreaterEqual(report['load_seconds'], self.duration)
        # Keep every accepted historical row and exactly one attempt per step;
        # retained history growth must never be disguised as a queue leak/purge.
        evidence = self.load('engine', 'evidence')
        runs = [r for r in evidence['runs'] if r['test_context'] is not None]
        self.assertEqual({r['id'] for r in runs}, accepted_ids)
        self.assertEqual(len(runs), cycle*(CAPACITY+1))
        for run in runs:
            self.assertEqual(run['status'], 'succeeded')
            self.assertEqual(run['attempts'], {'run': 1, 'notify': 1})
            self.assertEqual([h['status'] for h in run['history']], ['running', 'queued', 'running', 'succeeded'])
            self.assertTrue(run['test_context']['test'])
            self.assertEqual(run['test_context']['id'], run['id'])
            self.assertEqual(run['result_contexts'], {'run': run['test_context'], 'notify': run['test_context']})
        if self.quiescent_seconds:
            report['quiescence'] = self.observe_quiescence(evidence, original_auton_records)
        deliveries = self.deliveries()
        test_deliveries = [x for x in deliveries if x['path']=='/v1/test-alerts']
        self.assertEqual(len(test_deliveries), len(accepted_ids))
        self.assertEqual({x['body']['test_context']['id'] for x in test_deliveries}, accepted_ids)
        for n in range(WORKERS):
            role = f'load-worker-{n}'
            docker('stop', '--time', '10', self.name(role))
            self.assertEqual(docker('wait', self.name(role)).stdout.strip(), '0')
            self.remove_role(role)
        self.recreate()
        self.assertEqual(self.load('engine', 'evidence'), evidence)
        self.assertEqual(self.auton_records(), original_auton_records)
        self.assertEqual(self.queue()['max_outstanding'], CAPACITY)
        self.assertEqual(self.queue()['outstanding'], 0)
        self.engine('storage-resume', '--confirm-reconciled')
        self.dispatch('load-after')
        self.worker(); self.worker()
        self.assertEqual(self.status('load-after')['status'], 'succeeded')
        self.assertEqual(self.effects(), 2)
        final_auton_records = self.auton_records()
        self.assertEqual(len(final_auton_records), 2)
        self.assertIn(original_auton_records[0], final_auton_records)
        self.assertEqual(len(self.deliveries()), len(accepted_ids)+2)
        self.assertEqual(self.load('engine', 'evidence')['admissions'], baseline['admissions']+len(accepted_ids)+1)
        report.update(completed_rounds=cycle, accepted_test_runs=len(accepted_ids),
                      real_effects=2, recovery_verified=True)
        self.checkpoint(dict(phase='complete', report=report))
        print('LOAD_REPORT '+json.dumps(report, sort_keys=True), flush=True)

