"""SQLite account schema v3. Length, sign and relationships are enforced by SQL."""
SCHEMA_VERSION = 3
APPLICATION_ID = 1096045144
from atraxis.account_entities import ALL_OPERATIONS, GRANT_OPERATIONS as ENTITY_OPERATIONS
GLOBAL_AUDIT_OPERATIONS = ALL_OPERATIONS - {'organization.create','organization.update','group.create','group.update'}
AUDIT_OPERATIONS = tuple(sorted(ALL_OPERATIONS | {'membership.add','membership.remove','delegation.create','delegation.revoke'}))


def identifier(column, nullable=False):
    return f"{column} VARCHAR(128) {'NULL' if nullable else 'NOT NULL'} CHECK((typeof({column})='text' AND length({column}) BETWEEN 1 AND 128 AND {column} NOT GLOB '*[^A-Za-z0-9_.:-]*' AND substr({column},1,1) GLOB '[A-Za-z0-9]'){(' OR '+column+' IS NULL') if nullable else ''})"


def unsigned(column):
    return f"{column} INTEGER UNSIGNED NOT NULL CHECK(typeof({column})='integer' AND {column}>=0)"


def flag(column):
    return f"{column} INTEGER UNSIGNED NOT NULL CHECK(typeof({column})='integer' AND {column} IN (0,1))"


def fixed(column, length, nullable=False):
    return f"{column} CHAR({length}) {'NULL' if nullable else 'NOT NULL'} CHECK(typeof({column})='text' AND length({column})={length} AND {column} NOT GLOB '*[^0-9a-f]*'{(' OR '+column+' IS NULL') if nullable else ''})"


NAME = "name VARCHAR(128) NOT NULL CHECK(typeof(name)='text' AND length(name) BETWEEN 1 AND 128 AND trim(name)=name)"
ENTITIES = ('tenants', 'organizations', 'users', 'groups')
SCHEMA = [
    f"CREATE TABLE account_audit (id INTEGER PRIMARY KEY AUTOINCREMENT CHECK(id>0), {fixed('revision',32)}, {fixed('previous',32,True)}, {unsigned('uid')}, time VARCHAR(64) NOT NULL CHECK(length(time) BETWEEN 1 AND 64), {fixed('sha256',64)}, {identifier('actor_subject',True)}, actor_operation VARCHAR(32) CHECK(actor_operation IN {AUDIT_OPERATIONS!r}), {identifier('actor_organization',True)}, {identifier('actor_target',True)}, UNIQUE(revision), UNIQUE(previous), FOREIGN KEY(previous) REFERENCES account_audit(revision), CHECK((actor_subject IS NULL AND actor_operation IS NULL AND actor_organization IS NULL AND actor_target IS NULL) OR (actor_subject IS NOT NULL AND actor_operation IS NOT NULL AND actor_target IS NOT NULL AND (actor_organization IS NOT NULL OR actor_operation IN {tuple(sorted(GLOBAL_AUDIT_OPERATIONS))!r}))))",
    f"CREATE TABLE account_meta (id INTEGER UNSIGNED PRIMARY KEY NOT NULL CHECK(typeof(id)='integer' AND id=1), {fixed('revision',32)}, FOREIGN KEY(revision) REFERENCES account_audit(revision))",
    f"CREATE TABLE tenants ({identifier('id')} PRIMARY KEY, {NAME}, {flag('enabled')}, {unsigned('position')} UNIQUE)",
    f"CREATE TABLE organizations ({identifier('id')} PRIMARY KEY, {NAME}, {flag('enabled')}, {unsigned('position')} UNIQUE, {identifier('tenant')}, FOREIGN KEY(tenant) REFERENCES tenants(id) DEFERRABLE INITIALLY DEFERRED)",
    f"CREATE TABLE users ({identifier('id')} PRIMARY KEY, {NAME}, {flag('enabled')}, {unsigned('position')} UNIQUE)",
    f"CREATE TABLE groups ({identifier('id')} PRIMARY KEY, {NAME}, {flag('enabled')}, {unsigned('position')} UNIQUE, {identifier('organization')}, parent VARCHAR(128), {flag('inherit_parent')}, UNIQUE(id,organization), CHECK(parent IS NOT NULL OR inherit_parent=0), CHECK(parent IS NULL OR parent<>id), FOREIGN KEY(organization) REFERENCES organizations(id) DEFERRABLE INITIALLY DEFERRED, FOREIGN KEY(parent,organization) REFERENCES groups(id,organization) DEFERRABLE INITIALLY DEFERRED)",
    f"CREATE TABLE organization_memberships ({identifier('user')}, {identifier('organization')}, {unsigned('position')} UNIQUE, PRIMARY KEY(user,organization), FOREIGN KEY(user) REFERENCES users(id) DEFERRABLE INITIALLY DEFERRED, FOREIGN KEY(organization) REFERENCES organizations(id) DEFERRABLE INITIALLY DEFERRED)",
    f"CREATE TABLE group_memberships ({identifier('user')}, {identifier('organization')}, {identifier('group_id')}, {unsigned('position')} UNIQUE, PRIMARY KEY(user,organization,group_id), FOREIGN KEY(user,organization) REFERENCES organization_memberships(user,organization) DEFERRABLE INITIALLY DEFERRED, FOREIGN KEY(group_id,organization) REFERENCES groups(id,organization) DEFERRABLE INITIALLY DEFERRED)",
    f"CREATE TABLE delegations ({identifier('id')} PRIMARY KEY, {identifier('subject')}, {identifier('organization')}, parent VARCHAR(128), {flag('organization_membership')}, {flag('delegable')}, {flag('enabled')}, {unsigned('position')} UNIQUE, UNIQUE(id,organization), CHECK(parent IS NULL OR parent<>id), FOREIGN KEY(subject) REFERENCES users(id) DEFERRABLE INITIALLY DEFERRED, FOREIGN KEY(organization) REFERENCES organizations(id) DEFERRABLE INITIALLY DEFERRED, FOREIGN KEY(parent,organization) REFERENCES delegations(id,organization) DEFERRABLE INITIALLY DEFERRED)",
    f"CREATE TABLE delegation_groups ({identifier('delegation')}, {identifier('organization')}, {identifier('group_id')}, {unsigned('position')}, PRIMARY KEY(delegation,group_id), UNIQUE(delegation,position), FOREIGN KEY(delegation,organization) REFERENCES delegations(id,organization) DEFERRABLE INITIALLY DEFERRED, FOREIGN KEY(group_id,organization) REFERENCES groups(id,organization) DEFERRABLE INITIALLY DEFERRED)",
    f"CREATE TABLE delegation_operations ({identifier('delegation')}, operation VARCHAR(17) NOT NULL CHECK(operation IN ('membership.add','membership.remove')), {unsigned('position')}, PRIMARY KEY(delegation,operation), UNIQUE(delegation,position), FOREIGN KEY(delegation) REFERENCES delegations(id) DEFERRABLE INITIALLY DEFERRED)",
    f"CREATE TABLE membership_members ({identifier('subject')}, {identifier('organization')}, PRIMARY KEY(subject,organization), FOREIGN KEY(subject,organization) REFERENCES organization_memberships(user,organization) DEFERRABLE INITIALLY DEFERRED)",
    f"CREATE TABLE membership_groups ({identifier('subject')}, {identifier('organization')}, {identifier('group_id')}, PRIMARY KEY(subject,organization,group_id), FOREIGN KEY(subject,organization) REFERENCES membership_members(subject,organization) DEFERRABLE INITIALLY DEFERRED, FOREIGN KEY(group_id,organization) REFERENCES groups(id,organization) DEFERRABLE INITIALLY DEFERRED)",
    f"CREATE TABLE account_search_scope ({identifier('subject')}, {identifier('organization')}, kind VARCHAR(12) NOT NULL CHECK(kind IN ('user','group','organization')), {identifier('id')}, {NAME}, PRIMARY KEY(subject,organization,kind,id), FOREIGN KEY(subject) REFERENCES users(id) DEFERRABLE INITIALLY DEFERRED, FOREIGN KEY(organization) REFERENCES organizations(id) DEFERRABLE INITIALLY DEFERRED)",
    'CREATE UNIQUE INDEX account_audit_root ON account_audit((1)) WHERE previous IS NULL',
    'CREATE INDEX account_search_kind ON account_search_scope(subject,kind,organization,id)',
    'CREATE INDEX organization_tenant ON organizations(tenant,id)',
    'CREATE INDEX groups_parent ON groups(organization,parent,id)',
    'CREATE INDEX organization_members ON organization_memberships(organization,user)',
    'CREATE INDEX group_members ON group_memberships(organization,group_id,user)',
    'CREATE INDEX delegation_subject ON delegations(subject,organization)',
]
SCHEMA += [
    f"CREATE TABLE entity_delegations ({identifier('id')} PRIMARY KEY, {identifier('subject')}, scope_kind VARCHAR(12) NOT NULL CHECK(scope_kind IN ('global','tenant','organization','group','user')), tenant VARCHAR(128), organization VARCHAR(128), group_id VARCHAR(128), user_id VARCHAR(128), parent VARCHAR(128), {flag('enabled')}, {flag('delegable')}, {unsigned('position')} UNIQUE, CHECK((scope_kind='global' AND tenant IS NULL AND organization IS NULL AND group_id IS NULL AND user_id IS NULL) OR (scope_kind='tenant' AND tenant IS NOT NULL AND organization IS NULL AND group_id IS NULL AND user_id IS NULL) OR (scope_kind='organization' AND organization IS NOT NULL AND tenant IS NULL AND group_id IS NULL AND user_id IS NULL) OR (scope_kind='group' AND group_id IS NOT NULL AND tenant IS NULL AND organization IS NULL AND user_id IS NULL) OR (scope_kind='user' AND user_id IS NOT NULL AND tenant IS NULL AND organization IS NULL AND group_id IS NULL)), CHECK(parent IS NULL OR parent<>id), FOREIGN KEY(subject) REFERENCES users(id) DEFERRABLE INITIALLY DEFERRED, FOREIGN KEY(tenant) REFERENCES tenants(id) DEFERRABLE INITIALLY DEFERRED, FOREIGN KEY(organization) REFERENCES organizations(id) DEFERRABLE INITIALLY DEFERRED, FOREIGN KEY(group_id) REFERENCES groups(id) DEFERRABLE INITIALLY DEFERRED, FOREIGN KEY(user_id) REFERENCES users(id) DEFERRABLE INITIALLY DEFERRED, FOREIGN KEY(parent) REFERENCES entity_delegations(id) DEFERRABLE INITIALLY DEFERRED)",
    f"CREATE TABLE entity_delegation_operations ({identifier('delegation')}, operation VARCHAR(32) NOT NULL CHECK(operation IN {tuple(sorted(ENTITY_OPERATIONS))!r}), {unsigned('position')}, PRIMARY KEY(delegation,operation), UNIQUE(delegation,position), FOREIGN KEY(delegation) REFERENCES entity_delegations(id) DEFERRABLE INITIALLY DEFERRED)",
    f"CREATE TABLE entity_search_scope ({identifier('subject')}, organization VARCHAR(128), kind VARCHAR(12) NOT NULL CHECK(kind IN ('tenant','organization','group','user')), {identifier('id')}, {NAME}, PRIMARY KEY(subject,kind,id), CHECK((kind IN ('tenant','user') AND organization IS NULL) OR (kind IN ('organization','group') AND organization IS NOT NULL)), FOREIGN KEY(subject) REFERENCES users(id) DEFERRABLE INITIALLY DEFERRED, FOREIGN KEY(organization) REFERENCES organizations(id) DEFERRABLE INITIALLY DEFERRED)",
    'CREATE INDEX entity_delegation_subject ON entity_delegations(subject,id)',
]
DIRECTORY_TABLES = ('entity_search_scope','entity_delegation_operations','entity_delegations','account_search_scope','membership_groups','membership_members','delegation_operations','delegation_groups','delegations','group_memberships','organization_memberships','groups','users','organizations','tenants')
