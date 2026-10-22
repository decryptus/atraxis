from contextlib import redirect_stdout, redirect_stderr
from copy import deepcopy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from atraxis.account_backup import backup, restore, read_backup, canonical
from atraxis.account_store import AccountStore
from atraxis.account_search import AccountQuery
from atraxis.accounts import AccountError, Directory
from atraxis.cli import main
from test_account_delegation import fixture


class AccountBackupTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.path = self.root / 'source.db'; self.target = self.root / 'restored.db'
        self.archive = self.root / 'accounts.json'
        self.store = AccountStore(self.path)
        self.revision = self.store.apply(Directory(fixture()), None)

    def test_restore_preserves_audit_revision_search_and_current_membership(self):
        document = fixture(); document['users'][0]['name'] = 'Changed'
        self.store.apply(Directory(document), self.revision)
        before = self.store.snapshot()
        query = AccountQuery(kind='group', q='ops')
        search = self.store.search('alice', query)
        backup(self.path, self.archive)
        self.assertEqual(self.archive.stat().st_mode & 0o777, 0o600)
        restore(self.target, self.archive, services_stopped=True)
        restored = AccountStore(self.target)
        self.assertEqual(restored.snapshot(), before)
        self.assertEqual(restored.search('alice', query), search)
        document['users'][0]['enabled'] = False
        restored.apply(Directory(document), before['revision'])
        self.assertEqual(len(restored.snapshot()['audit']), 3)
        self.assertEqual(self.store.snapshot(), before)
        self.assertEqual(self.target.stat().st_mode & 0o777, 0o600)

    def test_no_confirmation_existing_file_or_sidecar_never_replaced(self):
        backup(self.path, self.archive)
        with self.assertRaises(AccountError): restore(self.target, self.archive)
        self.assertFalse(self.target.exists())
        sidecar = Path(str(self.target) + '-wal'); sidecar.write_bytes(b'keep')
        with self.assertRaises(AccountError): restore(self.target, self.archive, services_stopped=True)
        self.assertEqual(sidecar.read_bytes(), b'keep'); sidecar.unlink()
        self.target.write_bytes(b'keep')
        with self.assertRaises(AccountError): restore(self.target, self.archive, services_stopped=True)
        self.assertEqual(self.target.read_bytes(), b'keep')

    def test_tampered_digest_and_inconsistent_projection_cannot_publish(self):
        import hashlib
        backup(self.path, self.archive)
        outer = json.loads(self.archive.read_text())
        outer['payload']['snapshot']['membership']['members'] = []
        self.archive.write_text(json.dumps(outer))
        with self.assertRaises(AccountError): read_backup(self.archive)
        outer['sha256'] = hashlib.sha256(canonical(outer['payload'])).hexdigest()
        self.archive.write_text(json.dumps(outer))
        with self.assertRaises(AccountError): restore(self.target, self.archive, services_stopped=True)
        self.assertFalse(self.target.exists())

    def test_failed_atomic_publication_cleans_temporary_database(self):
        backup(self.path, self.archive)
        with patch('atraxis.account_backup.os.link', side_effect=OSError('publication failed')):
            with self.assertRaises(OSError): restore(self.target, self.archive, services_stopped=True)
        self.assertFalse(self.target.exists())
        self.assertEqual(list(self.root.glob('.account-restore-*')), [])
        restore(self.target, self.archive, services_stopped=True)
        self.assertEqual(AccountStore(self.target).snapshot(), self.store.snapshot())

    def test_cli_backup_restore_and_overwrite_refusal(self):
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(main(['accounts', '--store', str(self.path), 'backup', '--output', str(self.archive)]), 0)
            self.assertEqual(main(['accounts', '--store', str(self.path), 'backup', '--output', str(self.archive)]), 2)
            self.assertEqual(main(['accounts', '--store', str(self.target), 'restore', '--input', str(self.archive), '--confirm-services-stopped']), 0)
        self.assertEqual(AccountStore(self.target).snapshot(), self.store.snapshot())

    def test_limit_and_unsupported_adapter_do_not_create_an_archive(self):
        with patch('atraxis.account_backup.MAX_AUDIT_ROWS', 0):
            with self.assertRaises(AccountError): backup(self.path, self.archive)
        self.assertFalse(self.archive.exists())
        with self.assertRaises(AccountError): backup({'backend':'json', 'url':str(self.path)}, self.archive)
