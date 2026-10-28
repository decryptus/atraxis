"""Offline operator changes bind reviewed requests, authority and data revisions."""
from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
import io
import json
import os
import unittest
from unittest.mock import patch

import yaml

from atraxis.accounts import Directory
from atraxis.cli import main
from atraxis.config import load_config
from atraxis.configuration_admin import ConfigurationAdmin
from atraxis.inventory_access import load_profile as load_inventory_profile
from atraxis.server import build_context
from test_configuration_admin import ConfigurationFixture
import test_inventory_admin as inventory_fixture


class CommandMixin:
    def command(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main([self.command_name, '--config', str(self.command_path), *args])
        self.assertNotIn('private-source-secret', out.getvalue() + err.getvalue())
        return code, json.loads(out.getvalue()) if out.getvalue() else None, err.getvalue()

    def request_file(self, request):
        path = self.root / 'request.yml'
        path.write_text(yaml.safe_dump(request))
        path.chmod(0o600)
        return str(path)

    def check(self, request):
        path = self.request_file(request)
        code, result, error = self.command('check', '--file', path)
        self.assertEqual((code, error), (0, ''))
        return path, result

    def apply(self, path, revision):
        return self.command('apply', '--file', path, '--expected-revision', revision, '--confirm')


class SourceCommandTests(CommandMixin, ConfigurationFixture):
    def setUp(self):
        super().setUp()
        self.command_name = 'sources-admin'
        self.command_path = self.path
        self.profile['identities'].append(dict(uid=os.getuid(), subject='alice', organization='default'))
        self.write('administration.yml', self.profile)
        del self.document['general']['configuration_web_origin']
        self.save()

    def test_offline_add_update_remove_preserves_settings_and_records_actor(self):
        before = self.path.read_bytes()
        with patch('socket.getaddrinfo', side_effect=AssertionError('No network')):
            path, review = self.check(self.request)
            self.assertEqual(self.path.read_bytes(), before)
            self.assertEqual(review['actor'], dict(subject='alice', organization='default'))
            code, result, error = self.apply(path, review['revision'])
            self.assertEqual((code, error), (0, ''))
            self.assertEqual((result['saved'], result['runtime_changed']), (True, False))
            self.assertEqual(self.apply(path, review['revision'])[0], 2)
            for request in (dict(self.request, operation='source.update', max_age=90),
                            dict(operation='source.remove', source_id='monitor-a')):
                path, review = self.check(request)
                self.assertEqual(self.apply(path, review['revision'])[0], 0)
        saved = yaml.safe_load(self.path.read_text())
        self.assertEqual(saved['general'], self.document['general'])
        self.assertEqual(saved['sources'], [])
        self.assertEqual([event['actor'] for event in saved['configuration_history']], ['alice'] * 3)
        self.assertEqual(saved['configuration_history'][0]['id'], result['event_id'])
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_show_and_check_do_not_expose_storage_or_credential_paths(self):
        _, result, _ = self.command('show')
        _, review = self.check(self.request)
        output = json.dumps([result, review])
        for secret in ('accounts.db', 'source.json', 'credentials_file', 'store', str(self.root)):
            self.assertNotIn(secret, output)
        self.assertEqual(result['sources'], [])
        self.assertNotIn('restart_required', result)  # Offline CLI cannot observe running state.

    def test_profile_without_web_origin_does_not_construct_web_editor(self):
        config = load_config(self.path)
        self.assertIsNone(config.configuration_web_origin)
        with patch('atraxis.web_configuration.WebConfiguration') as web:
            build_context(config)
        web.assert_not_called()
        self.document['general']['configuration_web_origin'] = 'https://atraxis.invalid'
        self.save()
        with patch('atraxis.web_configuration.WebConfiguration') as web:
            build_context(load_config(self.path))
        web.assert_called_once()
        del self.document['general']['configuration_web_file']
        self.save()
        self.assertEqual(self.command('show')[0], 2)

    def test_web_identity_cannot_authorize_local_command_or_environment_override(self):
        self.profile['identities'] = [e for e in self.profile['identities'] if 'uid' not in e]
        self.write('administration.yml', self.profile)
        with patch.dict(os.environ, USER='alice', LOGNAME='alice'):
            self.assertEqual(self.command('show')[0], 3)
        # Explicit web users continue to work independently.
        self.assertEqual(ConfigurationAdmin(self.root/'administration.yml', self.path, 'alice').inspect()['identity']['subject'], 'alice')

    def test_uid_mappings_are_strict_unique_and_separate_from_web_usernames(self):
        original = deepcopy(self.profile)
        for uid in (True, -1, 2**32, '1000', None):
            with self.subTest(uid=uid):
                self.profile = deepcopy(original)
                self.profile['identities'][-1]['uid'] = uid
                self.write('administration.yml', self.profile)
                self.assertEqual(self.command('show')[0], 2)
        for extra in (dict(uid=os.getuid(), subject='bob', organization='default'),
                      dict(uid=123, username='mixed', subject='alice', organization='default')):
            self.profile = deepcopy(original)
            self.profile['identities'].append(extra)
            self.write('administration.yml', self.profile)
            self.assertEqual(self.command('show')[0], 2)

    def test_review_rejects_changed_connection_even_if_daemon_is_unchanged(self):
        path, review = self.check(self.request)
        before = self.path.read_bytes()
        self.profile['connections']['monitor']['origin'] = 'https://replacement.invalid'
        self.write('administration.yml', self.profile)
        self.assertEqual(self.apply(path, review['revision'])[0], 2)
        self.assertEqual(self.path.read_bytes(), before)

    def test_review_rejects_changed_request_directory_and_configuration(self):
        path, review = self.check(self.request)
        self.request_file(dict(self.request, max_age=60))
        self.assertEqual(self.apply(path, review['revision'])[0], 2)
        path, review = self.check(self.request)
        snap = self.store.snapshot()
        self.store.apply(Directory(snap['directory']), snap['revision'])
        self.assertEqual(self.apply(path, review['revision'])[0], 2)
        path, review = self.check(self.request)
        self.document['general']['max_workers'] = 2
        self.save()
        self.assertEqual(self.apply(path, review['revision'])[0], 2)
        self.assertEqual(yaml.safe_load(self.path.read_text())['sources'], [])

    def test_revocation_after_check_denies_apply_without_mutation(self):
        path, review = self.check(self.request)
        self.profile['administrators'] = []
        self.write('administration.yml', self.profile)
        before = self.path.read_bytes()
        self.assertEqual(self.apply(path, review['revision'])[0], 3)
        self.assertEqual(self.path.read_bytes(), before)

    def test_invalid_private_requests_and_revision_do_not_write_or_leak(self):
        path = self.root / 'request.yml'
        before = self.path.read_bytes()
        for raw in ('operation: source.add\noperation: source.remove\n',
                    'operation: &op source.add\nsource_id: *op\n',
                    'x' * 32769, 'password: private-source-secret\n', '[]'):
            path.write_text(raw)
            path.chmod(0o600)
            self.assertEqual(self.command('check', '--file', str(path))[0], 2)
        request_path, review = self.check(self.request)
        self.assertEqual(self.apply(request_path, 'bad-revision')[0], 2)
        path.chmod(0o644)
        self.assertEqual(self.command('check', '--file', str(path))[0], 2)
        path.unlink()
        path.symlink_to(self.path)
        self.assertEqual(self.command('check', '--file', str(path))[0], 2)
        self.assertEqual(self.path.read_bytes(), before)

    def test_uncertain_write_reports_no_success_and_does_not_retry(self):
        path, review = self.check(self.request)
        real_fsync = os.fsync
        calls = []
        def fail_parent_sync(fd):
            calls.append(fd)
            if len(calls) == 2:
                raise OSError('private-source-secret')
            return real_fsync(fd)
        with patch('atraxis.configuration_admin.os.fsync', side_effect=fail_parent_sync):
            code, result, error = self.apply(path, review['revision'])
        self.assertEqual(code, 2)
        self.assertIsNone(result)
        self.assertIn('outcome unconfirmed', error)
        self.assertEqual(len(yaml.safe_load(self.path.read_text())['configuration_history']), 1)
        self.assertEqual(self.apply(path, review['revision'])[0], 2)


class InventoryCommandTests(CommandMixin, unittest.TestCase):
    def setUp(self):
        fixture = inventory_fixture.GrantAdminTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.root, self.profile, self.store = fixture.root, fixture.profile, fixture.store
        self.command_name, self.command_path = 'inventory-admin', self.profile
        data = load_inventory_profile(self.profile)[0]
        data['identities'].append(dict(uid=os.getuid(), subject='alice'))
        self.write(data)

    def write(self, data):
        self.profile.write_text(yaml.safe_dump(data))
        self.profile.chmod(0o600)

    def test_add_and_remove_scoped_grant_without_textual(self):
        before = self.profile.read_bytes()
        path, review = self.check(inventory_fixture.candidate())
        self.assertEqual(self.profile.read_bytes(), before)
        self.assertEqual(self.apply(path, review['revision'])[0], 0)
        code, state, _ = self.command('show')
        self.assertEqual(code, 0)
        self.assertNotIn('second-source', json.dumps(state))
        grant = next(g for org in state['organizations'] for g in org['grants'] if g['subject'] == 'bob')
        path, review = self.check(dict(operation='grant.remove', grant=grant['id']))
        self.assertEqual(self.apply(path, review['revision'])[0], 0)
        self.assertEqual(len(load_inventory_profile(self.profile)[0]['audit']), 2)
        self.assertEqual(self.apply(path, review['revision'])[0], 2)

    def test_account_change_between_check_and_apply_invalidates_review(self):
        path, review = self.check(inventory_fixture.candidate())
        snapshot = self.store.snapshot()
        self.store.apply(Directory(snapshot['directory']), snapshot['revision'])
        self.assertEqual(self.apply(path, review['revision'])[0], 2)
        self.assertEqual(load_inventory_profile(self.profile)[0]['audit'], [])

    def test_revoked_authority_and_out_of_scope_requests_fail_closed(self):
        path = self.request_file(dict(inventory_fixture.candidate(), organization='second'))
        self.assertEqual(self.command('check', '--file', path)[0], 3)
        path, review = self.check(inventory_fixture.candidate())
        data = load_inventory_profile(self.profile)[0]
        data['administrators'] = []
        self.write(data)
        self.assertEqual(self.apply(path, review['revision'])[0], 3)
        self.assertEqual(load_inventory_profile(self.profile)[0]['audit'], [])

    def test_profile_or_request_change_invalidates_review(self):
        path, review = self.check(inventory_fixture.candidate())
        self.request_file(dict(inventory_fixture.candidate(), subject='alice'))
        self.assertEqual(self.apply(path, review['revision'])[0], 2)
        path, review = self.check(inventory_fixture.candidate())
        data = load_inventory_profile(self.profile)[0]
        data['identities'].append(dict(uid=os.getuid()+1, subject='alice'))
        self.write(data)
        self.assertEqual(self.apply(path, review['revision'])[0], 2)
        self.assertEqual(load_inventory_profile(self.profile)[0]['audit'], [])

    def test_review_cannot_be_reused_for_identical_different_profile(self):
        path, review = self.check(inventory_fixture.candidate())
        duplicate = self.root / 'other-access.yml'
        duplicate.write_bytes(self.profile.read_bytes())
        duplicate.chmod(0o600)
        self.command_path = duplicate
        self.assertEqual(self.apply(path, review['revision'])[0], 2)
        self.assertEqual(load_inventory_profile(duplicate)[0]['audit'], [])
