from copy import deepcopy
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch
import yaml

from atraxis.accounts import AccountError, Directory
from atraxis.account_entities import GRANT_OPERATIONS, owned, view
from atraxis.account_delegation import propose
from atraxis.account_admin import AccountAdmin
from atraxis.account_store import AccountStore
from atraxis.account_search import AccountQuery
from test_account_delegation import fixture as memberships, membership


def fixture():
    data = memberships()
    data['delegations'] = []
    data['entity_delegations'] = [dict(id='root-entities', subject='alice',
        scope=dict(kind='global', id=None), operations=sorted(GRANT_OPERATIONS),
        parent=None, enabled=True, delegable=True)]
    return data


def change(kind, id, action='create', **fields):
    return dict(operation=kind+'.'+action, entity=dict(id=id, name=id, enabled=True, **fields))


def delegate(subject='bob', parent='root-entities', kind='organization', id='default',
             operations=('group.create','group.update'), delegable=True):
    return dict(operation='entity_delegation.create', subject=subject, parent=parent,
                scope=dict(kind=kind,id=id), operations=list(operations), delegable=delegable)


class EntityTests(unittest.TestCase):
    def test_create_update_disable_all_kinds_and_audit(self):
        d=Directory(fixture())
        requests=[change('tenant','company'),change('organization','office',tenant='company'),
                  change('user','eve'),change('group','team',organization='office',parent=None,inherit_parent=False)]
        for request in requests:
            d,audit=propose(d,'alice',request)
            self.assertEqual(audit['operation'],request['operation'])
            kind=request['operation'].split('.')[0]
            request=deepcopy(request);request['operation']=kind+'.update'
            request['entity']['enabled']=False;request['entity']['name']='Renamed'
            d,_=propose(d,'alice',request)
            self.assertFalse(getattr(d,kind+'s')[request['entity']['id']]['enabled'])
        self.assertEqual(d.contexts('eve'),())

    def test_organization_scope_does_not_own_global_users_or_other_tenants(self):
        d,_=propose(Directory(fixture()),'alice',delegate(),new_id='child')
        for request in (change('tenant','foreign'), change('user','eve'), change('user','charlie','update'),
                        change('organization','foreign',tenant='default')):
            with self.subTest(request=request),self.assertRaises(PermissionError):propose(d,'bob',request)
        d,_=propose(d,'bob',change('group','new',organization='default',parent=None,inherit_parent=False))
        self.assertEqual(d.groups['new']['organization'],'default')
        self.assertEqual(view(d,'bob')['entities']['users'],[])

    def test_bounded_delegation_cycles_revocation_and_no_union(self):
        d,_=propose(Directory(fixture()),'alice',delegate(),new_id='child')
        d,_=propose(d,'bob',delegate('charlie','child','group','ops',('group.create',)),new_id='grandchild')
        self.assertEqual(len(owned(d,'charlie')),1)
        for request in (delegate('alice','child'),delegate('bob','child'),
                        delegate('dave','child','global',None),delegate('dave','child',operations=('user.update',))):
            with self.assertRaises(PermissionError):propose(d,'bob',request)
        d,_=propose(d,'alice',dict(operation='entity_delegation.revoke',grant='child'))
        self.assertEqual(owned(d,'bob'),[]);self.assertEqual(owned(d,'charlie'),[])
        data=fixture();root=data['entity_delegations'][0]
        data['entity_delegations']=[dict(root,scope=dict(kind='group',id='ops'),operations=['group.update']),
            dict(root,id='second',scope=dict(kind='group',id='secret'),operations=['group.update'])]
        with self.assertRaises(PermissionError):propose(Directory(data),'alice',change('group','night','update',organization='default',parent='secret',inherit_parent=False))

    def test_reparent_cycle_scope_escape_self_expansion_and_admin_recovery_denied(self):
        d,_=propose(Directory(fixture()),'alice',delegate(kind='group',id='ops'),new_id='child')
        with self.assertRaises(PermissionError):propose(d,'bob',change('group','night','update',organization='default',parent='secret',inherit_parent=False))
        with self.assertRaises(AccountError):propose(d,'alice',change('group','ops','update',organization='default',parent='night',inherit_parent=False))
        data=fixture();data['memberships'].append(dict(user='alice',organization='default',group='ops'))
        with self.assertRaises(PermissionError):propose(Directory(data),'alice',change('group','new',organization='default',parent='ops',inherit_parent=True))
        data=d.document();data['users'][1]['enabled']=False
        with self.assertRaises(PermissionError):propose(Directory(data),'alice',change('user','bob','update'))
        with self.assertRaises(PermissionError):propose(d,'alice',membership('bob'))

    def test_entity_membership_authority_is_explicit_and_bounded(self):
        d,_=propose(Directory(fixture()),'alice',delegate(),new_id='child')
        with self.assertRaises(PermissionError):propose(d,'bob',membership())
        d,_=propose(d,'alice',delegate(parent='root-entities',kind='group',id='ops',operations=('membership.add',)),new_id='members')
        d,_=propose(d,'bob',membership())
        self.assertEqual(d.effective_groups('charlie','default'),('night','ops'))
        with self.assertRaises(PermissionError):propose(d,'bob',membership('dave','secret'))
        with self.assertRaises(PermissionError):propose(d,'bob',membership('alice'))

    def test_inactive_subjects_scopes_invalid_types_and_unsupported_directory(self):
        d,_=propose(Directory(fixture()),'alice',delegate(),new_id='child')
        for kind in ('users','tenants','organizations'):
            data=d.document();data[kind][0]['enabled']=False
            self.assertEqual(owned(Directory(data),'bob'),[])
        mutations=[lambda d:d.update(version=2),lambda d:d['entity_delegations'][0].update(enabled=1),
            lambda d:d['entity_delegations'][0].update(parent='root-entities'),
            lambda d:d['entity_delegations'][0]['scope'].update(id='unknown'),
            lambda d:d['entity_delegations'][0].update(operations=['engine.execute'])]
        for mutate in mutations:
            data=fixture();mutate(data)
            with self.assertRaises(AccountError):Directory(data)
        data=fixture();data['users'][1]['enabled']=False
        with self.assertRaises(PermissionError):propose(Directory(data),'alice',delegate())


class EntityServiceTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup);self.root=Path(tmp.name)
        self.store=AccountStore(self.root/'accounts.sqlite')
        self.rev=self.store.apply(Directory(fixture()),None)
        self.profile=self.root/'admin.yml'
        self.profile.write_text(yaml.safe_dump(dict(version=1,store='accounts.sqlite',identities=[dict(uid=os.getuid(),subject='alice')])))
        self.profile.chmod(0o600);self.admin=AccountAdmin(self.profile)

    def test_sql_roundtrip_atomic_revision_audit_and_live_search(self):
        plan=self.admin.prepare(change('user','new-user'),self.rev)
        rev=self.admin.commit(plan)
        self.assertEqual(self.store.snapshot()['audit'][-1]['actor'],dict(subject='alice',operation='user.create',organization=None,target='new-user'))
        self.assertEqual(self.admin.search(AccountQuery(q='new-user'))['total'],1)
        self.assertEqual(self.admin.search(AccountQuery(kind='tenant'))['total'],1)
        self.assertEqual(self.admin.search(AccountQuery(organization='default',q='new-user'))['total'],0)
        with self.assertRaises(AccountError):self.admin.commit(plan)
        plan=self.admin.prepare(change('user','another'),rev)
        data=self.store.snapshot()['directory'];data['entity_delegations'][0]['enabled']=False
        self.store.apply(Directory(data),rev)
        with self.assertRaises(AccountError):self.admin.commit(plan)
        with self.assertRaises(PermissionError):self.admin.search(AccountQuery())

    def test_sql_enforces_unsigned_flags_scopes_operations_and_actor(self):
        with sqlite3.connect(self.store.path) as c:
            c.execute('PRAGMA foreign_keys=ON')
            for statement in ("UPDATE account_audit SET actor_subject='bad/id'", "UPDATE entity_delegations SET enabled=-1", "UPDATE entity_delegations SET position=0.5",
                              "UPDATE entity_delegations SET scope_kind='tenant'", "UPDATE entity_delegation_operations SET operation='execute'",
                              "UPDATE account_audit SET actor_subject='alice',actor_operation='membership.add',actor_target='bob'"):
                with self.subTest(statement=statement),self.assertRaises(sqlite3.IntegrityError):c.execute(statement)
            self.assertEqual(c.execute('PRAGMA foreign_key_check').fetchall(),[])

    def test_json_sql_search_have_identical_authorized_projection(self):
        from atraxis.account_storage import storage_config
        config=dict(backend='json',url='json:///'+str(self.root/'explicit.json'))
        other=AccountStore(config);other.apply(Directory(fixture()),None)
        for query in (AccountQuery(),AccountQuery(kind='tenant'),AccountQuery(q='A',limit=2,page=2)):
            sql=self.store.search('alice',query);js=other.search('alice',query)
            for key in ('items','total','has_more'):self.assertEqual(sql[key],js[key])

    def test_cli_explicit_bootstrap_then_review_and_apply(self):
        from atraxis.cli import main
        candidate=self.root/'request.yml';candidate.write_text(yaml.safe_dump(change('tenant','new-tenant')));candidate.chmod(0o600)
        with redirect_stdout(io.StringIO()):
            self.assertEqual(main(['accounts-admin','--config',str(self.profile),'check','--file',str(candidate)]),0)
            self.assertEqual(main(['accounts-admin','--config',str(self.profile),'apply','--file',str(candidate),'--expected-revision',self.rev,'--confirm']),0)
        self.assertIn('new-tenant',Directory(self.store.snapshot()['directory']).tenants)

    def test_new_entity_tables_survive_data_only_backup_and_restore(self):
        from atraxis.account_backup import backup,restore
        # Exercise the same public backup contract used by the administration CLI.
        archive=self.root/'backup.json';destination=self.root/'restored.sqlite'
        backup(self.store.path,archive)
        restore(destination,archive,services_stopped=True)
        self.assertEqual(AccountStore(destination).snapshot(),self.store.snapshot())


    def test_bootstrap_requires_explicit_administrator_option(self):
        from atraxis.cli import main
        for enabled in (False,True):
            path=self.root/('bootstrap-'+str(enabled)+'.db')
            with redirect_stdout(io.StringIO()):
                self.assertEqual(main(['accounts','--store',str(path),'init','--subject','owner']+(['--administrator'] if enabled else [])),0)
            grants=AccountStore(path).snapshot()['directory']['entity_delegations']
            self.assertEqual(bool(grants),enabled)
