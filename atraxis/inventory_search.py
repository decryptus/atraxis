"""Pure search over an already authorized inventory snapshot."""
from copy import deepcopy
from dataclasses import dataclass, asdict
from urllib.parse import parse_qsl, urlsplit
from atraxis.accounts import identifier
from atraxis.resource_scopes import ResourceScopes, source_id
from atraxis.client import SOURCE_KINDS

MAX_QUERY_LENGTH = 128
MAX_QUERY_BYTES = 4096
MAX_PAGE = 100000
DEFAULT_LIMIT = 50
MAX_LIMIT = 200
STATES = frozenset({'all', 'current', 'historical'})
KINDS = frozenset({'all', 'container', 'certificate', 'metric'})
FILTER_FIELDS = ('q', 'state', 'status', 'kind', 'source', 'server', 'group')


@dataclass(frozen=True)
class SearchQuery:
    q: str = ''
    state: str = 'all'
    status: str = ''
    kind: str = 'all'
    source: str = ''
    server: str = ''
    group: str = ''
    page: int = 1
    limit: int = DEFAULT_LIMIT

    def __post_init__(self):
        if (type(self.q) is not str or len(self.q) > MAX_QUERY_LENGTH or
                self.q and not self.q.isprintable() or
                type(self.status) is not str or len(self.status)>MAX_QUERY_LENGTH or self.status and not self.status.isprintable() or
                type(self.state) is not str or self.state not in STATES or
                type(self.kind) is not str or self.kind not in KINDS or
                type(self.source) is not str or self.source and not source_id(self.source) or
                any(type(v) is not str or v and not identifier(v) for v in (self.server, self.group)) or
                type(self.page) is not int or not 1 <= self.page <= MAX_PAGE or
                type(self.limit) is not int or not 1 <= self.limit <= MAX_LIMIT):
            raise ValueError('invalid_inventory_search')

    def parameters(self):
        return asdict(self)


def parse_query(path):
    if type(path) is not str or len(path.encode()) > MAX_QUERY_BYTES:
        raise ValueError('invalid_inventory_search')
    pairs = parse_qsl(urlsplit(path).query, keep_blank_values=True, strict_parsing=True,
                      max_num_fields=10, errors='strict')
    fields = dict(pairs)
    if len(fields) != len(pairs) or set(fields) - set(FILTER_FIELDS) - {'organization', 'page', 'limit'}:
        raise ValueError('invalid_inventory_search')
    organization = fields.pop('organization', None)
    if organization is not None and not identifier(organization):
        raise ValueError('invalid_inventory_search')
    for key in ('page', 'limit'):
        if key in fields:
            if not fields[key].isascii() or not fields[key].isdigit() or len(fields[key]) > 6:
                raise ValueError('invalid_inventory_search')
            fields[key] = int(fields[key])
    return organization, SearchQuery(**fields)


def search_inventory(payload, query, *, servers=(), resource_groups=()):
    """Filter before counting/paging; never change collection readiness."""
    organization = payload.get('context', {}).get('organization')
    scopes = ResourceScopes(list(servers), list(resource_groups))
    selection = None
    if query.group:
        try:
            selection = scopes.selection(organization, [dict(targets=[dict(kind='group', id=query.group)])])
        except (ValueError, KeyError):
            selection = {}  # Unknown and inaccessible group filters have the same empty result.
    server_sources = None
    if query.server:
        server_sources = set(scopes.servers.get((organization, query.server), ()))
    matches, unavailable = [], []
    needle = query.q.casefold()
    for source in sorted(payload['sources'], key=lambda s: s['source_id']):
        sid = source['source_id']
        if query.source and sid != query.source or server_sources is not None and sid not in server_sources:
            continue
        if selection is not None and sid not in selection:
            continue
        source_kind = SOURCE_KINDS[source['kind']]
        if query.kind != 'all' and query.kind != source_kind:
            continue
        snapshot = source['last_valid_snapshot']
        state = 'current' if source['current'] else 'historical'
        # Unknown or historical data remains explicit, even when a text query has no matches.
        if not source['current']:
            unavailable.append(dict(source_id=sid, state='historical' if snapshot is not None else 'unknown'))
        if snapshot is None or query.state != 'all' and query.state != state:
            continue
        for resource in snapshot['resources']:
            kind = resource.get('kind', source_kind)
            native_id = resource['native_id']
            if query.kind != 'all' and kind != query.kind:
                continue
            if selection is not None and selection[sid] is not None and (kind, native_id) not in selection[sid]:
                continue
            fields = resource['fields']
            if query.status and str(fields.get('status', '')).casefold() != query.status.casefold():
                continue
            values = [sid, native_id, fields.get('name', ''), *fields.get('domains', [])]
            if kind == 'metric':
                values.extend(label['value'] for label in fields['labels'])
            if needle and not any(needle in str(v).casefold() for v in values):
                continue
            matches.append(dict(source_id=sid, kind=kind, native_id=native_id, state=state,
                                fields=fields))
    matches.sort(key=lambda r: (r['source_id'], r['kind'], r['native_id']))
    start = (query.page - 1) * query.limit
    return dict(schema_version=1, context=deepcopy(payload.get('context')), query=query.parameters(),
                ready=payload['ready'], reason=payload['reason'], total=len(matches),
                items=deepcopy(matches[start:start + query.limit]), unavailable=unavailable,
                has_more=start + query.limit < len(matches))
