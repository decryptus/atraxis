"""Bounded synthetic load probes; never a production worker or benchmark claim."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import math
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from unittest.mock import patch

sys.path.insert(0, "/")
from node import client, inventory

CLIENTS = 8
READS = 64
WORKER_SECONDS = 600
ENDURANCE_WORKER_SECONDS = 9600
MEMORY_WORKER_SECONDS = 18000
READINESS_REASONS = frozenset(('collectors_not_configured', 'collector_stopped', 'sources_not_current'))
SOURCE_OUTCOMES = frozenset(('success', 'timeout', 'transport_error', 'tls_error',
    'authentication_error', 'forbidden', 'endpoint_unavailable', 'source_error',
    'redirect_refused', 'invalid_payload', 'response_too_large',
    'configuration_error', 'busy', 'cancelled', 'collector_error'))
DIAGNOSTIC_SOURCE_LIMIT = 8
MEMORY_STAT_FIELDS = ('anon', 'file', 'kernel', 'sock', 'shmem')
CPU_STAT_FIELDS = ('usage_usec', 'user_usec', 'system_usec', 'nr_periods',
                   'nr_throttled', 'throttled_usec')
READ_STAGES = frozenset(('inventory', 'active', 'search_interventions'))


class ReadDiagnostics:
    """Observe the existing client subprocess without retrying or changing it."""
    def __init__(self, transport):
        self.transport = transport
        self.local = threading.local()

    def __getattr__(self, name):
        return getattr(self.transport, name)

    def run(self, *args, **kwargs):
        started = time.monotonic()
        # Never retain arguments, input, output, environment or exception text.
        record = {'outcome': 'exception'}
        self.local.worker = record
        try:
            result = self.transport.run(*args, **kwargs)
            record['outcome'] = 'completed' if result.returncode == 0 else 'nonzero_exit'
            record['stdout_bytes'] = len(result.stdout) if type(result.stdout) is bytes else None
            return result
        except subprocess.TimeoutExpired:
            record['outcome'] = 'deadline_exceeded'
            raise
        except OSError:
            record['outcome'] = 'spawn_error'
            raise
        finally:
            record['elapsed_ms'] = round((time.monotonic() - started) * 1000, 3)

    def call(self, stage, operation):
        if stage not in READ_STAGES:
            raise ValueError('unsupported_read_diagnostic_stage')
        self.local.worker = None
        started = time.monotonic()
        try:
            return operation()
        except Exception:
            # One bounded line for the original failing call. Re-raise the
            # original exception; no retry, fresh probe or successful fallback.
            print('LOAD_READ_FAILURE ' + json.dumps(dict(stage=stage,
                elapsed_ms=round((time.monotonic() - started) * 1000, 3),
                worker=self.local.worker), sort_keys=True), file=sys.stderr, flush=True)
            raise


def require_ready(status):
    """Fail on the original response, retaining only bounded diagnostic fields."""
    if type(status) is dict and status.get('ready') is True:
        return
    status = status if type(status) is dict else {}
    def flag(value):
        return value if type(value) is bool else None
    def number(value):
        return value if type(value) in (int, float) and 0 <= value <= 1e12 and math.isfinite(value) else None
    reason = status.get('reason')
    evidence = dict(ready=flag(status.get('ready')), collecting=flag(status.get('collecting')),
                    reason=reason if type(reason) is str and reason in READINESS_REASONS else None,
                    sources=[])
    sources = status.get('sources')
    for source in sources[:DIAGNOSTIC_SOURCE_LIMIT] if type(sources) is list else ():
        if type(source) is not dict:
            continue
        attempt = source.get('last_attempt')
        attempt = attempt if type(attempt) is dict else {}
        snapshot = source.get('last_valid_snapshot')
        snapshot = snapshot if type(snapshot) is dict else {}
        outcome = attempt.get('outcome')
        evidence['sources'].append(dict(current=flag(source.get('current')),
            outcome=outcome if type(outcome) is str and outcome in SOURCE_OUTCOMES else None,
            attempted_at=number(attempt.get('attempted_at')),
            completed_at=number(attempt.get('completed_at')),
            received_at=number(snapshot.get('received_at')),
            age_seconds=number(snapshot.get('age_seconds')),
            max_age_seconds=number(snapshot.get('max_age_seconds'))))
    raise AssertionError('load_inventory_not_ready ' + json.dumps(evidence, sort_keys=True))


def latency(values):
    ordered = sorted(values)
    def percentile(fraction):
        return ordered[min(len(ordered)-1, int((len(ordered)-1)*fraction))]
    return dict(count=len(values), p50_ms=percentile(.50), p95_ms=percentile(.95), max_ms=max(values))


def burst(binding, events):
    from atropos_client.client import RemoteRuleError
    def submit(event):
        c = client()
        start = time.monotonic()
        try:
            result = c.test_dispatch('observed', ['container', 'web'], {'cpu_percent': 250}, binding, event_id=event)
            return dict(event=event, receipt=result, ms=(time.monotonic()-start)*1000)
        except RemoteRuleError as error:
            if str(error) != 'rule_queue_full':
                raise  # Unknown outcomes are never reclassified or retried.
            return dict(event=event, error='rule_queue_full', ms=(time.monotonic()-start)*1000)
        finally:
            c.close()
    start = time.monotonic()
    with ThreadPoolExecutor(max_workers=CLIENTS) as pool:
        results = list(pool.map(submit, events))
    return dict(results=results, seconds=time.monotonic()-start,
                latency=latency([r['ms'] for r in results]))


def read_burst():
    from atropos_client import client as client_module
    diagnostic = ReadDiagnostics(client_module.subprocess)
    def read(_):
        c = client()
        start = time.monotonic()
        try:
            diagnostic.call('inventory', lambda: require_ready(inventory()))
            active = diagnostic.call('active', lambda: c.active('observed'))
            assert active is not None
            result = diagnostic.call('search_interventions', lambda:
                c.search_interventions('observed', mode='test', page=1, limit=50))
            assert type(result['total']) is int
            return (time.monotonic()-start)*1000
        finally:
            c.close()
    # Patch only this client module's reference, not subprocess.run globally.
    # Per-thread records prevent a concurrent success overwriting a failure.
    with patch.object(client_module, 'subprocess', diagnostic):
        with ThreadPoolExecutor(max_workers=CLIENTS) as pool:
            values = list(pool.map(read, range(READS)))
    return latency(values)


def worker(seconds=WORKER_SECONDS):
    if seconds not in (WORKER_SECONDS, ENDURANCE_WORKER_SECONDS, MEMORY_WORKER_SECONDS):
        raise ValueError('unsupported_load_worker_duration')
    # One long-lived configured runtime and consumer per process. Current
    # identity/permissions/binding are still reloaded by the product authorizer.
    from atropos.rule_execution import configured_runtime
    stopped = False
    def stop(*_):
        nonlocal stopped
        stopped = True
    signal.signal(signal.SIGTERM, stop)
    async def run():
        with configured_runtime('/state/engine.yml') as runtime:
            runtime.bindings()
            runtime.test_store.initialize()
            guarded = runtime.worker(test_mode=True)
            deadline = time.monotonic() + seconds
            while not stopped:
                assert time.monotonic() < deadline, 'load_worker_deadline_exceeded'
                if not await guarded.run_once(0):
                    await asyncio.sleep(.05)
    asyncio.run(run())


def metrics():
    # Read-only measurement. Do not checkpoint, vacuum, trim or otherwise hide
    # growth. cgroup peak includes transient pressure between snapshots.
    root = Path('/state')
    cg = Path('/sys/fs/cgroup')
    memory = {name: int((cg / name).read_text()) for name in ('memory.current', 'memory.peak', 'pids.current')}
    memory['events'] = dict(line.split() for line in (cg/'memory.events').read_text().splitlines())
    for filename, fields in (('memory.stat', MEMORY_STAT_FIELDS), ('cpu.stat', CPU_STAT_FIELDS)):
        stats = dict(line.split() for line in (cg/filename).read_text().splitlines())
        memory[filename] = {key: int(stats[key]) for key in fields if key in stats}
    result = dict(cgroup=memory, state_bytes=sum(p.stat().st_size for p in root.rglob('*') if p.is_file()))
    database = root / 'engine.db'
    if database.exists():
        with sqlite3.connect('file:' + str(database) + '?mode=ro', uri=True) as c:
            assert c.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
            assert c.execute('PRAGMA foreign_key_check').fetchone() is None
            result['sqlite'] = dict(page_count=c.execute('PRAGMA page_count').fetchone()[0],
                page_size=c.execute('PRAGMA page_size').fetchone()[0],
                freelist_count=c.execute('PRAGMA freelist_count').fetchone()[0],
                rows={table: c.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0]
                      for table in ('runs', 'admissions', 'journal', 'records')},
                files={suffix or 'db': Path(str(database)+suffix).stat().st_size
                       if Path(str(database)+suffix).exists() else 0 for suffix in ('', '-wal', '-shm')})
        import redis
        config = json.loads((root/'engine.yml').read_text())
        with redis.Redis.from_url(config['queue']['url'], decode_responses=True) as r:
            info = r.info('memory')
            streams = [k for k in r.scan_iter() if r.type(k) == 'stream']
            result['redis'] = dict(keys=r.dbsize(), used_memory=info['used_memory'],
                used_memory_peak=info['used_memory_peak'],
                stream_entries=sum(r.xlen(k) for k in streams),
                pending=sum(group['pending'] for k in streams for group in r.xinfo_groups(k)),
                consumers=sum(group['consumers'] for k in streams for group in r.xinfo_groups(k)))
    return result


def evidence():
    # Independent durable evidence, not a replacement for API authorization.
    with sqlite3.connect('file:/state/engine.db?mode=ro', uri=True) as c:
        states = [json.loads(row[0]) for row in c.execute('SELECT state FROM runs ORDER BY id')]
        return dict(runs=[dict(id=s['id'], status=s['status'], attempts=s['attempts'],
            history=s['history'], test_context=s.get('test_context'),
            result_contexts={k: v.get('_test_context') for k,v in s['results'].items()}) for s in states],
            admissions=c.execute('SELECT COUNT(*) FROM admissions').fetchone()[0],
            journal=c.execute('SELECT COUNT(*) FROM journal').fetchone()[0])


if __name__ == '__main__':
    command = sys.argv[1]
    if command == 'worker':
        worker(int(sys.argv[2]) if len(sys.argv) == 3 else WORKER_SECONDS)
    elif command == 'burst':
        print(json.dumps(burst(sys.argv[2], json.load(sys.stdin))))
    elif command == 'reads':
        print(json.dumps(read_burst()))
    elif command == 'metrics':
        print(json.dumps(metrics()))
    elif command == 'evidence':
        print(json.dumps(evidence()))
    else:
        raise ValueError('unknown_load_command')

