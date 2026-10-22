"""Finite account form fields shared by web and terminal adapters."""
LABELS = {'membership.add':'Add membership','membership.remove':'Remove membership',
          'delegation.create':'Delegate administration','delegation.revoke':'Revoke delegation'}
FIELDS = {
 'membership.add': [('organization','Organization ID',''),('subject','User ID',''),('group','Group ID (empty for organization membership)','')],
 'membership.remove': [('organization','Organization ID',''),('subject','User ID',''),('group','Group ID (empty for organization membership)','')],
 'delegation.create': [('parent','Your delegation ID',''),('subject','User ID',''),('groups','Group IDs (comma separated)',''),
    ('operations','Permissions (membership.add, membership.remove)','membership.add'),
    ('organization_membership','Manage organization membership (yes/no)','no'),('delegable','Allow further delegation (yes/no)','no')],
 'delegation.revoke': [('grant','Delegation ID to revoke','')]}


for kind in ('tenant', 'organization', 'group', 'user'):
    for action in ('create', 'update'):
        operation = kind + '.' + action
        LABELS[operation] = action.capitalize() + ' ' + kind
        fields = [('id',kind.capitalize()+' ID (immutable)',''),('name','Name',''),('enabled','Enabled (yes/no)','yes')]
        if kind == 'organization': fields.append(('tenant','Tenant ID (immutable ownership)',''))
        if kind == 'group': fields += [('organization','Organization ID (immutable ownership)',''),('parent','Parent group ID (optional)',''),('inherit_parent','Inherit parent members (yes/no)','no')]
        FIELDS[operation] = fields
LABELS.update({'entity_delegation.create':'Delegate entity administration','entity_delegation.revoke':'Revoke entity delegation'})
FIELDS['entity_delegation.create'] = [('parent','Your entity delegation ID',''),('subject','User ID',''),('scope_kind','Scope (global, tenant, organization, group, user)','organization'),('scope_id','Scope ID (empty for global)',''),('operations','Permissions (comma separated)','group.create,group.update'),('delegable','Allow further delegation (yes/no)','no')]
FIELDS['entity_delegation.revoke'] = [('grant','Entity delegation ID to revoke','')]
