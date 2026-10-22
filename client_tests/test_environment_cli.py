"""Exercise the public CLI entry point and its Textual adapter selection."""
from contextlib import redirect_stderr
import io
import os
import types
import unittest
from unittest.mock import Mock, patch

from atraxis.cli import main


class EnvironmentCliTests(unittest.TestCase):
    def test_remote_environment_reaches_textual_with_cli_precedence(self):
        # The real widget lifecycle has its own textual_tests root. Here the
        # CLI must build the same remote client and select that adapter.
        renderer = Mock()
        module = types.ModuleType('atraxis.rule_textual'); module.run = renderer
        env = {'ATRAXIS_SERVER_URL':'https://environment.example',
               'ATRAXIS_TOKEN':'a'*43, 'ATRAXIS_TIMEOUT':'12',
               'ATROPOS_SERVER_URL':'https://engine-must-not-be-used.example'}
        with patch.dict(os.environ,env,clear=True), patch.dict('sys.modules',{'atraxis.rule_textual':module}):
            result=main(['rules','--remote','--server-url','https://explicit.example',
                         '--request-timeout','20','tui','--name','cpu','--ui','textual'])
        self.assertEqual(result,0)
        renderer.assert_called_once()
        client,name,cases=renderer.call_args.args
        self.assertEqual((name,cases),('cpu',None))
        self.assertEqual((client.config.origin,client.config.timeout,client.config.service),
                         ('https://explicit.example',20,'atraxis'))

    def test_local_operator_rejects_remote_overrides_before_connecting(self):
        errors=io.StringIO()
        with patch('atraxis.rule_operator.LocalRuleOperator') as operator, redirect_stderr(errors):
            result=main(['rules','--operator-config','unused.yml',
                         '--server-url','https://unexpected.example','catalog'])
        self.assertEqual(result,2)
        operator.assert_not_called()
        self.assertNotIn('unexpected.example',errors.getvalue())

    def test_no_profile_requires_explicit_remote_selection(self):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
            main(['rules','catalog'])
        self.assertEqual(caught.exception.code,2)
