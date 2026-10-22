"""Source-signed browser observations under current web and inventory authority."""
import hashlib
from pathlib import Path

import yaml

from atraxis.config import ConfigurationError, _StrictLoader
from atraxis.rule_files import read_private
from atraxis.rule_observation_config import validate_observations
from atraxis.rule_observation_dispatch import LiveRuleDispatcher
from atraxis.rule_observations import ObservationBinding, ObservedInventory
from atraxis.web_rules import RuleReader

MAX_PROFILE_BYTES = 262144
PROFILE_FIELDS = frozenset({'version', 'bindings', 'observation_signing'})
OBSERVATION_METHODS = frozenset({'observation_challenge', 'plan_observed_dispatch', 'dispatch_observed'})


def load_profile(path):
    try:
        raw = read_private(path, MAX_PROFILE_BYTES)
        data = yaml.load(raw, Loader=_StrictLoader)
        if (type(data) is not dict or set(data) != PROFILE_FIELDS
                or type(data['version']) is not int or data['version'] != 1):
            raise ValueError()
        validate_observations(data)
        return data, hashlib.sha256(raw).hexdigest()
    except (OSError, ValueError, TypeError, KeyError, UnicodeError, RecursionError, yaml.YAMLError):
        raise ConfigurationError('invalid_web_observation_configuration') from None


class ObservationReader(RuleReader):
    methods = OBSERVATION_METHODS


class WebObservationSession:
    """One request, no OS identity, ambient inventory grant or cached authority."""
    def __init__(self, reader, profile, access, service):
        self.reader, self.path, self.access, self.service = reader, Path(profile), access, service
        self.document, self.revision = load_profile(self.path)
        self.scope = None
        self.submitted = False
        self.dispatcher = LiveRuleDispatcher(self, self.binding, self.inventory, self.sign)

    def guard(self):
        entry = self.reader.entry()
        if load_profile(self.path)[1] != self.revision:
            raise PermissionError('observation_configuration_changed')
        if self.scope is not None and self.access.resolve(self.reader.username, entry['organization']) != self.scope:
            raise PermissionError('inventory_access_changed')
        return entry

    def observation_challenge(self, *args):
        self.guard()
        result = self.reader.call('observation_challenge', *args)
        self.guard()
        return result

    def plan_observed_dispatch(self, proof):
        self.guard()
        result = self.reader.call('plan_observed_dispatch', proof)
        self.guard()
        return result

    def dispatch_observed(self, proof, **kwargs):
        self.guard()
        self.submitted = True
        result = self.reader.call('dispatch_observed', proof, **kwargs)
        self.guard()
        return result

    def binding(self, target):
        actor = self.guard()
        matches = [item for item in self.document['bindings']
                   if item['organization'] == actor['organization'] and tuple(item['target']) == tuple(target)]
        if len(matches) != 1:
            raise ValueError('observation_binding_missing')
        item = matches[0]
        return ObservationBinding(actor['organization'], tuple(target), item['source_id'], item['native_id'],
                                  tuple(item['filesystem']) if 'filesystem' in item else None)

    def inventory(self):
        actor = self.guard()
        scope, payload = self.access.snapshot(self.reader.username, actor['organization'], self.service)
        if scope['subject'] != actor['subject'] or scope['organization'] != actor['organization'] or payload is None:
            raise PermissionError('inventory_identity_mismatch')
        self.scope = scope
        self.guard()
        return ObservedInventory(payload)

    def sign(self, payload):
        from atropos_client.observation_signing import sign_observation, MAX_KEY_BYTES
        self.guard()
        signing = self.document['observation_signing']
        path = Path(signing['private_key_file'])
        if not path.is_absolute():
            path = self.path.parent / path
        result = sign_observation(read_private(path, MAX_KEY_BYTES), signing['key_id'], payload)
        self.guard()
        return result
