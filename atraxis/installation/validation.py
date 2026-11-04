"""Typed, bounded inputs shared by future simple, advanced and batch frontends."""
from dataclasses import dataclass
from ipaddress import ip_address, ip_network
import json
import re
from urllib.parse import urlsplit

MAX_PLAN_BYTES = 16384
MAX_PLAN_DEPTH = 8
MAX_SOURCES = 16
MAX_SECRET_BYTES = 4096
MAX_TLS_BYTES = 131072
IDENTIFIER = re.compile(r'[a-z][a-z0-9_-]{0,47}')
DNS_LABEL = re.compile(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?')
KINDS = ('monit_docker', 'certlord')
PLAN_FIELDS = frozenset(('version', 'name', 'listen_address', 'listen_port', 'server_name', 'sources'))
SOURCE_FIELDS = frozenset(('source_id', 'kind', 'origin'))
ERROR_CODES = frozenset(('invalid_plan', 'invalid_name', 'invalid_port', 'invalid_address',
    'invalid_origin', 'invalid_secret', 'invalid_tls', 'validation_timeout', 'destination_denied', 'unsafe_root',
    'invalid_bundle', 'destination_exists', 'write_failed', 'publication_uncertain',
    'deployment_denied', 'manager_unavailable', 'deployment_failed', 'startup_uncertain',
    'port_unavailable', 'probe_failed', 'probe_timeout', 'source_unready', 'redirect_refused',
    'authentication_failed', 'tls_failed', 'invalid_response', 'invalid_authority'))
FIELDS = frozenset(('plan', 'name', 'listen_address', 'listen_port', 'server_name', 'sources',
                    'credentials', 'tls', 'destination'))


class InstallError(ValueError):
    def __init__(self, code, field='plan'):
        self.code = code if code in ERROR_CODES else 'invalid_plan'
        self.field = field if field in FIELDS else 'plan'
        super().__init__(self.code)

    def report(self):
        return {'status': 'failed', 'field': self.field, 'code': self.code}


def require(condition, code='invalid_plan', field='plan'):
    if not condition:
        raise InstallError(code, field)


def unique(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result)
        result[key] = value
    return result


def reject_constant(_value):
    raise InstallError('invalid_plan')


def decode(raw):
    try:
        require(type(raw) is bytes and len(raw) <= MAX_PLAN_BYTES)
        value = json.loads(raw.decode('utf-8'), object_pairs_hook=unique, parse_constant=reject_constant)
        pending = [(value, 0)]
        while pending:
            item, depth = pending.pop()
            require(depth <= MAX_PLAN_DEPTH)
            if type(item) is dict:
                pending.extend((v, depth + 1) for v in item.values())
            elif type(item) is list:
                pending.extend((v, depth + 1) for v in item)
        return value
    except (ValueError, UnicodeError, RecursionError):
        raise InstallError('invalid_plan') from None


def identifier(value):
    require(type(value) is str and IDENTIFIER.fullmatch(value), 'invalid_name', 'name')
    return value


def port(value):
    require(type(value) is int and 1024 <= value <= 65535, 'invalid_port', 'listen_port')
    return value


def address(value):
    try:
        require(type(value) is str)
        result = ip_address(value)
        require(result.compressed == value and not result.is_unspecified and not result.is_multicast
                and not result.is_link_local and (not result.is_reserved or result.is_loopback)
                and not getattr(result, 'ipv4_mapped', None)
                and not getattr(result, 'sixtofour', None) and not getattr(result, 'teredo', None))
        return result
    except ValueError:
        raise InstallError('invalid_address', 'listen_address') from None


def hostname(value):
    require(type(value) is str and 1 <= len(value) <= 253, 'invalid_origin', 'sources')
    try:
        return str(address(value))
    except InstallError:
        require(value.isascii() and '.' in value and not value.replace('.', '').isdigit()
                and all(DNS_LABEL.fullmatch(label) for label in value.split('.')),
                'invalid_origin', 'sources')
        return value


def origin(value):
    try:
        require(type(value) is str and 1 <= len(value) <= 2048)
        require(value.isascii() and not any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value))
        require(not any(c in value for c in ('%', '\\', '?', '#', '@', '$', '{', '}')))
        parsed = urlsplit(value)
        require(parsed.scheme == 'https' and parsed.path == '' and parsed.hostname)
        host = hostname(parsed.hostname)
        number = parsed.port
        require(number is None or 1 <= number <= 65535)
        rendered = '[' + host + ']' if ':' in host else host
        rendered = 'https://' + rendered + (':' + str(number) if number is not None else '')
        require(rendered == value)  # No silent trimming, IDN conversion, ambiguous IP or port normalization.
        return value
    except (ValueError, TypeError):
        raise InstallError('invalid_origin', 'sources') from None


@dataclass(frozen=True, repr=False)
class Source:
    source_id: str
    kind: str
    origin: str


@dataclass(frozen=True, repr=False)
class Plan:
    name: str
    listen_address: str
    listen_port: int
    server_name: str
    sources: tuple[Source, ...]

    def document(self):
        return {'version': 1, 'name': self.name, 'listen_address': self.listen_address,
                'listen_port': self.listen_port, 'server_name': self.server_name,
                'sources': [{'source_id': s.source_id, 'kind': s.kind, 'origin': s.origin} for s in self.sources]}


def parse(document):
    try:
        require(type(document) is dict and set(document) == PLAN_FIELDS)
        require(type(document['version']) is int and document['version'] == 1)
        name = identifier(document['name'])
        bind = address(document['listen_address'])
        require(bind.version == 4, 'invalid_address', 'listen_address')
        number = port(document['listen_port'])
        try:
            host = hostname(document['server_name'])
        except InstallError:
            raise InstallError('invalid_name', 'server_name') from None
        rows = document['sources']
        require(type(rows) is list and 1 <= len(rows) <= MAX_SOURCES)
        sources = []
        for row in rows:
            require(type(row) is dict and set(row) == SOURCE_FIELDS)
            require(row['kind'] in KINDS)
            source = Source(identifier(row['source_id']), row['kind'], origin(row['origin']))
            require(source.source_id not in {s.source_id for s in sources})
            sources.append(source)
        return Plan(name, str(bind), number, host, tuple(sources))
    except InstallError:
        raise
    except (TypeError, ValueError, KeyError, RecursionError):
        raise InstallError('invalid_plan') from None


@dataclass(frozen=True, repr=False)
class Authority:
    """Trusted host policy, never loaded from an installation form or plan.

    Exact origins (including ports), narrow address ranges and listener addresses
    are capabilities supplied by the operator-owned caller. The UI cannot expand them.
    """
    origins: tuple[str, ...]
    networks: tuple[str, ...]
    listeners: tuple[str, ...] = ('127.0.0.1',)

    def validate(self):
        try:
            require(type(self.origins) is tuple and 1 <= len(self.origins) <= MAX_SOURCES)
            require(type(self.networks) is tuple and 1 <= len(self.networks) <= MAX_SOURCES)
            require(type(self.listeners) is tuple and 1 <= len(self.listeners) <= MAX_SOURCES)
            for value in self.origins: origin(value)
            for value in self.networks:
                net = ip_network(value, strict=True)
                require(str(net) == value and net.prefixlen >= (8 if net.version == 4 else 32))
            for value in self.listeners: require(address(value).version == 4)
        except (ValueError, TypeError):
            raise InstallError('invalid_authority') from None

    def check(self, plan):
        self.validate()
        require(plan.listen_address in self.listeners, 'destination_denied', 'listen_address')
        for source in plan.sources:
            require(source.origin in self.origins, 'destination_denied', 'sources')

    def check_address(self, value):
        self.validate()
        ip = address(value)
        require(any(ip in ip_network(net) for net in self.networks), 'destination_denied', 'sources')
        return ip


def credentials(value, *, login=False):
    require(type(value) is dict and set(value) == {'username', 'password'}, 'invalid_secret', 'credentials')
    for name, text in value.items():
        require(type(text) is str, 'invalid_secret', 'credentials')
        try:
            raw = text.encode('utf-8')
        except UnicodeError:
            raise InstallError('invalid_secret', 'credentials') from None
        require(0 < len(raw) <= MAX_SECRET_BYTES and not any(ord(c) < 32 or ord(c) == 127 for c in text),
                'invalid_secret', 'credentials')
    require(':' not in value['username'] and len(value['username']) <= 128, 'invalid_secret', 'credentials')
    if login:
        identifier(value['username'])
        require(len(value['password']) >= 16, 'invalid_secret', 'credentials')
    return dict(value)
