"""Atraxis must remain a client: no engine, rule store or rule interpreter."""
import ast
import importlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from atraxis.rule_operator import ForwardApplication, RuleActor, load_operator_config
from atraxis.config import ConfigurationError


class RuleApplicationTests(unittest.TestCase):
    def setUp(self):
        self.operator = Mock()
        self.operator.entry.return_value = {'atropos_client':'engine.yml'}
        self.operator.relative.return_value = Path('/private/engine.yml')
        self.operator.actor.return_value = RuleActor('alice','org')
        self.application = ForwardApplication(self.operator)

    def test_each_operation_uses_a_remote_client_with_pinned_identity(self):
        with patch('atraxis.rule_operator.RuleClient') as client:
            self.application.read('rule','revision')
            client.assert_called_once_with(Path('/private/engine.yml'),expected_actor={'subject':'alice','organization':'org'})
            client.return_value.read.assert_called_once_with('rule','revision')
        self.operator.actor.return_value = RuleActor('bob','other')
        with patch('atraxis.rule_operator.RuleClient') as client:
            self.application.active('rule')
            self.assertEqual(client.call_args.kwargs['expected_actor'],{'subject':'bob','organization':'other'})

    def test_identity_rejection_precedes_outbound_connection(self):
        self.operator.entry.side_effect = PermissionError()
        with patch('atraxis.rule_operator.RuleClient') as client, self.assertRaises(PermissionError):
            self.application.catalog()
        client.assert_not_called()

    def test_template_creation_is_forwarded_without_local_interpretation(self):
        with patch('atraxis.rule_operator.RuleClient') as client:
            self.application.create_from_templates(['cpu-high'],name='rule',rule_kind='intervention',selections=[('container','web')])
            client.return_value.create_from_templates.assert_called_once()
            client.return_value.save_draft.assert_not_called()
            client.return_value.activate.assert_not_called()

    def test_atraxis_runtime_never_imports_engine_storage_or_zen(self):
        root = Path(importlib.import_module('atraxis').__file__).parent
        for path in root.glob('*.py'):
            for node in ast.walk(ast.parse(path.read_text())):
                names = ([node.module] if isinstance(node,ast.ImportFrom) else
                         [item.name for item in node.names] if isinstance(node,ast.Import) else [])
                self.assertFalse(any(name and name.split('.')[0] in ('atropos','redis','zen') for name in names),str(path))
        self.assertFalse(hasattr(importlib.import_module('atraxis.rules'),'build_atropos_rule_application'))

    def test_embedded_storage_profiles_are_rejected_for_explicit_migration(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'old.yml'
            path.write_text('version: 1\nstorage: {backend: redis, url: "redis://localhost"}\nidentities: []\n')
            path.chmod(0o600)
            with self.assertRaises(ConfigurationError):load_operator_config(path)

    def test_default_server_routes_remain_read_only(self):
        server=importlib.import_module('atraxis.server')
        self.assertEqual(server._METHODS,('GET','HEAD'))
        self.assertEqual({path for path,_ in server._ROUTES},{'healthz','readyz','v1/status'})
