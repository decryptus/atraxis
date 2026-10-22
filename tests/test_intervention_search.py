from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from atraxis.intervention_search import InterventionQuery,SearchService,checked,parse_query,date_value
from atraxis.web_intervention_search import handle


def result(query=None,**changes):
    query=query or InterventionQuery()
    row=dict(id='a'*64,revision='b'*32,rule='cpu',event_id='event-1',mode='test',status='queued',target='org/container/web',workflow='inspect',created_at=1791480000,test_context=dict(test=True,id='a'*64))
    data=dict(schema_version=1,subject='alice',organization='org',rule='cpu',query=query.parameters(),total=1,items=[row],has_more=False)
    data.update(changes);return data

class InterventionSearchTests(unittest.TestCase):
    def test_query_dates_bounds_and_result_context(self):
        name,query=parse_query('/ui/interventions?name=cpu&since=2026-10-08T00%3A00&mode=test')
        self.assertEqual(name,'cpu');self.assertEqual(query.since,date_value('2026-10-08T00:00:00Z'))
        for path in ('?name=cpu&page=0','?name=cpu&q=a&q=b','?name=cpu&actor=bob','?name=cpu&since=2026-99-99T00:00'):
            with self.subTest(path=path),self.assertRaises(ValueError):parse_query(path)
        data=result();self.assertEqual(checked(data,'cpu',InterventionQuery())['total'],1)
        for mutation in (lambda d:d.update(subject='bob'),lambda d:d.update(total=True),lambda d:d['items'][0].update(mode='unknown'),lambda d:d['items'][0].update(target='secret/container/web')):
            bad=deepcopy(data);mutation(bad)
            with self.assertRaises(ValueError):checked(bad,'cpu',InterventionQuery(),dict(subject='alice',organization='org'))

    def test_cli_dispatches_only_search_and_checks_echo(self):
        from atraxis.cli import main
        from atraxis.rule_commands import run_command
        calls=[]
        def search(name,**values):calls.append((name,values));return result()
        options=SimpleNamespace(rule_command='search-interventions',name='cpu',query='',mode='all',status='',target='',workflow='',since='',until='',page=1,limit=50,tui=False)
        output=run_command(options,SimpleNamespace(application=SimpleNamespace(search_interventions=search)))
        self.assertEqual(output['total'],1);self.assertEqual(len(calls),1)
        service=SearchService(SimpleNamespace(search_interventions=lambda *a,**kw:result(rule='other')),'cpu')
        with self.assertRaises(ValueError):service.search(InterventionQuery())

    def test_web_read_only_context_denial_and_invalid_response(self):
        calls=[]
        class Reader:
            def __init__(self,*a,**kw):pass
            def entry(self):return dict(subject='alice',organization='org')
            def call(self,method,name,**values):calls.append(method);return result()
        with patch('atraxis.web_intervention_search.SearchReader',Reader), patch('atraxis.web_organization_context.choices',return_value=[dict(subject='alice',organization='org')]):
            code,body=handle('/ui/interventions?name=cpu','alice','unused')
            self.assertEqual(code,200);self.assertIn('1 matches',body);self.assertIn('test',body)
            self.assertEqual(calls,['search_interventions'])
            self.assertEqual(handle('/ui/interventions?name=cpu&page=0','alice','unused')[0],400)
            with patch.object(Reader,'call',return_value=result(subject='bob')):
                self.assertEqual(handle('/ui/interventions?name=cpu','alice','unused')[0],503)
            with patch.object(Reader,'call',side_effect=PermissionError()):
                code,body=handle('/ui/interventions?name=cpu','alice','unused');self.assertEqual(code,403);self.assertNotIn('event-1',body)
            with patch.object(Reader,'call',side_effect=ValueError('rule_service_unavailable')):
                self.assertEqual(handle('/ui/interventions?name=cpu','alice','unused')[0],503)


import test_web_rules as webfixture
class InterventionSearchHTTPTests(unittest.TestCase):
    setUp=webfixture.WebRulesTests.setUp
    stop=webfixture.WebRulesTests.stop
    write_mapping=webfixture.WebRulesTests.write_mapping
    request=webfixture.WebRulesTests.request

    def test_route_authentication_and_live_mapping_recheck(self):
        def search(client,name,**values):
            data=result();data.update(subject=client.actor['subject'],organization=client.actor['organization'])
            data['items'][0]['target']=client.actor['organization']+'/container/web'
            if self.revoke:self.write_mapping([])
            return data
        with patch.object(webfixture.FakeClient,'search_interventions',search,create=True):
            self.assertEqual(self.request('/ui/interventions?name=cpu',username=None)[0],401)
            status,headers,body=self.request('/ui/interventions?name=cpu')
            self.assertEqual(status,200,body);self.assertEqual(headers['cache-control'],'no-store')
            self.assertIn('1 matches',body)
            self.assertEqual(self.request('/ui/interventions?name=cpu',method='HEAD')[2],'')
            self.revoke=True
            status,_,body=self.request('/ui/interventions?name=cpu');self.assertEqual(status,403);self.assertNotIn('one/container/web',body)
