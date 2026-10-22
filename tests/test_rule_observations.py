import copy
from dataclasses import replace
import unittest
from unittest.mock import Mock

from atraxis.rule_observations import LiveRuleEvaluator, ObservationBinding, ObservationError, ObservedInventory
from test_client import inventory
from test_projection_details import filesystem


class RuleObservationTests(unittest.TestCase):
    def setUp(self):
        self.payload = inventory()
        self.fields = self.payload['sources'][0]['last_valid_snapshot']['resources'][0]['fields']
        self.fields.update(cpu_percent=250, mem_percent=92, health='unhealthy',
                           maintenance_active=False, restart_attempts=2, filesystems=[filesystem()])
        self.binding = ObservationBinding('org', ('container', 'web'), 'a', 'a'*64, ('data', '/var/lib/data'))
        self.now = 100.

    def observation(self):
        return ObservedInventory(self.payload, clock=lambda: self.now)

    def test_explicit_mappings_and_filesystem_flags(self):
        facts, age = self.observation().facts(self.binding, dict.fromkeys(
            ('cpu_percent','memory_percent','health_status','maintenance','restart_attempts',
             'disk_percent','inode_percent','readable','writable')))
        self.assertEqual(facts, {'cpu_percent':250,'memory_percent':92,'health_status':'unhealthy',
                         'maintenance':False,'restart_attempts':2,'disk_percent':60,'inode_percent':50,
                         'readable':True,'writable':False})
        self.assertEqual(age, 5)

    def test_no_restart_count_or_missing_value_guess(self):
        for field in ('restart_count', 'unknown'):
            with self.assertRaises(ObservationError):
                self.observation().facts(self.binding, {field:'integer'})
        self.fields['mem_percent'] = None
        with self.assertRaises(ObservationError):
            self.observation().facts(self.binding, {'memory_percent':'number'})

    def test_expiry_and_clock_regression(self):
        observed = self.observation()
        self.now += 56
        with self.assertRaises(ObservationError):
            observed.facts(self.binding, {'cpu_percent':'number'})
        self.now = 99
        with self.assertRaises(ObservationError):
            observed.resource(self.binding)
        self.now = float('nan')
        with self.assertRaises(ObservationError):
            self.observation()

    def test_exact_source_and_container_identity(self):
        for changed in (replace(self.binding, source_id='other'), replace(self.binding,native_id='b'*64)):
            with self.assertRaises(ObservationError):
                self.observation().resource(changed)

    def test_unknown_health_and_invalid_access_never_become_healthy(self):
        for value in ('unknown','none','starting'):
            self.fields['health'] = value
            with self.assertRaises(ObservationError):
                self.observation().facts(self.binding, {'health_status':'string'})
        self.fields['filesystems'][0]['fs_readable'] = None
        with self.assertRaises(ObservationError):
            self.observation().facts(self.binding, {'readable':'boolean'})
        self.fields['filesystems'][0]['fs_readable'] = True
        with self.assertRaises(ObservationError):
            self.observation().facts(self.binding, {'readable':'boolean'})

    def test_filesystem_selection_must_be_unambiguous(self):
        for binding in (replace(self.binding,filesystem=None),replace(self.binding,filesystem=('other','/missing'))):
            with self.assertRaises(ObservationError):
                self.observation().facts(binding, {'disk_percent':'number'})
        self.fields['filesystems'] *= 2
        with self.assertRaises(ObservationError):
            self.observation().facts(self.binding, {'disk_percent':'number'})

    def test_snapshot_copy_is_immutable_to_caller_changes(self):
        observed = self.observation()
        self.fields['cpu_percent'] = 0
        facts,_ = observed.facts(self.binding, {'cpu_percent':'number'})
        self.assertEqual(facts['cpu_percent'],250)

    def test_historical_data_is_rejected(self):
        self.payload.update(ready=False,reason='sources_not_current')
        self.payload['sources'][0].update(current=False,availability='unavailable')
        self.payload['sources'][0]['last_attempt']['outcome']='timeout'
        with self.assertRaises(ObservationError):
            self.observation().facts(self.binding,{'cpu_percent':'number'})

    def test_live_evaluation_binds_version_and_rechecks_age(self):
        app=Mock()
        app.active.return_value={'revision':'revision','generation':'generation'}
        app.read.return_value={'context':{'targets':[['container','web']]},'document':{'inputs':{'cpu_percent':'number'}}}
        app.evaluate_active.return_value={'revision':'revision','generation':'generation','result':{'decision':'review'}}
        bridge=LiveRuleEvaluator(app,lambda target:self.binding,self.observation)
        self.assertEqual(bridge.evaluate('cpu',('container','web'))['facts']['cpu_percent'],250)
        app.evaluate_active.return_value['generation']='new'
        with self.assertRaises(ObservationError):
            bridge.evaluate('cpu',('container','web'))
        app.evaluate_active.return_value['generation']='generation'
        def slow(*args):
            self.now+=56
            return app.evaluate_active.return_value
        app.evaluate_active.side_effect=slow
        with self.assertRaises(ObservationError):
            bridge.evaluate('cpu',('container','web'))

    def test_denied_scope_never_fetches_inventory(self):
        app=Mock()
        app.active.return_value={'revision':'revision','generation':'generation'}
        app.read.return_value={'context':{'targets':[['container','web']]}}
        loader=Mock()
        with self.assertRaises(PermissionError):
            LiveRuleEvaluator(app,Mock(),loader).evaluate('cpu',('container','other'))
        loader.assert_not_called()
