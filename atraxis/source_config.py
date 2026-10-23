"""Offline source settings; credentials and HTTP objects never enter the core."""
from dataclasses import dataclass, field
from ipaddress import ip_address
import json
from pathlib import Path
import ssl
from urllib.parse import urlsplit

from sonicprobe.libs import network, xys


MAX_SOURCES = 32
MAX_CREDENTIAL_BYTES = 8192
MAX_CA_BYTES = 1024 * 1024
MIN_TIMEOUT = 1
MAX_TIMEOUT = 60
MIN_RESPONSE_BYTES = 1024
MAX_RESPONSE_BYTES = 4194304
SOURCE_ROUTES = {'monit_docker': '/v1/status', 'certlord': '/api/certificates/observations',
                 'prometheus': '/api/v1/query'}
SOURCE_SCHEMA = xys.load('''
source_id![1,128]: !!str
kind: !~~enum(monit_docker,certlord,prometheus)
origin![1,2048]: !!str
credentials_file![1,4096]: !!str
ca_file?[1,4096]: !!str
allow_loopback_http?: false
poll_interval?: !~~between(1,3600) 1
timeout?: !~~between(1,60) 1
max_age?: !~~between(1,86400) 1
max_response_bytes?: !~~between(1024,4194304) 1
max_resources?: !~~between(1,10000) 1
max_clock_skew?: !~~between(0,300) 0
''')
DEFAULT_POLL_INTERVAL = 60
DEFAULT_TIMEOUT = 10
DEFAULT_MAX_AGE = 180
DEFAULT_MAX_RESPONSE_BYTES = 1024 * 1024
DEFAULT_MAX_RESOURCES = 1000
DEFAULT_MAX_CLOCK_SKEW = 30


class InvalidSourceConfiguration(ValueError):
    def __init__(self):
        super().__init__('invalid_source_configuration')


def _require(condition):
    if not condition:
        raise InvalidSourceConfiguration()


@dataclass(frozen=True)
class SourceConfig:
    source_id: str
    kind: str
    origin: str
    credentials_file: str = field(repr=False)
    ca_file: str | None = field(repr=False)
    poll_interval: int
    timeout: int
    max_age: int
    max_response_bytes: int
    max_resources: int
    max_clock_skew: int
    metric: dict | None = field(default=None, repr=False)

    @property
    def url(self):
        return self.origin + SOURCE_ROUTES[self.kind]


def _read_file(value, config_path, limit):
    path = Path(value)
    if not path.is_absolute():
        path = Path(config_path).resolve().parent / path
    _require(path.is_file())
    with path.open('rb') as stream:
        data = stream.read(limit + 1)
    _require(0 < len(data) <= limit)
    return str(path), data


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        _require(key not in result)
        result[key] = value
    return result


def read_credentials(value, config_path):
    path, data = _read_file(value, config_path, MAX_CREDENTIAL_BYTES)
    document = json.loads(data.decode('utf-8'), object_pairs_hook=_unique_pairs)
    _require(type(document) is dict and set(document) == {'username', 'password'})
    for value in document.values():
        _require(type(value) is str and bool(value))
        value.encode('utf-8')
        _require(not any(ord(c) < 32 or ord(c) == 127 for c in value))
    _require(':' not in document['username'])
    return path, document


def _credentials(value, config_path):
    return read_credentials(value, config_path)[0]


def _origin(value, allow_http):
    _require(not any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value))
    _require(not any(c in value for c in ('%', '\\', '?', '#')))
    parsed = urlsplit(value)
    _require(parsed.scheme in ('http', 'https') and bool(parsed.hostname))
    _require(parsed.username is None and parsed.password is None)
    _require(parsed.path in ('', '/') and not parsed.query and not parsed.fragment)
    _require(not parsed.netloc.endswith(':'))
    port = parsed.port
    _require(port is None or 1 <= port <= 65535)
    try:
        address = ip_address(parsed.hostname)
    except ValueError:
        address = None
    if parsed.scheme == 'http':
        _require(allow_http and address is not None and address.is_loopback)
    else:
        _require(not allow_http)
    if address is not None:
        host = address.compressed
        if address.version == 6:
            host = '[' + host + ']'
    else:
        host = network.encode_idn(parsed.hostname, True)
        _require(network.valid_domain(host))
    return parsed.scheme + '://' + host + (':' + str(port) if port is not None else '')


def parse_connection(document, config_path):
    """Shared offline destination, explicit credential and CA validation."""
    origin = _origin(document['origin'], document.get('allow_loopback_http', False))
    credentials = _credentials(document['credentials_file'], config_path)
    ca_file = None
    if 'ca_file' in document:
        _require(origin.startswith('https://'))
        ca_file, data = _read_file(document['ca_file'], config_path, MAX_CA_BYTES)
        ssl.create_default_context(cadata=data.decode('ascii'))
    return origin, credentials, ca_file


def parse_sources(documents, config_path):
    """Validate without resolving DNS, contacting a source or retaining passwords."""
    try:
        _require(type(documents) is list and len(documents) <= MAX_SOURCES)
        sources, seen = [], set()
        for document in documents:
            _require(type(document) is dict)
            plain = {k: v for k, v in document.items() if k != 'metric'}
            _require(xys.validate(plain, SOURCE_SCHEMA))
            metric = None
            if document['kind'] == 'prometheus':
                from atraxis.prometheus import selection, query_parameters
                metric = selection(document.get('metric'))
                query_parameters(metric, document.get('max_age', DEFAULT_MAX_AGE),
                                 document.get('timeout', DEFAULT_TIMEOUT),
                                 document.get('max_resources', DEFAULT_MAX_RESOURCES))
            else:
                _require('metric' not in document)
            identity = document['source_id']
            identity.encode('utf-8')
            _require(identity == identity.strip())
            _require(not any(ord(c) < 32 or ord(c) == 127 for c in identity))
            _require(identity not in seen)
            seen.add(identity)
            origin, credentials, ca_file = parse_connection(document, config_path)
            interval = document.get('poll_interval', DEFAULT_POLL_INTERVAL)
            timeout = document.get('timeout', DEFAULT_TIMEOUT)
            max_age = document.get('max_age', DEFAULT_MAX_AGE)
            _require(timeout <= interval <= max_age)
            sources.append(SourceConfig(identity, document['kind'], origin, credentials, ca_file,
                           interval, timeout, max_age,
                           document.get('max_response_bytes', DEFAULT_MAX_RESPONSE_BYTES),
                           document.get('max_resources', DEFAULT_MAX_RESOURCES),
                           document.get('max_clock_skew', DEFAULT_MAX_CLOCK_SKEW), metric))
        return tuple(sources)
    except InvalidSourceConfiguration:
        raise
    except (OSError, ValueError, TypeError, UnicodeError, RecursionError):
        raise InvalidSourceConfiguration() from None
