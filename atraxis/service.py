"""Thread-safe application inventory, independent of HTTP and CLI adapters."""
import threading
import time
from copy import deepcopy

from atraxis.models import SnapshotStore, public_fields
from atraxis.parsing import InvalidPayload
from atraxis.source_adapters import DecodeContext, get_adapter


SCHEMA_VERSION = 1
FAILURE_OUTCOMES = frozenset(('timeout', 'transport_error', 'tls_error',
    'authentication_error', 'forbidden', 'endpoint_unavailable', 'source_error',
    'redirect_refused', 'invalid_payload', 'response_too_large',
    'configuration_error', 'busy', 'cancelled', 'collector_error'))


class ReadService:
    def __init__(self, sources=(), *, clock=time.monotonic):
        self._sources = {source.source_id: source for source in sources}
        self._clock = clock
        self._lock = threading.Lock()
        self._store = SnapshotStore()
        self._attempts = {}
        self._running = False

    def set_running(self, running):
        with self._lock:
            self._running = running

    def record(self, source_id, result):
        """Only the coordinator writes; HTTP readers take the same store lock."""
        source = self._sources[source_id]
        snapshot = None
        outcome = result.outcome
        if result.source_id != source_id:
            outcome = 'collector_error'
        elif outcome == 'success':
            context = DecodeContext(source_id=source_id, received_at=result.received_at,
                monotonic=result.received_monotonic, max_age=source.max_age,
                max_resources=source.max_resources, max_clock_skew=source.max_clock_skew,
                transport_elapsed=result.duration_seconds, metric=source.metric)
            try:
                snapshot = get_adapter(source.kind).decode(result.payload, context)
            except InvalidPayload:
                outcome = 'invalid_payload'
        elif outcome not in FAILURE_OUTCOMES:
            outcome = 'collector_error'
        with self._lock:
            if snapshot is None:
                self._store.fail(source_id)
            else:
                self._store.accept(snapshot)
            self._attempts[source_id] = dict(outcome=outcome,
                attempted_at=result.attempted_at, completed_at=result.received_at,
                source_metadata=public_fields(snapshot.metadata) if snapshot else None,
                observation_freshness=snapshot.freshness.value if snapshot else None)

    def health(self):
        return {'alive': True}

    def _view(self, source_id, source, now):
        snapshot, current = self._store.view(source_id, now)
        attempt = self._attempts.get(source_id)
        current = current and self._running
        value = dict(source_id=source_id, kind=source.kind,
            availability=('unknown' if attempt is None else
                          'available' if attempt['outcome'] == 'success' else 'unavailable'),
            current=current, last_attempt=deepcopy(attempt) if attempt else None,
            last_valid_snapshot=None)
        if snapshot is not None:
            age = (snapshot.age_at_receipt + now - snapshot.received_monotonic
                   if now >= snapshot.received_monotonic else None)
            value['last_valid_snapshot'] = dict(received_at=snapshot.received_at,
                max_age_seconds=snapshot.max_age,
                source_metadata=public_fields(snapshot.metadata),
                age_seconds=age, freshness=snapshot.freshness_at(now).value,
                resources=[dict(source_id=resource.source_id, kind=resource.kind,
                    native_id=resource.native_id, fields=public_fields(resource.fields))
                    for resource in snapshot.resources])
        return value

    def _readiness(self, now):
        ready = (bool(self._sources) and self._running
                 and all(self._store.view(key, now)[1] for key in self._sources))
        reason = ('collectors_not_configured' if not self._sources else
                  'collector_stopped' if not self._running else
                  'sources_not_current' if not ready else None)
        return dict(ready=ready, reason=reason)

    def status(self):
        with self._lock:
            now = self._clock()
            sources = [self._view(key, source, now)
                       for key, source in self._sources.items()]
            return dict(schema_version=SCHEMA_VERSION, **self._readiness(now),
                        collecting=self._running, sources=sources)

    def readiness(self):
        with self._lock:
            return self._readiness(self._clock())
