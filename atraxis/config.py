"""Strict YAML configuration; no process lifecycle or source access."""
from dataclasses import dataclass
from ipaddress import ip_address
from pathlib import Path

import yaml
from dwho.configuration import parse_conf
from sonicprobe.helpers import load_yaml
from sonicprobe.libs import xys

from atraxis.source_config import SourceConfig, parse_sources


DEFAULT_CONFIG = '/etc/atraxis/atraxis.yml'
_MAX_CONFIG_BYTES = 65536
CONFIG_SCHEMA = xys.load('''
general:
  listen_addr?: !!str
  listen_port?: !~~between(1,65535) 1
  max_workers?: !~~between(1,32) 1
  auth_basic_file![1,]: !!str
  behind_tls_proxy?: false
  rules_web_file?: !!str
  rules_web_origin?: !!str
  rules_web_observations?: !!str
  accounts_web_file?: !!str
  accounts_web_origin?: !!str
  inventory_access_file?: !!str
  inventory_admin_origin?: !!str
  configuration_web_file?: !!str
  configuration_web_origin?: !!str
sources?: [ !!any ]
configuration_history?: [ !!any ]
''')


class ConfigurationError(ValueError):
    pass


class _StrictLoader(yaml.SafeLoader):
    def compose_node(self, parent, index):
        if self.check_event(yaml.AliasEvent):
            raise ConfigurationError('yaml_alias_not_allowed')
        return super().compose_node(parent, index)

    def construct_mapping(self, node, deep=False):
        result = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if not isinstance(key, str) or key in result:
                raise ConfigurationError('invalid_or_duplicate_key')
            result[key] = self.construct_object(value_node, deep=deep)
        return result


@dataclass(frozen=True)
class ServerConfig:
    listen_addr: str
    listen_port: int
    max_workers: int
    auth_basic_file: str
    behind_tls_proxy: bool
    sources: tuple[SourceConfig, ...] = ()
    rules_web_file: str | None = None
    rules_web_origin: str | None = None
    accounts_web_file: str | None = None
    accounts_web_origin: str | None = None
    inventory_access_file: str | None = None
    inventory_admin_origin: str | None = None
    configuration_web_file: str | None = None
    configuration_web_origin: str | None = None
    configuration_path: str | None = None
    rules_web_observations: str | None = None


def _require(condition):
    if not condition:
        raise ConfigurationError('invalid_configuration')


def load_document(path):
    """Read bounded strict YAML without contacting sources or loading plugins."""
    try:
        with open(path, 'rb') as stream:
            raw = stream.read(_MAX_CONFIG_BYTES + 1)
        _require(len(raw) <= _MAX_CONFIG_BYTES)
        return load_yaml(raw.decode('utf-8'), Loader=_StrictLoader)
    except ConfigurationError:
        raise
    except (OSError, UnicodeError, ValueError, TypeError, RecursionError, yaml.YAMLError):
        raise ConfigurationError('invalid_configuration') from None


def load_config(path):
    return parse_config(load_document(path), path)


def parse_config(data, path):
    """Validate an in-memory candidate with the same rules as the daemon."""
    try:
        # XYS checks shape and scalar types; no modifiers/coercions are used.
        _require(xys.validate(data, CONFIG_SCHEMA))
        sources = parse_sources(data.get('sources', []), path)
        if 'configuration_history' in data:
            from atraxis.configuration_admin import validate_history
            validate_history(data['configuration_history'])
        general = data['general']
        host = general.get('listen_addr', '127.0.0.1')
        address = ip_address(host)
        _require(address.version == 4)
        port = general.get('listen_port', 9810)
        workers = general.get('max_workers', 4)
        proxy = general.get('behind_tls_proxy', False)
        mapping = general.get('rules_web_file')
        if mapping is not None:
            _require(bool(mapping) and len(mapping) <= 4096)
            mapping_path = Path(mapping)
            if not mapping_path.is_absolute():
                mapping_path = Path(path).resolve().parent / mapping_path
            from atraxis.web_rules import load_mapping
            load_mapping(mapping_path)
            mapping = str(mapping_path)
        origin = general.get('rules_web_origin')
        if origin is not None:
            _require(mapping is not None)
            from atraxis.web_rule_editor import validate_origin
            validate_origin(origin)
        accounts = general.get('accounts_web_file')
        accounts_origin = general.get('accounts_web_origin')
        _require((accounts is None) == (accounts_origin is None))
        if accounts is not None:
            _require(bool(accounts) and len(accounts) <= 4096)
            account_path = Path(accounts)
            if not account_path.is_absolute():account_path = Path(path).resolve().parent / account_path
            from atraxis.account_admin import load_admin_profile
            from atraxis.web_rule_editor import validate_origin
            load_admin_profile(account_path)
            validate_origin(accounts_origin)
            accounts = str(account_path)
        inventory_access = general.get('inventory_access_file')
        if inventory_access is not None:
            _require(bool(inventory_access) and len(inventory_access) <= 4096)
            access_path = Path(inventory_access)
            if not access_path.is_absolute():
                access_path = Path(path).resolve().parent / access_path
            from atraxis.inventory_access import load_profile
            load_profile(access_path)
            inventory_access = str(access_path)
        observations = general.get('rules_web_observations')
        if observations is not None:
            _require(origin is not None and inventory_access is not None and bool(observations) and len(observations) <= 4096)
            observations = Path(observations)
            if not observations.is_absolute(): observations = Path(path).resolve().parent / observations
            from atraxis.web_observations import load_profile as load_observation_profile
            load_observation_profile(observations)
            observations = str(observations)
        inventory_admin_origin = general.get('inventory_admin_origin')
        if inventory_admin_origin is not None:
            _require(inventory_access is not None)
            from atraxis.web_rule_editor import validate_origin
            validate_origin(inventory_admin_origin)
        _require(address.is_loopback or proxy)
        configuration_file = general.get('configuration_web_file')
        configuration_origin = general.get('configuration_web_origin')
        _require(configuration_origin is None or configuration_file is not None)
        if configuration_file is not None:
            _require(bool(configuration_file) and len(configuration_file) <= 4096)
            configuration_file = Path(configuration_file)
            if not configuration_file.is_absolute(): configuration_file = Path(path).absolute().parent / configuration_file
            from atraxis.configuration_admin import load_profile
            from atraxis.web_rule_editor import validate_origin
            load_profile(configuration_file)
            if configuration_origin is not None:
                validate_origin(configuration_origin)
            configuration_file = str(configuration_file)
        secret = general['auth_basic_file']
        secret_path = Path(secret)
        if not secret_path.is_absolute():
            secret_path = Path(path).resolve().parent / secret_path
        _require(secret_path.is_file())
        with secret_path.open('rb') as stream:
            credentials = stream.read(_MAX_CONFIG_BYTES + 1)
        _require(0 < len(credentials) <= _MAX_CONFIG_BYTES)
        # Framework normalization stays data-only; no modules/plugins are loaded.
        normalized = parse_conf({'general': dict(listen_addr=str(address), listen_port=port,
                                max_workers=workers, auth_basic_file=str(secret_path))})['general']
        return ServerConfig(normalized['listen_addr'], normalized['listen_port'],
                            normalized['max_workers'], str(secret_path), proxy, sources, mapping, origin, accounts, accounts_origin, inventory_access, inventory_admin_origin,
                            configuration_file, configuration_origin, str(Path(path).absolute()), observations)
    except ConfigurationError:
        raise
    except (OSError, UnicodeError, ValueError, TypeError, RecursionError, yaml.YAMLError):
        raise ConfigurationError('invalid_configuration') from None
