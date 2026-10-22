"""Synthetic independent client responses for simulation browser forms."""
from copy import deepcopy
from web_editor_fixture import EditorFake, REVISION

BINDING = 'e' * 64
RUN = 'f' * 64


class SimulationFake(EditorFake):
    def test_bindings(self, name, target):
        if getattr(self.owner, 'denied', False):
            raise PermissionError()
        return deepcopy(getattr(self.owner, 'bindings', [dict(id=BINDING, rule=name, revision=REVISION,
            workflow='notify', actions=['ops.run', 'ops.notify'], test_supported=True)]))

    def test_dispatch(self, name, target, facts, binding, *, event_id):
        self.owner.calls.append(('test_dispatch', self.actor, name, target, facts, binding, event_id))
        if self.owner.error:
            raise self.owner.error
        if self.owner.revoke:
            self.owner.write_mapping([])
        result = dict(id=RUN, rule=name, revision=REVISION, status='queued', history=[],
                      error=None, results={}, test_context=dict(test=True, id=RUN))
        result.update(getattr(self.owner, 'response', {}))
        return result
