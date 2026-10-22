"""Atraxis identity and observation adapters. All rule operations go to Atropos."""
from dataclasses import asdict, dataclass, replace
import hmac
import os
from pathlib import Path
import re

import yaml
from atropos_client.client import RuleClient
from atropos_client.protocol import METHODS
from atraxis.account_storage import storage_config
from atraxis.config import ConfigurationError, _StrictLoader
from atraxis.rule_files import read_private
from atraxis.rule_observations import ObservationBinding, ObservedInventory, LiveRuleEvaluator

MAX_OPERATOR_BYTES = 262144
MAX_IDENTITIES = 128
IDENTIFIER = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}')
DIGEST = re.compile(r'[0-9a-f]{64}')
REQUIRED = frozenset({'version', 'identities'})
OPTIONAL = frozenset({'bindings', 'inventory_client', 'observation_signing', 'accounts_file'})


def _require(value):
    if not value:
        raise ConfigurationError('invalid_rule_gateway_configuration')


@dataclass(frozen=True)
class RuleActor:
    subject: str
    organization: str


def load_operator_config(path, *, remote=False):
    try:
        document = yaml.load(read_private(path, MAX_OPERATOR_BYTES), Loader=_StrictLoader)
        _require(type(document) is dict and REQUIRED <= set(document)
                 and set(document) <= REQUIRED | OPTIONAL
                 and type(document['version']) is int and document['version'] == 2)
        if 'accounts_file' in document:
            storage_config(document['accounts_file'], Path(path).absolute().parent)
        identities = document['identities']
        _require(isinstance(identities, list) and 1 <= len(identities) <= MAX_IDENTITIES)
        seen = set(); subjects = {}
        key = 'token_sha256' if remote else 'uid'
        for item in identities:
            _require(type(item) is dict and set(item) == {key, 'subject', 'organization', 'atropos_client'})
            value = item[key]
            _require((isinstance(value, str) and DIGEST.fullmatch(value) is not None)
                     if remote else (type(value) is int and value >= 0))
            _require((value,item['organization']) not in seen); seen.add((value,item['organization']))
            _require(value not in subjects or subjects[value]==item['subject']); subjects[value]=item['subject']
            _require(all(isinstance(item[k], str) and IDENTIFIER.fullmatch(item[k])
                         for k in ('subject', 'organization')))
            _require(isinstance(item['atropos_client'], str) and 0 < len(item['atropos_client']) <= 4096)
        from atraxis.rule_observation_config import validate_observations
        validate_observations(document)
        if 'inventory_client' in document:
            _require(isinstance(document['inventory_client'], str) and 0 < len(document['inventory_client']) <= 4096)
        return document
    except (OSError, ValueError, TypeError, KeyError, UnicodeError, RecursionError, yaml.YAMLError):
        raise ConfigurationError('invalid_rule_gateway_configuration') from None


class ForwardApplication:
    def __init__(self, operator):
        self.operator = operator

    def __getattr__(self, method):
        if method not in METHODS or method == 'observe':
            raise AttributeError(method)
        def call(*args, **kwargs):
            entry = self.operator.entry()  # Reload authentication mapping for every call.
            actor = self.operator.actor()
            path = self.operator.relative(entry['atropos_client'])
            client = RuleClient(path, expected_actor=asdict(actor))
            try:
                result = getattr(client, method)(*args, **kwargs)
                self.operator.entry()
                return result
            finally:
                client.close()
        return call


class LocalRuleOperator:
    """Trusted local POSIX mapping to an Atropos API credential, never to Redis."""
    remote = False

    def __init__(self, path, *, organization=None):
        self.organization = organization
        self.path = Path(path).absolute()
        self.expected_actor = None
        self.actor()
        self.application = ForwardApplication(self)
        self.live = LiveRuleEvaluator(self.application, self.binding, self.inventory)
        from atraxis.rule_observation_dispatch import LiveRuleDispatcher
        self.observed_dispatch = LiveRuleDispatcher(self.application, self.binding, self.inventory, self.sign_observation)

    def relative(self, value):
        path = Path(value)
        return path if path.is_absolute() else self.path.parent / path

    def document(self):
        return load_operator_config(self.path, remote=self.remote)

    def entry(self):
        key, value = ('token_sha256', self.digest) if self.remote else ('uid', os.getuid())
        document = self.document()
        entries = document['identities']
        matches = [item for item in entries if (hmac.compare_digest(item[key], value)
                   if self.remote else item[key] == value)]
        from atraxis.organization_context import selected
        entry = selected(matches,self.organization)
        from atraxis.account_store import require_membership
        require_membership(self.path, document.get('accounts_file'), entry['subject'], entry['organization'])
        actor = RuleActor(entry['subject'], entry['organization'])
        if self.expected_actor is not None and self.expected_actor != actor:
            raise PermissionError('rule_identity_changed')
        self.expected_actor = actor
        return entry

    def actor(self):
        self.entry()
        return self.expected_actor

    def binding(self, target):
        actor = self.actor()
        matches = [item for item in self.document().get('bindings', [])
                   if item['organization'] == actor.organization and tuple(item['target']) == tuple(target)]
        if len(matches) != 1:
            raise ValueError('observation_binding_missing')
        item = matches[0]
        return ObservationBinding(actor.organization, tuple(target), item['source_id'], item['native_id'],
                                  tuple(item['filesystem']) if 'filesystem' in item else None)

    def inventory(self):
        from atraxis.client import read_inventory
        from atraxis.client_config import load_client_config
        value = self.document().get('inventory_client')
        if value is None:
            raise ConfigurationError('inventory_client_required')
        request = replace(load_client_config(self.relative(value)), organization=self.actor().organization)
        payload, error = read_inventory(request)
        if error is not None:
            raise ValueError('inventory_read_failed')
        return ObservedInventory(payload)

    def sign_observation(self, payload):
        from atropos_client.observation_signing import sign_observation, MAX_KEY_BYTES
        self.actor()  # Reload the authenticated mapping before using the server key.
        config = self.document().get('observation_signing')
        if config is None:
            raise ConfigurationError('observation_signing_required')
        key = read_private(self.relative(config['private_key_file']), MAX_KEY_BYTES)
        return sign_observation(key, config['key_id'], payload)

    def close(self):
        pass
