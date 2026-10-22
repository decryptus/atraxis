"""Trusted identity mapping and shared administration service for web/CLI/Textual."""
import hashlib
import json
import os
from pathlib import Path
import uuid

import yaml

from atraxis.accounts import AccountError, Directory, identifier
from atraxis.account_store import AccountStore
from atraxis.account_storage import storage_config
from atraxis.account_delegation import checked_request, propose, view
from atraxis.config import _StrictLoader
from atraxis.rule_files import read_private

MAX_PROFILE_BYTES = 65536


def load_admin_profile(path):
    try:
        raw = read_private(path,MAX_PROFILE_BYTES)
        data = yaml.load(raw,Loader=_StrictLoader)
        if (type(data) is not dict or set(data) != {'version','store','identities'}
                or type(data['version']) is not int or data['version'] != 1
                or type(data['identities']) is not list
                or not 1 <= len(data['identities']) <= 128):
            raise ValueError()
        seen=set()
        for entry in data['identities']:
            if type(entry) is not dict or set(entry) not in ({'username','subject'},{'uid','subject'}) or not identifier(entry['subject']):
                raise ValueError()
            key = 'username' if 'username' in entry else 'uid'
            value=entry[key]
            if (key=='username' and not identifier(value) or key=='uid' and (type(value) is not int or value<0)
                    or (key,value) in seen):
                raise ValueError()
            seen.add((key,value))
        destination=storage_config(data['store'], Path(path).absolute().parent)
        return data['identities'],destination,hashlib.sha256(raw).hexdigest()
    except (OSError,ValueError,TypeError,KeyError,RecursionError,yaml.YAMLError):
        raise AccountError('invalid_account_admin_profile') from None


class AccountAdmin:
    def __init__(self,path,username=None):
        self.path=Path(path).absolute()
        self.web=username is not None
        self.username=username if self.web else 'uid:'+str(os.getuid())
        self.initial=self.entry()

    def entry(self):
        entries,path,digest=load_admin_profile(self.path)
        key,value=('username',self.username) if self.web else ('uid',os.getuid())
        matches=[entry for entry in entries if entry.get(key)==value]
        if len(matches)!=1:raise PermissionError('account_admin_not_mapped')
        result={'subject':matches[0]['subject'],'store':path,'profile':digest}
        if hasattr(self,'initial') and result!=self.initial:
            raise PermissionError('account_admin_identity_changed')
        return result

    def inspect(self):
        entry=self.entry();store=AccountStore(entry['store']);snapshot=store.snapshot(audit=False)
        result={'revision':snapshot['revision'],'subject':entry['subject'],
                'organizations':view(Directory(snapshot['directory']),entry['subject'])}
        from atraxis.account_entities import view as entity_view
        result.update(entity_view(Directory(snapshot['directory']), entry['subject']))
        if store.revision()!=snapshot['revision']:raise PermissionError('account_administration_changed')
        self.entry()
        if not result['organizations'] and not result['entity_grants']:raise PermissionError('account_administration_denied')
        return result

    def search(self,query):
        entry = self.entry()
        store = AccountStore(entry['store'])
        result = store.search(entry['subject'],query)
        self.entry()
        if store.revision() != result['revision']:
            raise PermissionError('account_search_access_changed')
        self.entry()
        return result

    def prepare(self,request,expected_revision):
        entry=self.entry();snapshot=AccountStore(entry['store']).snapshot(audit=False)
        if snapshot['revision']!=expected_revision:raise AccountError('account_revision_conflict')
        request=checked_request(request);created_id=uuid.uuid4().hex
        _,audit=propose(Directory(snapshot['directory']),entry['subject'],request,new_id=created_id)
        self.entry()
        return {'request':request,'revision':snapshot['revision'],'identity':entry,'created_id':created_id,'audit':audit}

    def commit(self,plan):
        entry=self.entry()
        if plan['identity']!=entry:raise PermissionError('account_admin_identity_changed')
        store=AccountStore(entry['store']);snapshot=store.snapshot(audit=False)
        if snapshot['revision']!=plan['revision']:raise AccountError('account_revision_conflict')
        candidate,audit=propose(Directory(snapshot['directory']),entry['subject'],plan['request'],new_id=plan['created_id'])
        def authorize(current,proposed):
            self.entry()  # Current identity and delegation under the snapshot writer lock.
            checked,metadata=propose(current,entry['subject'],plan['request'],new_id=plan['created_id'])
            if checked.document()!=proposed.document() or metadata!=audit:
                raise PermissionError('account_administration_changed')
        return store.apply(candidate,plan['revision'],authorize=authorize,actor=audit)


def request_from_fields(fields):
    """Finite form contract shared by web and terminal; no identity/grant overrides."""
    operation=fields.get('operation')
    from atraxis.account_entities import OPERATIONS as ENTITY_OPERATIONS, DELEGATE_OPERATIONS, checked_request as entity_request
    from atraxis.account_forms import FIELDS
    if operation in ENTITY_OPERATIONS | DELEGATE_OPERATIONS:
        if set(fields) != {'operation'} | {field[0] for field in FIELDS[operation]}:
            raise AccountError('invalid_account_form')
        values = {key:value for key,value in fields.items() if key!='operation'}
        for key in ('enabled','inherit_parent','delegable'):
            if key in values:
                if values[key] not in ('yes','no'): raise AccountError('invalid_account_form')
                values[key] = values[key]=='yes'
        if operation in ENTITY_OPERATIONS:
            if 'parent' in values: values['parent'] = values['parent'] or None
            return entity_request(dict(operation=operation,entity=values))
        if operation == 'entity_delegation.create':
            values['scope'] = dict(kind=values.pop('scope_kind'),id=values.pop('scope_id') or None)
            values['operations'] = [value.strip() for value in values['operations'].split(',') if value.strip()]
        return entity_request(dict(values, operation=operation))
    if operation in ('membership.add','membership.remove'):
        if set(fields)!={'operation','organization','subject','group'}:raise AccountError('invalid_account_form')
        return checked_request(dict(operation=operation,organization=fields['organization'],subject=fields['subject'],group=fields['group'] or None))
    if operation=='delegation.revoke':
        if set(fields)!={'operation','grant'}:raise AccountError('invalid_account_form')
        return checked_request(fields)
    if operation=='delegation.create':
        if set(fields)!={'operation','parent','subject','groups','operations','organization_membership','delegable'}:
            raise AccountError('invalid_account_form')
        if fields['organization_membership'] not in ('yes','no') or fields['delegable'] not in ('yes','no'):
            raise AccountError('invalid_account_form')
        return checked_request(dict(operation=operation,parent=fields['parent'],subject=fields['subject'],
            groups=[g.strip() for g in fields['groups'].split(',') if g.strip()],
            operations=[o.strip() for o in fields['operations'].split(',') if o.strip()],
            organization_membership=fields['organization_membership']=='yes',delegable=fields['delegable']=='yes'))
    raise AccountError('invalid_account_form')
