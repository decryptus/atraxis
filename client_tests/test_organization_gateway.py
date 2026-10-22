"""Real independent-client subprocess and HTTPdis gateway, synthetic engine boundary."""
import hashlib
import json
from http.server import ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import yaml
from atraxis.rule_remote import RemoteRuleOperator
from atraxis.rule_server import build_context, RuleHandler


class OrganizationGatewayTests(unittest.TestCase):
    def test_remote_context_is_required_scoped_and_rechecked(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);token='a'*43;mapping=root/'gateway.yml'
            identities=[dict(token_sha256=hashlib.sha256(token.encode()).hexdigest(),subject='alice',organization=o,atropos_client=o+'.yml') for o in ('one','two')]
            def save():
                mapping.write_text(yaml.safe_dump(dict(version=2,identities=identities)));mapping.chmod(0o600)
            save();context=build_context(mapping)
            server=ThreadingHTTPServer(('127.0.0.1',0),context.bind_handler(RuleHandler))
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            self.addCleanup(lambda:(server.shutdown(),server.server_close(),thread.join()))
            secret=root/'token';secret.write_text(token);secret.chmod(0o600)
            config=root/'client.yml';config.write_text(yaml.safe_dump(dict(origin='http://127.0.0.1:'+str(server.server_port),allow_loopback_http=True,token_file='token',timeout=5)));config.chmod(0o600)
            class Engine:
                def __init__(self,path,expected_actor):self.actor=expected_actor
                def catalog(self):return [self.actor]
                def close(self):pass
            with patch('atraxis.rule_operator.RuleClient',Engine):
                with self.assertRaises(PermissionError):RemoteRuleOperator(config).catalog()
                one=RemoteRuleOperator(config,organization='one');two=RemoteRuleOperator(config,organization='two')
                self.assertEqual(one.catalog(),[dict(subject='alice',organization='one')])
                self.assertEqual(two.catalog(),[dict(subject='alice',organization='two')])
                with self.assertRaises(PermissionError):RemoteRuleOperator(config,organization='foreign').catalog()
                identities.pop();save()
                with self.assertRaises(PermissionError):two.catalog()
                self.assertEqual(one.catalog(),[dict(subject='alice',organization='one')])
