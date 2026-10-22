"""Optional Textual rule workbench; lifecycle intelligence stays in Atropos."""
import sys
import uuid

from dwho.tui.textual import Confirmation, DetailPanel, InputForm, ServiceDashboard, TableRow, plain_text
from textual.containers import Vertical, Horizontal
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Static
from rich.text import Text

from atraxis.rule_workbench import PAGE_SIZE, RuleWorkbench

OPERATIONS = ('activate', 'rollback', 'deactivate')


class TestRunForm(InputForm):
    """Keep confirmation controls visible while four fields scroll in short terminals."""
    DEFAULT_CSS = '''
    TestRunForm .dw-form { height: 90%; max-height: 36; }
    TestRunForm .dw-fields { height: 1fr; }
    '''


class BindingPicker(ModalScreen):
    """Select server-filtered metadata; selection itself grants no permission."""
    DEFAULT_CSS = '''
    BindingPicker { align: center middle; background: #000000 65%; }
    BindingPicker .binding-dialog { width: 110; max-width: 95%; height: 80%;
        padding: 1 2; background: $panel; border: round $primary; }
    BindingPicker Static { height: auto; margin-bottom: 1; }
    BindingPicker DataTable { height: 1fr; }
    BindingPicker Horizontal { height: 3; align: right middle; margin-top: 1; }
    BindingPicker Button { margin-left: 1; }
    '''
    BINDINGS = [('escape', 'cancel', 'Cancel')]

    def __init__(self, records, target):
        super().__init__()
        self.records, self.target = records, target

    def compose(self):
        with Vertical(classes='binding-dialog'):
            yield Static('Authorized TEST workflows · ' + plain_text(self.target), markup=False)
            yield DataTable(id='test-bindings', cursor_type='row', classes='dw-table')
            yield Static('Choose a workflow. Preview and confirmation come next.', id='binding-notice', markup=False)
            with Horizontal():
                yield Button('Cancel', id='binding-cancel')
                yield Button('Choose', id='binding-select', variant='primary')

    def on_mount(self):
        table = self.query_one(DataTable)
        table.add_columns('WORKFLOW', 'REVISION', 'SIMULATOR', 'ACTIONS')
        for row in self.records:
            table.add_row(*(Text(plain_text(value)) for value in
                (row['workflow'], (row['revision'] if len(row['revision']) <= 24 else row['revision'][:12] + '…'), 'Available' if row['test_supported'] else 'Unavailable',
                 ', '.join(row['actions']))), key=row['id'])
        table.focus()

    def choose(self):
        row = self.records[self.query_one(DataTable).cursor_row]
        if not row['test_supported']:
            self.query_one('#binding-notice', Static).update('Simulator unavailable. This workflow cannot be tested here.')
            return
        self.dismiss(dict(row))

    def on_data_table_row_selected(self, event):
        event.stop(); self.choose()

    def on_button_pressed(self, event):
        event.stop()
        if event.button.id == 'binding-select':
            self.choose()
        else:
            self.action_cancel()

    def action_cancel(self):
        self.dismiss(None)


class RuleApp(ServiceDashboard):
    BINDINGS = ServiceDashboard.BINDINGS + [
        ('r', 'refresh', 'Refresh'), ('t', 'test', 'Test'), ('a', 'activate', 'Activate'),
        ('b', 'rollback', 'Rollback'), ('d', 'deactivate', 'Deactivate'),
        ('shift+a', 'archive', 'Archive'), ('shift+u', 'unarchive', 'Unarchive'),
        ('shift+d', 'delete_rule', 'Delete draft'),
        ('i', 'import_file', 'Import'), ('o', 'observe', 'Observe'),
        ('e', 'test_run', 'End-to-end test'), ('s', 'test_status', 'Test status'),
        ('pagedown', 'next_page', 'Next page'), ('pageup', 'previous_page', 'Previous page')]

    def __init__(self, operator, name, cases=None, demo=False):
        super().__init__(product='Atraxis', heading='Rule workbench · ' + name,
                         columns=('REVISION', 'AUTHOR', 'STATE'), mode='interactive',
                         navigation=(('revision', 'Revision'), ('audit', 'Audit'), ('catalog', 'Templates'),
                                     ('test_run', 'End-to-end test'), ('test_status', 'Test status')),
                         subtitle='SYNTHETIC DEMO' if demo else 'ATROPOS RULE LIFECYCLE')
        self.model = RuleWorkbench(operator, name, cases)
        self._rendered_versions = None

    def on_mount(self):
        self.action_refresh()

    def action_refresh(self):
        if self.model.pending is None:
            self.perform(self.model.refresh, self.render_model)

    def render_model(self, result=None):
        active = self.model.state['revision'] if self.model.state else None
        rows = [TableRow(row['revision'], (row['revision'], row['author'],
                          'ACTIVE' if row['revision'] == active else 'DRAFT / HISTORY'),
                          row['revision'], 'Press t to test this exact revision before activation.')
                           for row in self.model.rows]
        if rows != self._rendered_versions:
            self.display_rows(rows)
            self._rendered_versions = rows
        self.set_notice('info', self.model.message)
        self.set_activity('Active: %s | page offset %s\nTests and observations do not execute interventions.' % (active or 'inactive', self.model.offset))
        if self.model.detail:
            self.query_one(DetailPanel).show_details(self.model.name, '\n'.join(self.model.detail))

    def select_revision(self):
        if self.busy or self.model.pending is not None:
            return False
        table = self.query_one('.dw-table')
        if table.cursor_row >= len(self._visible_keys):
            self.set_notice('warning', 'Select a visible revision first.')
            return False
        revision = self._visible_keys[table.cursor_row]
        self.model.index = next(index for index, row in enumerate(self.model.rows) if row['revision'] == revision)
        return True

    def on_dashboard_app_navigation_requested(self, event):
        if event.key in ('test_run', 'test_status'):
            getattr(self, 'action_' + event.key)()
            return
        if self.select_revision() or (event.key != 'revision' and not self.busy and self.model.pending is None):
            self.perform(lambda: self.model.show(event.key), self.render_model)

    def on_data_table_row_selected(self, event):
        if self.select_revision():
            self.perform(lambda: self.model.show('revision'), self.render_model)

    def action_test(self):
        if not self.select_revision():
            return
        def done(result):
            self.render_model()
            passed = self.model.reports[self.model.selected()]['passed']
            self.set_notice('success' if passed else 'failure', self.model.message)
        self.perform(self.model.test_selected, done, label='Running synthetic rule cases; no intervention...')

    def prepare(self, operation):
        if operation == 'deactivate':
            if self.busy or self.model.pending is not None or self.model.state is None:
                return
        elif not self.select_revision():
            return
        def reviewed(result):
            self.push_screen(Confirmation('Confirm ' + operation, '\n'.join(self.model.detail[:-1])), self.confirmed)
        self.perform(lambda: self.model.prepare(operation), reviewed, label='Reading the exact revision and active generation...')

    def confirmed(self, accepted):
        if self.model.pending is None:
            return
        if not accepted:
            self.model.pending = None
            self.set_notice('info', 'Change cancelled.')
            return
        if self.model.pending['operation'] == 'test_dispatch':
            self.perform(self.model.confirm, self.render_test_result, self.test_submission_failed,
                         label='Submitting the reviewed test to the isolated queue...')
        else:
            self.perform(self.model.confirm, self.render_model, label='Applying the reviewed change...')

    def action_activate(self):
        self.prepare('activate')

    def action_rollback(self):
        self.prepare('rollback')

    def action_deactivate(self):
        self.prepare('deactivate')

    def action_archive(self):
        self.prepare('archive')

    def action_unarchive(self):
        self.prepare('unarchive')

    def action_delete_rule(self):
        self.prepare('delete')

    def action_import_file(self):
        if self.busy or self.model.pending is not None:
            return
        def received(values):
            if values is not None:
                self.push_screen(Confirmation('Import draft', 'File: ' + values['path'] + '\nSaves a new draft; does not activate it.'),
                    lambda yes: self.perform(lambda: self.model.import_file(values['path']), self.render_model,
                                            label='Importing draft...') if yes else None)
        self.push_screen(InputForm('Import rule file', [('path', 'YAML or JSON file', '', False)]), received)

    def action_observe(self):
        if self.busy or self.model.pending is not None:
            return
        def received(values):
            if values is None:
                return
            kind, separator, identity = values['target'].partition(':')
            if not separator or kind not in ('server', 'container', 'server_group', 'container_group') or not identity:
                self.set_notice('warning', 'Expected kind:identity.')
                return
            self.perform(lambda: self.model.observe((kind, identity)), self.render_model,
                         label='Evaluating a fresh observation; no intervention...')
        self.push_screen(InputForm('Observe target', [('target', 'Target kind:identity', '', False)]), received)

    def render_test_result(self, result=None):
        self.render_model()
        self.set_notice(self.model.test_notice, self.model.message)
        self.set_activity('END-TO-END TEST | event: %s\nOnly registered simulators; approved test alert delivery may occur.' % (self.model.test_event or 'not submitted'))

    def test_submission_failed(self, error):
        self.render_test_result()
        self.set_notice('warning', 'Test submission not confirmed. Use Test status for event %s; do not resubmit.' % self.model.test_event)

    def test_read_failed(self, error):
        self.set_notice('warning', 'Test request did not complete. Check inputs and test permissions; no automatic retry.')

    def prepare_test_run(self, values):
        if values is None or self.busy or self.model.pending is not None:
            return
        kind, separator, identity = values['target'].partition(':')
        if not separator:
            self.set_notice('warning', 'Expected kind:identity.')
            return
        def reviewed(ready):
            self.render_test_result()
            if ready:
                self.push_screen(Confirmation('Confirm end-to-end TEST', '\n'.join(self.model.detail)), self.confirmed)
        self.perform(lambda: self.model.prepare_test((kind, identity), values['binding'],
                     values['facts'], values['event']), reviewed, self.test_read_failed,
                     label='Previewing the test decision; no queue submission...')

    def action_test_run(self):
        if self.busy or self.model.pending is not None:
            return
        def received(values):
            if values is None:
                return
            kind, separator, identity = values['target'].partition(':')
            if not separator:
                self.set_notice('warning', 'Expected server:identity or container:identity.')
                return
            target = (kind, identity)
            def listed(records):
                if not records:
                    self.set_notice('info', 'No authorized test workflows for this rule and target.')
                    return
                self.push_screen(BindingPicker(records, values['target']),
                                 lambda selected: self.test_binding_selected(values['target'], selected))
            self.perform(lambda: self.model.available_test_bindings(target), listed, self.test_read_failed,
                         label='Reading authorized test workflows...')
        self.push_screen(InputForm('Choose test target', [
            ('target', 'Target server:identity or container:identity', '', False)],
            hint='Only workflows permitted by your current test rights are listed.', submit='List workflows'), received)

    def test_binding_selected(self, target, selected):
        if selected is None:
            return
        def received(values):
            if values is not None:
                self.prepare_test_run(dict(values, target=target, binding=selected['id']))
        self.push_screen(TestRunForm('End-to-end TEST', [
            ('facts', 'Synthetic facts JSON file', '', False),
            ('event', 'Stable test event ID (keep it for status recovery)', uuid.uuid4().hex, False)],
            hint='Target: %s\nWorkflow: %s\nRevision: %s\nPreview and confirmation follow; permissions are checked again.' %
                 (target, selected['workflow'], selected['revision'])), received)

    def action_test_status(self):
        if self.busy or self.model.pending is not None:
            return
        def received(values):
            if values is not None:
                self.perform(lambda: self.model.test_status(values['event']), self.render_test_result,
                             self.test_read_failed, label='Reading test status; no submission...')
        self.push_screen(InputForm('Read TEST status', [
            ('event', 'Test event ID', self.model.test_event or '', False)],
            hint='Read-only recovery. Use the same rule, account and test event ID.', submit='Read status'), received)

    def page(self, delta):
        if self.busy or self.model.pending is not None:
            return
        self.model.offset = max(0, self.model.offset + delta)
        self.action_refresh()

    def action_next_page(self):
        self.page(PAGE_SIZE)

    def action_previous_page(self):
        self.page(-PAGE_SIZE)


def run(operator, name, cases=None):
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise ValueError('terminal_required')
    RuleApp(operator, name, cases).run()
