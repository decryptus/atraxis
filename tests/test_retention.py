from contextlib import redirect_stdout, redirect_stderr
from copy import deepcopy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import yaml

from atraxis.account_store import AccountStore
from atraxis.accounts import default_directory
from atraxis.account_backup import backup
from atraxis.retention import inspect_file
from atraxis.cli import main


class RetentionTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup);self.root=Path(temp.name)
        self.config=self.root/'daemon.yml'
        self.document=dict(general=dict(auth_basic_file='not-provisioned-users'),sources=[])
        self.write()
    def write(self):
        self.config.write_text(yaml.safe_dump(self.document));self.config.chmod(0o600)
    def history(self,count):
        return [dict(id=f'{i:032x}',actor='private-actor',organization='private-org',operation='source.add',
            source_id='private-source',previous='a'*64,time='2026-10-11T00:00:00+00:00') for i in range(count)]

    def test_config_warning_at_eighty_percent_and_full_history_are_read_only(self):
        for count in (0,79,80,100):
            self.document['configuration_history']=self.history(count);self.write();before=self.config.read_bytes()
            with patch('socket.getaddrinfo',side_effect=AssertionError('no network')):
                report=inspect_file(self.config)
            self.assertEqual(report['attention_required'],count>=80)
            self.assertEqual(report['capacity']['configuration_events']['remaining'],100-count)
            self.assertFalse(report['runtime_validated']);self.assertFalse(report['purge_supported'])
            self.assertNotIn('private-',json.dumps(report));self.assertEqual(self.config.read_bytes(),before)
            self.assertFalse((self.root/'not-provisioned-users').exists())

    def test_account_archive_integrity_counts_and_no_live_database_access(self):
        path=self.root/'accounts.db';archive=self.root/'accounts.json'
        store=AccountStore(path);store.apply(default_directory('private-account'),None);backup(path,archive)
        before=archive.read_bytes()
        with patch('atraxis.account_backup.sqlite_adapter',side_effect=AssertionError('no database')):
            report=inspect_file(archive,accounts=True)
        self.assertEqual(report['capacity']['audit_entries']['used'],1)
        self.assertEqual(report['capacity']['file_bytes']['used'],len(before))
        self.assertNotIn('private-account',json.dumps(report));self.assertEqual(archive.read_bytes(),before)
        outer=json.loads(before);outer['payload']['snapshot']['revision']='a'*32;archive.write_text(json.dumps(outer))
        with self.assertRaises(ValueError):inspect_file(archive,accounts=True)

    def test_malformed_history_duplicate_keys_and_insecure_paths_are_refused(self):
        self.document['configuration_history']=self.history(101);self.write()
        with self.assertRaises(ValueError):inspect_file(self.config)
        self.document['configuration_history']=[{}];self.write()
        with self.assertRaises(ValueError):inspect_file(self.config)
        self.document.pop('configuration_history');self.write();self.config.chmod(0o644)
        with self.assertRaises(ValueError):inspect_file(self.config)
        self.config.chmod(0o600);link=self.root/'link.yml';link.symlink_to(self.config)
        with self.assertRaises((ValueError,OSError)):inspect_file(link)
        self.config.write_text('general: {}\ngeneral: {}\n')
        with self.assertRaises(ValueError):inspect_file(self.config)

    def test_cli_reports_warning_and_errors_without_echoing_payloads(self):
        self.document['configuration_history']=self.history(80);self.write()
        out=io.StringIO()
        with redirect_stdout(out):self.assertEqual(main(['retention','--config',str(self.config)]),1)
        self.assertTrue(json.loads(out.getvalue())['attention_required'])
        self.document['configuration_history']=[];self.write()
        with redirect_stdout(io.StringIO()):self.assertEqual(main(['retention','--config',str(self.config)]),0)
        out=io.StringIO();error=io.StringIO()
        with redirect_stdout(out),redirect_stderr(error):self.assertEqual(main(['retention','--config',str(self.root/'missing')]),2)
        self.assertEqual(out.getvalue(),'');self.assertNotIn(str(self.root),error.getvalue())
