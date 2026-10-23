"""Synthetic source contract, HTTP reads, permissions and failure/recovery."""
from copy import deepcopy
from dataclasses import replace
import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from urllib.parse import parse_qs, urlsplit

from atraxis.client import validate_inventory, render_inventory
from atraxis.inventory_search import search_inventory, SearchQuery
from atraxis.models import Freshness
from atraxis.parsing import InvalidPayload
from atraxis.prometheus import parse_prometheus, query_parameters, selection
from atraxis.resource_scopes import filter_source, target_key
from atraxis.search_client import validate_result
from atraxis.service import ReadService
from atraxis.source_config import parse_sources, InvalidSourceConfiguration
from atraxis.transport import HTTPTransport, TransportResult
from atraxis.web import render_inventory as render_web
from atraxis.web_inventory_search import render_search
from test_observations import monit, NOW

SPEC = dict(name='orders_pending', match={'job': 'covenant-orders'}, labels=['job', 'instance'])


def response(stamp=NOW, value='12', instance='fixture:9118'):
    return dict(status='success', data=dict(resultType='matrix', result=[dict(
        metric={'__name__': 'orders_pending', 'job': 'covenant-orders', 'instance': instance,
                'private_annotation': 'never-publish-this-label'}, values=[[stamp, value]])]))


class ParsingTests(unittest.TestCase):
    def parse(self, payload=None, **changes):
        args=dict(metric=SPEC, source_id='orders', received_at=NOW, monotonic=100,
                  max_age=180, max_resources=10, max_clock_skew=2)
        args.update(changes)
        return parse_prometheus(response() if payload is None else payload, **args)

    def test_actual_sample_timestamp_and_signed_value(self):
        snapshot=self.parse(response(NOW-40, '-2.5'))
        self.assertEqual(snapshot.age_at_receipt, 40)
        self.assertEqual(snapshot.freshness, Freshness.FRESH)
        self.assertEqual(dict(snapshot.resources[0].fields)['value'], -2.5)
        self.assertNotIn('private_annotation', repr(snapshot))
        self.assertEqual(snapshot.freshness_at(241), Freshness.STALE)

    def test_identity_survives_value_timestamp_and_label_order_changes(self):
        first=self.parse().resources[0].native_id
        changed=response(NOW-2, '99')
        changed['data']['result'][0]['metric']=dict(reversed(list(changed['data']['result'][0]['metric'].items())))
        self.assertEqual(self.parse(changed).resources[0].native_id, first)
        self.assertNotEqual(self.parse(response(instance='other')).resources[0].native_id, first)

    def test_only_latest_sample_is_projected_without_storing_timeseries(self):
        payload=response()
        payload['data']['result'][0]['values']=[[NOW-60,'1'],[NOW-30,'4'],[NOW,'12']]
        fields=dict(self.parse(payload).resources[0].fields)
        self.assertEqual(fields['sampled_at'], NOW)
        self.assertEqual(fields['value'], 12)
        self.assertNotIn('values',fields)

    def test_empty_result_is_unknown_and_not_authoritative_empty_inventory(self):
        payload=response();payload['data']['result']=[]
        snapshot=self.parse(payload)
        self.assertFalse(snapshot.authoritative)
        self.assertEqual(snapshot.freshness, Freshness.UNKNOWN)
        self.assertIsNone(snapshot.age_at_receipt)

    def test_stale_and_future_samples_are_not_authoritative(self):
        for stamp,state in ((NOW-181,Freshness.STALE),(NOW+3,Freshness.UNKNOWN)):
            with self.subTest(stamp=stamp):
                snapshot=self.parse(response(stamp))
                self.assertEqual(snapshot.freshness,state)
                self.assertFalse(snapshot.authoritative)

    def test_partial_warnings_and_nonmatrix_results_rejected(self):
        cases=[dict(response(),status='error'),dict(response(),warnings=['partial']),
               dict(response(),infos=['omitted']),{'status':'success','data':{'resultType':'vector','result':[]}}]
        for payload in cases:
            with self.subTest(payload=payload),self.assertRaises(InvalidPayload):self.parse(payload)

    def test_nonfinite_and_malformed_samples_rejected(self):
        for raw in ('NaN','+Inf','-Inf','1e999','',True,{},'12junk'):
            with self.subTest(raw=raw),self.assertRaises(InvalidPayload):self.parse(response(value=raw))
        for samples in ([],[[NOW, '2'],[NOW, '3']],[[True,'2']],[[NOW,'2','extra']]):
            payload=response();payload['data']['result'][0]['values']=samples
            with self.subTest(samples=samples),self.assertRaises(InvalidPayload):self.parse(payload)

    def test_wrong_metric_match_missing_identity_and_ambiguous_series_rejected(self):
        for key,val in (('__name__','other'),('job','other'),('instance',None)):
            payload=response();payload['data']['result'][0]['metric'][key]=val
            with self.subTest(key=key),self.assertRaises(InvalidPayload):self.parse(payload)
        payload=response();other=deepcopy(payload['data']['result'][0]);other['metric']['private_annotation']='other'
        payload['data']['result'].append(other)
        with self.assertRaises(InvalidPayload):self.parse(payload)

    def test_resource_and_sample_budgets_are_enforced(self):
        payload=response();payload['data']['result'].append(response(instance='other')['data']['result'][0])
        with self.assertRaises(InvalidPayload):self.parse(payload,max_resources=1)
        payload=response();payload['data']['result'][0]['values']=[[NOW-1001+i,'1'] for i in range(1001)]
        with self.assertRaises(InvalidPayload):self.parse(payload)

    def test_total_sample_budget_is_enforced_across_distinct_series(self):
        payload=response()
        payload['data']['result']=[]
        for index in range(11):
            series=response(instance=str(index))['data']['result'][0]
            series['values']=[[NOW-999+i,'1'] for i in range(1000)]
            payload['data']['result'].append(series)
        with self.assertRaises(InvalidPayload):self.parse(payload,max_resources=11)

    def test_selector_is_exact_bounded_and_has_a_series_overflow_sentinel(self):
        spec=deepcopy(SPEC);spec['match']['job']='quote"\\value'
        params=query_parameters(spec,180,2,5)
        self.assertEqual(params['query'],'orders_pending{job="quote\\"\\\\value"}[180s]')
        self.assertEqual(params['limit'],6)
        self.assertEqual(params['timeout'],'2s')

    def test_arbitrary_expressions_and_invalid_identity_configuration_rejected(self):
        cases=[dict(SPEC,name='rate(orders_pending[5m])'),dict(SPEC,match={}),
               dict(SPEC,labels=[]),dict(SPEC,labels=['job','job']),dict(SPEC,labels=['instance']),
               dict(SPEC,match={'__name__':'other'}),dict(SPEC,labels=['job','__name__'])]
        for spec in cases:
            with self.subTest(spec=spec),self.assertRaises(InvalidPayload):selection(spec)


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.credentials=self.root/'reader.json'
        self.credentials.write_text(json.dumps(dict(username='reader',password='synthetic-secret')))
        self.document=dict(source_id='orders',kind='prometheus',origin='https://prometheus.example',
                           credentials_file=str(self.credentials),metric=deepcopy(SPEC))
        self.source=parse_sources([self.document],self.root/'atraxis.yml')[0]
        self.clock=100.
        self.service=ReadService([self.source],clock=lambda:self.clock)
        self.service.set_running(True)

    def record(self,payload=None,outcome='success'):
        self.service.record('orders',TransportResult('orders',outcome,NOW,NOW,self.clock,
                            response() if payload is None else payload))
        return self.service.status()

    def test_configuration_is_explicit_and_does_not_expose_selection_in_repr(self):
        self.assertNotIn('covenant-orders',repr(self.source))
        self.assertEqual(self.source.url,'https://prometheus.example/api/v1/query')
        for changes in ({'metric':None},{'kind':'certlord'},{'metric':dict(SPEC,unexpected=True)}):
            with self.subTest(changes=changes),self.assertRaises(InvalidSourceConfiguration):
                parse_sources([dict(self.document,**changes)],self.root/'atraxis.yml')

    def test_failure_empty_and_stale_retain_history_then_recover(self):
        self.record()
        empty=response();empty['data']['result']=[]
        for payload,outcome in (({},'timeout'),(empty,'success'),(response(NOW-200),'success'),(response(value='NaN'),'success')):
            status=self.record(payload,outcome)
            self.assertFalse(status['ready'])
            self.assertFalse(status['sources'][0]['current'])
            self.assertEqual(status['sources'][0]['last_valid_snapshot']['resources'][0]['fields']['value'],12)
            validate_inventory(status)
        self.assertTrue(self.record(response(value='0'))['ready'])

    def test_metrics_do_not_change_other_source_availability(self):
        monitor=replace(self.source,source_id='docker',kind='monit_docker',metric=None)
        self.service=ReadService([self.source,monitor],clock=lambda:self.clock);self.service.set_running(True)
        self.service.record('docker',TransportResult('docker','success',NOW,NOW,100,monit()))
        status=self.record({},'timeout')
        self.assertTrue(status['sources'][1]['current'])
        self.assertFalse(status['sources'][0]['current'])

    def test_api_cli_web_and_search_project_only_approved_labels(self):
        status=validate_inventory(self.record(response(value='-2.5',instance='<script>alert(1)</script>')))
        rendered=render_web(status)
        self.assertIn('Prometheus sample',rendered)
        self.assertIn('-2.5',rendered)
        self.assertNotIn('<script>',rendered)
        self.assertNotIn('never-publish-this-label',rendered)
        self.assertIn('sampled_at',render_inventory(status,details=True))
        query=SearchQuery(kind='metric',q='script')
        result=validate_result(search_inventory(status,query),query,None)
        self.assertEqual(result['total'],1)
        self.assertIn('orders_pending',render_search(result))

    def test_client_rejects_tampered_fields_and_identity(self):
        status=self.record()
        for key,val in (('value',float('nan')),('labels',[{'name':'__private','value':'secret'}]),('extra','secret')):
            altered=deepcopy(status);altered['sources'][0]['last_valid_snapshot']['resources'][0]['fields'][key]=val
            with self.subTest(key=key),self.assertRaises(InvalidPayload):validate_inventory(altered)

    def test_metric_grants_filter_before_search_counts(self):
        payload=response();payload['data']['result'].append(response(instance='second')['data']['result'][0])
        status=self.record(payload)
        allowed=status['sources'][0]['last_valid_snapshot']['resources'][0]
        self.assertEqual(target_key(dict(kind='metric',source='orders',id=allowed['native_id'])),
                         ('metric','orders',allowed['native_id']))
        status['sources']=[filter_source(status['sources'][0],[('metric',allowed['native_id'])])]
        result=search_inventory(status,SearchQuery(kind='metric'))
        self.assertEqual(result['total'],1)
        self.assertEqual(result['items'][0]['native_id'],allowed['native_id'])
        self.assertEqual(search_inventory(status,SearchQuery(kind='container'))['total'],0)

    def test_metrics_cannot_be_used_as_live_intervention_facts(self):
        from atraxis.rule_observations import ObservedInventory, ObservationBinding, ObservationError
        status=self.record();resource=status['sources'][0]['last_valid_snapshot']['resources'][0]
        binding=ObservationBinding('org',('org','server','test'),'orders',resource['native_id'])
        with self.assertRaises(ObservationError):ObservedInventory(status).facts(binding,{'cpu_percent'})

    def test_real_transport_uses_get_only_fixed_route_and_explicit_credentials(self):
        calls=[]
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_GET(self):
                calls.append((self.command,self.path,self.headers.get('Authorization')))
                data=json.dumps(response(time.time())).encode()
                self.send_response(200);self.send_header('Content-Type','application/json')
                self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            source=parse_sources([dict(self.document,origin='http://127.0.0.1:'+str(server.server_port),
                                 allow_loopback_http=True,timeout=2)],self.root/'atraxis.yml')[0]
            service=ReadService([source]);service.set_running(True)
            result=HTTPTransport().fetch(source)
            self.assertEqual(result.outcome,'success')
            service.record(source.source_id,result)
            self.assertTrue(validate_inventory(service.status())['ready'])
            self.assertEqual(len(calls),1)
            method,path,auth=calls[0]
            self.assertEqual(method,'GET');self.assertEqual(urlsplit(path).path,'/api/v1/query')
            self.assertEqual(parse_qs(urlsplit(path).query)['query'],['orders_pending{job="covenant-orders"}[180s]'])
            self.assertEqual(auth,'Basic '+base64.b64encode(b'reader:synthetic-secret').decode())
        finally:
            server.shutdown();server.server_close();thread.join(2)
