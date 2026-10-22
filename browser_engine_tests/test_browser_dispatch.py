"""Real browser admission using signed source observations and guarded workers."""
from copy import deepcopy
import re
import json

import yaml
from test_browser_interventions import BrowserInterventionFixture
import test_browser_simulation as simulation_tests


class BrowserDispatchTests(BrowserInterventionFixture):
    submit = simulation_tests.BrowserSimulationTests.submit

    def setUp(self):
        super().setUp()
        operator = yaml.safe_load((self.root/'operator.yml').read_text())
        self.write_yaml('web-observations.yml',dict(version=1, bindings=operator['bindings'],
                                                   observation_signing=operator['observation_signing']))
        config = yaml.safe_load((self.root/'web.yml').read_text())
        config['general'].update(rules_web_origin=self.https_origin, rules_web_observations='web-observations.yml')
        self.write_yaml('web.yml', config);self.stop('web');self.start_web()
        self.ready('web',lambda:self.request('/readyz')[0]==200)

    def review(self):
        self.assertEqual(self.page.goto(self.https_origin+'/ui/rules?organization=demo').status,200)
        with self.page.expect_navigation():
            self.page.get_by_role('link',name='Review a real intervention',exact=True).click()
        self.page.get_by_label('Rule name',exact=True).fill('observed')
        self.page.get_by_label('Container target ID',exact=True).fill('web')
        self.page.get_by_label('Approved binding ID',exact=True).fill(self.binding)
        self.submit('Review current observations','dispatch')
        self.page.get_by_role('heading',name='Confirm a real intervention',exact=True).wait_for()
        body=self.page.locator('main').inner_text()
        self.assertIn('"cpu_percent": 95',body)
        for value in (self.password,self.auton.password,self.receiver.token,str(self.auton.root)):
            self.assertNotIn(value,self.page.content())
        event=re.search(r'Event: ([0-9a-f]{32})',body).group(1)
        self.page.get_by_label('I authorize this real intervention',exact=False).check()
        return event,self.page.locator('input[name=token]').input_value()

    def no_effect(self):
        self.worker();self.worker(test=True)
        self.assertFalse((self.auton.root/'effect.txt').exists())
        self.assertEqual(self.receiver.requests,[]);self.auton_history(0)
        self.assertEqual(self.clients['alice'].search_interventions('observed',mode='real')['total'],0)
        self.assert_integrity_and_delivery_drained()

    def test_browser_signed_admission_real_effect_single_use_and_restart(self):
        event,token=self.review()
        self.assertEqual(self.clients['alice'].search_interventions('observed',mode='real')['total'],0)
        self.assertFalse((self.auton.root/'effect.txt').exists());self.assertEqual(self.receiver.requests,[])
        for width,height in ((1440,1100),(390,844)):
            self.page.set_viewport_size(dict(width=width,height=height))
            self.assertTrue(self.page.evaluate('document.documentElement.scrollWidth <= innerWidth'))
        self.submit('Confirm real intervention','dispatch-confirm',303)
        self.assert_status('queued')
        receipt=self.clients['alice'].dispatch_status('observed',event)
        self.assertTrue(receipt['observation']['verified_at_admission'])
        self.worker();self.worker();self.page.reload();self.assert_status('succeeded')
        state=deepcopy(self.clients['alice'].dispatch_status('observed',event));auton=self.auton_history(1)
        self.assertEqual((self.auton.root/'effect.txt').read_text(),'done')
        self.assertEqual(len(self.receiver.requests),1)
        self.assertEqual(self.receiver.requests[0][0],'/api/v2/alerts')
        self.assertEqual(self.receiver.requests[0][1][0]['annotations']['atropos_run_id'],receipt['id'])
        destination=self.page.url
        self.review();self.page.locator('input[name=token]').evaluate('(element,value)=>element.value=value',token)
        self.submit('Confirm real intervention','dispatch-confirm',403)
        self.stop('web');self.stop('engine');self.stop('storage')
        self.start_storage();self.start_engine();self.start_web()
        self.page.goto(destination);self.assert_status('succeeded')
        self.assertEqual(self.clients['alice'].dispatch_status('observed',event),state)
        self.assertEqual(self.auton_history(1),auton);self.assertEqual(len(self.receiver.requests),1)
        self.assert_integrity_and_delivery_drained()

    def test_source_lost_after_review_blocks_real_admission(self):
        self.review();self.source_ready=False
        self.ready('web',lambda:self.request('/readyz')[0]==503)
        self.submit('Confirm real intervention','dispatch-confirm',503)
        self.assertNotIn('may already have been admitted',self.page.content())
        self.no_effect()

    def test_new_source_measurement_is_collected_at_confirmation(self):
        self.review();self.cpu=1
        self.ready('web',lambda:json.loads(self.request('/v1/status')[2])['sources'][0]['last_valid_snapshot']['resources'][0]['fields']['cpu_percent']==1)
        self.submit('Confirm real intervention','dispatch-confirm',409)
        self.no_effect()

    def test_active_generation_change_after_review_blocks_real_admission(self):
        self.review()
        self.clients['alice'].deactivate('observed',expected_generation=self.active['generation'])
        self.submit('Confirm real intervention','dispatch-confirm',409)
        self.no_effect()

    def test_current_inventory_and_engine_rights_required_at_confirmation(self):
        self.review()
        access=yaml.safe_load((self.root/'access.yml').read_text());original=deepcopy(access)
        access['grants']=[];self.write_yaml('access.yml',access)
        self.submit('Confirm real intervention','dispatch-confirm',503)
        self.no_effect()
        self.write_yaml('access.yml',original);self.review()
        for grant in self.engine_config['grants']:
            if grant['subject']=='alice':grant['operations']=[op for op in grant['operations'] if op!='execute']
        self.write_engine()
        self.submit('Confirm real intervention','dispatch-confirm',403)
        self.no_effect()
