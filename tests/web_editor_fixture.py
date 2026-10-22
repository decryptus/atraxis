"""Synthetic Atropos boundary shared by HTTP and Chromium acceptance."""
from copy import deepcopy
import hashlib
import json
from test_web_rules import FakeClient, REVISION

NEW_REVISION = 'c'*32
REPORT = 'd'*32
ORIGIN = 'https://console.example.test'
CASES = [dict(name='High CPU', input=dict(cpu_percent=95), expected=dict(decision='review', matched_rule='high_cpu'))]


class EditorFake(FakeClient):
    def read(self, name, revision):
        result = super().read(name, revision)
        if revision == NEW_REVISION and hasattr(self.owner, 'saved_document'):
            result['document'] = deepcopy(self.owner.saved_document)
        result['context'] = dict(version=1, rule_kind='supervision', targets=[['server', 'lab']])
        for field, value in [('content_sha256', result['document']), ('context_sha256', result['context'])]:
            result[field] = hashlib.sha256(json.dumps(value,sort_keys=True).encode()).hexdigest()
        return result

    def draft(self, name):
        result = self.read(name, self.owner.head)
        result['context'] = dict(version=1, rule_kind='supervision', targets=[['server', 'lab']])
        return result

    def save_draft(self, document, **values):
        self.owner.calls.append(('save_draft', self.actor, deepcopy(document), deepcopy(values)))
        if self.owner.error:
            raise self.owner.error
        if values['expected_revision'] != self.owner.head:
            raise ValueError('rule_conflict')
        self.owner.head = NEW_REVISION
        self.owner.saved_document = deepcopy(document)
        if self.owner.revoke:
            self.owner.write_mapping([])
        return dict(revision=NEW_REVISION, document=document)

    def test(self, name, revision, cases):
        self.owner.calls.append(('test', self.actor, name, revision, cases))
        if self.owner.error:
            raise self.owner.error
        record = self.read(name, revision)
        self.owner.report = dict(id=REPORT, revision=revision, passed=True,
            content_sha256=record['content_sha256'], context_sha256=record['context_sha256'],
            results=[dict(name=case['name'], passed=True, actual=case['expected']) for case in cases])
        return self.owner.report

    def test_report(self, name, revision, report_id):
        self.owner.calls.append(('test_report', self.actor, name, revision, report_id))
        if self.owner.error:
            raise self.owner.error
        return self.owner.report




class ActivationFake(EditorFake):
    def active(self, name):
        self.owner.calls.append(('active', self.actor, name))
        return deepcopy(getattr(self.owner, 'active_state', dict(revision=REVISION,generation='b'*32)))

    def activate(self, name, revision, *, test_id, expected_generation):
        self.owner.calls.append(('activate', self.actor, name, revision,
                                 dict(test_id=test_id,expected_generation=expected_generation)))
        if self.owner.error:
            raise self.owner.error
        current=getattr(self.owner,'active_state',dict(revision=REVISION,generation='b'*32))
        if current['generation'] != expected_generation:
            raise ValueError('rule_conflict')
        if self.owner.report['id'] != test_id or not self.owner.report['passed']:
            raise ValueError('invalid_rule_request')
        self.owner.active_state=dict(revision=revision,generation='f'*32,test_id=test_id)
        if self.owner.revoke:
            self.owner.write_mapping([])
        if getattr(self.owner,'malformed_activation',False):
            return dict(revision=REVISION,generation='f'*32,test_id=test_id)
        return deepcopy(self.owner.active_state)


class LifecycleFake(ActivationFake):
    """Deterministic API fixture, never an alternate rule engine."""
    def activate(self, name, revision, *, test_id, expected_generation):
        return self._change('activate', name, revision, test_id, expected_generation)

    def rollback(self, name, revision, *, test_id, expected_generation):
        return self._change('rollback', name, revision, test_id, expected_generation)

    def deactivate(self, name, *, expected_generation):
        return self._change('deactivate', name, None, None, expected_generation)

    def _change(self, operation, name, revision, test_id, expected_generation):
        values = dict(expected_generation=expected_generation)
        if operation != 'deactivate':
            values['test_id'] = test_id
        self.owner.calls.append((operation, self.actor, name, revision, values))
        if self.owner.error:
            raise self.owner.error
        previous = deepcopy(getattr(self.owner, 'active_state', dict(revision=REVISION, generation='b'*32)))
        if previous['generation'] != expected_generation:
            raise ValueError('rule_conflict')
        if operation == 'deactivate':
            if previous['revision'] is None:
                raise ValueError('rule_conflict')
        else:
            report = self.owner.report
            if report['id'] != test_id or report['revision'] != revision or not report['passed']:
                raise ValueError('invalid_rule_request')
            if operation == 'rollback' and revision not in getattr(self.owner, 'was_active', {REVISION}):
                raise ValueError('invalid_rule_request')
        events = getattr(self.owner, 'events', [])
        generation = '%032x' % (len(events)+1)
        event = dict(generation=generation, previous_generation=previous['generation'], operation=operation,
            author=self.actor['subject'], organization=self.actor['organization'], created_at=1791360000+len(events),
            previous_revision=previous['revision'], revision=revision, test_id=test_id,
            scope_revisions=sorted({r for r in (previous['revision'], revision) if r}))
        events.append(event)
        self.owner.events = events
        self.owner.was_active = getattr(self.owner, 'was_active', {REVISION}) | ({revision} if revision else set())
        self.owner.active_state = dict(revision=revision, generation=generation, test_id=test_id)
        if self.owner.revoke:
            self.owner.write_mapping([])
        if getattr(self.owner, 'malformed_lifecycle', False):
            return dict(revision=revision, generation=expected_generation, test_id=test_id)
        return deepcopy(self.owner.active_state)

    def audit(self, name, offset=0, limit=50):
        self.owner.calls.append(('audit', self.actor, name, offset, limit))
        if self.owner.error:
            raise self.owner.error
        if getattr(self.owner, 'revoke_audit', False):
            self.owner.write_mapping([])
        return deepcopy(getattr(self.owner, 'events', [])[offset:offset+limit])


class RetirementLifecycleFake(LifecycleFake):
    """Deterministic response fixture; production eligibility remains engine-owned."""
    def availability(self,name):
        self.owner.calls.append(('availability',self.actor,name))
        return deepcopy(getattr(self.owner,'availability',dict(status='available',generation=None)))

    def _retire(self,operation,name,**values):
        import uuid
        self.owner.calls.append((operation,self.actor,name,values))
        previous=self.availability(name)
        if values['expected_revision']!=self.owner.head or values['expected_generation']!=previous['generation']:
            raise ValueError('rule_conflict')
        result=dict(status={'archive':'archived','unarchive':'available','delete':'deleted'}[operation],
                    revision=self.owner.head,generation=uuid.uuid4().hex)
        self.owner.availability=result
        self.owner.events.append(dict(operation=operation,generation=result['generation'],previous_generation=previous['generation'],
            author=self.actor['subject'],organization=self.actor['organization'],created_at=1791385200,
            revision=self.owner.head,scope_revisions=[self.owner.head]))
        return deepcopy(result)

    def archive(self,name,**values):return self._retire('archive',name,**values)
    def unarchive(self,name,**values):return self._retire('unarchive',name,**values)
    def delete(self,name,**values):return self._retire('delete',name,**values)
