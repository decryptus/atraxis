"""Bounded account directory. Authentication and rule permissions stay separate."""
from copy import deepcopy
import re

IDENTIFIER = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}')
MAX_ENTITIES = 4096
MAX_MEMBERSHIPS = 16384
MAX_GROUP_DEPTH = 16
MAX_PROJECTED_MEMBERS = 4096
MAX_PROJECTED_GROUP_REFERENCES = 65536
FIELDS = frozenset({'version', 'tenants', 'organizations', 'groups', 'users', 'memberships', 'delegations', 'entity_delegations'})


class AccountError(ValueError):
    pass


def require(condition):
    if not condition:
        raise AccountError('invalid_account_directory')


def identifier(value):
    return type(value) is str and IDENTIFIER.fullmatch(value) is not None


class Directory:
    """Global subject/org identifiers; resource groups are a different domain."""
    def __init__(self, document):
        require(type(document) is dict and set(document) == FIELDS)
        require(type(document['version']) is int and document['version'] == 3)
        self._document = deepcopy(document)
        self.tenants = self._entities('tenants', set())
        self.organizations = self._entities('organizations', {'tenant'})
        self.groups = self._entities('groups', {'organization', 'parent', 'inherit_parent'})
        self.users = self._entities('users', set())
        for org in self.organizations.values():
            require(identifier(org['tenant']) and org['tenant'] in self.tenants)
        for group in self.groups.values():
            require(identifier(group['organization']) and group['organization'] in self.organizations)
            require(group['parent'] is None or identifier(group['parent']))
            require(type(group['inherit_parent']) is bool)
            require(group['parent'] is not None or group['inherit_parent'] is False)
            parent = group['parent']
            if parent is not None:
                require(parent in self.groups and self.groups[parent]['organization'] == group['organization'])
        for group in self.groups.values():
            seen = set()
            current = group
            while current is not None:
                require(current['id'] not in seen and len(seen) < MAX_GROUP_DEPTH)
                seen.add(current['id'])
                current = self.groups.get(current['parent'])
        memberships = self._document['memberships']
        require(type(memberships) is list and len(memberships) <= MAX_MEMBERSHIPS)
        self.memberships = set()
        for entry in memberships:
            require(type(entry) is dict and set(entry) == {'user', 'organization', 'group'})
            require(identifier(entry['user']) and entry['user'] in self.users)
            require(identifier(entry['organization']) and entry['organization'] in self.organizations)
            require(entry['group'] is None or identifier(entry['group']))
            if entry['group'] is not None:
                require(entry['group'] in self.groups and
                        self.groups[entry['group']]['organization'] == entry['organization'])
            key = entry['user'], entry['organization'], entry['group']
            require(key not in self.memberships)
            self.memberships.add(key)
        for user, org, group in self.memberships:
            require(group is None or (user, org, None) in self.memberships)

        from atraxis.account_delegation import validate_delegations
        self.delegations = validate_delegations(self, self._document['delegations'])
        from atraxis.account_entities import validate
        self.entity_delegations = validate(self, self._document['entity_delegations'])

    def _entities(self, kind, extra):
        entries = self._document[kind]
        require(type(entries) is list and len(entries) <= MAX_ENTITIES)
        result = {}
        for entry in entries:
            require(type(entry) is dict and set(entry) == {'id', 'name', 'enabled'} | extra)
            require(identifier(entry['id']) and entry['id'] not in result)
            require(type(entry['name']) is str and 0 < len(entry['name']) <= 128
                    and entry['name'].isprintable() and entry['name'].strip() == entry['name'])
            require(type(entry['enabled']) is bool)
            result[entry['id']] = entry
        return result

    def document(self):
        return deepcopy(self._document)

    def allows_membership(self, subject, organization):
        user = self.users.get(subject)
        org = self.organizations.get(organization)
        return bool(user and org and user['enabled'] and org['enabled']
                    and self.tenants[org['tenant']]['enabled']
                    and (subject, organization, None) in self.memberships)

    def contexts(self, subject):
        return tuple(sorted(org for org in self.organizations if self.allows_membership(subject, org)))

    def effective_groups(self, subject, organization):
        """A child opts into parent's members; child membership never flows up."""
        if not self.allows_membership(subject, organization):
            return ()
        result = []
        for group in self.groups.values():
            if group['organization'] != organization:
                continue
            chain, current = [], group
            while current is not None:
                chain.append(current)
                current = self.groups.get(current['parent'])
            # Disabled parents disable their subtree, even with inheritance off.
            if not all(item['enabled'] for item in chain):
                continue
            for item in chain:
                if (subject, organization, item['id']) in self.memberships:
                    result.append(group['id'])
                    break
                if not item['inherit_parent']:
                    break
        return tuple(sorted(result))


    def membership_projection(self, revision):
        require(type(revision) is str and re.fullmatch(r'[0-9a-f]{32}', revision) is not None)
        members, references = [], 0
        pairs = sorted((user, org) for user, org, group in self.memberships if group is None)
        for user, org in pairs:
            if not self.allows_membership(user, org):
                continue
            groups = list(self.effective_groups(user, org))
            references += len(groups)
            require(len(members) < MAX_PROJECTED_MEMBERS and references <= MAX_PROJECTED_GROUP_REFERENCES)
            members.append({'subject': user, 'organization': org, 'groups': groups})
        return {'version': 1, 'revision': revision, 'members': members}


def default_directory(subject):
    require(identifier(subject))
    return Directory({'version': 3, 'delegations': [], 'entity_delegations': [],
        'tenants': [{'id': 'default', 'name': 'Default company group', 'enabled': True}],
        'organizations': [{'id': 'default', 'name': 'Default organization',
                           'tenant': 'default', 'enabled': True}],
        'groups': [], 'users': [{'id': subject, 'name': subject, 'enabled': True}],
        'memberships': [{'user': subject, 'organization': 'default', 'group': None}]})
