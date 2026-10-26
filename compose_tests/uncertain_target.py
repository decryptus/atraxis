"""Disposable HTTP receipt fixture: record acceptance, then lose the response.

The ledger is synthetic evidence copied with the cold volume, not a live target
or proof that a real external recipient survived host loss.
"""
from contextlib import contextmanager
import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import socket
import threading

HOST = '127.0.0.1'
PORT = 18743
ORIGIN = 'http://{host}:{port}'.format(host=HOST, port=PORT)
API_PATH = '/api/v2/alerts'
CONFIG_PATH = '/etc/atropos/engine.yml'
TOKEN_PATH = Path('/etc/atropos/recovery-alert.token')
LEDGER_PATH = Path('/var/lib/atropos/recovery-receipts.jsonl')
MAX_BODY_BYTES = 65536
MAX_LEDGER_BYTES = 65536
IO_TIMEOUT_SECONDS = 3
WORKER_POLLS = 3


def receipts(path):
    if not path.exists():
        return []
    with path.open('rb') as stream:
        data = stream.read(MAX_LEDGER_BYTES + 1)
    if len(data) > MAX_LEDGER_BYTES:
        raise ValueError('fixture_receipts_oversized')
    return [json.loads(line) for line in data.splitlines()]


@contextmanager
def receiver(path, token, port=PORT):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            self.connection.settimeout(IO_TIMEOUT_SECONDS)
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if (self.path != API_PATH or not 0 < size <= MAX_BODY_BYTES
                        or self.headers.get('Authorization') != 'Bearer ' + token):
                    raise ValueError('fixture_request_denied')
                raw = self.rfile.read(size)
                if len(raw) != size:
                    raise ValueError('fixture_request_incomplete')
                payload = json.loads(raw)
                alert, = payload
                record = dict(run=alert['annotations']['atropos_run_id'],
                    step=alert['annotations']['atropos_step'],
                    delivery=alert['labels']['atropos_delivery_id'],
                    sha256=hashlib.sha256(raw).hexdigest())
                previous = receipts(path)
                encoded = (json.dumps(record, sort_keys=True) + '\n').encode()
                if sum(len(json.dumps(r, sort_keys=True).encode()) + 1 for r in previous) + len(encoded) > MAX_LEDGER_BYTES:
                    raise ValueError('fixture_receipts_oversized')
                # Deliberately no deduplication: a replay must be visible in the count.
                fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
                with os.fdopen(fd, 'ab') as stream:
                    stream.write(encoded)
                    stream.flush()
                    os.fsync(stream.fileno())
            except (ValueError, KeyError, TypeError, OSError):
                self.send_error(400)
                return
            self.close_connection = True
            self.connection.shutdown(socket.SHUT_RDWR)
            self.connection.close()  # Accepted, with no HTTP acknowledgement.

    server = HTTPServer((HOST, port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=IO_TIMEOUT_SECONDS)
        if thread.is_alive():
            raise RuntimeError('fixture_receiver_did_not_stop')


def main():
    import asyncio
    import sys
    from atropos.rule_execution import configured_runtime
    operation, run_id = sys.argv[1:]
    if operation not in ('snapshot', 'probe'):
        raise ValueError('fixture_operation_invalid')
    with configured_runtime(CONFIG_PATH) as runtime:
        if operation == 'probe':
            with receiver(LEDGER_PATH, TOKEN_PATH.read_text().strip()):
                worker = runtime.worker()
                for _ in range(WORKER_POLLS):
                    asyncio.run(worker.run_once(0))
        state = runtime.store.get(run_id)
        print(json.dumps(dict(state=state,
            lock=runtime.store.client.get(runtime.store.target_key(state['target'])),
            uncertain=runtime.store.queue_status()['uncertain_runs'],
            receipts=receipts(LEDGER_PATH)), sort_keys=True))


if __name__ == '__main__':
    main()
