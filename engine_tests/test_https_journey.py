"""Verified HTTPS on every source/engine/executor/alert hop, never production."""
import base64
import http.client
import json
import ssl
from urllib.parse import urlsplit
import yaml

from atropos_client.client import RuleClient, RemoteRuleError
from test_source_journey import SourceJourneyFixture
from tls_fixture import Authority, TLSIngress


class HTTPSJourneyFixture(SourceJourneyFixture):
    def setUp(self):
        super().setUp()
        self.ca=Authority(self.root);self.proxies={}
        def ingress(name,origin):
            proxy=TLSIngress(origin,self.ca.leaf(name));self.addCleanup(proxy.close)
            self.proxies[name]=proxy;return proxy.origin
        web=yaml.safe_load((self.root/'web.yml').read_text())
        web['sources'][0].update(origin=ingress('source',web['sources'][0]['origin']),
            allow_loopback_http=False,ca_file=str(self.ca.path))
        self.write_yaml('web.yml',web);self.stop('web');self.start_web()
        self.https_origin=ingress('atraxis',self.origin)
        self.write_yaml('inventory.yml',dict(origin=self.https_origin,credentials_file='inventory.json',ca_file=str(self.ca.path)))
        engine_origin=ingress('engine','http://127.0.0.1:'+str(self.engine_port))
        for user in self.tokens:
            self.write_yaml(user+'.yml',dict(origin=engine_origin,token_file=user+'.token',ca_file=str(self.ca.path),timeout=5))
            self.clients[user]=RuleClient(self.root/(user+'.yml'),expected_actor=dict(subject=user,organization='other' if user=='charlie' else 'demo'))
            self.addCleanup(self.clients[user].close)
        self.engine_config['execution_adapters']['ops.run'].update(origin=ingress('auton',self.auton.origin),allow_loopback_http=False,ca_file=str(self.ca.path))
        alert=ingress('alert',self.receiver.origin)
        self.engine_config['execution_adapters']['ops.notify'].update(origin=alert,allow_loopback_http=False,ca_file=str(self.ca.path))
        self.engine_config['test_alert_delivery'].update(url=alert,allow_loopback_http=False,ca_file=str(self.ca.path))
        self.write_engine()
        self.refresh_binding()
        self.stop('engine');self.start_engine()
        self.ready('web',lambda:self.request('/readyz')[0]==200)

    def refresh_binding(self):
        records=yaml.safe_load((self.root/'bindings.yml').read_text())
        revisions=self.engine('execution-adapters','--config',str(self.root/'engine.yml'))
        records[0]['adapter_revisions']={name:entry['revision'] for name,entry in revisions.items()}
        self.write_yaml('bindings.yml',records)
        self.binding=self.engine('rules-bindings','--config',str(self.root/'engine.yml'))[0]['id']

    def https(self,ca=None,*,auth=True,forwarded=None):
        parsed=urlsplit(self.https_origin)
        conn=http.client.HTTPSConnection(parsed.hostname,parsed.port,context=ssl.create_default_context(cafile=str(ca or self.ca.path)),timeout=5)
        headers={}
        if auth:headers['Authorization']='Basic '+base64.b64encode(('alice:'+self.password).encode()).decode()
        if forwarded:headers['X-Forwarded-User']=forwarded
        try:
            conn.request('GET','/ui?organization=demo',headers=headers)
            response=conn.getresponse();return response.status,response.read()
        finally:conn.close()


class HTTPSJourneyTests(HTTPSJourneyFixture):
    def test_verified_https_from_inventory_through_auton_and_alert(self):
        self.assertEqual(self.https()[0],200)
        self.dispatch();self.worker();self.worker()
        state=self.clients['alice'].dispatch_status('observed','observed')
        self.assertEqual(state['status'],'succeeded',state)
        self.assertEqual((self.auton.root/'effect.txt').read_text(),'done')
        self.assertEqual(self.receiver.requests[0][0],'/api/v2/alerts')
        for name,proxy in self.proxies.items():self.assertTrue(proxy.calls,name)

    def test_https_test_receiver_preserves_context_with_private_ca(self):
        (self.root/'executor.password').unlink()
        self.private('facts.json',json.dumps(dict(cpu_percent=95)))
        receipt=self.operator('test-dispatch','observed','--target','container:web','--binding',self.binding,
            '--input',str(self.root/'facts.json'),'--event-id','tls-test')
        self.worker(test=True);self.worker(test=True)
        result=self.operator('test-dispatch-status','observed','--event-id','tls-test')
        self.assertEqual(result['status'],'succeeded',result)
        self.assertEqual(result['results']['notify']['alert']['test_context'],receipt['test_context'])
        self.assertEqual(self.proxies['auton'].calls,[])
        self.assertEqual(self.receiver.requests[0][0],'/v1/test-alerts')

    def test_ingress_rejects_bad_trust_hostname_expiry_and_forwarded_identity(self):
        wrong=Authority(self.root,'untrusted')
        with self.assertRaises(ssl.SSLCertVerificationError):self.https(wrong.path)
        for name,options in [('wrong-host',dict(wrong_host=True)),('expired',dict(expired=True))]:
            self.proxies['atraxis'].certificate(self.ca.leaf(name,**options))
            with self.subTest(name=name),self.assertRaises(ssl.SSLCertVerificationError):self.https()
        self.proxies['atraxis'].certificate(self.ca.leaf('restored'))
        self.assertEqual(self.https(auth=False,forwarded='alice')[0],401)
        self.assertEqual(self.https()[0],200)

    def test_source_bad_hostname_and_engine_bad_ca_fail_before_admission(self):
        self.proxies['source'].certificate(self.ca.leaf('source-mismatch',wrong_host=True))
        self.ready('web',lambda:self.request('/readyz')[0]==503)
        self.operator('observe-dispatch','observed','--target','container:web','--binding',self.binding,
            '--expected-generation',self.active['generation'],'--event-id','bad-source','--confirm',expected=2)
        wrong=Authority(self.root,'engine-untrusted')
        self.proxies['engine'].certificate(wrong.leaf('engine-bad'))
        with self.assertRaises(RemoteRuleError):self.clients['alice'].catalog()
        self.assertFalse((self.auton.root/'effect.txt').exists());self.assertEqual(self.receiver.requests,[])

    def test_executor_bad_hostname_prevents_effect_and_alert(self):
        self.proxies['auton'].certificate(self.ca.leaf('executor-mismatch',wrong_host=True))
        self.dispatch();self.worker();self.worker()
        state=self.clients['alice'].dispatch_status('observed','observed')
        self.assertIn(state['status'],('failed','uncertain'))
        self.assertFalse((self.auton.root/'effect.txt').exists());self.assertEqual(self.receiver.requests,[])

    def test_alert_untrusted_ca_retains_uncertainty_without_resend(self):
        self.dispatch();self.worker()
        wrong=Authority(self.root,'alert-untrusted')
        self.proxies['alert'].certificate(wrong.leaf('alert-bad'))
        self.worker();self.worker()
        self.assertEqual(self.clients['alice'].dispatch_status('observed','observed')['status'],'uncertain')
        self.assertEqual(self.receiver.requests,[])
        self.assertEqual((self.auton.root/'effect.txt').read_text(),'done')
