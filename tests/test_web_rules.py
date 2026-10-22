import base64
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
import hashlib
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest

import yaml

from atraxis.config import ConfigurationError, load_config
from atraxis.server import _Handler, build_context
from atraxis.web_rules import RuleReader, handle, load_mapping, query

REVISION = 'a' * 32
GENERATION = 'b' * 32


class FakeClient:
    """Network boundary double; records actor and allows explicit refusal."""
    def __init__(self, owner, path, expected_actor):
        self.owner, self.actor = owner, expected_actor
        owner.destinations.append((str(path), expected_actor))

    def history(self, name, offset, limit):
        self.owner.calls.append(('history', self.actor, name, offset, limit))
        if self.owner.error:
            raise self.owner.error
        if self.owner.revoke:
            self.owner.write_mapping([])
        return [dict(revision=REVISION, author=self.actor['subject'], message='Synthetic revision')]

    def active(self, name):
        self.owner.calls.append(('active', self.actor, name))
        return dict(revision=REVISION, generation=GENERATION)

    def read(self, name, revision):
        self.owner.calls.append(('read', self.actor, name, revision))
        return dict(revision=revision, document=dict(version=1, name=name,
            description=self.owner.document_message, inputs={'cpu_percent':'number'},
            rules=[dict(id='high_cpu', when={'field':'cpu_percent','gt':90}, then='review')],
            default='ignore'))

    def close(self):
        self.owner.closed += 1


class WebRulesTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.mapping = self.root / 'rules-web.yml'
        self.identities = [dict(username=user, subject=user+'-subject', organization=org,
                          atropos_client=user+'.yml') for user,org in [('alice','one'),('bob','two')]]
        self.write_mapping(self.identities)
        digest = base64.b64encode(hashlib.sha1(b'fixture-password').digest()).decode()
        (self.root/'users').write_text(''.join(user+':{SHA}'+digest+'\n' for user in ('alice','bob','unmapped')))
        self.config_path = self.root/'config.yml'
        self.config_path.write_text('general:\n  auth_basic_file: users\n  rules_web_file: rules-web.yml\n')
        self.calls, self.destinations = [], []
        self.closed = 0
        self.error = None
        self.revoke = False
        self.document_message = 'Synthetic content'
        self.factory = lambda path, expected_actor: FakeClient(self, path, expected_actor)
        config = replace(load_config(self.config_path), listen_port=0)
        self.context = build_context(config, rule_client_factory=self.factory)
        started = threading.Event()
        self.context.named_commands['GET /healthz'].at_start = lambda options: started.set()
        self.thread = threading.Thread(target=lambda:self.context.run(http_req_handler=_Handler), daemon=True)
        self.thread.start(); self.addCleanup(self.stop)
        self.assertTrue(started.wait(5))
        self.port = self.context.server.server_address[1]

    def stop(self):
        self.context.stop(); self.thread.join(7)
        self.assertFalse(self.thread.is_alive())

    def write_mapping(self, identities):
        self.mapping.write_text(yaml.safe_dump(dict(version=1, identities=identities)))
        self.mapping.chmod(0o600)

    def request(self, path='/ui/rules', username='alice', method='GET', extra=None):
        headers = dict(extra or {})
        if username is not None:
            headers['Authorization'] = 'Basic ' + base64.b64encode((username+':fixture-password').encode()).decode()
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=5)
        try:
            connection.request(method, path, headers=headers)
            response = connection.getresponse()
            return response.status, {k.lower():v for k,v in response.getheaders()}, response.read().decode()
        finally:
            connection.close()

    def test_account_directory_revocation_is_enforced_over_http(self):
        from atraxis.accounts import Directory, default_directory
        from atraxis.account_store import AccountStore
        data = default_directory('alice-subject').document()
        data['organizations'][0]['id'] = 'one'
        data['memberships'][0]['organization'] = 'one'
        store = AccountStore(self.root/'accounts.json')
        revision = store.apply(Directory(data), None)
        self.mapping.write_text(yaml.safe_dump(dict(version=1, accounts_file='accounts.json',
                                                   identities=self.identities)))
        self.assertEqual(self.request('/ui/rules?name=cpu')[0], 200)
        self.calls.clear()
        data['users'][0]['enabled'] = False
        store.apply(Directory(data), revision)
        self.assertEqual(self.request('/ui/rules?name=cpu')[0], 403)
        self.assertEqual(self.calls, [])
        (self.root/'accounts.json').unlink()
        self.assertEqual(self.request('/ui/rules?name=cpu')[0], 503)
        self.assertEqual(self.calls, [])

    def test_entry_screen_and_navigation_need_authentication_and_mapping(self):
        self.assertEqual(self.request(username=None)[0], 401)
        self.assertEqual(self.request(username='unmapped')[0], 403)
        status, _, page = self.request()
        self.assertEqual(status, 200)
        self.assertIn('Enter a rule name', page)
        self.assertIn('alice-subject', page)
        self.assertIn('/ui/rules', self.request('/ui')[2])
        self.assertEqual(self.calls, [])

    def test_actual_http_requests_select_only_server_mapped_actors(self):
        for user,org in [('alice','one'),('bob','two')]:
            status, headers, page = self.request('/ui/rules?name=cpu&revision='+REVISION, username=user,
                                                extra={'X-User':'administrator'})
            self.assertEqual(status, 200)
            self.assertIn(user+'-subject', page)
            self.assertIn('Active at page load', page)
            self.assertIn('field: cpu_percent', page)
            self.assertEqual(self.calls[-1][1], dict(subject=user+'-subject', organization=org))
            self.assertEqual(self.destinations[-1][0], str(self.root/(user+'.yml')))
            self.assertEqual(headers['cache-control'], 'no-store')
            self.assertIn("form-action 'self'", headers['content-security-policy'])
        self.assertEqual(self.closed, 6)

    def test_parallel_accounts_keep_distinct_identities(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            results=list(pool.map(lambda user:(user,self.request('/ui/rules?name=cpu',username=user)), ['alice','bob']*5))
        for user,(status,_,page) in results:
            self.assertEqual(status,200)
            self.assertIn('Account: '+user+'-subject',page)
            other='bob' if user=='alice' else 'alice'
            self.assertNotIn(other+'-subject',page)

    def test_identity_arguments_duplicates_and_invalid_values_are_rejected(self):
        queries=['name=cpu&subject=admin',  'name=cpu&name=other',
                 'name=cpu&offset=-1', 'name=cpu&offset=1000001','name=cpu&revision=bad',
                 'name=../../file','revision='+REVISION,'name=cpu&offset=true','name='+('a'*1024)]
        for suffix in queries:
            with self.subTest(query=suffix):
                self.assertEqual(self.request('/ui/rules?'+suffix)[0],400)
        self.assertEqual(self.calls,[])

    def test_revocation_before_and_during_read_discards_data(self):
        self.write_mapping([self.identities[1]])
        self.assertEqual(self.request('/ui/rules?name=cpu')[0],403)
        self.assertEqual(self.calls,[])
        self.write_mapping(self.identities);self.revoke=True
        status,_,page=self.request('/ui/rules?name=cpu')
        self.assertEqual(status,403)
        self.assertNotIn('Synthetic revision',page)
        self.assertEqual(len(self.calls),1)

    def test_identity_change_invalidates_an_existing_reader(self):
        reader=RuleReader(self.mapping,'alice',self.factory)
        entries=deepcopy(self.identities);entries[0]['organization']='two'
        self.write_mapping(entries)
        with self.assertRaises(PermissionError):reader.call('history','cpu',0,20)
        self.assertEqual(self.calls,[])

    def test_engine_refusals_and_failures_never_echo_details_or_partial_rows(self):
        for error,status in [(PermissionError('secret-marker'),403),
                             (ValueError('rule_not_found'),404),
                             (RuntimeError('private-token secret-marker'),503)]:
            self.error=error
            with self.subTest(status=status):
                code,_,page=self.request('/ui/rules?name=cpu')
                self.assertEqual(code,status)
                self.assertNotIn('secret-marker',page)
                self.assertNotIn('Synthetic revision',page)

    def test_content_is_escaped_and_head_post_do_not_mutate(self):
        self.document_message='</pre><script>secret-marker</script>'
        path='/ui/rules?name=cpu&revision='+REVISION
        status,_,page=self.request(path)
        self.assertEqual(status,200)
        self.assertIn('&lt;script&gt;',page)
        self.assertNotIn('<script>',page)
        self.assertEqual(self.request(path,method='HEAD')[2],'')
        count=len(self.calls)
        self.assertEqual(self.request(path,method='POST')[0],405)
        self.assertEqual(len(self.calls),count)
        reader=RuleReader(self.mapping,'alice',self.factory)
        with self.assertRaises(PermissionError):reader.call('activate','cpu')

    def test_mapping_is_private_bounded_strict_and_empty_revokes_all(self):
        self.mapping.chmod(0o644)
        with self.assertRaises(ConfigurationError):load_mapping(self.mapping)
        self.mapping.chmod(0o600)
        invalid=[dict(version=True,identities=[]),dict(version=1,identities=self.identities*2),
                 dict(version=1,identities=[dict(self.identities[0],subject='../root')]),
                 dict(version=1,identities=[dict(self.identities[0],grants=['all'])])]
        for document in invalid:
            self.mapping.write_text(yaml.safe_dump(document))
            with self.assertRaises(ConfigurationError):load_mapping(self.mapping)
        self.mapping.write_text('version: 1\nversion: 1\nidentities: []')
        with self.assertRaises(ConfigurationError):load_mapping(self.mapping)
        self.write_mapping([])
        self.assertEqual(load_mapping(self.mapping),[])
        self.assertEqual(self.request()[0],403)

    def test_configuration_resolves_mapping_relative_to_daemon(self):
        self.assertEqual(load_config(self.config_path).rules_web_file,str(self.mapping))
        self.mapping.unlink()
        with self.assertRaises(ConfigurationError):load_config(self.config_path)

    def test_empty_filtered_history_still_allows_next_window(self):
        class EmptyClient(FakeClient):
            def history(self,*args):return []
        status,page=handle('/ui/rules?name=cpu','alice',self.mapping,
                           factory=lambda path,expected_actor:EmptyClient(self,path,expected_actor))
        self.assertEqual(status,200)
        self.assertIn('Next window',page)
        self.assertIn('Later windows may still contain accessible revisions',page)

    def test_mismatched_revision_response_is_discarded(self):
        class WrongClient(FakeClient):
            def read(self,*args):
                return dict(revision='c'*32,document=dict(name='other',secret='private-marker'))
        status,page=handle('/ui/rules?name=cpu&revision='+REVISION,'alice',self.mapping,
                           factory=lambda path,expected_actor:WrongClient(self,path,expected_actor))
        self.assertEqual(status,503)
        self.assertNotIn('private-marker',page)
        self.assertNotIn('Synthetic revision',page)
