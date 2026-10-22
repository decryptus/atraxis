"""Scoped entity administration; authority never implies engine permissions."""
from copy import deepcopy

from atraxis.accounts import AccountError, identifier, require

KINDS = ('tenant', 'organization', 'group', 'user')
OPERATIONS = frozenset(kind + '.' + operation for kind in KINDS for operation in ('create', 'update'))
GRANT_OPERATIONS = OPERATIONS | {'membership.add','membership.remove'}
DELEGATE_OPERATIONS = frozenset({'entity_delegation.create', 'entity_delegation.revoke'})
ALL_OPERATIONS = OPERATIONS | DELEGATE_OPERATIONS
MAX_GRANTS = 1024
MAX_DEPTH = 8
GRANT_FIELDS = frozenset({'id','subject','scope','operations','parent','enabled','delegable'})
SCOPE_FIELDS = frozenset({'kind','id'})
ENTITY_FIELDS = {'tenant': frozenset({'id','name','enabled'}), 'user': frozenset({'id','name','enabled'}),
                 'organization': frozenset({'id','name','enabled','tenant'}),
                 'group': frozenset({'id','name','enabled','organization','parent','inherit_parent'})}


def scope_checked(scope, directory=None):
    require(type(scope) is dict and set(scope) == SCOPE_FIELDS)
    require(scope['kind'] in ('global',) + KINDS)
    if scope['kind'] == 'global': require(scope['id'] is None)
    else:
        require(identifier(scope['id']))
        if directory is not None: require(scope['id'] in getattr(directory, scope['kind'] + 's'))
    return scope


def contains(directory, outer, inner):
    if outer == inner or outer['kind'] == 'global': return True
    kind, key = inner['kind'], inner['id']
    if kind == 'group':
        group = directory.groups[key]
        if outer['kind'] == 'group':
            while group['parent'] is not None:
                if group['parent'] == outer['id']: return True
                group = directory.groups[group['parent']]
            return False
        return contains(directory, outer, {'kind':'organization', 'id':group['organization']})
    if kind == 'organization' and outer['kind'] == 'tenant':
        return directory.organizations[key]['tenant'] == outer['id']
    # Users are global identities, never implicitly owned by one organization.
    return False


def validate(directory, entries):
    require(type(entries) is list and len(entries) <= MAX_GRANTS)
    grants = {}
    for grant in entries:
        require(type(grant) is dict and set(grant) == GRANT_FIELDS)
        require(identifier(grant['id']) and grant['id'] not in grants)
        require(identifier(grant['subject']) and grant['subject'] in directory.users)
        scope_checked(grant['scope'], directory)
        require(type(grant['operations']) is list and bool(grant['operations'])
                and all(type(x) is str and x in GRANT_OPERATIONS for x in grant['operations'])
                and len(set(grant['operations'])) == len(grant['operations']))
        require(type(grant['enabled']) is bool and type(grant['delegable']) is bool)
        require(grant['parent'] is None or identifier(grant['parent']))
        grants[grant['id']] = grant
    for grant in grants.values():
        current, seen, subjects = grant, set(), set()
        while current is not None:
            require(current['id'] not in seen and current['subject'] not in subjects and len(seen) < MAX_DEPTH)
            seen.add(current['id']); subjects.add(current['subject'])
            if current['parent'] is None: break
            require(current['parent'] in grants)
            parent = grants[current['parent']]
            require(parent['delegable'] and set(current['operations']) <= set(parent['operations'])
                    and contains(directory, parent['scope'], current['scope']))
            current = parent
    return grants


def active(directory, grant):
    current = grant
    while current is not None:
        if not current['enabled'] or not directory.users[current['subject']]['enabled']: return False
        # A global root remains able to recover a disabled organizational hierarchy.
        # Narrower authorities require a live membership in their scoped organization/tenant.
        scope = current['scope']
        contexts = directory.contexts(current['subject'])
        if scope['kind'] in ('organization', 'group'):
            org = scope['id'] if scope['kind'] == 'organization' else directory.groups[scope['id']]['organization']
            if org not in contexts: return False
        elif scope['kind'] == 'tenant':
            if not any(directory.organizations[o]['tenant'] == scope['id'] for o in contexts): return False
        current = directory.entity_delegations.get(current['parent'])
    return True


def owned(directory, subject):
    return [g for g in directory.entity_delegations.values() if g['subject'] == subject and active(directory, g)]


def checked_request(request):
    require(type(request) is dict and request.get('operation') in ALL_OPERATIONS)
    operation = request['operation']
    if operation in OPERATIONS:
        kind = operation.split('.')[0]
        require(set(request) == {'operation','entity'} and type(request['entity']) is dict
                and set(request['entity']) == ENTITY_FIELDS[kind])
        entity = request['entity']
        require(identifier(entity['id']) and type(entity['name']) is str and 0 < len(entity['name']) <= 128
                and entity['name'].isprintable() and entity['name'].strip() == entity['name']
                and type(entity['enabled']) is bool)
        for key in ('tenant', 'organization'):
            if key in entity: require(identifier(entity[key]))
        if kind == 'group':
            require(entity['parent'] is None or identifier(entity['parent']))
            require(type(entity['inherit_parent']) is bool)
    elif operation == 'entity_delegation.create':
        require(set(request) == {'operation','parent','subject','scope','operations','delegable'})
        require(identifier(request['parent']) and identifier(request['subject']) and type(request['delegable']) is bool)
        scope_checked(request['scope'])
        require(type(request['operations']) is list and bool(request['operations'])
                and all(type(x) is str and x in GRANT_OPERATIONS for x in request['operations'])
                and len(set(request['operations'])) == len(request['operations']))
    else:
        require(set(request) == {'operation','grant'} and identifier(request['grant']))
    return deepcopy(request)


def propose(directory, subject, request, *, new_id):
    from atraxis.accounts import Directory
    request = checked_request(request); operation = request['operation']
    grants = owned(directory, subject); candidate = directory.document()
    org = None
    if operation in OPERATIONS:
        kind, action = operation.split('.'); entity = request['entity']; target = entity['id']
        existing = getattr(directory, kind + 's').get(target)
        scope = {'kind':kind,'id':target} if existing is not None else (
            {'kind':'tenant','id':entity['tenant']} if kind=='organization' else
            {'kind':'group','id':entity['parent']} if kind=='group' and entity['parent'] is not None else
            {'kind':'organization','id':entity['organization']} if kind=='group' else {'kind':'global','id':None})
        try:
            scope_checked(scope, directory)
            permitted = any(operation in grant['operations'] and contains(directory, grant['scope'], scope) for grant in grants)
        except AccountError:
            permitted = False
        if not permitted: raise PermissionError('entity_administration_denied')
        if (existing is None) != (action == 'create'): raise AccountError('account_entity_conflict')
        if kind == 'organization': org = target
        elif kind == 'group': org = entity['organization']
        if existing is not None:
            # Reparenting across organizations/tenants changes authority ownership.
            # Use a separately designed transfer operation, never an ordinary edit.
            if any(entity[k] != existing[k] for k in ('tenant','organization') if k in entity):
                raise PermissionError('entity_scope_transfer_denied')
            candidate[kind + 's'] = [deepcopy(entity) if x['id'] == target else x for x in candidate[kind + 's']]
        else: candidate[kind + 's'].append(deepcopy(entity))
        updated = Directory(candidate)
        target_scope = {'kind':kind,'id':target}
        if not any(operation in g['operations'] and contains(updated, g['scope'], target_scope) for g in grants):
            raise PermissionError('entity_administration_denied')
        if existing is not None and kind == 'group':
            if not any(operation in g['operations'] and contains(directory, g['scope'], target_scope)
                       and contains(updated, g['scope'], target_scope) for g in grants):
                raise PermissionError('entity_scope_transfer_denied')
        # Edits may revoke the actor's rights, but must never expand their own membership.
        if set(updated.contexts(subject)) - set(directory.contexts(subject)):
            raise PermissionError('entity_self_escalation_denied')
        for organization in updated.organizations:
            if set(updated.effective_groups(subject, organization)) - set(directory.effective_groups(subject, organization)):
                raise PermissionError('entity_self_escalation_denied')
        # Re-enabling another administrator or its ancestry is a trusted-local recovery.
        for attr in ('delegations', 'entity_delegations'):
            from atraxis.account_delegation import active as membership_active
            check = membership_active if attr == 'delegations' else active
            before = getattr(directory, attr); after = getattr(updated, attr)
            if any(not check(directory, g) and check(updated, after[key]) for key, g in before.items()):
                raise PermissionError('administrator_reactivation_requires_local_authority')
    elif operation == 'entity_delegation.create':
        parent = next((g for g in grants if g['id'] == request['parent']), None)
        target = request['subject']; scope_checked(request['scope'], directory)
        if (parent is None or not parent['delegable'] or target == subject or target not in directory.users
                or not directory.users[target]['enabled'] or not set(request['operations']) <= set(parent['operations'])
                or not contains(directory, parent['scope'], request['scope'])):
            raise PermissionError('entity_delegation_denied')
        current = parent
        while current is not None:
            if current['subject'] == target:
                raise PermissionError('entity_delegation_cycle')
            current = directory.entity_delegations.get(current['parent'])
        candidate['entity_delegations'].append(dict(id=new_id, subject=target, parent=parent['id'],
            scope=request['scope'], operations=request['operations'], delegable=request['delegable'], enabled=True))
        updated = Directory(candidate)
        if not active(updated, updated.entity_delegations[new_id]):
            raise PermissionError('entity_delegation_denied')
    else:
        child = directory.entity_delegations.get(request['grant'])
        parent = next((g for g in grants if child is not None and g['id'] == child['parent']), None)
        if child is None or parent is None or not parent['delegable'] or not child['enabled']:
            raise PermissionError('entity_delegation_denied')
        target = child['subject']
        next(g for g in candidate['entity_delegations'] if g['id'] == child['id'])['enabled'] = False
        updated = Directory(candidate)
    return updated, dict(subject=subject, operation=operation, organization=org, target=target)


def view(directory, subject):
    grants = owned(directory, subject)
    entities = {}
    for kind in KINDS:
        entities[kind + 's'] = [deepcopy(e) for e in getattr(directory, kind + 's').values()
            if any(contains(directory, g['scope'], {'kind':kind,'id':e['id']}) for g in grants)]
    return dict(entities=entities, entity_grants=deepcopy(grants),
        entity_delegated=[deepcopy(g) for g in directory.entity_delegations.values() if g['parent'] in {p['id'] for p in grants}])


def membership_grants(directory, subject, organization):
    """Explicit membership permissions on entity scopes; never used as grant parents."""
    if organization not in directory.organizations: return []
    result = []
    for grant in owned(directory, subject):
        operations = set(grant['operations']) & {'membership.add','membership.remove'}
        if not operations: continue
        whole = contains(directory, grant['scope'], {'kind':'organization','id':organization})
        groups = [g['id'] for g in directory.groups.values() if g['organization']==organization
                  and contains(directory, grant['scope'], {'kind':'group','id':g['id']})]
        if whole or groups:
            result.append(dict(grant, organization=organization,groups=groups,operations=sorted(operations),organization_membership=whole))
    return result
