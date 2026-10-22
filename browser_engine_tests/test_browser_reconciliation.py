"""Real HTTPS attestation of lost synthetic alert acknowledgements."""
from copy import deepcopy
import base64
import http.client
import json
import ssl
from urllib.parse import urlencode, urlsplit
import yaml

from test_browser_interventions import BrowserInterventionFixture
import test_browser_simulation as simulation_tests


class BrowserReconciliationTests(BrowserInterventionFixture):
    submit = simulation_tests.BrowserSimulationTests.submit

    def setUp(self):
        super().setUp()
        config=yaml.safe_load((self.root/'web.yml').read_text())
        config['general']['rules_web_origin']=self.https_origin
        self.write_yaml('web.yml',config);self.stop('web');self.start_web()
        for grant in self.engine_config['grants']:
            if grant['subject']=='alice':grant['operations'].append('reconcile')
        self.write_engine()

    def uncertain(self,mode='real'):
        if mode=='real':self.dispatch('reconcile-event')
        else:
            self.private('facts.json',json.dumps(dict(cpu_percent=95)))
            self.operator('test-dispatch','observed','--target','container:web','--binding',self.binding,
                '--input',str(self.root/'facts.json'),'--event-id','reconcile-event')
        self.worker(test=mode=='test');self.receiver.mode='lost';self.worker(test=mode=='test')
        self.search(mode);self.open_event('reconcile-event');self.assert_status('uncertain')
        client=self.clients['alice']
        method=client.dispatch_status if mode=='real' else client.test_dispatch_status
        return deepcopy(method('observed','reconcile-event'))

    def review(self):
        with self.page.expect_navigation() as navigation:
            self.page.get_by_role('link',name='Review reconciliation',exact=True).click()
        self.assertEqual(navigation.value.status,200,self.page.locator('main').inner_text())
        self.page.get_by_label('Externally verified outcome',exact=True).select_option('failed')
        self.page.get_by_label('Verification reference or note',exact=True).fill('synthetic receiver inspection 42')
        self.submit('Review attestation','reconcile')
        self.page.get_by_label('I verified the external outcome',exact=False).check()
        return self.page.locator('input[name=token]').input_value()

    def test_real_effect_attestation_preserves_history_after_restart_without_replay(self):
        before=self.uncertain();auton=self.auton_history(1);token=self.review()
        self.assertEqual(self.clients['alice'].dispatch_status('observed','reconcile-event'),before)
        for width,height in ((1440,1100),(390,844)):
            self.page.set_viewport_size(dict(width=width,height=height))
            self.assertTrue(self.page.evaluate('document.documentElement.scrollWidth <= innerWidth'))
        self.submit('Confirm reconciliation','reconcile-confirm',303)
        self.assert_status('failed');self.page.get_by_role('heading',name='Operator-confirmed outcome',exact=True).wait_for()
        state=self.clients['alice'].dispatch_status('observed','reconcile-event')
        self.assertEqual(state['history'],before['history'])
        self.assertEqual(state['error'],before['error'])
        self.assertEqual(state['reconciliation']['evidence'],'synthetic receiver inspection 42')
        audit=self.clients['alice'].audit('observed')
        self.assertEqual(audit[-1]['operation'],'reconcile');self.assertEqual(audit[-1]['run_id'],state['id'])
        origin=urlsplit(self.https_origin)
        connection=http.client.HTTPSConnection(origin.hostname,origin.port,
            context=ssl.create_default_context(cafile=str(self.ca.path)),timeout=5)
        try:
            connection.request('POST','/ui/rules/reconcile-confirm?organization=demo',
                headers={'Origin':self.https_origin,'Content-Type':'application/x-www-form-urlencoded',
                    'Authorization':'Basic '+base64.b64encode(('alice:'+self.password).encode()).decode()},
                body=urlencode(dict(token=token,confirm='reconcile-confirm')))
            response=connection.getresponse();response.read()
            self.assertEqual(response.status,403)
        finally:connection.close()
        self.stop('web');self.stop('engine');self.stop('storage')
        self.start_storage();self.start_engine();self.start_web();self.receiver.mode='ok';self.worker()
        self.page.reload();self.assert_status('failed')
        self.assertEqual(self.clients['alice'].dispatch_status('observed','reconcile-event'),state)
        self.assertEqual(self.auton_history(1),auton);self.assertEqual(len(self.receiver.requests),1)
        self.assertEqual((self.auton.root/'effect.txt').read_text(),'done')
        self.assert_integrity_and_delivery_drained()

    def test_test_mode_attestation_preserves_marker_without_real_effect(self):
        (self.root/'executor.password').unlink()
        before=self.uncertain('test');self.review()
        self.submit('Confirm reconciliation','reconcile-confirm',303);self.assert_status('failed')
        state=self.clients['alice'].test_dispatch_status('observed','reconcile-event')
        for key in ('test_context','history','results'):self.assertEqual(state[key],before[key])
        self.worker(test=True);self.worker()
        self.assertEqual(len(self.receiver.requests),1)
        self.assertFalse((self.auton.root/'effect.txt').exists());self.auton_history(0)
        self.assertEqual(self.proxies['auton'].calls,[]);self.assert_integrity_and_delivery_drained()

    def test_permission_revoked_after_review_does_not_close_uncertainty(self):
        before=self.uncertain();self.review()
        for grant in self.engine_config['grants']:
            grant['operations']=[op for op in grant['operations'] if op!='reconcile']
        self.write_engine()
        self.submit('Confirm reconciliation','reconcile-confirm',503)
        self.assertIn('outcome unconfirmed',self.page.locator('main').inner_text())
        self.assertEqual(self.clients['alice'].dispatch_status('observed','reconcile-event'),before)
        self.assertNotEqual(self.clients['alice'].audit('observed')[-1]['operation'],'reconcile')
        self.assertEqual(len(self.receiver.requests),1);self.auton_history(1)
        self.assert_integrity_and_delivery_drained()
