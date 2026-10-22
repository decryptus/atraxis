"""Rule workbench state over the application API; independent of terminal frameworks."""
import hashlib
import json
import re
from pathlib import Path
from atraxis.rule_inputs import read_json, read_text

PAGE_SIZE = 20
MAX_TEST_BINDINGS = 64
TEST_BINDING_FIELDS = frozenset(('id', 'rule', 'revision', 'workflow', 'actions', 'test_supported'))
TEST_STATUSES = {'queued': 'info', 'running': 'running', 'retrying': 'running',
                 'succeeded': 'success', 'failed': 'failure', 'cancelled': 'failure',
                 'uncertain': 'warning', 'not_matched': 'warning'}
TEST_HASH = re.compile(r'[0-9a-f]{64}')
TEST_EVENT = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}')
TARGET_KINDS = frozenset(('server', 'container', 'server_group', 'container_group'))

class RuleWorkbench:
    def __init__(self, operator, name, cases=None):
        self.operator = operator
        self.app = operator.application
        self.name = name
        self.cases = cases
        self.offset = 0
        self.index = 0
        self.rows = []
        self.state = None
        self.reports = {}
        self.pending = None
        self.detail = []
        self.detail_offset = 0
        self.message = ''
        self.test_event = None
        self.test_context = None
        self.test_notice = 'info'


    def refresh(self):
        self.rows = self.app.history(self.name, self.offset, PAGE_SIZE)
        self.state = self.app.active(self.name)
        self.index = min(self.index, max(0, len(self.rows) - 1))
        self.pending = None
        self.detail = []
        self.detail_offset = 0
        self.message = 'Refreshed. Changes require an explicit review and confirmation.'


    def selected(self):
        if not self.rows:
            raise ValueError('no_revision_selected')
        return self.rows[self.index]['revision']


    def test_selected(self):
        if not self.cases:
            raise ValueError('scenario_file_required_use_cases_option')
        revision = self.selected()
        report = self.app.test(self.name, revision, read_json(self.cases))
        self.reports[revision] = report
        self.pending = None
        self.message = 'Tests passed.' if report['passed'] else 'Tests failed. Activation is unavailable.'
        self.detail_offset = 0
        self.detail = json.dumps(report['results'], ensure_ascii=True, indent=2).splitlines()


    def prepare(self, operation):
        if operation in ('archive', 'unarchive', 'delete'):
            return self.prepare_retirement(operation)
        if operation == 'deactivate':
            revision = self.state['revision']
            if revision is None:
                raise ValueError('rule_not_active')
            report = None
        else:
            revision = self.selected()
            report = self.reports.get(revision)
            if not report or not report['passed']:
                raise ValueError('run_passing_tests_for_this_revision_first')
        record = self.app.read(self.name, revision)
        self.pending = {'operation': operation, 'revision': revision,
                        'test_id': report['id'] if report else None,
                        'expected_generation': self.state['generation']}
        self.detail = [operation.upper() + ' ' + self.name, 'Revision: ' + revision,
                       'Current generation: ' + str(self.state['generation']),
                       'Domain: ' + record['context']['rule_kind'],
                       'Targets (%d):' % len(record['context']['targets'])] + [
                       '  %s: %s' % tuple(target) for target in record['context']['targets']] + [
                       'ENTER confirms this exact change. ESC cancels.']
        self.detail_offset = 0
        self.message = 'Review below. Left/Right scroll details; ENTER confirms, ESC cancels.'


    def confirm(self):
        if self.pending is None:
            return
        pending = self.pending
        self.pending = None  # Never retry a mutation automatically, including after an error.
        if pending['operation'] == 'test_dispatch':
            return self._submit_test(pending)
        if pending['operation'] in ('archive', 'unarchive', 'delete'):
            return self.confirm_retirement(pending)
        if pending['operation'] == 'deactivate':
            self.app.deactivate(self.name, expected_generation=pending['expected_generation'])
        else:
            getattr(self.app, pending['operation'])(self.name, pending['revision'],
                test_id=pending['test_id'], expected_generation=pending['expected_generation'])
        self.refresh()
        self.message = 'Change saved. Decision evaluation remains separate from action execution.'


    def import_file(self, path):
        head = self.app.draft(self.name)
        suffix = Path(path).suffix.lower()
        if suffix not in ('.yml', '.yaml', '.json'):
            raise ValueError('invalid_rule_file_extension')
        self.app.import_draft(read_text(path), format='json' if suffix == '.json' else 'yaml',
             rule_kind=head['context']['rule_kind'], selections=head['context']['targets'],
             expected_revision=head['revision'], message='Imported from terminal workbench')
        self.refresh()
        self.message = 'New draft saved; the active version was not changed.'


    def observe(self, target):
        result = self.operator.live.evaluate(self.name, target)
        self.pending = None
        self.detail_offset = 0
        self.detail = json.dumps(result, ensure_ascii=True, indent=2).splitlines()
        self.message = 'Fresh observation evaluated. No action was executed.'


    def show(self, view):
        if view == 'audit':
            value = self.app.audit(self.name, self.offset, PAGE_SIZE)
        elif view == 'catalog':
            value = self.app.catalog()
        else:
            value = self.app.read(self.name, self.selected())
        self.pending = None
        self.detail_offset = 0
        self.detail = json.dumps(value, ensure_ascii=True, indent=2).splitlines()



    def prepare_test(self, target, binding, facts_path, event_id):
        # Read once: confirmation submits the exact facts reviewed here, even if
        # the source file changes while the modal is open.
        self.pending = None
        if (not isinstance(target, (tuple, list)) or len(target) != 2
                or target[0] not in TARGET_KINDS or not isinstance(target[1], str)
                or not target[1] or not isinstance(binding, str) or not TEST_HASH.fullmatch(binding)
                or not isinstance(event_id, str) or not TEST_EVENT.fullmatch(event_id)):
            raise ValueError('invalid_test_request')
        facts = read_json(facts_path)
        if type(facts) is not dict:
            raise ValueError('test_facts_must_be_object')
        preview = self.app.simulate_dispatch(self.name, list(target), facts, binding)
        if (type(preview) is not dict or preview.get('simulation') is not True
                or preview.get('binding') != binding or preview.get('target') != list(target)
                or type(preview.get('matches_binding')) is not bool
                or not isinstance(preview.get('revision'), str)):
            raise ValueError('invalid_test_preview')
        self.detail_offset = 0
        self.detail = ['END-TO-END TEST · ' + self.name,
                       'Only registered simulators run. No real intervention.',
                       'An approved receiver may receive a labelled test alert.',
                       'Revision: ' + preview['revision'], 'Target: %s:%s' % tuple(target),
                       'Binding: ' + binding, 'Test event: ' + event_id,
                       'Facts SHA-256: ' + hashlib.sha256(json.dumps(facts, sort_keys=True,
                           ensure_ascii=True, allow_nan=False).encode()).hexdigest(),
                       'This does not activate a rule or create activation evidence.']
        self.test_notice = 'warning' if not preview['matches_binding'] else 'info'
        if not preview['matches_binding']:
            self.message = 'Decision does not match this binding. No test was submitted.'
            return False
        self.pending = {'operation': 'test_dispatch', 'target': list(target),
                        'binding': binding, 'facts': facts, 'event_id': event_id,
                        'revision': preview['revision']}
        self.message = 'Review the test before submitting it to the isolated queue.'
        return True

    def _submit_test(self, pending):
        self.test_event, self.test_context = pending['event_id'], None
        # Keep the recovery identity before the mutation; never resubmit on a
        # timeout or malformed response. Status uses the read-only test API.
        self.message = 'Test outcome unconfirmed. Check status for event ' + self.test_event + '.'
        self.test_notice = 'warning'
        self.detail = ['END-TO-END TEST', 'Test event: ' + self.test_event,
                       'Submission outcome unconfirmed; use Test status. Do not resubmit.']
        try:
            receipt = self.app.test_dispatch(self.name, pending['target'], pending['facts'],
                                            pending['binding'], event_id=self.test_event)
        except ValueError as error:
            if str(error) != 'rule_queue_full':
                raise
            self.message = 'Test queue full. New test was not admitted; no automatic retry.'
            self.detail = ['END-TO-END TEST', 'Test event: ' + self.test_event, self.message]
            return
        self._test_receipt(receipt, pending['revision'])

    def test_status(self, event_id):
        if not isinstance(event_id, str) or not TEST_EVENT.fullmatch(event_id):
            raise ValueError('invalid_test_event')
        if event_id != self.test_event:
            self.test_context = None
        self.test_event = event_id
        self._test_receipt(self.app.test_dispatch_status(self.name, event_id))

    def _test_receipt(self, receipt, revision=None):
        context = receipt.get('test_context') if type(receipt) is dict else None
        if (type(context) is not dict or set(context) != {'test', 'id'}
                or context['test'] is not True or not isinstance(context['id'], str)
                or not TEST_HASH.fullmatch(context['id'])
                or receipt.get('status') not in TEST_STATUSES
                or (self.test_context is not None and context != self.test_context)
                or (revision is not None and receipt.get('revision') != revision)
                or (receipt['status'] != 'not_matched' and
                    (receipt.get('rule') != self.name or receipt.get('id') != context['id']))):
            raise ValueError('invalid_test_receipt')
        self.test_context = dict(context)
        self.test_notice = TEST_STATUSES[receipt['status']]
        self.message = 'TEST ' + receipt['status'].upper() + ' · event ' + self.test_event
        self.detail_offset = 0
        self.detail = ['END-TO-END TEST · ' + self.name,
                       'Status: ' + receipt['status'].upper(), 'Test event: ' + self.test_event,
                       'Revision: ' + str(receipt.get('revision', '')), '',
                       'Test results; no real intervention.']
        for event in receipt.get('history', []):
            self.detail.append('After step %s: workflow %s' % (event.get('step', ''), event.get('status', '')))
        for step, result in receipt.get('results', {}).items():
            alert = result.get('alert') if isinstance(result, dict) else None
            if isinstance(alert, dict):
                self.detail.append('Alert: ' + str(alert.get('subject', '[TEST]')))
                self.detail.append('Delivery: ' + str(alert.get('delivery', 'unconfirmed')))
        self.detail += ['', 'Full test receipt:']
        self.detail += json.dumps(receipt, ensure_ascii=True, indent=2).splitlines()

    def available_test_bindings(self, target):
        if (not isinstance(target, (list, tuple)) or len(target) != 2
                or target[0] not in ('server', 'container') or not isinstance(target[1], str)
                or not TEST_EVENT.fullmatch(target[1])):
            raise ValueError('invalid_test_target')
        records = self.app.test_bindings(self.name, list(target))
        if type(records) is not list or len(records) > MAX_TEST_BINDINGS:
            raise ValueError('invalid_test_bindings')
        seen = set()
        for row in records:
            if (type(row) is not dict or set(row) != TEST_BINDING_FIELDS
                    or not isinstance(row['id'], str) or not TEST_HASH.fullmatch(row['id'])
                    or row['id'] in seen or row['rule'] != self.name
                    or any(not isinstance(row[key], str) or not 1 <= len(row[key]) <= 128
                           for key in ('revision', 'workflow'))
                    or type(row['test_supported']) is not bool
                    or type(row['actions']) is not list or not 1 <= len(row['actions']) <= 64
                    or any(not isinstance(action, str) or not 1 <= len(action) <= 128 for action in row['actions'])):
                raise ValueError('invalid_test_bindings')
            seen.add(row['id'])
        return records

    def prepare_retirement(self, operation):
        head = self.app.draft(self.name)
        state = self.app.availability(self.name)
        if state['status'] == 'deleted': raise ValueError('rule_deleted')
        self.pending = dict(operation=operation, revision=head['revision'], expected_generation=state['generation'])
        description = {'archive': 'Preserves history and audit. The rule must first be inactive.',
                       'unarchive': 'Makes the rule available. Does not activate it.',
                       'delete': 'Permanently removes draft contents and test reports. Audit remains. Never-activated, unlinked drafts only.'}
        self.detail = [operation.upper()+' '+self.name, 'Current status: '+state['status'],
                       'Draft revision: '+head['revision'], description[operation],
                       'Atropos checks this permission for every historical scope.',
                       'ENTER confirms this exact change. ESC cancels.']
        self.detail_offset = 0
        self.message = 'Review this rule lifecycle change before confirming.'

    def confirm_retirement(self, pending):
        getattr(self.app, pending['operation'])(self.name, expected_revision=pending['revision'],
                                              expected_generation=pending['expected_generation'])
        if pending['operation'] == 'delete':
            self.rows=[]; self.state=None
            self.show('audit')
            self.message='Draft contents deleted. Audit preserved; this rule name remains reserved.'
        else:
            self.refresh()
            self.message='Rule '+('archived.' if pending['operation']=='archive' else 'available; not activated.')
