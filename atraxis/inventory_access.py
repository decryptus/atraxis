"""Private read grants and live organization context for inventory endpoints."""
import hashlib
import re
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

import yaml

from atraxis.accounts import AccountError, Directory, identifier, require
from atraxis.account_store import AccountStore
from atraxis.account_storage import storage_config
from atraxis.config import _StrictLoader
from atraxis.rule_files import read_private
from atraxis.resource_scopes import ResourceScopes, filter_source, MAX_TOTAL_EXPANDED_TARGETS, MAX_TARGETS, target_key

MAX_PROFILE_BYTES = 262144
MAX_IDENTITIES = 128
MAX_GRANTS = 1024
MAX_PATH_BYTES = 2048


MAX_GRANT_AUDIT = 256

PROFILE_FIELDS = frozenset({'version','store','identities','grants','servers','resource_groups','administrators','audit'})
GRANT_FIELDS = frozenset({'organization','subject_kind','subject','targets'})
ADMIN_FIELDS = frozenset({'subject','organization','targets'})
AUDIT_FIELDS = frozenset({'id','actor','organization','operation','grant','time','previous_profile'})
GRANT_OPERATIONS = frozenset({'grant.add','grant.remove'})

def validate_profile(data):
    require(type(data) is dict and set(data) == PROFILE_FIELDS)
    require(type(data['version']) is int and data['version'] == 3)
    storage_config(data['store'])
    require(type(data['identities']) is list and 1 <= len(data['identities']) <= MAX_IDENTITIES)
    seen = set()
    for entry in data['identities']:
        require(type(entry) is dict and set(entry) in ({'username', 'subject'}, {'uid', 'subject'}))
        key = 'username' if 'username' in entry else 'uid'
        value = entry[key]
        require(identifier(entry['subject']))
        require(identifier(value) if key == 'username' else type(value) is int and value >= 0)
        require((key, value) not in seen)
        seen.add((key, value))
    require(type(data['grants']) is list and len(data['grants']) <= MAX_GRANTS)
    scopes = ResourceScopes(data['servers'], data['resource_groups'])
    expanded = sum(len(values) for values in scopes.groups.values())
    for grant in data['grants']:
        require(type(grant) is dict and set(grant) == GRANT_FIELDS)
        require(identifier(grant['organization']) and identifier(grant['subject']))
        require(type(grant['subject_kind']) is str and grant['subject_kind'] in ('user', 'group'))
        expanded += len(scopes.expand(grant['organization'], grant['targets']))
        require(expanded <= MAX_TOTAL_EXPANDED_TARGETS)
    require(type(data['administrators']) is list and len(data['administrators']) <= MAX_IDENTITIES)
    seen_admins = set()
    for admin in data['administrators']:
        require(type(admin) is dict and set(admin) == ADMIN_FIELDS)
        require(identifier(admin['subject']) and identifier(admin['organization']))
        key = admin['subject'], admin['organization']
        require(key not in seen_admins)
        seen_admins.add(key)
        expanded += len(scopes.expand(admin['organization'], admin['targets']))
        require(expanded <= MAX_TOTAL_EXPANDED_TARGETS)
    require(type(data['audit']) is list and len(data['audit']) <= MAX_GRANT_AUDIT)
    for event in data['audit']:
        require(type(event) is dict and set(event) == AUDIT_FIELDS)
        require(type(event['id']) is str and re.fullmatch(r'[0-9a-f]{32}',event['id']) is not None)
        require(identifier(event['actor']) and identifier(event['organization']))
        require(type(event['operation']) is str and event['operation'] in GRANT_OPERATIONS)
        require(type(event['time']) is str and 0 < len(event['time']) <= 64 and event['time'].isprintable())
        require(type(event['previous_profile']) is str and re.fullmatch(r'[0-9a-f]{64}',event['previous_profile']) is not None)
        grant = event['grant']
        require(type(grant) is dict and set(grant) == GRANT_FIELDS)
        require(grant['organization'] == event['organization'] and identifier(grant['subject']))
        require(type(grant['subject_kind']) is str and grant['subject_kind'] in ('user','group'))
        # Historical catalog references can outlive their catalog; validate shape, not current expansion.
        require(type(grant['targets']) is list and 1 <= len(grant['targets']) <= MAX_TARGETS)
        for target in grant['targets']:
            target_key(target)


def load_profile(path):
    try:
        raw = read_private(path, MAX_PROFILE_BYTES)
        data = yaml.load(raw, Loader=_StrictLoader)
        validate_profile(data)
        destination = storage_config(data['store'], Path(path).absolute().parent)
        return data, destination, hashlib.sha256(raw).hexdigest()
    except (OSError, ValueError, TypeError, KeyError, RecursionError, yaml.YAMLError):
        raise AccountError('inventory_access_unavailable') from None


def requested_organization(path):
    if type(path) is not str or len(path.encode()) > MAX_PATH_BYTES:
        raise ValueError('invalid_inventory_context')
    pairs = parse_qsl(urlsplit(path).query, keep_blank_values=True, strict_parsing=True,
                      max_num_fields=1, errors='strict')
    if not pairs:
        return None
    if pairs[0][0] != 'organization' or not identifier(pairs[0][1]):
        raise ValueError('invalid_inventory_context')
    return pairs[0][1]


class InventoryAccess:
    def __init__(self, path):
        self.path = path
        load_profile(path)

    def resolve(self, username, organization):
        profile, store, digest = load_profile(self.path)
        identities = [e for e in profile['identities'] if e.get('username') == username]
        if len(identities) != 1:
            raise PermissionError('inventory_access_denied')
        subject = identities[0]['subject']
        snapshot = AccountStore(store).snapshot(audit=False)
        directory = Directory(snapshot['directory'])
        contexts = [{'id': org, 'name': directory.organizations[org]['name']}
                    for org in directory.contexts(subject)]
        if not contexts or organization is not None and organization not in {c['id'] for c in contexts}:
            raise PermissionError('inventory_access_denied')
        if organization is None and len(contexts) == 1:
            organization = contexts[0]['id']
        groups = directory.effective_groups(subject, organization) if organization else ()
        applicable = [grant for grant in profile['grants'] if grant['organization'] == organization and (
            grant['subject_kind'] == 'user' and grant['subject'] == subject or
            grant['subject_kind'] == 'group' and grant['subject'] in groups)]
        sources = ResourceScopes(profile['servers'], profile['resource_groups']).selection(organization, applicable)
        return dict(subject=subject, organization=organization, organizations=contexts,
                    sources=sources, revision=snapshot['revision'], profile=digest, store=store)

    def snapshot(self, username, organization, service):
        scope = self.resolve(username, organization)
        payload = service.status() if scope['organization'] else None
        # Membership/profile replacement during collection of the view cannot release stale rights.
        if self.resolve(username, organization) != scope:
            raise PermissionError('inventory_access_changed')
        return scope, project(payload, scope) if payload is not None else None

    def search(self, username, organization, query, service):
        from atraxis.inventory_search import search_inventory
        scope, payload = self.snapshot(username, organization, service)
        if payload is None:
            return scope, None
        profile, _, digest = load_profile(self.path)
        if digest != scope['profile']:
            raise PermissionError('inventory_access_changed')
        result = search_inventory(payload, query, servers=profile['servers'], resource_groups=profile['resource_groups'])
        if self.resolve(username, organization) != scope:
            raise PermissionError('inventory_access_changed')
        return scope, result


def project(payload, scope):
    """Project current permissions before counting resources or computing readiness."""
    sources = [filter_source(s, scope['sources'][s['source_id']]) for s in payload['sources']
               if s['source_id'] in scope['sources']]
    collecting = payload['collecting']
    ready = bool(sources) and collecting and all(s['current'] for s in sources)
    reason = ('no_visible_sources' if not sources else 'collector_stopped' if not collecting
              else 'sources_not_current' if not ready else None)
    return dict(schema_version=payload['schema_version'], collecting=collecting,
                ready=ready, reason=reason, sources=sources,
                context=dict(subject=scope['subject'], organization=scope['organization'],
                             organizations=scope['organizations']))
