"""Explicit trusted source bindings and conservative live-fact projection."""
import copy
import math
import time
from dataclasses import dataclass

from atraxis.client import CONTAINER_SOURCES, validate_inventory

SCALAR_FIELDS = {'cpu_percent': 'cpu_percent', 'memory_percent': 'mem_percent',
                 'maintenance': 'maintenance_active', 'health_status': 'health',
                 'restart_attempts': 'restart_attempts'}
FILESYSTEM_FIELDS = {'disk_percent': 'disk_percent', 'inode_percent': 'inode_percent',
                     'readable': 'fs_readable', 'writable': 'fs_writable'}
HEALTH_VALUES = frozenset({'healthy', 'unhealthy'})


class ObservationError(ValueError):
    pass


@dataclass(frozen=True)
class ObservationBinding:
    organization: str
    target: tuple
    source_id: str
    native_id: str
    filesystem: tuple | None = None


class ObservedInventory:
    def __init__(self, payload, *, clock=time.monotonic):
        self.payload = validate_inventory(copy.deepcopy(payload))
        self.clock = clock
        self.received = clock()
        if not math.isfinite(self.received):
            raise ObservationError('observation_clock_invalid')

    def resource(self, binding):
        now = self.clock()
        if not math.isfinite(now) or now < self.received:
            raise ObservationError('observation_clock_invalid')
        sources = [s for s in self.payload['sources'] if s['source_id'] == binding.source_id]
        if len(sources) != 1:
            raise ObservationError('observation_source_missing')
        source = sources[0]
        snapshot = source['last_valid_snapshot']
        if (source['kind'] not in CONTAINER_SOURCES or not source['current'] or snapshot is None
                or snapshot['freshness'] != 'fresh' or snapshot['age_seconds'] is None):
            raise ObservationError('observation_not_current')
        age = snapshot['age_seconds'] + now - self.received
        if age > snapshot['max_age_seconds']:
            raise ObservationError('observation_stale')
        resources = [r for r in snapshot['resources'] if r['native_id'] == binding.native_id
                     and r['kind'] == 'container' and r['source_id'] == binding.source_id]
        if len(resources) != 1:
            raise ObservationError('observation_resource_missing')
        return resources[0]['fields'], age

    def facts(self, binding, inputs):
        fields, age = self.resource(binding)
        sample = None
        if set(inputs) & FILESYSTEM_FIELDS.keys():
            if binding.filesystem is None:
                raise ObservationError('filesystem_binding_required')
            samples = fields.get('filesystems')
            if not isinstance(samples, list):
                raise ObservationError('filesystem_observation_missing')
            matches = [s for s in samples if (s.get('group'), s.get('path')) == binding.filesystem]
            if len(matches) != 1:
                raise ObservationError('filesystem_observation_ambiguous_or_missing')
            sample = matches[0]
        values = {}
        for key in inputs:
            if key in SCALAR_FIELDS:
                value = fields.get(SCALAR_FIELDS[key])
            elif key in FILESYSTEM_FIELDS:
                value = sample.get(FILESYSTEM_FIELDS[key])
            else:
                # In particular, starts_recent is not silently renamed restart_count.
                raise ObservationError('unsupported_observation_input')
            if value is None:
                raise ObservationError('observation_value_missing')
            if key in ('readable', 'writable'):
                if type(value) is not int or value not in (0, 1):
                    raise ObservationError('observation_value_invalid')
                value = bool(value)
            elif key == 'maintenance':
                if type(value) is not bool:
                    raise ObservationError('observation_value_invalid')
            elif key == 'health_status':
                if not isinstance(value, str) or value not in HEALTH_VALUES:
                    raise ObservationError('health_observation_not_ready')
            elif key == 'restart_attempts':
                if type(value) is not int or value < 0:
                    raise ObservationError('observation_value_invalid')
            elif type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                raise ObservationError('observation_value_invalid')
            values[key] = value
        return values, age

    def attestation(self, binding, inputs):
        facts, _ = self.facts(binding, inputs)
        _, age = self.resource(binding)
        source = next(s for s in self.payload['sources'] if s['source_id'] == binding.source_id)
        return {'facts': facts, 'source': {'source_id': binding.source_id, 'native_id': binding.native_id,
                    'filesystem': list(binding.filesystem) if binding.filesystem is not None else None},
                'age_seconds': age, 'max_age_seconds': source['last_valid_snapshot']['max_age_seconds']}


class LiveRuleEvaluator:
    def __init__(self, application, binding_loader, inventory_loader):
        self.application = application
        self.binding_loader = binding_loader
        self.inventory_loader = inventory_loader

    def evaluate(self, name, target):
        # Authorize and inspect the exact version before contacting the read API.
        state = self.application.active(name)
        if state['revision'] is None:
            raise ObservationError('rule_not_active')
        record = self.application.read(name, state['revision'])
        if list(target) not in record['context']['targets']:
            raise PermissionError('target_outside_rule_scope')
        binding = self.binding_loader(tuple(target))
        if binding.target != tuple(target):
            raise ObservationError('observation_binding_mismatch')
        observed = self.inventory_loader()
        facts, _ = observed.facts(binding, record['document']['inputs'])
        decision = self.application.evaluate_active(name, target, facts)
        if decision['generation'] != state['generation'] or decision['revision'] != state['revision']:
            raise ObservationError('active_version_changed')
        _, age = observed.resource(binding)  # Expiry during evaluation invalidates the result.
        return dict(decision, observation={'source_id': binding.source_id,
                    'native_id': binding.native_id, 'age_seconds': age}, facts=facts)
