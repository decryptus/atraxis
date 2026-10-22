from copy import deepcopy
from urllib.parse import urlencode
import unittest
from unittest.mock import patch

from atraxis.intervention_search import InterventionQuery
from atraxis.web_intervention_detail import DetailReader, handle, receipt
import test_intervention_search as search_tests
import test_web_rules as webfixture


def state(row):
    data = dict(id=row['id'], status='uncertain', rule=row['rule'], revision=row['revision'],
                error='action_outcome_unknown', history=[dict(step='notify', status='uncertain',
                at=1791480001, error='action_outcome_unknown')])
    if row['mode'] == 'test':
        data.update(test_context=row['test_context'], results={'secret-output': {'value': 'DO-NOT-DISPLAY'}})
        data['history'][0]['test_context'] = row['test_context']
    else:
        data.update(generation='c'*32, binding='d'*64, event_id=row['event_id'], target=row['target'])
    return data


def url(mode='test'):
    return '/ui/interventions/detail?' + urlencode(dict(name='cpu', mode=mode, id='a'*64))


class DetailTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.row = search_tests.result()['items'][0]
        owner = self
        class Reader:
            def __init__(self, *args, **kwargs): pass
            def entry(self): return dict(subject='alice', organization='org')
            def call(self, method, name, *args, **kwargs):
                owner.calls.append(method)
                if method == 'search_interventions':
                    q = dict(kwargs); q['q'] = q.pop('query')
                    return search_tests.result(InterventionQuery(**q), items=[deepcopy(owner.row)])
                return state(owner.row)
        self.reader = Reader
        for target, replacement in [('atraxis.web_intervention_detail.DetailReader', Reader),
                                    ('atraxis.web_organization_context.choices', lambda *a: [dict(subject='alice', organization='org')])]:
            p = patch(target, replacement); p.start(); self.addCleanup(p.stop)

    def test_real_and_test_details_only_call_reads_and_hide_raw_output(self):
        self.assertEqual(DetailReader.methods, {'search_interventions', 'dispatch_status', 'test_dispatch_status'})
        for mode in ('test', 'real'):
            self.row.update(mode=mode, test_context=dict(test=True, id='a'*64) if mode == 'test' else None)
            self.calls.clear()
            code, body = handle(url(mode), 'alice', 'unused')
            self.assertEqual(code, 200, body)
            self.assertIn('Outcome unknown.', body)
            self.assertNotIn('DO-NOT-DISPLAY', body)
            self.assertIn('organization=org', body)
            self.assertEqual(self.calls, ['search_interventions', 'test_dispatch_status' if mode == 'test' else 'dispatch_status', 'search_interventions'])

    def test_strict_queries_never_call_engine(self):
        for suffix in ('&actor=alice', '&mode=test', '&id=bad'):
            self.assertEqual(handle(url()+suffix, 'alice', 'unused')[0], 400)
        for path in ('?name=cpu&mode=all&id='+'a'*64, '?name=cpu&mode=test', '?name=cpu&mode=test&id=x'):
            self.assertEqual(handle(path, 'alice', 'unused')[0], 400)
        self.assertEqual(self.calls, [])

    def test_receipt_rejects_cross_run_mode_marker_and_unbounded_data(self):
        good = state(self.row)
        mutations = [lambda d: d.update(id='f'*64), lambda d: d.update(rule='other'),
            lambda d: d.update(revision='e'*32), lambda d: d.update(status='invented'),
            lambda d: d.update(test_context=dict(test=1, id='a'*64)),
            lambda d: d['history'][0].update(test_context=dict(test=True, id='f'*64)),
            lambda d: d['history'][0].update(at=float('nan')),
            lambda d: d['history'][0].update(step='<script>'),
            lambda d: d.update(history=[d['history'][0]]*1001),
            lambda d: d.update(results={'large': 'x'*524289}), lambda d: d.update(secret='raw')]
        for change in mutations:
            bad = deepcopy(good); change(bad)
            with self.subTest(change=change), self.assertRaises((ValueError, TypeError)):
                receipt(bad, self.row)
        self.row.update(mode='real', test_context=None)
        for key, value in [('target', 'other/container/web'), ('event_id', 'other'), ('generation', 'x'), ('binding', 'x')]:
            bad = state(self.row); bad[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError): receipt(bad, self.row)

    def test_denial_and_revocation_after_receipt_discard_all_details(self):
        original = self.reader.call
        def revoked(reader, method, *args, **kwargs):
            if len(self.calls) == 2: raise PermissionError('SECRET')
            return original(reader, method, *args, **kwargs)
        with patch.object(self.reader, 'call', revoked):
            code, body = handle(url(), 'alice', 'unused')
        self.assertEqual(code, 403)
        for secret in ('SECRET', 'event-1', 'a'*64, 'notify'): self.assertNotIn(secret, body)

    def test_absent_run_and_unavailable_service_do_not_show_partial_details(self):
        self.row['id'] = 'f'*64; self.row['test_context']['id'] = 'f'*64
        self.assertEqual(handle(url(), 'alice', 'unused')[0], 404)
        with patch.object(self.reader, 'call', side_effect=ValueError('rule_not_found')):
            self.assertEqual(handle(url(), 'alice', 'unused')[0], 404)
        with patch.object(self.reader, 'call', side_effect=RuntimeError('SECRET')):
            code, body = handle(url(), 'alice', 'unused')
            self.assertEqual(code, 503); self.assertNotIn('SECRET', body)

    def test_unknown_errors_are_fixed_text_not_remote_messages(self):
        original = self.reader.call
        def secret_error(reader, method, *args, **kwargs):
            data = original(reader, method, *args, **kwargs)
            if method != 'search_interventions':
                data['error'] = 'PRIVATE-REMOTE-BODY'; data['history'][0]['error'] = 'PRIVATE-REMOTE-BODY'
            return data
        with patch.object(self.reader, 'call', secret_error):
            code, body = handle(url(), 'alice', 'unused')
        self.assertEqual(code, 200); self.assertNotIn('PRIVATE-REMOTE-BODY', body)


class DetailHTTPTests(unittest.TestCase):
    setUp = webfixture.WebRulesTests.setUp
    stop = webfixture.WebRulesTests.stop
    write_mapping = webfixture.WebRulesTests.write_mapping
    request = webfixture.WebRulesTests.request

    def test_auth_head_no_store_and_mapping_revoked_during_status(self):
        def search(client, name, **kwargs):
            args = dict(kwargs); args['q'] = args.pop('query')
            data = search_tests.result(InterventionQuery(**args))
            data.update(subject=client.actor['subject'], organization=client.actor['organization'])
            data['items'][0]['target'] = data['organization']+'/container/web'
            return data
        def status(client, *args):
            data = state(search_tests.result()['items'][0])
            if self.revoke: self.write_mapping([])
            return data
        with patch.object(webfixture.FakeClient, 'search_interventions', search, create=True), patch.object(webfixture.FakeClient, 'test_dispatch_status', status, create=True):
            self.assertEqual(self.request(url(), username=None)[0], 401)
            code, headers, body = self.request(url())
            self.assertEqual(code, 200, body); self.assertEqual(headers['cache-control'], 'no-store')
            self.assertEqual(self.request(url(), method='HEAD')[2], '')
            self.assertNotEqual(self.request(url(), method='POST')[0], 200)
            self.revoke = True
            code, _, body = self.request(url())
            self.assertEqual(code, 403); self.assertNotIn('a'*64, body)
