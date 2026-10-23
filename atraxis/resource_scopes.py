"""Pure, finite inventory read selectors. No execution or account administration."""
from copy import deepcopy
import re
from uuid import UUID

from atraxis.accounts import identifier, require

MAX_SERVERS = 256
MAX_RESOURCE_GROUPS = 256
MAX_TARGETS = 256
MAX_EXPANDED_TARGETS = 16384
MAX_TOTAL_EXPANDED_TARGETS = 65536
MAX_SOURCES = 32
SOURCE_ID_LENGTH = 128
RESOURCE_KINDS = frozenset({'container', 'certificate', 'metric'})
TARGET_KINDS = RESOURCE_KINDS | {'source', 'server', 'group'}
DOCKER_ID = re.compile(r'[0-9a-f]{64}')
SOURCE_FIELDS = ('source_id', 'kind', 'availability', 'current')
ATTEMPT_FIELDS = ('outcome', 'attempted_at', 'completed_at', 'observation_freshness')
SNAPSHOT_FIELDS = ('received_at', 'max_age_seconds', 'age_seconds', 'freshness')


def source_id(value):
    return (type(value) is str and 0 < len(value) <= SOURCE_ID_LENGTH
            and value.isprintable() and value.strip() == value)


def target_key(target):
    require(type(target) is dict and type(target.get('kind')) is str and target['kind'] in TARGET_KINDS)
    kind = target['kind']
    if kind in RESOURCE_KINDS:
        require(set(target) == {'kind', 'source', 'id'} and source_id(target['source']))
        value = target['id']
        require(type(value) is str and len(value) <= 64)
        if kind in ('container', 'metric'):
            require(DOCKER_ID.fullmatch(value) is not None)
        else:
            require(str(UUID(value)) == value)
        return kind, target['source'], value
    require(set(target) == {'kind', 'id'})
    require(source_id(target['id']) if kind == 'source' else identifier(target['id']))
    return kind, target['id']


class ResourceScopes:
    def __init__(self, servers, resource_groups):
        require(type(servers) is list and len(servers) <= MAX_SERVERS)
        require(type(resource_groups) is list and len(resource_groups) <= MAX_RESOURCE_GROUPS)
        self.servers, self.groups = {}, {}
        assigned = set()
        self.source_organizations = {}
        for server in servers:
            require(type(server) is dict and set(server) == {'id', 'organization', 'sources'})
            require(identifier(server['id']) and identifier(server['organization']))
            key = server['organization'], server['id']
            require(key not in self.servers)
            sources = server['sources']
            require(type(sources) is list and 1 <= len(sources) <= MAX_SOURCES)
            require(all(source_id(s) for s in sources) and len(set(sources)) == len(sources))
            # A feed cannot be declared as two distinct servers, even across organizations.
            require(not assigned.intersection(sources))
            assigned.update(sources)
            self.source_organizations.update({s: server['organization'] for s in sources})
            self.servers[key] = tuple(sources)
        expanded = 0
        for group in resource_groups:
            require(type(group) is dict and set(group) == {'id', 'organization', 'targets'})
            require(identifier(group['id']) and identifier(group['organization']))
            key = group['organization'], group['id']
            require(key not in self.groups)
            self.groups[key] = self.expand(group['organization'], group['targets'], allow_groups=False)
            expanded += len(self.groups[key])
            require(expanded <= MAX_TOTAL_EXPANDED_TARGETS)

    def expand(self, organization, targets, *, allow_groups=True):
        require(type(targets) is list and 1 <= len(targets) <= MAX_TARGETS)
        seen, result = set(), []
        for target in targets:
            key = target_key(target)
            require(key not in seen)
            seen.add(key)
            if key[0] == 'server':
                require((organization, key[1]) in self.servers)
                result.extend(('source', s) for s in self.servers[organization, key[1]])
            elif key[0] == 'group':
                require(allow_groups and (organization, key[1]) in self.groups)
                result.extend(self.groups[organization, key[1]])
            else:
                source = key[1]
                require(source not in self.source_organizations or self.source_organizations[source] == organization)
                result.append(key)
            require(len(result) <= MAX_EXPANDED_TARGETS)
        return tuple(result)

    def selection(self, organization, grants):
        selected = {}
        for grant in grants:
            for target in self.expand(organization, grant['targets']):
                if target[0] == 'source':
                    selected[target[1]] = None
                else:
                    _, source, native_id = target
                    if source not in selected:
                        selected[source] = set()
                    if selected[source] is not None:
                        selected[source].add((target[0], native_id))
        return {source: None if values is None else tuple(sorted(values)) for source, values in sorted(selected.items())}


def filter_source(source, selected):
    """Partial views preserve collection timing, never source-wide aggregate metadata."""
    if selected is None:
        result = deepcopy(source)
        result['visibility'] = 'source'
        return result
    selected = frozenset(selected)
    result = {key: source[key] for key in SOURCE_FIELDS}
    result['visibility'] = 'resources'
    attempt = source['last_attempt']
    result['last_attempt'] = (dict({key: attempt[key] for key in ATTEMPT_FIELDS if key in attempt},
                                   source_metadata=None) if attempt is not None else None)
    snapshot = source['last_valid_snapshot']
    result['last_valid_snapshot'] = None
    if snapshot is not None:
        rows = [r for r in snapshot['resources'] if r.get('source_id') == source['source_id']
                and (r.get('kind'), r.get('native_id')) in selected]
        result['last_valid_snapshot'] = dict(
            {key: snapshot[key] for key in SNAPSHOT_FIELDS if key in snapshot}, source_metadata=None,
            resources=deepcopy(rows))
    return result
