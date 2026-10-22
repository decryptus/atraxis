"""Bounded read-only transport; one shared instance owns admission control."""
import base64
from dataclasses import asdict, dataclass, field
import json
import subprocess
import sys
import threading
import time

from atraxis.source_config import (MAX_RESPONSE_BYTES, MAX_TIMEOUT,
                                   MIN_RESPONSE_BYTES, MIN_TIMEOUT, SourceConfig)
from atraxis.read_request import ReadRequest


DEFAULT_CONCURRENCY = 4
MAX_CONCURRENCY = 32
CANCELLATION_INTERVAL = 0.05
WORKER_MODULE = 'atraxis.transport_worker'
WORKER_OUTCOMES = frozenset(('success', 'timeout', 'transport_error', 'tls_error',
                      'authentication_error', 'forbidden', 'endpoint_unavailable',
                      'source_error', 'redirect_refused', 'invalid_payload',
                      'response_too_large', 'organization_required', 'configuration_error'))


@dataclass(frozen=True)
class TransportResult:
    source_id: str
    outcome: str
    attempted_at: float
    received_at: float
    received_monotonic: float
    payload: dict | None = field(default=None, repr=False)
    duration_seconds: float = 0.0


class HTTPTransport:
    """GET-only adapter. Busy sources fail immediately instead of queueing work."""

    def __init__(self, max_concurrency=DEFAULT_CONCURRENCY):
        if type(max_concurrency) is not int or not 1 <= max_concurrency <= MAX_CONCURRENCY:
            raise ValueError('invalid_transport_concurrency')
        self._slots = threading.BoundedSemaphore(max_concurrency)
        self._lock = threading.Lock()
        self._active = set()

    def fetch(self, source, cancel=None):
        if not isinstance(source, (SourceConfig, ReadRequest)):
            raise TypeError('invalid_source_configuration')
        attempted = time.time()
        started = time.monotonic()

        def result(outcome, payload=None):
            finished = time.monotonic()
            return TransportResult(source.source_id, outcome, attempted, time.time(),
                                   finished, payload, finished - started)

        if (type(source.timeout) is not int or not MIN_TIMEOUT <= source.timeout <= MAX_TIMEOUT
                or type(source.max_response_bytes) is not int
                or not MIN_RESPONSE_BYTES <= source.max_response_bytes <= MAX_RESPONSE_BYTES):
            return result('configuration_error')

        with self._lock:
            if source.source_id in self._active or not self._slots.acquire(False):
                return result('busy')
            self._active.add(source.source_id)
        process = None
        try:
            if cancel is not None and cancel.is_set():
                return result('cancelled')
            request = json.dumps(asdict(source)).encode('utf-8')
            process = subprocess.Popen([sys.executable, '-m', WORKER_MODULE],
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       stderr=subprocess.DEVNULL, close_fds=True)
            deadline = started + source.timeout
            while True:
                if cancel is not None and cancel.is_set():
                    return result('cancelled')
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return result('timeout')
                try:
                    output, _ = process.communicate(input=request,
                                timeout=min(remaining, CANCELLATION_INTERVAL))
                    break
                except subprocess.TimeoutExpired:
                    request = None
            if time.monotonic() > deadline:
                return result('timeout')
            if process.returncode:
                return result('transport_error')
            # Only the trusted worker writes this bounded protocol. Network data
            # never becomes executable code, an exception message or a log entry.
            envelope = json.loads(output)
            outcome = envelope['outcome']
            if outcome not in WORKER_OUTCOMES:
                return result('transport_error')
            payload = None
            if outcome == 'success':
                raw = base64.b64decode(envelope['body'], validate=True)
                if len(raw) > source.max_response_bytes:
                    return result('response_too_large')
                payload = json.loads(raw)
                if type(payload) is not dict:
                    return result('invalid_payload')
            if time.monotonic() > deadline:
                return result('timeout')
            return result(outcome, payload)
        except (OSError, ValueError, TypeError, KeyError, RecursionError):
            return result('transport_error')
        finally:
            try:
                if process is not None:
                    if process.poll() is None:
                        process.kill()
                    process.communicate()
            finally:
                with self._lock:
                    self._active.remove(source.source_id)
                    self._slots.release()
