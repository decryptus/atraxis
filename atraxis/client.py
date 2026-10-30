"""Read-only API client and non-interactive presentation; no daemon startup."""
import json
import re
from uuid import UUID

from atraxis.parsing import InvalidPayload, number, require, text
from atraxis.transport import HTTPTransport
from atraxis.source_config import MAX_SOURCES
from atraxis.prometheus import validate_fields as metric_fields
from atraxis.prometheus_certlord import validate_fields as certificate_metric_fields


EXIT_OK = 0
EXIT_ERROR = 1
EXIT_DEGRADED = 2
SOURCE_KINDS = {'monit_docker': 'container', 'certlord': 'certificate', 'prometheus': 'metric', 'prometheus_monit': 'container', 'prometheus_certlord': 'certificate'}
CONTAINER_SOURCES = frozenset(('monit_docker', 'prometheus_monit'))
AVAILABILITY = frozenset(('unknown', 'available', 'unavailable'))
FRESHNESS = frozenset(('fresh', 'stale', 'unknown'))
REASONS = frozenset(('no_visible_sources', 'collectors_not_configured', 'collector_stopped', 'sources_not_current'))
OUTCOMES = frozenset(('success', 'timeout', 'transport_error', 'tls_error', 'authentication_error',
    'forbidden', 'endpoint_unavailable', 'source_error', 'redirect_refused', 'invalid_payload',
    'response_too_large', 'configuration_error', 'busy', 'cancelled', 'collector_error'))
MAX_RESOURCES = 10000
MAX_NESTING = 8
CONTAINER_FIELDS = frozenset(('name', 'status', 'health', 'mem_usage', 'mem_limit', 'mem_percent',
    'cpu_percent', 'io_read', 'io_write', 'net_rx', 'net_tx', 'pid', 'oom_events', 'starts_recent',
    'pids_current', 'pids_limit', 'pids_percent', 'event_window_seconds', 'event_window_end',
    'event_history_complete', 'manual_actions_protected', 'restart_attempts', 'restart_limit',
    'maintenance_active', 'maintenance_until', 'filesystems'))
CERTIFICATE_FIELDS = frozenset(('domains', 'version', 'status', 'origin', 'renewal_owner',
    'pending_action', 'material_state', 'not_before', 'not_after', 'not_after_timestamp_seconds',
    'fingerprint_sha256', 'issuance_retry_count', 'last_issuance_retry_at', 'verification', 'observed_at'))
FILESYSTEM_FIELDS = frozenset(('group', 'path', 'disk_usage', 'disk_available', 'disk_total',
    'disk_percent', 'inode_usage', 'inode_available', 'inode_total', 'inode_percent', 'fs_mode',
    'fs_readable', 'fs_writable', 'fs_executable'))
VERIFICATION_FIELDS = frozenset(('status', 'checked_at', 'error_code',
    'expected_fingerprint_sha256', 'observed_fingerprint_sha256', 'target_sha256'))
SOURCE_METADATA = frozenset(('ready', 'running', 'last_cycle_success', 'cycles_total', 'errors_total',
    'last_success_at', 'last_cycle_finished_at', 'age_seconds', 'last_error_code', 'actions',
    'started_at', 'completed_at', 'completed_timestamp_seconds'))
ACTION_FIELDS = frozenset(('executed', 'cooldown', 'pending', 'restart-limit', 'maintenance', 'dry-run'))
DOCKER_ID = re.compile(r'[0-9a-f]{64}')


def _boolean(value):
    require(type(value) is bool)
    return value


def _enum(value, allowed):
    require(type(value) is str and value in allowed)
    return value


def _safe_value(value, depth=0):
    require(depth <= MAX_NESTING)
    if value is None or type(value) is bool:
        return value
    if type(value) in (int, float):
        return number(value)
    if type(value) is str:
        return text(value)
    if type(value) is list:
        require(len(value) <= MAX_RESOURCES)
        return [_safe_value(item, depth + 1) for item in value]
    # Objects are only accepted through an explicit field allowlist below.
    raise InvalidPayload()


def _object(value, allowed):
    require(type(value) is dict)
    result = {}
    for key in sorted(allowed & value.keys()):
        item = value[key]
        if key == 'filesystems' and item is not None:
            require(type(item) is list and len(item) <= MAX_RESOURCES)
            result[key] = [_object(sample, FILESYSTEM_FIELDS) for sample in item]
        elif key in ('verification', 'actions') and item is not None:
            result[key] = _object(item, VERIFICATION_FIELDS if key == 'verification' else ACTION_FIELDS)
        else:
            result[key] = _safe_value(item)
    return result


def validate_inventory(payload):
    """Validate envelope/provenance and allowlist display facts, never echo raw JSON."""
    require(type(payload) is dict and type(payload.get('schema_version')) is int
            and payload['schema_version'] == 1)
    ready, collecting = _boolean(payload.get('ready')), _boolean(payload.get('collecting'))
    reason = payload.get('reason')
    require((ready and reason is None) or (not ready and type(reason) is str and reason in REASONS))
    source_list = payload.get('sources')
    require(type(source_list) is list and len(source_list) <= MAX_SOURCES)
    sources, identities = [], set()
    for item in source_list:
        require(type(item) is dict)
        identity = text(item.get('source_id'))
        require(identity not in identities)
        identities.add(identity)
        kind = _enum(item.get('kind'), SOURCE_KINDS)
        current = _boolean(item.get('current'))
        availability = _enum(item.get('availability'), AVAILABILITY)
        attempt = item.get('last_attempt')
        if attempt is not None:
            require(type(attempt) is dict)
            freshness = attempt.get('observation_freshness')
            require(freshness is None or type(freshness) is str and freshness in FRESHNESS)
            metadata = attempt.get('source_metadata')
            attempt = dict(outcome=_enum(attempt.get('outcome'), OUTCOMES),
                attempted_at=number(attempt.get('attempted_at')),
                completed_at=number(attempt.get('completed_at')),
                observation_freshness=freshness,
                source_metadata=None if metadata is None else _object(metadata, SOURCE_METADATA))
        expected = ('unknown' if attempt is None else
                    'available' if attempt['outcome'] == 'success' else 'unavailable')
        require(availability == expected)
        visibility = _enum(item.get('visibility', 'source'), {'source', 'resources'})
        if visibility == 'resources':
            require(attempt is None or attempt['source_metadata'] is None)
        snapshot = item.get('last_valid_snapshot')
        if snapshot is not None:
            require(type(snapshot) is dict)
            resources = snapshot.get('resources')
            require(type(resources) is list and len(resources) <= MAX_RESOURCES)
            rows, native_ids = [], set()
            for resource in resources:
                require(type(resource) is dict and resource.get('source_id') == identity
                        and resource.get('kind') == SOURCE_KINDS[kind])
                native_id = text(resource.get('native_id'))
                try:
                    require(DOCKER_ID.fullmatch(native_id) is not None if kind in CONTAINER_SOURCES or kind == 'prometheus'
                            else str(UUID(native_id)) == native_id)
                except ValueError:
                    raise InvalidPayload() from None
                require(native_id not in native_ids)
                native_ids.add(native_id)
                fields = (certificate_metric_fields(resource.get('fields'), native_id) if kind == 'prometheus_certlord' else
                          metric_fields(resource.get('fields'), native_id) if kind == 'prometheus' else
                          _object(resource.get('fields'),
                                 CONTAINER_FIELDS if kind in CONTAINER_SOURCES else CERTIFICATE_FIELDS))
                if kind in CONTAINER_SOURCES:
                    text(fields.get('name'))
                elif kind == 'certlord':
                    require(type(fields.get('domains')) is list and bool(fields['domains']))
                    for domain in fields['domains']:
                        text(domain)
                rows.append(dict(source_id=identity, kind=SOURCE_KINDS[kind],
                                 native_id=native_id, fields=fields))
            metadata = snapshot.get('source_metadata')
            snapshot = dict(received_at=number(snapshot.get('received_at')),
                max_age_seconds=number(snapshot.get('max_age_seconds')),
                age_seconds=number(snapshot.get('age_seconds'), nullable=True),
                freshness=_enum(snapshot.get('freshness'), FRESHNESS), resources=rows,
                source_metadata=None if metadata is None else _object(metadata, SOURCE_METADATA))
            if visibility == 'resources':
                require(snapshot['source_metadata'] is None)
            require(snapshot['max_age_seconds'] > 0)
            require(snapshot['freshness'] != 'fresh' or snapshot['age_seconds'] is not None
                    and snapshot['age_seconds'] <= snapshot['max_age_seconds'])
        require(not current or (collecting and attempt is not None and attempt['outcome'] == 'success'
                and attempt['observation_freshness'] == 'fresh'
                and snapshot is not None and snapshot['freshness'] == 'fresh'
                and snapshot['age_seconds'] is not None))
        sources.append(dict(source_id=identity, kind=kind, availability=availability,
                            current=current, last_attempt=attempt, last_valid_snapshot=snapshot,
                            **({'visibility':visibility} if 'visibility' in item else {})))
    require(ready == (bool(sources) and collecting and all(item['current'] for item in sources)))
    context = payload.get('context')
    if context is not None:
        from atraxis.accounts import identifier
        require(type(context) is dict and set(context) == {'subject','organization','organizations'})
        require(identifier(context['subject']) and identifier(context['organization']))
        require(type(context['organizations']) is list and 1 <= len(context['organizations']) <= 4096)
        ids = set()
        for org in context['organizations']:
            require(type(org) is dict and set(org) == {'id','name'} and identifier(org['id']) and org['id'] not in ids)
            require(type(org['name']) is str and 0 < len(org['name']) <= 128)
            text(org['name'])
            ids.add(org['id'])
        require(context['organization'] in ids)
    require(context is not None or all(s.get('visibility') != 'resources' for s in sources))
    expected_reason = (('no_visible_sources' if context is not None else 'collectors_not_configured') if not sources else 'collector_stopped'
                       if not collecting else 'sources_not_current' if not ready else None)
    require(reason == expected_reason)
    return dict(schema_version=1, ready=ready, reason=reason, collecting=collecting, sources=sources,
                **({'context': context} if context is not None else {}))


def read_inventory(config, cancel=None):
    result = HTTPTransport().fetch(config, cancel=cancel)
    if result.outcome != 'success':
        return None, result.outcome if result.outcome in OUTCOMES | {'organization_required'} else 'transport_error'
    try:
        payload = validate_inventory(result.payload)
        if config.organization is not None:
            require(payload.get('context', {}).get('organization') == config.organization)
        elapsed = number(result.duration_seconds)
        for source in payload['sources']:
            snapshot = source['last_valid_snapshot']
            if snapshot is not None and snapshot['age_seconds'] is not None:
                snapshot['age_seconds'] = number(snapshot['age_seconds'] + elapsed)
                if snapshot['freshness'] == 'fresh' and snapshot['age_seconds'] > snapshot['max_age_seconds']:
                    snapshot['freshness'] = 'stale'
                    source['current'] = False
        if payload['ready'] and not all(source['current'] for source in payload['sources']):
            payload.update(ready=False, reason='sources_not_current')
        return payload, None
    except (InvalidPayload, ValueError, TypeError, OverflowError, RecursionError):
        return None, 'invalid_payload'


def render_inventory(payload, *, details=False, as_json=False):
    if as_json:
        return json.dumps(payload, ensure_ascii=True, allow_nan=False, indent=2) + '\n'
    lines = ['READY' if payload['ready'] else 'DEGRADED: ' + payload['reason']]
    if 'context' in payload:
        lines.append('Organization: ' + json.dumps(payload['context']['organization'], ensure_ascii=True))
    for item in payload['sources']:
        snapshot = item['last_valid_snapshot']
        state = 'current' if item['current'] else 'historical' if snapshot is not None else 'unknown'
        age = 'unknown' if snapshot is None or snapshot['age_seconds'] is None else '%.1fs' % snapshot['age_seconds']
        count = 'unknown' if snapshot is None else str(len(snapshot['resources']))
        lines.append('%s | %s | %s | %s | age=%s | resources=%s' % (
            json.dumps(item['source_id'], ensure_ascii=True), item['kind'], item['availability'], state, age, count))
        if item.get('visibility') == 'resources':
            lines.append('  Selected resources only; source-wide metadata hidden.')
        if details and snapshot is not None:
            for resource in snapshot['resources']:
                lines.append('  %s %s %s' % (resource['kind'],
                    json.dumps(resource['native_id'], ensure_ascii=True),
                    json.dumps(resource['fields'], ensure_ascii=True, allow_nan=False, sort_keys=True)))
    return '\n'.join(lines) + '\n'
