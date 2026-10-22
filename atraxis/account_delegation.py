"""Bounded account administration; no rule/engine permissions are assigned here."""
from copy import deepcopy
import uuid

from atraxis.accounts import AccountError, identifier, require

MAX_DELEGATIONS = 1024
MAX_DELEGATION_DEPTH = 8
MEMBERSHIP_OPERATIONS = frozenset({'membership.add', 'membership.remove'})
OPERATIONS = MEMBERSHIP_OPERATIONS | {'delegation.create', 'delegation.revoke'}
GRANT_FIELDS = frozenset({'id','subject','organization','groups','operations',
                         'organization_membership','delegable','enabled','parent'})


def validate_delegations(directory, entries):
    require(type(entries) is list and len(entries) <= MAX_DELEGATIONS)
    grants = {}
    for item in entries:
        require(type(item) is dict and set(item) == GRANT_FIELDS)
        require(identifier(item['id']) and item['id'] not in grants)
        require(identifier(item['subject']) and item['subject'] in directory.users)
        require(identifier(item['organization']) and item['organization'] in directory.organizations)
        require(type(item['groups']) is list and len(item['groups']) <= len(directory.groups))
        require(all(identifier(g) and g in directory.groups and directory.groups[g]['organization'] == item['organization'] for g in item['groups']))
        require(len(set(item['groups'])) == len(item['groups']))
        require(type(item['operations']) is list and all(type(o) is str for o in item['operations']))
        require(bool(item['operations']) and set(item['operations']) <= MEMBERSHIP_OPERATIONS
                and len(set(item['operations'])) == len(item['operations']))
        require(all(type(item[k]) is bool for k in ('organization_membership','delegable','enabled')))
        require(item['organization_membership'] or bool(item['groups']))
        require(item['parent'] is None or identifier(item['parent']))
        grants[item['id']] = item
    for grant in grants.values():
        seen, subjects, current = set(), set(), grant
        while current is not None:
            require(current['id'] not in seen and current['subject'] not in subjects and len(seen) < MAX_DELEGATION_DEPTH)
            seen.add(current['id']); subjects.add(current['subject'])
            parent_id = current['parent']
            if parent_id is None:
                break
            require(parent_id in grants)
            parent = grants[parent_id]
            require(parent['subject'] != current['subject'] and parent['delegable'])
            require(parent['organization'] == current['organization'])
            require(set(current['groups']) <= set(parent['groups']) and set(current['operations']) <= set(parent['operations']))
            require(not current['organization_membership'] or parent['organization_membership'])
            current = parent
    return grants


def active(directory, grant):
    current = grant
    while current is not None:
        if not current['enabled'] or not directory.allows_membership(current['subject'], current['organization']):
            return False
        current = directory.delegations.get(current['parent'])
    return True


def owned(directory, subject):
    return [g for g in directory.delegations.values() if g['subject'] == subject and active(directory,g)]


def checked_request(request):
    from atraxis.account_entities import ALL_OPERATIONS, checked_request as entity_request
    if type(request) is dict and request.get('operation') in ALL_OPERATIONS:
        return entity_request(request)
    if type(request) is not dict or request.get('operation') not in OPERATIONS:
        raise AccountError('invalid_account_operation')
    operation = request['operation']
    fields = ({'operation','organization','subject','group'} if operation in MEMBERSHIP_OPERATIONS else
              {'operation','parent','subject','groups','operations','organization_membership','delegable'}
              if operation == 'delegation.create' else {'operation','grant'})
    require(set(request) == fields)
    if operation in MEMBERSHIP_OPERATIONS:
        require(identifier(request['organization']) and identifier(request['subject']))
        require(request['group'] is None or identifier(request['group']))
    elif operation == 'delegation.create':
        require(identifier(request['parent']) and identifier(request['subject']))
        require(type(request['groups']) is list and len(request['groups']) <= 4096
                and all(identifier(g) for g in request['groups']) and len(set(request['groups'])) == len(request['groups']))
        require(type(request['operations']) is list and len(request['operations']) <= 2
                and all(type(o) is str for o in request['operations']) and bool(request['operations'])
                and set(request['operations']) <= MEMBERSHIP_OPERATIONS and len(set(request['operations'])) == len(request['operations']))
        require(type(request['organization_membership']) is bool and type(request['delegable']) is bool)
    else:
        require(identifier(request['grant']))
    return deepcopy(request)


def propose(directory, subject, request, *, new_id=None):
    """Return a candidate and scoped audit metadata, or deny without side effects."""
    from atraxis.accounts import Directory
    from atraxis.account_entities import ALL_OPERATIONS, propose as entity_propose
    if type(request) is dict and request.get('operation') in ALL_OPERATIONS:
        return entity_propose(directory, subject, request, new_id=new_id or uuid.uuid4().hex)
    request = checked_request(request)
    operation = request['operation']
    grants = owned(directory, subject)
    candidate = directory.document()
    if operation in MEMBERSHIP_OPERATIONS:
        org, target, group = request['organization'], request['subject'], request['group']
        from atraxis.account_entities import membership_grants
        grants = grants + membership_grants(directory,subject,org)
        if target == subject or target not in directory.users:
            raise PermissionError('account_administration_denied')
        if group is not None and (group not in directory.groups or directory.groups[group]['organization'] != org):
            raise PermissionError('account_administration_denied')
        if group is not None and (target,org,None) not in directory.memberships:
            raise PermissionError('account_administration_denied')
        row = {'user':target,'organization':org,'group':group}
        adding = operation == 'membership.add'
        exists = row in candidate['memberships']
        if exists == adding:
            raise AccountError('account_membership_unchanged')
        if adding:
            candidate['memberships'].append(row)
        else:
            candidate['memberships'].remove(row)
        updated = Directory(candidate)
        before = set(directory.effective_groups(target,org))
        after = set(updated.effective_groups(target,org))
        # An explicit parent membership can affect inherited descendants. Bound all effects.
        affected = before ^ after
        if group is not None:
            affected.add(group)
        if not any(g['organization'] == org and operation in g['operations']
                   and affected <= set(g['groups']) and (group is not None or g['organization_membership']) for g in grants):
            raise PermissionError('account_administration_denied')
        # Membership changes must not indirectly reactivate another administrator's chain.
        if (any(g['subject'] == target and g['organization'] == org for g in directory.delegations.values())
                or any(g['subject']==target for g in directory.entity_delegations.values())):
            raise PermissionError('administrator_membership_requires_local_authority')
    elif operation == 'delegation.create':
        parent = next((g for g in grants if g['id'] == request['parent']),None)
        target = request['subject']
        if parent is None or not parent['delegable'] or target == subject or not directory.allows_membership(target,parent['organization']):
            raise PermissionError('account_delegation_denied')
        org = parent['organization']
        if (not set(request['groups']) <= set(parent['groups']) or not set(request['operations']) <= set(parent['operations'])
                or request['organization_membership'] and not parent['organization_membership']):
            raise PermissionError('account_delegation_denied')
        # Do not delegate back to an ancestor subject, even through a different grant ID.
        current = parent
        while current is not None:
            if current['subject'] == target:
                raise PermissionError('account_delegation_cycle')
            current = directory.delegations.get(current['parent'])
        candidate['delegations'].append(dict(id=new_id or uuid.uuid4().hex,subject=target,organization=org,
            groups=request['groups'],operations=request['operations'],organization_membership=request['organization_membership'],
            delegable=request['delegable'],enabled=True,parent=parent['id']))
        updated = Directory(candidate)
    else:
        grant = directory.delegations.get(request['grant'])
        parent = next((g for g in grants if grant is not None and g['id'] == grant['parent']),None)
        if grant is None or parent is None or not parent['delegable'] or not grant['enabled']:
            raise PermissionError('account_delegation_denied')
        org, target = grant['organization'], grant['subject']
        next(g for g in candidate['delegations'] if g['id']==grant['id'])['enabled']=False
        updated = Directory(candidate)
    return updated, {'subject':subject,'operation':operation,'organization':org,'target':target}


def view(directory, subject):
    grants = owned(directory,subject)
    legacy_ids = {g['id'] for g in grants}
    from atraxis.account_entities import membership_grants
    for organization in directory.organizations:
        grants += membership_grants(directory,subject,organization)
    result = []
    for org in sorted({g['organization'] for g in grants}):
        applicable = [g for g in grants if g['organization']==org]
        groups = set().union(*(set(g['groups']) for g in applicable))
        organization_membership = any(g['organization_membership'] for g in applicable)
        memberships = [m for m in directory.document()['memberships'] if m['organization']==org
                       and (m['group'] in groups or m['group'] is None and organization_membership)]
        tenant = directory.tenants[directory.organizations[org]['tenant']]
        result.append({'id':org,'name':directory.organizations[org]['name'],'tenant':tenant['name'],
            'groups':[{'id':g,'name':directory.groups[g]['name']} for g in sorted(groups)],
            'memberships':memberships,'grants':deepcopy(applicable),
            'delegated':[deepcopy(g) for g in directory.delegations.values() if g['parent'] in legacy_ids & {p['id'] for p in applicable}]})
    return result
