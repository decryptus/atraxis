"""CLI/TUI connection to Atraxis; the independently packaged client owns transport."""
from atropos_client.client import (RuleClient, RemoteRuleError, RemoteConfig,
                                   load_remote_config, read_token)
from atropos_client.config import ConfigurationError as RuleClientConfigurationError


class RemoteRuleOperator(RuleClient):
    def __init__(self, path=None, *, overrides=None, organization=None):
        context = {} if organization is None else dict(organization=organization)
        super().__init__(path, service='atraxis', env_prefix='ATRAXIS', overrides=overrides, **context)
        self.observed_dispatch = RemoteObservedDispatcher(self)


class RemoteObservedDispatcher:
    def __init__(self, client):
        self.client = client

    def plan(self, name, target, binding):
        return self.client.observe_dispatch_plan(name, target, binding)

    def dispatch(self, name, target, binding, *, expected_generation, event_id):
        return self.client.observe_dispatch(name, target, binding,
                    expected_generation=expected_generation, event_id=event_id)
