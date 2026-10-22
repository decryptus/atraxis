"""Local Atraxis operator uses the same remote engine boundary as the gateway."""
from contextlib import redirect_stdout, redirect_stderr
import io
import json
import os
import unittest
from unittest.mock import patch
import yaml

import test_rule_remote as remote_fixture
from test_rules_atropos import fixture_module
from atraxis.cli import main
from atraxis.config import ConfigurationError
from atraxis.rule_operator import LocalRuleOperator, load_operator_config


class RuleOperatorIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.fixture = fixture_module.RedisFixture()

    @classmethod
    def tearDownClass(cls): cls.fixture.close()

    write_config = remote_fixture.RemoteRuleIntegrationTests.write_config

    def setUp(self):
        remote_fixture.RemoteRuleIntegrationTests.setUp(self)
        self.operator_config = self.root/'operator.yml'
        self.local_doc = {'version':2,'identities':[{'uid':os.getuid(),'subject':'alice',
                           'organization':'org','atropos_client':'alice-engine.yml'}]}
        self.write_local()
        self.casesfile=self.root/'cases.json';self.casesfile.write_text(json.dumps(self.cases))

    def write_local(self):
        self.operator_config.write_text(yaml.safe_dump(self.local_doc));self.operator_config.chmod(0o600)

    def invoke(self,*args):
        out,err=io.StringIO(),io.StringIO()
        with redirect_stdout(out),redirect_stderr(err):
            code=main(['rules','--operator-config',str(self.operator_config),*args])
        return code,out.getvalue(),err.getvalue()

    def test_local_cli_saves_and_tests_only_through_engine_api(self):
        code,out,err=self.invoke('create','cpu-high','--name','cpu','--kind','intervention','--target','container:web')
        self.assertEqual((code,err),(0,''));draft=json.loads(out)
        code,out,err=self.invoke('test','cpu',draft['revision'],'--cases',str(self.casesfile))
        self.assertEqual((code,err),(0,''));report=json.loads(out)
        self.assertTrue(report['passed'])
        self.assertEqual(self.invoke('activate','cpu',draft['revision'],'--test-id',report['id'],'--expected-generation','none')[0],0)
        self.assertEqual(self.client.active('cpu')['revision'],draft['revision'])

    def test_private_profile_and_symlink_are_required(self):
        load_operator_config(self.operator_config)
        self.operator_config.chmod(0o644)
        with self.assertRaises(ConfigurationError):load_operator_config(self.operator_config)
        self.operator_config.chmod(0o600)
        link=self.root/'link.yml';link.symlink_to(self.operator_config)
        with self.assertRaises(ConfigurationError):load_operator_config(link)

    def test_unmapped_uid_and_legacy_storage_profile_fail_before_network(self):
        self.local_doc['identities'][0]['uid'] += 1000;self.write_local()
        with patch('atraxis.rule_operator.RuleClient') as client:
            self.assertEqual(self.invoke('catalog')[0],3)
            client.assert_not_called()
        self.local_doc['storage']={'backend':'redis','url':'redis://localhost'};self.write_local()
        self.assertEqual(self.invoke('catalog')[0],2)

    def test_live_engine_permission_and_export_provider_changes_are_enforced(self):
        code,out,_=self.invoke('create','cpu-high','--name','cpu','--kind','intervention','--target','container:web')
        self.assertEqual(code,0);draft=json.loads(out)
        self.assertEqual(self.invoke('export','cpu',draft['revision'])[0],0)
        self.doc['grants'][0]['providers'].remove('download');self.write_config()
        self.assertEqual(self.invoke('export','cpu',draft['revision'])[0],3)
        self.doc['grants']=[];self.write_config()
        self.assertEqual(self.invoke('read','cpu',draft['revision'])[0],3)

    def test_open_operator_rejects_changed_identity(self):
        operator=LocalRuleOperator(self.operator_config)
        self.local_doc['identities'][0]['subject']='bob';self.write_local()
        with self.assertRaises(PermissionError):operator.application.catalog()

    def test_failed_scenarios_and_conflicts_preserve_cli_exit_codes(self):
        code,out,_=self.invoke('create','cpu-high','--name','cpu','--kind','intervention','--target','container:web')
        draft=json.loads(out)
        self.cases[0]['expected']={'decision':'ignore','matched_rule':'__default__'}
        self.casesfile.write_text(json.dumps(self.cases))
        code,out,_=self.invoke('test','cpu',draft['revision'],'--cases',str(self.casesfile))
        self.assertEqual(code,1)
        report=json.loads(out)
        self.assertEqual(self.invoke('activate','cpu',draft['revision'],'--test-id',report['id'],'--expected-generation','none')[0],2)
        self.assertEqual(self.invoke('create','cpu-high','--name','cpu','--kind','intervention','--target','container:web')[0],2)
