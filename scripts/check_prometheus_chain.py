"""Real Prometheus + Covenant collector, synthetic input, loopback only."""
import argparse
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time

import bcrypt
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from atraxis.client import validate_inventory
from atraxis.service import ReadService
from atraxis.source_config import parse_sources
from atraxis.transport import HTTPTransport


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prometheus', required=True)
    parser.add_argument('--covenant-source', required=True)
    args = parser.parse_args()
    sys.path.insert(0, str(Path(args.covenant_source).resolve()))
    from covenant.classes.target import CovenantRegistry, CovenantTarget
    from covenant.filters import fjmespath  # Registers the actual filter.
    from prometheus_client import generate_latest

    logging.getLogger('covenant.collect').setLevel(logging.CRITICAL)
    lock = threading.Lock()
    state = {'input': {'orders': {'pending': 12}}}

    def configure(expression='orders.pending', **options):
        registry = CovenantRegistry()
        target = CovenantTarget(name='orders', config={}, registry=registry,
            collects=[{'orders_pending': dict(type='gauge', documentation='Synthetic orders',
                value_tasks=[{'@filter': 'jmespath', 'expression': expression}], **options)}])
        state.update(registry=registry, target=target)

    configure()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            if self.path != '/metrics/orders':
                self.send_error(404)
                return
            with lock:
                state['target'](deepcopy(state['input']))
                body = generate_latest(state['registry'])
            self.send_response(200)
            self.send_header('Content-Type', 'text/plain; version=0.0.4')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    exporter = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    exporter_thread = threading.Thread(target=exporter.serve_forever, daemon=True)
    exporter_thread.start()
    try:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            port = free_port()
            config = {'global': {'scrape_interval': '1s', 'scrape_timeout': '1s'},
                      'scrape_configs': [{'job_name': 'covenant-orders',
                        'metrics_path': '/metrics/orders',
                        'static_configs': [{'targets': ['127.0.0.1:' + str(exporter.server_port)]}]}]}
            (root / 'prometheus.yml').write_text(yaml.safe_dump(config))
            (root / 'web.yml').write_text(yaml.safe_dump({'basic_auth_users': {
                'reader': bcrypt.hashpw(b'synthetic-only', bcrypt.gensalt(rounds=4)).decode()}}))
            credentials = root / 'credentials.json'
            credentials.write_text(json.dumps(dict(username='reader', password='synthetic-only')))
            credentials.chmod(0o600)
            source = parse_sources([dict(source_id='orders', kind='prometheus',
                origin='http://127.0.0.1:' + str(port), allow_loopback_http=True,
                credentials_file=str(credentials), timeout=2, poll_interval=2, max_age=30,
                metric=dict(name='orders_pending', match={'job': 'covenant-orders'},
                            labels=['job', 'instance']))], root / 'atraxis.yml')[0]
            service = ReadService([source])
            service.set_running(True)
            with (root / 'prometheus.log').open('w') as log:
                process = subprocess.Popen([str(Path(args.prometheus).resolve()),
                    '--config.file=' + str(root / 'prometheus.yml'),
                    '--web.config.file=' + str(root / 'web.yml'),
                    '--web.listen-address=127.0.0.1:' + str(port),
                    '--storage.tsdb.path=' + str(root / 'data')], stdout=log, stderr=log)
                try:
                    def wait_sample(expected, after=0):
                        deadline = time.monotonic() + 20
                        while time.monotonic() < deadline:
                            if process.poll() is not None:
                                raise AssertionError('Prometheus exited unexpectedly')
                            result = HTTPTransport().fetch(source)
                            service.record('orders', result)
                            status = validate_inventory(service.status())
                            if status['ready']:
                                fields = status['sources'][0]['last_valid_snapshot']['resources'][0]['fields']
                                if fields['value'] == expected and fields['sampled_at'] > after:
                                    return fields['sampled_at']
                            time.sleep(0.2)
                        raise AssertionError('Expected sample was not observed: ' + str(expected))

                    first = wait_sample(12)
                    with lock:
                        state['input'] = {'orders': {'pending': {'count': 99}}}
                        changed = time.time()
                    retained = wait_sample(12, after=max(first, changed))
                    assert retained > first
                    print('PASS: rejected object retains 12 with a later Prometheus sample timestamp')
                    with lock:
                        configure('to_number(orders.pending)', on_noresult={'value': -1})
                        changed = time.time()
                    fallback = wait_sample(-1, after=changed)
                    print('PASS: configured filter/on_noresult exposes -1 through the same chain')
                    with lock:
                        state['input'] = {'orders': {'pending': 9}}
                        changed = time.time()
                    wait_sample(9, after=max(fallback, changed))
                    print('PASS: recovery exposes 9 without changing Covenant code')
                finally:
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
    finally:
        exporter.shutdown()
        exporter.server_close()
        exporter_thread.join(timeout=5)


if __name__ == '__main__':
    main()
