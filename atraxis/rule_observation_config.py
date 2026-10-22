"""Shared bounded source binding and signing configuration; no identities."""
import re
from atraxis.config import ConfigurationError

MAX_BINDINGS = 10000
IDENTIFIER = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}')


def _require(value):
    if not value:
        raise ConfigurationError('invalid_observation_configuration')


def validate_observations(document):
    bindings = document.get('bindings', [])
    _require(isinstance(bindings, list) and len(bindings) <= MAX_BINDINGS)
    seen = set()
    for item in bindings:
        _require(type(item) is dict and {'organization','target','source_id','native_id'} <= set(item)
                 and set(item) <= {'organization','target','source_id','native_id','filesystem'})
        target = item['target']
        _require(isinstance(target, list) and len(target) == 2 and target[0] == 'container'
                 and isinstance(target[1], str) and IDENTIFIER.fullmatch(target[1]))
        _require(all(isinstance(item[k], str) and IDENTIFIER.fullmatch(item[k])
                     for k in ('organization','source_id')))
        _require(isinstance(item['native_id'], str) and re.fullmatch(r'[0-9a-f]{64}', item['native_id']))
        identity = item['organization'], tuple(target)
        _require(identity not in seen); seen.add(identity)
        if 'filesystem' in item:
            fs = item['filesystem']
            _require(isinstance(fs, list) and len(fs) == 2
                     and all(isinstance(v, str) and 0 < len(v) <= 4096 and v.isprintable() for v in fs))
    if 'observation_signing' in document:
        signing = document['observation_signing']
        _require(type(signing) is dict and set(signing) == {'key_id', 'private_key_file'})
        _require(isinstance(signing['key_id'], str) and IDENTIFIER.fullmatch(signing['key_id']))
        _require(isinstance(signing['private_key_file'], str) and 0 < len(signing['private_key_file']) <= 4096)
