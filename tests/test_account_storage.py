"""Account adapter contracts: persistence, transaction isolation and fail-closed selection."""
from concurrent.futures import ProcessPoolExecutor
from contextlib import contextmanager
import json
import io
from contextlib import redirect_stdout, redirect_stderr
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from atraxis.accounts import AccountError, default_directory
from atraxis.account_sqlite import SQLiteTransaction
from atraxis.account_storage import storage_config
from atraxis.account_store import AccountStore, require_membership


def competing_write(arguments):
    path, revision, name = arguments
    directory = default_directory(name)
    try:
        return AccountStore(path).apply(directory, revision)
    except AccountError:
        return None


class MemoryStorage:
    """Synthetic non-SQL adapter, with the same atomic transaction contract."""
    def __init__(self):
        self.payload = None
        self.lock = threading.Lock()

    def read(self, *, audit=True):
        return self.payload

    def revision(self):
        return self.payload['revision']

    def search(self, subject, query):
        raise NotImplementedError()

    @contextmanager
    def transaction(self, *, initialize=False):
        with self.lock:
            before = self.payload
            try:
                yield self
            except BaseException:
                self.payload = before
                raise

    def write(self, payload):
        self.payload = payload


class AccountStorageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / 'accounts.sqlite3'
        self.store = AccountStore(self.path)

    def test_sqlite_default_persists_across_instances_and_processes(self):
        revision = self.store.apply(default_directory('alice'), None)
        self.assertTrue(self.path.read_bytes().startswith(b'SQLite format 3\x00'))
        self.assertEqual(AccountStore(self.path).snapshot()['revision'], revision)
        with ProcessPoolExecutor(max_workers=3) as pool:
            results = list(pool.map(competing_write, [(str(self.path), revision, 'user'+str(i)) for i in range(3)]))
        self.assertEqual(sum(result is not None for result in results), 1)
        self.assertEqual(len(AccountStore(self.path).snapshot()['audit']), 2)

    def test_relational_constraints_reject_invalid_identifiers_and_relationships(self):
        self.store.apply(default_directory('alice'), None)
        with sqlite3.connect(self.path) as connection:
            connection.execute('PRAGMA foreign_keys=ON')
            for sql,args in (("UPDATE account_audit SET uid=?",(-1,)),
                             ("UPDATE account_audit SET revision=?",('a'*31,)),
                             ("UPDATE account_audit SET sha256=?",('f'*65,)),
                             ("UPDATE users SET enabled=?",(2,)),
                             ("UPDATE account_meta SET id=?",(None,))):
                with self.subTest(sql=sql), self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(sql,args)
                connection.rollback()
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("UPDATE organizations SET tenant='missing'")
                connection.commit()
            connection.rollback()
            tables={r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertIn('users',tables);self.assertNotIn('account_snapshot',tables)

    def test_rollback_after_database_write_preserves_audit_and_directory(self):
        revision = self.store.apply(default_directory('alice'), None)
        before = self.store.snapshot()
        original = SQLiteTransaction.write
        def fail(transaction, payload):
            original(transaction, payload)
            raise sqlite3.OperationalError('synthetic secret must not escape')
        with patch.object(SQLiteTransaction, 'write', fail):
            with self.assertRaisesRegex(AccountError, '^account_store_unavailable$'):
                self.store.apply(default_directory('bob'), revision)
        self.assertEqual(AccountStore(self.path).snapshot(), before)
        self.store.apply(default_directory('bob'), revision)

    def test_authorization_denial_rolls_back_without_replacing_permission_error(self):
        revision = self.store.apply(default_directory('alice'), None)
        before = self.store.snapshot()
        def deny(current, proposed):
            raise PermissionError('synthetic_denial')
        with self.assertRaisesRegex(PermissionError, '^synthetic_denial$'):
            self.store.apply(default_directory('bob'), revision, authorize=deny)
        self.assertEqual(self.store.snapshot(), before)

    def test_read_missing_does_not_initialize_and_bad_schema_is_not_migrated(self):
        with self.assertRaises(AccountError):
            self.store.snapshot()
        self.assertFalse(self.path.exists())
        self.store.apply(default_directory('alice'), None)
        with sqlite3.connect(self.path) as connection:
            connection.execute('PRAGMA user_version=999')
        before = self.path.read_bytes()
        for operation in (self.store.snapshot, lambda: self.store.apply(default_directory('bob'), None)):
            with self.assertRaises(AccountError):
                operation()
        self.assertEqual(self.path.read_bytes(), before)

    def test_existing_json_is_never_implicitly_converted(self):
        config = {'backend': 'json', 'url': str(self.path)}
        old = AccountStore(config)
        revision = old.apply(default_directory('alice'), None)
        before = self.path.read_bytes()
        for operation in (self.store.snapshot, lambda: self.store.apply(default_directory('bob'), None)):
            with self.assertRaises(AccountError):
                operation()
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(old.snapshot()['revision'], revision)
        require_membership(self.root / 'profile.yml', config, 'alice', 'default')
        self.assertEqual(json.loads(before)['membership']['revision'], revision)

    def test_json_adapter_failure_does_not_replace_previous_snapshot(self):
        store = AccountStore({'backend': 'json', 'url': str(self.path)})
        revision = store.apply(default_directory('alice'), None)
        before = store.snapshot()
        with patch('atraxis.account_json.os.replace', side_effect=OSError('synthetic')):
            with self.assertRaises(OSError):
                store.apply(default_directory('bob'), revision)
        self.assertEqual(store.snapshot(), before)
        self.assertEqual(list(self.root.glob('.accounts-*')), [])

    def test_sidecar_symlinks_and_hardlinked_database_rejected(self):
        self.store.apply(default_directory('alice'), None)
        sidecar = Path(str(self.path) + '-journal')
        sidecar.symlink_to(self.root / 'missing')
        with self.assertRaises(AccountError):
            self.store.snapshot()
        sidecar.unlink()
        os.link(self.path, self.root / 'alias')
        with self.assertRaises(AccountError):
            self.store.snapshot()

    def test_writes_read_only_latest_audit_and_preserve_history(self):
        revision = self.store.apply(default_directory('alice'), None)
        for name in ('bob','charlie'):
            revision = self.store.apply(default_directory(name),revision)
        self.assertEqual(len(self.store.snapshot()['audit']),3)
        self.assertEqual(len(self.store.snapshot(audit=False)['audit']),1)
        self.assertEqual(self.store.snapshot()['revision'],revision)

    def test_config_relative_urls_defaults_and_strict_validation(self):
        expected = {'backend': 'sqlite', 'url': 'sqlite:///' + str(self.path), 'options': {}}
        self.assertEqual(storage_config({'url': 'accounts.sqlite3'}, self.root), expected)
        self.assertEqual(storage_config({'backend': 'sqlite', 'url': 'sqlite:///accounts.sqlite3'}, self.root), expected)
        for value in ({}, {'url': ':memory:'}, {'url': 'sqlite:///x?mode=ro'}, {'url': 'file:db'},
                      {'url': 'x', 'options': {'ignored': True}}, {'url': 'x', 'backend': '../module'},
                      {'url': 'x', 'password': 'secret'}, {'url': ''}, {'url': '\n'}):
            with self.subTest(value=value), self.assertRaises(AccountError):
                storage_config(value)

    def test_non_sql_plugin_opaque_url_preserves_policy_and_revision_checks(self):
        memory = MemoryStorage()
        received = []
        class Plugin:
            def load(self):
                def factory(config):
                    received.append(config)
                    return memory
                return factory
        config = {'backend': 'document', 'url': 'opaque:accounts', 'options': {'namespace': 'synthetic'}}
        with patch('atraxis.account_storage.entry_points', return_value=[Plugin()]):
            store = AccountStore(config)
            revision = store.apply(default_directory('alice'), None)
            with self.assertRaises(AccountError):
                store.apply(default_directory('bob'), 'stale')
            self.assertEqual(store.snapshot()['revision'], revision)
            with store._locked():
                self.assertEqual(AccountStore(config).snapshot()['revision'], revision)
        self.assertEqual(received, [config, config])
        self.assertFalse(self.path.exists())

    def test_cli_private_storage_config_initializes_and_resolves_relative_url(self):
        from atraxis.cli import main
        profile = self.root / 'storage.yml'
        profile.write_text('backend: sqlite\nurl: accounts.sqlite3\n')
        profile.chmod(0o600)
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(main(['accounts', '--storage-config', str(profile), 'init', '--subject', 'alice']), 0)
            self.assertEqual(main(['accounts', '--storage-config', str(profile), '--backend', 'json', 'show']), 2)
            profile.write_text('url: x\nurl: y\n')
            self.assertEqual(main(['accounts', '--storage-config', str(profile), 'show']), 2)
        self.assertEqual(self.store.snapshot()['directory']['users'][0]['id'], 'alice')

    def test_plugin_missing_duplicate_or_broken_has_no_fallback(self):
        class Broken:
            def load(self):
                raise RuntimeError('secret URL')
        for matches in ([], [Broken(), Broken()], [Broken()]):
            with patch('atraxis.account_storage.entry_points', return_value=matches):
                with self.assertRaisesRegex(AccountError, '^account_storage_backend_unavailable$'):
                    AccountStore({'backend': 'document', 'url': 'opaque:accounts'})
        self.assertFalse(self.path.exists())

    def test_audit_above_previous_ceiling_preserves_history_and_accepts_new_write(self):
        import uuid
        revision=self.store.apply(default_directory('alice'),None)
        event=self.store.snapshot()['audit'][-1]
        with sqlite3.connect(self.path) as c:
            for _ in range(4096):
                next_revision=uuid.uuid4().hex
                c.execute('INSERT INTO account_audit(revision,previous,uid,time,sha256) VALUES(?,?,?,?,?)',
                          (next_revision,revision,event['uid'],event['time'],event['sha256']))
                revision=next_revision
            c.execute('UPDATE account_meta SET revision=?',(revision,))
        new=self.store.apply(default_directory('bob'),revision)
        snapshot=self.store.snapshot()
        self.assertEqual(snapshot['revision'],new)
        self.assertEqual(len(snapshot['audit']),4098)

    def test_old_blob_schema_is_rejected_without_migration(self):
        with sqlite3.connect(self.path) as c:
            c.execute('CREATE TABLE account_snapshot(id INTEGER PRIMARY KEY,payload BLOB)')
            c.execute('PRAGMA user_version=1')
        self.path.chmod(0o600)
        before=self.path.read_bytes()
        with self.assertRaisesRegex(AccountError,'unsupported_account_store_schema'):self.store.snapshot()
        self.assertEqual(self.path.read_bytes(),before)
