"""Separate CLI settings: no daemon settings or upstream credentials required."""
import yaml
from sonicprobe.helpers import load_yaml
from sonicprobe.libs import xys

from atraxis.config import ConfigurationError, _StrictLoader
from atraxis.read_request import INVENTORY_KIND, ReadRequest
from atraxis.source_config import MAX_RESPONSE_BYTES, DEFAULT_TIMEOUT, parse_connection


DEFAULT_CLIENT_CONFIG = '/etc/atraxis/client.yml'
MAX_CLIENT_CONFIG_BYTES = 65536
CLIENT_SCHEMA = xys.load('''
origin![1,2048]: !!str
credentials_file![1,4096]: !!str
ca_file?[1,4096]: !!str
allow_loopback_http?: false
timeout?: !~~between(1,60) 1
max_response_bytes?: !~~between(1024,4194304) 1
''')


def load_client_config(path):
    try:
        with open(path, 'rb') as stream:
            raw = stream.read(MAX_CLIENT_CONFIG_BYTES + 1)
        if len(raw) > MAX_CLIENT_CONFIG_BYTES:
            raise ConfigurationError('invalid_client_configuration')
        document = load_yaml(raw.decode('utf-8'), Loader=_StrictLoader)
        if not xys.validate(document, CLIENT_SCHEMA):
            raise ConfigurationError('invalid_client_configuration')
        origin, credentials, ca_file = parse_connection(document, path)
        return ReadRequest('atraxis-api', INVENTORY_KIND, origin, credentials, ca_file,
            document.get('timeout', DEFAULT_TIMEOUT), document.get('max_response_bytes', MAX_RESPONSE_BYTES))
    except (OSError, ValueError, TypeError, UnicodeError, RecursionError, yaml.YAMLError):
        raise ConfigurationError('invalid_client_configuration') from None
