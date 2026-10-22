from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import tempfile
import sqlite3
import unittest
from unittest.mock import patch
import yaml
from atraxis.accounts import AccountError, Directory, default_directory
from atraxis.account_store import AccountStore, load_candidate, require_membership
from atraxis.cli import main
from atraxis.web_rules import RuleReader


def fixture():
    d=default_directory('alice').document()
    d['tenants'].append(dict(id='other',name='Other',enabled=True))
    d['organizations'] += [dict(id='sub',name='Sub',tenant='default',enabled=True),
                          dict(id='foreign',name='Foreign',tenant='other',enabled=True)]
    d['users'].append(dict(id='bob',name='Bob',enabled=True))
    d['groups']=[dict(id=i,name=i,organization='default',parent=p,inherit_parent=h,enabled=True)
                 for i,p,h in [('ops',None,False),('child','ops',True),('isolated','ops',False)]]
    d['memberships'] += [dict(user=u,organization=o,group=g) for u,o,g in
        [('alice','sub',None),('alice','default','ops'),('bob','default',None),('bob','default','child')]]
    return d


class DirectoryTests(unittest.TestCase):
    def test_one_subject_multiple_organizations_without_implicit_tenant_access(self):
        d=Directory(fixture())
        self.assertEqual(d.contexts('alice'),('default','sub'))
        self.assertEqual(d.contexts('unknown'),())
        self.assertFalse(d.allows_membership('alice','foreign'))
        data=fixture();data['memberships'].pop(0)
        with self.assertRaises(AccountError):Directory(data)

    def test_inheritance_opt_in_downward_not_upward(self):
        d=Directory(fixture())
        self.assertEqual(d.effective_groups('alice','default'),('child','ops'))
        self.assertEqual(d.effective_groups('bob','default'),('child',))
        self.assertEqual(d.effective_groups('alice','foreign'),())

    def test_disabled_entities_fail_closed(self):
        for kind in ('tenants','organizations','users'):
            data=fixture();data[kind][0]['enabled']=False
            self.assertFalse(Directory(data).allows_membership('alice','default'))
        data=fixture();data['groups'][0]['enabled']=False
        self.assertEqual(Directory(data).effective_groups('bob','default'),())

    def test_cycles_depth_cross_org_and_missing_parents_rejected(self):
        for change in ('cycle','self','foreign','missing','depth'):
            data=fixture()
            if change=='cycle':data['groups'][0]['parent']='child'
            if change=='self':data['groups'][0]['parent']='ops'
            if change=='foreign':data['groups'][1]['organization']='foreign'
            if change=='missing':data['groups'][0]['parent']='missing'
            if change=='depth':
                for i in range(16):data['groups'].append(dict(id='g'+str(i),name='Group',organization='default',
                    enabled=True,parent='ops' if i==0 else 'g'+str(i-1),inherit_parent=False))
            with self.subTest(change=change),self.assertRaises(AccountError):Directory(data)

    def test_exact_schema_types_references_and_duplicates(self):
        changes=[lambda d:d.update(version=True),lambda d:d.update(grants=[]),
            lambda d:d['users'].append(d['users'][0]),lambda d:d['users'][0].update(enabled=1),
            lambda d:d['users'][0].update(id='../alice'),lambda d:d['users'][0].update(name='bad\nname'),
            lambda d:d['organizations'][0].update(tenant='missing'),
            lambda d:d['memberships'].append(d['memberships'][0]),
            lambda d:d['memberships'][0].update(user='missing'),
            lambda d:d['memberships'][0].update(organization='foreign',group='ops'),
            lambda d:d['groups'][0].update(inherit_parent=True)]
        for change in changes:
            data=fixture();change(data)
            with self.subTest(change=change),self.assertRaises(AccountError):Directory(data)

    def test_input_and_export_copied(self):
        data=fixture();d=Directory(data);data['users'][0]['enabled']=False
        exported=d.document();exported['users'][0]['enabled']=False
        self.assertTrue(d.allows_membership('alice','default'))


class StoreTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        self.root=Path(tmp.name);self.path=self.root/'accounts.json';self.store=AccountStore(self.path)
        self.revision=self.store.apply(Directory(fixture()),None)

    def tamper(self,data):
        with sqlite3.connect(self.path) as connection:
            if data['version']!=3:
                connection.execute('PRAGMA user_version=999')
            elif data['membership']!=self.store.snapshot()['membership']:
                connection.execute('DELETE FROM membership_groups')
                connection.execute('DELETE FROM membership_members')
            else:
                connection.execute('UPDATE users SET enabled=? WHERE id=?',(data['directory']['users'][0]['enabled'],data['directory']['users'][0]['id']))

    def invoke(self,*args):
        out,err=io.StringIO(),io.StringIO()
        with redirect_stdout(out),redirect_stderr(err):
            code=main(['accounts','--store',str(self.path),*args])
        return code,out.getvalue(),err.getvalue()

    def test_projection_and_directory_share_one_atomic_revision(self):
        snapshot=self.store.snapshot()
        self.assertEqual(snapshot['membership']['revision'],self.revision)
        member=next(m for m in snapshot['membership']['members'] if m['subject']=='alice' and m['organization']=='default')
        self.assertEqual(member['groups'],['child','ops'])
        data=fixture();data['groups'][0]['enabled']=False
        revision=self.store.apply(Directory(data),self.revision)
        snapshot=self.store.snapshot()
        self.assertEqual(snapshot['membership']['revision'],revision)
        self.assertTrue(all(not m['groups'] for m in snapshot['membership']['members']))
        data['tenants'][0]['enabled']=False
        self.store.apply(Directory(data),revision)
        self.assertEqual(self.store.snapshot()['membership']['members'],[])

    def test_tampered_projection_and_unsupported_store_version_rejected(self):
        original=self.store.snapshot()
        for kind in ('projection','version'):
            data=json.loads(json.dumps(original))
            if kind=='projection':data['membership']['members']=[]
            else:data['version']=1
            self.tamper(data)
            with self.assertRaises(AccountError):self.store.snapshot()

    def test_atomic_revision_and_audit(self):
        data=fixture();data['users'][0]['enabled']=False
        revision=self.store.apply(Directory(data),self.revision)
        self.assertNotEqual(revision,self.revision)
        saved=self.store.snapshot();self.assertEqual(len(saved['audit']),2)
        self.assertEqual(saved['audit'][-1]['previous'],self.revision)
        self.assertEqual(saved['audit'][-1]['uid'],os.getuid())
        self.assertEqual(self.path.stat().st_mode&0o777,0o600)
        for revision in (None,self.revision):
            with self.assertRaises(AccountError):self.store.apply(Directory(fixture()),revision)
        self.assertEqual(self.store.snapshot(),saved)

    def test_concurrent_admins_only_one_wins(self):
        def update(i):
            data=fixture();data['users'][0]['name']='Alice '+str(i)
            try:return self.store.apply(Directory(data),self.revision)
            except AccountError:return None
        with ThreadPoolExecutor(max_workers=8) as executor:results=list(executor.map(update,range(8)))
        self.assertEqual(sum(r is not None for r in results),1)
        self.assertEqual(len(self.store.snapshot()['audit']),2)

    def test_private_files_symlinks_parent(self):
        self.path.chmod(0o644)
        with self.assertRaises(AccountError):self.store.snapshot()
        self.path.chmod(0o600)
        link=self.root/'link';link.symlink_to(self.path)
        with self.assertRaises(AccountError):AccountStore(link).snapshot()
        self.root.chmod(0o755)
        with self.assertRaises(AccountError):self.store.apply(Directory(fixture()),self.revision)
        self.root.chmod(0o700)

    def test_failure_before_commit_preserves_previous(self):
        before=self.path.read_bytes()
        with patch('atraxis.account_sqlite.SQLiteTransaction.write',side_effect=OSError('synthetic')):
            with self.assertRaises(OSError):self.store.apply(Directory(fixture()),self.revision)
        self.assertEqual(self.path.read_bytes(),before)
        self.assertEqual(list(self.root.glob('.accounts-*')),[])

    def test_modified_snapshot_rejected(self):
        data=self.store.snapshot();data['directory']['users'][0]['enabled']=False
        self.tamper(data)
        with self.assertRaises(AccountError):self.store.snapshot()

    def test_cli_preview_export_apply_conflict_contexts_audit(self):
        code,out,err=self.invoke('export');self.assertEqual((code,err),(0,''))
        data=json.loads(out);data['users'][0]['name']='Updated'
        candidate=self.root/'candidate.yml';candidate.write_text(yaml.safe_dump(data));candidate.chmod(0o600)
        code,out,_=self.invoke('check','--file',str(candidate));self.assertEqual(code,0)
        preview=json.loads(out);self.assertEqual(preview['revision'],self.revision)
        self.assertEqual(preview['changes']['users']['added'][0]['name'],'Updated')
        self.assertEqual(self.store.snapshot()['revision'],self.revision)
        args=('apply','--file',str(candidate),'--expected-revision',self.revision,'--confirm')
        self.assertEqual(self.invoke(*args)[0],0);self.assertEqual(self.invoke(*args)[0],2)
        self.assertEqual(self.invoke('contexts','--subject','alice')[0],0)
        self.assertEqual(len(json.loads(self.invoke('audit')[1])),2)

    def test_init_never_overwrites_and_confirmation_required(self):
        self.assertEqual(self.invoke('init','--subject','alice')[0],2)
        with redirect_stderr(io.StringIO()),self.assertRaises(SystemExit):
            self.invoke('apply','--file','x','--expected-revision',self.revision)
        self.path.unlink();self.assertEqual(self.invoke('init','--subject','alice')[0],0)
        self.assertEqual(Directory(self.store.snapshot()['directory']).contexts('alice'),('default',))

    def test_yaml_duplicate_fields_rejected(self):
        path=self.root/'bad.yml';path.write_text('version: 1\nversion: 1\n');path.chmod(0o600)
        with self.assertRaises(AccountError):load_candidate(path)

    def test_membership_reload_fail_closed(self):
        require_membership(self.root/'profile.yml','accounts.json','alice','default')
        with self.assertRaises(PermissionError):require_membership(self.root/'profile.yml','accounts.json','alice','foreign')
        data=fixture();data['users'][0]['enabled']=False;self.store.apply(Directory(data),self.revision)
        with self.assertRaises(PermissionError):require_membership(self.root/'profile.yml','accounts.json','alice','default')
        self.path.unlink()
        with self.assertRaises(AccountError):require_membership(self.root/'profile.yml','accounts.json','alice','default')

    def test_browser_rechecks_membership_after_request(self):
        mapping=self.root/'web.yml'
        mapping.write_text(yaml.safe_dump(dict(version=1,accounts_file='accounts.json',identities=[
            dict(username='login',subject='alice',organization='default',atropos_client='client.yml')])))
        mapping.chmod(0o600);owner=self
        class Client:
            def history(self,*args):
                data=fixture();data['users'][0]['enabled']=False;owner.store.apply(Directory(data),owner.revision)
                return ['sensitive result']
            def close(self):owner.closed=True
        reader=RuleReader(mapping,'login',lambda *args,**kwargs:Client());self.closed=False
        with self.assertRaises(PermissionError):reader.call('history','cpu',0,20)
        self.assertTrue(self.closed)
        with self.assertRaises(PermissionError):reader.entry()
        with self.assertRaises(PermissionError):RuleReader(mapping,'login')

