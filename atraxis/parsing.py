"""Pure, bounded projections of already-decoded source responses. No I/O."""
from datetime import datetime
import math
import re
from uuid import UUID

from atraxis.models import Facts, Freshness, Resource, Snapshot


_DOCKER_ID = re.compile(r'[0-9a-f]{64}')
_FINGERPRINT = re.compile(r'[0-9a-f]{64}')
_HEALTH = frozenset(('healthy', 'unhealthy', 'starting', 'none', 'unknown'))
_MATERIAL = frozenset(('readable', 'missing', 'invalid'))
_LIFECYCLE = frozenset(('create', 'exists', 'invalid-dns', 'delete', 'renew',
                        'processing', 'generated', 'deployed', 'unknown'))
_ORIGINS = frozenset(('external', 'acme', 'provider-managed', 'unknown'))
_MEASUREMENTS = ('mem_usage', 'mem_limit', 'mem_percent', 'cpu_percent',
                 'io_read', 'io_write', 'net_rx', 'net_tx', 'pids_percent', 'maintenance_until')
_COUNTS = ('pid', 'oom_events', 'starts_recent', 'pids_current', 'pids_limit',
           'event_window_seconds', 'event_window_end', 'restart_attempts')
_POLICY_FLAGS = ('manual_actions_protected', 'maintenance_active')
_FILESYSTEM_MEASUREMENTS = ('disk_usage', 'disk_available', 'disk_total', 'disk_percent',
                          'inode_usage', 'inode_available', 'inode_total', 'inode_percent')
_FILESYSTEM_ACCESS = ('fs_readable', 'fs_writable', 'fs_executable')
_FILESYSTEM_MODES = frozenset(('ro', 'rw'))
_ACTION_COUNTERS = ('executed', 'cooldown', 'pending', 'restart-limit', 'maintenance', 'dry-run')
_VERIFICATION_HASHES = ('expected_fingerprint_sha256', 'observed_fingerprint_sha256', 'target_sha256')
_VERIFICATION_STATES = frozenset(('verified', 'failed'))
_VERIFICATION_ERRORS = frozenset(('fingerprint_mismatch', 'tls_validation_failed', 'timeout',
    'tls_handshake_failed', 'connection_or_trust_store_failed', 'probe_failed', 'invalid_stored_certificate'))


class InvalidPayload(ValueError):
    """Fixed safe code; do not incorporate upstream values or exceptions."""

    def __init__(self):
        super().__init__('invalid_payload')


def require(condition):
    if not condition:
        raise InvalidPayload()


def number(value, nullable=False):
    if nullable and value is None:
        return None
    require(type(value) in (int, float))
    try:
        require(math.isfinite(value) and value >= 0)
    except OverflowError:
        raise InvalidPayload() from None
    return value


def integer(value, minimum=0):
    require(type(value) is int and value >= minimum)
    return value


def text(value):
    require(isinstance(value, str) and 0 < len(value) <= 4096)
    require(not any(ord(c) < 32 or ord(c) == 127 for c in value))
    return value


def timestamp(value):
    text(value)
    try:
        parsed = datetime.fromisoformat(value)
        require(parsed.tzinfo is not None and parsed.utcoffset() is not None)
        return number(parsed.timestamp())
    except (ValueError, OverflowError, OSError):
        raise InvalidPayload() from None


def choice(value, allowed):
    require(isinstance(value, str) and value in allowed)
    return value


def records(value, limit):
    require(type(value) is list and len(value) <= limit)
    return value


def settings(source_id, received_at, monotonic, max_age, max_resources):
    text(source_id)
    number(received_at)
    number(monotonic)
    require(number(max_age) > 0)
    integer(max_resources, 1)


def unique(resources):
    require(len({r.key for r in resources}) == len(resources))


def nullable_integer(value, minimum=0):
    return None if value is None else integer(value, minimum)


def nullable_flag(value):
    require(value is None or (type(value) is int and value in (0, 1)))
    return value


def nullable_boolean(value):
    require(value is None or type(value) is bool)
    return value


def filesystems(value, limit):
    if value is None:
        return None
    results, seen = [], set()
    for item in records(value, limit):
        require(type(item) is dict)
        identity = text(item.get('group')), text(item.get('path'))
        require(identity not in seen)
        seen.add(identity)
        fields = [('group', identity[0]), ('path', identity[1])]
        fields.extend((key, number(item.get(key), nullable=True)) for key in _FILESYSTEM_MEASUREMENTS)
        mode = item.get('fs_mode')
        fields.append(('fs_mode', None if mode is None else choice(mode, _FILESYSTEM_MODES)))
        fields.extend((key, nullable_flag(item.get(key))) for key in _FILESYSTEM_ACCESS)
        results.append(Facts(tuple(fields)))
    return tuple(results)


def verification(value):
    if value is None:
        return None
    require(type(value) is dict)
    status = choice(value.get('status'), _VERIFICATION_STATES)
    checked = value.get('checked_at')
    timestamp(checked)
    error = value.get('error_code')
    if error is not None:
        choice(error, _VERIFICATION_ERRORS)
    fields = [('status', status), ('checked_at', checked), ('error_code', error)]
    for key in _VERIFICATION_HASHES:
        fingerprint = value.get(key)
        require(fingerprint is None or _FINGERPRINT.fullmatch(text(fingerprint)) is not None)
        fields.append((key, fingerprint))
    return Facts(tuple(fields))


def parse_monit(payload, *, source_id, received_at, monotonic, max_age, max_resources,
                transport_elapsed=0):
    settings(source_id, received_at, monotonic, max_age, max_resources)
    require(type(payload) is dict)
    require(type(payload.get('api_version')) is int and payload['api_version'] == 1)
    for field in ('ready', 'running', 'last_cycle_success'):
        require(type(payload.get(field)) is bool)
    for field in ('cycles_total', 'errors_total'):
        integer(payload.get(field))
    age = number(payload.get('age_seconds'), nullable=True)
    number(transport_elapsed)
    if age is not None:
        age = number(age + transport_elapsed)
    success = number(payload.get('last_success_at'), nullable=True)
    number(payload.get('last_cycle_finished_at'), nullable=True)
    items = records(payload.get('containers'), max_resources)
    ready = payload['ready']
    require(ready or not items)
    require(not ready or (payload['last_cycle_success'] and age is not None and success is not None))
    resources = []
    filesystem_count = 0
    for item in items:
        require(type(item) is dict)
        native_id = text(item.get('id'))
        require(_DOCKER_ID.fullmatch(native_id) is not None)
        fields = [('name', text(item.get('name'))), ('status', text(item.get('status'))),
                  ('health', choice(item.get('health'), _HEALTH))]
        for field in _MEASUREMENTS:
            fields.append((field, number(item.get(field), nullable=True)))
        fields.extend((field, nullable_integer(item.get(field))) for field in _COUNTS)
        fields.append(('restart_limit', nullable_integer(item.get('restart_limit'), 1)))
        fields.append(('event_history_complete', nullable_flag(item.get('event_history_complete'))))
        fields.extend((field, nullable_boolean(item.get(field))) for field in _POLICY_FLAGS)
        samples = filesystems(item.get('filesystems'), max_resources)
        filesystem_count += len(samples or ())
        require(filesystem_count <= max_resources)
        fields.append(('filesystems', samples))
        resources.append(Resource(source_id, 'container', native_id, tuple(fields)))
    unique(resources)
    freshness = Freshness.UNKNOWN
    if ready:
        freshness = Freshness.FRESH if age <= max_age else Freshness.STALE
    metadata = [(key, payload[key]) for key in ('ready', 'running', 'last_cycle_success',
                 'cycles_total', 'errors_total')]
    metadata.extend((key, number(payload.get(key), nullable=True)) for key in
                    ('last_success_at', 'last_cycle_finished_at', 'age_seconds'))
    metadata.append(('last_error_code', nullable_integer(payload.get('last_error_code'))))
    actions = payload.get('actions')
    if actions is not None:
        require(type(actions) is dict)
        actions = Facts(tuple((key, integer(actions.get(key))) for key in _ACTION_COUNTERS))
    metadata.append(('actions', actions))
    return Snapshot(source_id, 'monit_docker', received_at, monotonic, age,
                    max_age, freshness, ready and freshness == Freshness.FRESH, tuple(resources), tuple(metadata))


def parse_certlord(payload, *, source_id, received_at, monotonic, max_age,
                   max_resources, max_clock_skew):
    settings(source_id, received_at, monotonic, max_age, max_resources)
    number(max_clock_skew)
    require(type(payload) is dict)
    require(type(payload.get('schema_version')) is int and payload['schema_version'] == 1)
    started = timestamp(payload.get('started_at'))
    completed = timestamp(payload.get('completed_at'))
    completed_number = number(payload.get('completed_timestamp_seconds'))
    require(started <= completed and abs(completed - completed_number) <= 0.001)
    resources = []
    observed_times = []
    for item in records(payload.get('certificates'), max_resources):
        require(type(item) is dict)
        native_id = text(item.get('certificate_id'))
        try:
            require(str(UUID(native_id)) == native_id)
        except ValueError:
            raise InvalidPayload() from None
        domains = records(item.get('domains'), 100)
        require(bool(domains))
        domains = tuple(text(domain) for domain in domains)
        observed = timestamp(item.get('observed_at'))
        require(started <= observed <= completed)
        observed_times.append(observed)
        state = choice(item.get('material_state'), _MATERIAL)
        before, after, expiry, fingerprint = (item.get(k) for k in
                                            ('not_before', 'not_after', 'not_after_timestamp_seconds', 'fingerprint_sha256'))
        if state == 'readable':
            before_ts, after_ts = timestamp(before), timestamp(after)
            require(before_ts <= after_ts and abs(number(expiry) - after_ts) <= 0.001)
            require(_FINGERPRINT.fullmatch(text(fingerprint)) is not None)
        else:
            require(all(v is None for v in (before, after, expiry, fingerprint)))
        retry = item.get('issuance_retry_count')
        if retry is not None:
            integer(retry)
        last_retry = item.get('last_issuance_retry_at')
        if last_retry is not None:
            timestamp(last_retry)
        fields = (('domains', domains), ('version', integer(item.get('version'), 1)),
                  ('status', choice(item.get('status'), _LIFECYCLE)),
                  ('origin', choice(item.get('origin'), _ORIGINS)),
                  ('renewal_owner', choice(item.get('renewal_owner'), _ORIGINS)),
                  ('pending_action', choice(item.get('pending_action'), _LIFECYCLE | {'none'})),
                  ('material_state', state), ('not_before', before), ('not_after', after),
                  ('not_after_timestamp_seconds', expiry), ('fingerprint_sha256', fingerprint),
                  ('issuance_retry_count', retry), ('last_issuance_retry_at', last_retry),
                  ('verification', verification(item.get('verification'))), ('observed_at', observed))
        resources.append(Resource(source_id, 'certificate', native_id, fields))
    unique(resources)
    age = max(0, received_at - min(observed_times or [completed]))
    if completed > received_at + max_clock_skew:
        freshness = Freshness.UNKNOWN
    else:
        freshness = Freshness.FRESH if age <= max_age else Freshness.STALE
    metadata = (('started_at', payload['started_at']), ('completed_at', payload['completed_at']),
                ('completed_timestamp_seconds', completed_number))
    return Snapshot(source_id, 'certlord', received_at, monotonic, age, max_age,
                    freshness, freshness == Freshness.FRESH, tuple(resources), metadata)
