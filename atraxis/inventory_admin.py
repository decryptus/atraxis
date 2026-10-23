"""Bounded read-grant administration; profile and audit replace atomically."""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile
import uuid

from atraxis.accounts import AccountError, Directory, identifier, require
from atraxis.account_store import AccountStore
from atraxis.account_lock import private_lock
from atraxis.inventory_access import load_profile, validate_profile, MAX_PROFILE_BYTES, MAX_GRANT_AUDIT, GRANT_OPERATIONS
from atraxis.resource_scopes import ResourceScopes


def fingerprint(grant):
    return hashlib.sha256(json.dumps(grant,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def covers(scopes, organization, allowed, targets):
    requested = scopes.selection(organization,[dict(targets=targets)])
    for source, rows in requested.items():
        if source not in allowed:
            return False
        if allowed[source] is not None and (rows is None or not set(rows) <= set(allowed[source])):
            return False
    return True


class InventoryAdmin:
    def __init__(self,path,username=None):
        self.path=Path(path).absolute();self.username=username
        self.web=username is not None
        self.initial=self.entry()

    def entry(self):
        profile,store,_=load_profile(self.path)
        key,value=('username',self.username) if self.web else ('uid',os.getuid())
        matches=[e for e in profile['identities'] if e.get(key)==value]
        if len(matches)!=1:raise PermissionError('inventory_admin_not_mapped')
        entry=dict(subject=matches[0]['subject'],store=store)
        if hasattr(self,'initial') and self.initial!=entry:
            raise PermissionError('inventory_admin_identity_changed')
        return entry

    def state(self):
        entry=self.entry();profile,store,revision=load_profile(self.path)
        snapshot=AccountStore(store).snapshot(audit=False);directory=Directory(snapshot['directory'])
        scopes=ResourceScopes(profile['servers'],profile['resource_groups'])
        admins={a['organization']:a for a in profile['administrators'] if a['subject']==entry['subject']
                and directory.allows_membership(entry['subject'],a['organization'])}
        if not admins:raise PermissionError('inventory_administration_denied')
        if self.entry()!=entry:raise PermissionError('inventory_admin_identity_changed')
        return entry,profile,revision,snapshot,directory,scopes,admins

    def inspect(self):
        entry,profile,revision,snapshot,directory,scopes,admins=self.state()
        organizations=[]
        for org,admin in sorted(admins.items()):
            allowed=scopes.selection(org,[admin])
            grants=[dict(id=fingerprint(g),**deepcopy(g)) for g in profile['grants'] if g['organization']==org
                    and covers(scopes,org,allowed,g['targets'])]
            organizations.append(dict(id=org,name=directory.organizations[org]['name'],targets=deepcopy(admin['targets']),grants=grants))
        # Do not disclose an audit record whose historical catalog reference can no longer be scoped.
        audit=[]
        for event in profile['audit']:
            org=event['organization']
            if org not in admins:continue
            try:permitted=covers(scopes,org,scopes.selection(org,[admins[org]]),event['grant']['targets'])
            except (ValueError,KeyError,TypeError):permitted=False
            if permitted:audit.append(deepcopy(event))
        if load_profile(self.path)[2]!=revision or AccountStore(entry['store']).snapshot(audit=False)['revision']!=snapshot['revision']:
            raise AccountError('inventory_revision_conflict')
        return dict(subject=entry['subject'],revision=revision,organizations=organizations,audit=audit[-20:])

    def propose(self,request):
        entry,profile,revision,snapshot,directory,scopes,admins=self.state()
        require(type(request) is dict and type(request.get('operation')) is str and request['operation'] in GRANT_OPERATIONS)
        operation=request['operation']
        if operation=='grant.add':
            require(set(request)=={'operation','organization','subject_kind','subject','targets'})
            grant={k:deepcopy(v) for k,v in request.items() if k!='operation'}
        else:
            require(set(request)=={'operation','grant'})
            require(type(request['grant']) is str)
            matches=[g for g in profile['grants'] if fingerprint(g)==request['grant']]
            if len(matches)!=1:raise AccountError('inventory_grant_unavailable')
            grant=deepcopy(matches[0])
        org=grant.get('organization')
        if type(org) is not str or org not in admins:raise PermissionError('inventory_administration_denied')
        allowed=scopes.selection(org,[admins[org]])
        try:permitted=covers(scopes,org,allowed,grant['targets'])
        except (ValueError,KeyError,TypeError):permitted=False
        if not permitted:raise PermissionError('inventory_administration_denied')
        candidate=deepcopy(profile)
        if operation=='grant.add':
            require(identifier(grant['subject']))
            if grant['subject_kind']=='user':
                if not directory.allows_membership(grant['subject'],org):raise PermissionError('inventory_recipient_denied')
            elif grant['subject_kind']=='group':
                group=directory.groups.get(grant['subject'])
                if not group or group['organization']!=org:raise PermissionError('inventory_recipient_denied')
            else:raise AccountError('invalid_inventory_grant')
            if any(fingerprint(g)==fingerprint(grant) for g in profile['grants']):raise AccountError('inventory_grant_unchanged')
            candidate['grants'].append(grant)
        else:candidate['grants'].remove(grant)
        validate_profile(candidate)
        return dict(request=deepcopy(request),revision=revision,directory_revision=snapshot['revision'],identity=entry,grant=grant),candidate

    def prepare(self,request,expected_revision):
        plan,_=self.propose(request)
        if plan['revision']!=expected_revision:raise AccountError('inventory_revision_conflict')
        return plan

    def commit(self,plan):
        entry=self.entry()
        if entry!=plan['identity']:raise PermissionError('inventory_admin_identity_changed')
        # Global lock order: account directory, then inventory profile. Account writes never acquire the latter.
        store = AccountStore(entry['store'])
        if store.path == self.path:raise AccountError('invalid_inventory_store')
        with store._locked(), private_lock(self.path):
            current,candidate=self.propose(plan['request'])
            if current!=plan:raise AccountError('inventory_revision_conflict')
            if len(candidate['audit'])>=MAX_GRANT_AUDIT:raise AccountError('inventory_audit_capacity')
            event_id=uuid.uuid4().hex
            candidate['audit'].append(dict(id=event_id,actor=entry['subject'],organization=plan['grant']['organization'],
                operation=plan['request']['operation'],grant=plan['grant'],time=datetime.now(timezone.utc).isoformat(),previous_profile=plan['revision']))
            validate_profile(candidate)
            encoded=json.dumps(candidate,sort_keys=True,ensure_ascii=True).encode()
            if len(encoded)>MAX_PROFILE_BYTES:raise AccountError('inventory_profile_capacity')
            descriptor,temporary=tempfile.mkstemp(prefix='.inventory-',dir=self.path.parent)
            try:
                with os.fdopen(descriptor,'wb') as stream:
                    stream.write(encoded);stream.flush();os.fsync(stream.fileno())
                os.replace(temporary,self.path)
                parent=os.open(self.path.parent,os.O_RDONLY|os.O_DIRECTORY)
                try:os.fsync(parent)
                finally:os.close(parent)
            finally:
                if os.path.exists(temporary):os.unlink(temporary)
            return event_id


GRANT_FORM_FIELDS = (
    ('organization', 'Organization ID', ''),
    ('subject_kind', 'Recipient type (user or group)', 'user'),
    ('subject', 'Recipient ID', ''),
    ('kind', 'Target type (source, server, container, certificate, metric or group)', 'container'),
    ('source', 'Source ID (container, certificate or metric only)', ''),
    ('id', 'Target ID', ''),
)


def request_from_fields(fields):
    require(type(fields) is dict and set(fields) == {name for name, _, _ in GRANT_FORM_FIELDS})
    require(all(type(v) is str and len(v) <= 128 for v in fields.values()))
    target = dict(kind=fields['kind'], id=fields['id'])
    if fields['kind'] in ('container', 'certificate', 'metric'):
        target['source'] = fields['source']
    else:
        require(not fields['source'])
    return dict(operation='grant.add', organization=fields['organization'],
                subject_kind=fields['subject_kind'], subject=fields['subject'], targets=[target])
