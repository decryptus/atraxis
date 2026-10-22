import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import yaml
from atraxis.accounts import Directory, default_directory
from atraxis.account_store import AccountStore


def fixture():
    return default_directory('alice').document()


class AccountOperatorTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        self.root=Path(tmp.name);self.path=self.root/'accounts.json';self.store=AccountStore(self.path)
        self.revision=self.store.apply(Directory(fixture()),None)

    def test_local_operator_revoked_before_client_creation(self):
        from atraxis.rule_operator import LocalRuleOperator
        path=self.root/'operator.yml'
        path.write_text(yaml.safe_dump(dict(version=2,accounts_file='accounts.json',identities=[
            dict(uid=os.getuid(),subject='alice',organization='default',atropos_client='client.yml')])))
        path.chmod(0o600);operator=LocalRuleOperator(path)
        data=fixture();data['tenants'][0]['enabled']=False;self.store.apply(Directory(data),self.revision)
        with patch('atraxis.rule_operator.RuleClient') as client:
            with self.assertRaises(PermissionError):operator.application.catalog()
            client.assert_not_called()

    def test_explicit_local_context_and_post_call_membership_revocation(self):
        from atraxis.rule_operator import LocalRuleOperator
        data=fixture();data['organizations'].append(dict(data['organizations'][0],id='other'))
        data['memberships'].append(dict(user='alice',organization='other',group=None))
        self.revision=self.store.apply(Directory(data),self.revision)
        path=self.root/'operator.yml'
        path.write_text(yaml.safe_dump(dict(version=2,accounts_file='accounts.json',identities=[
            dict(uid=os.getuid(),subject='alice',organization=org,atropos_client=org+'.yml') for org in ('default','other')])));path.chmod(0o600)
        with self.assertRaises(PermissionError):LocalRuleOperator(path)
        operator=LocalRuleOperator(path,organization='other')
        self.assertEqual(operator.actor().organization,'other')
        with patch('atraxis.rule_operator.RuleClient') as client:
            def revoke():
                data['organizations'][1]['enabled']=False
                self.store.apply(Directory(data),self.revision)
                return []
            client.return_value.catalog.side_effect=revoke
            with self.assertRaises(PermissionError):operator.application.catalog()
            client.return_value.close.assert_called_once()
            self.assertEqual(client.call_args.kwargs['expected_actor'],dict(subject='alice',organization='other'))
