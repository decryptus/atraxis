from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import yaml
from atraxis.accounts import Directory, AccountError, default_directory
from atraxis.account_delegation import owned, propose, view
from atraxis.account_store import AccountStore
from atraxis.account_admin import AccountAdmin


def fixture():
    d=default_directory('alice').document()
    d['users'] += [dict(id=u,name=u,enabled=True) for u in ('bob','charlie','dave')]
    d['memberships'] += [dict(user=u,organization='default',group=None) for u in ('bob','charlie','dave')]
    d['groups']=[dict(id=g,name=g,organization='default',parent=p,inherit_parent=h,enabled=True)
                 for g,p,h in [('ops',None,False),('night','ops',True),('secret',None,False)]]
    d['delegations']=[dict(id='root',subject='alice',organization='default',groups=['ops','night'],
        operations=['membership.add','membership.remove'],organization_membership=True,delegable=True,enabled=True,parent=None)]
    return d


def child(subject='bob',parent='root',**kw):
    return dict(operation='delegation.create',parent=parent,subject=subject,groups=kw.get('groups',['ops','night']),
        operations=kw.get('operations',['membership.add']),organization_membership=kw.get('organization_membership',False),
        delegable=kw.get('delegable',True))


def membership(user='charlie',group='ops',operation='membership.add',organization='default'):
    return dict(operation=operation,subject=user,organization=organization,group=group)


class DelegationTests(unittest.TestCase):
    def test_membership_effects_include_inherited_descendants(self):
        d=Directory(fixture());new,audit=propose(d,'alice',membership())
        self.assertEqual(new.effective_groups('charlie','default'),('night','ops'))
        self.assertEqual(audit['subject'],'alice')
        data=fixture();data['delegations'][0]['groups']=['ops']
        with self.assertRaises(PermissionError):propose(Directory(data),'alice',membership())

    def test_self_outside_scope_and_other_organization_denied(self):
        d=Directory(fixture())
        for request in (membership('alice'),membership(group='secret'),membership(organization='foreign'),membership('unknown')):
            with self.subTest(request=request),self.assertRaises(PermissionError):propose(d,'alice',request)

    def test_child_permissions_are_bounded_and_revocation_cascades(self):
        d,_=propose(Directory(fixture()),'alice',child(),new_id='child')
        d,_=propose(d,'bob',child('charlie','child',groups=['night'],delegable=False),new_id='grandchild')
        self.assertEqual(len(owned(d,'charlie')),1)
        with self.assertRaises(PermissionError):propose(d,'bob',child('dave','child',operations=['membership.remove']))
        with self.assertRaises(PermissionError):propose(d,'bob',child('dave','child',groups=['secret']))
        d,_=propose(d,'alice',dict(operation='delegation.revoke',grant='child'))
        self.assertEqual(owned(d,'bob'),[]);self.assertEqual(owned(d,'charlie'),[])
        with self.assertRaises(PermissionError):propose(d,'bob',membership('dave'))

    def test_delegation_back_to_ancestor_or_self_is_denied(self):
        d,_=propose(Directory(fixture()),'alice',child(),new_id='child')
        for user in ('alice','bob'):
            with self.assertRaises(PermissionError):propose(d,'bob',child(user,'child'))
        with self.assertRaises(PermissionError):propose(d,'bob',dict(operation='delegation.revoke',grant='root'))

    def test_membership_changes_cannot_reactivate_administrators(self):
        d,_=propose(Directory(fixture()),'alice',child(),new_id='child')
        with self.assertRaises(PermissionError):propose(d,'alice',membership('bob'))
        with self.assertRaises(PermissionError):propose(d,'bob',membership('alice'))

    def test_disabled_parent_subject_or_tenant_invalidates_chain(self):
        d,_=propose(Directory(fixture()),'alice',child(),new_id='child')
        for kind in ('users','tenants','organizations'):
            data=d.document();data[kind][0]['enabled']=False
            self.assertEqual(owned(Directory(data),'bob'),[])

    def test_schema_rejects_cycles_scope_expansion_duplicates_and_types(self):
        base=fixture()
        mutations=[lambda d:d['delegations'][0].update(parent='root'),
            lambda d:d['delegations'][0].update(groups=['secret','unknown']),
            lambda d:d['delegations'][0].update(delegable=1),
            lambda d:d['delegations'].append(d['delegations'][0])]
        for mutate in mutations:
            data=deepcopy(base);mutate(data)
            with self.assertRaises(AccountError):Directory(data)

    def test_depth_limit_and_no_permission_union(self):
        data=fixture()
        for i in range(8):
            user='u'+str(i)
            data['users'].append(dict(id=user,name=user,enabled=True))
            data['memberships'].append(dict(user=user,organization='default',group=None))
            data['delegations'].append(dict(data['delegations'][0],id='g'+str(i),subject=user,parent='root' if i==0 else 'g'+str(i-1)))
        with self.assertRaises(AccountError):Directory(data)
        data=fixture();data['delegations'][0]['groups']=['ops']
        data['delegations'].append(dict(data['delegations'][0],id='separate',groups=['night']))
        with self.assertRaises(PermissionError):propose(Directory(data),'alice',membership())

    def test_view_hides_other_groups_and_unowned_grants(self):
        d=Directory(fixture());v=view(d,'alice')
        self.assertEqual([g['id'] for g in v[0]['groups']],['night','ops'])
        self.assertEqual(view(d,'bob'),[])
        self.assertNotIn('secret',json.dumps(v))


class AdminTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        self.root=Path(tmp.name);self.store=AccountStore(self.root/'directory.json')
        self.rev=self.store.apply(Directory(fixture()),None)
        self.profile=self.root/'admin.yml'
        self.profile.write_text(yaml.safe_dump(dict(version=1,store='directory.json',identities=[
            dict(uid=os.getuid(),subject='alice'),dict(username='alice-login',subject='alice'),dict(username='bob-login',subject='bob')])))
        self.profile.chmod(0o600);self.admin=AccountAdmin(self.profile)

    def test_cli_uses_os_identity_review_and_confirmation(self):
        import io
        from contextlib import redirect_stdout,redirect_stderr
        from atraxis.cli import main
        request=self.root/'request.yml';request.write_text(yaml.safe_dump(membership()));request.chmod(0o600)
        def call(*args):
            output=io.StringIO()
            with redirect_stdout(output),redirect_stderr(io.StringIO()):
                code=main(['accounts-admin','--config',str(self.profile),*args])
            return code,output.getvalue()
        code,output=call('check','--file',str(request));self.assertEqual(code,0)
        self.assertEqual(json.loads(output)['actor']['subject'],'alice')
        self.assertEqual(self.store.snapshot()['revision'],self.rev)
        self.assertEqual(call('apply','--file',str(request),'--expected-revision',self.rev,'--confirm')[0],0)
        self.assertEqual(call('apply','--file',str(request),'--expected-revision',self.rev,'--confirm')[0],2)

    def test_review_commit_recheck_and_actor_audit(self):
        plan=self.admin.prepare(membership(),self.rev)
        self.assertEqual(self.store.snapshot()['revision'],self.rev)
        new=self.admin.commit(plan)
        self.assertEqual(self.store.snapshot()['revision'],new)
        self.assertEqual(self.store.snapshot()['audit'][-1]['actor']['subject'],'alice')
        with self.assertRaises(AccountError):self.admin.commit(plan)

    def test_revoked_delegation_after_review_cannot_commit(self):
        plan=self.admin.prepare(membership(),self.rev)
        d=fixture();d['delegations'][0]['enabled']=False
        self.store.apply(Directory(d),self.rev)
        with self.assertRaises(AccountError):self.admin.commit(plan)
        with self.assertRaises(PermissionError):self.admin.prepare(membership(),self.store.snapshot()['revision'])

    def test_profile_switch_after_review_denies(self):
        plan=self.admin.prepare(membership(),self.rev)
        data=yaml.safe_load(self.profile.read_text());data['identities'][0]['subject']='bob'
        self.profile.write_text(yaml.safe_dump(data))
        with self.assertRaises(PermissionError):self.admin.commit(plan)

    def test_child_commit_preserves_reviewed_id_and_denies_outside_subscope(self):
        plan=self.admin.prepare(child(groups=['night']),self.rev)
        rev=self.admin.commit(plan);self.assertIn(plan['created_id'],Directory(self.store.snapshot()['directory']).delegations)
        bob=AccountAdmin(self.profile,'bob-login')
        self.assertEqual(len(bob.inspect()['organizations']),1)
        with self.assertRaises(PermissionError):bob.prepare(membership('dave'),rev)
        bob.commit(bob.prepare(membership('dave','night'),rev))

    def test_authority_rechecked_inside_writer_lock(self):
        plan=self.admin.prepare(membership(),self.rev)
        original=AccountStore.apply
        def revoke(store,candidate,revision,**kwargs):
            d=Directory(store.snapshot()['directory']).document();d['delegations'][0]['enabled']=False
            original(store,Directory(d),revision)
            return original(store,candidate,revision,**kwargs)
        with patch.object(AccountStore,'apply',revoke),self.assertRaises(AccountError):self.admin.commit(plan)
        self.assertFalse(Directory(self.store.snapshot()['directory']).effective_groups('charlie','default'))
