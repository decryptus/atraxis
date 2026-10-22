"""Local Textual grant editor using the same bounded service as the web."""
import sys
from dwho.tui.textual import ServiceDashboard, TableRow, InputForm, Confirmation, DetailPanel
from atraxis.inventory_admin import GRANT_FORM_FIELDS, request_from_fields


def describe_targets(targets):
    return '\n'.join(t['kind'] + ': ' + (t['source'] + ' / ' if 'source' in t else '') + t['id'] for t in targets)


class GrantForm(InputForm):
    DEFAULT_CSS = '''
    GrantForm .dw-form { height: 90%; max-height: 38; }
    GrantForm .dw-fields { height: 1fr; }
    '''


class InventoryAdminApp(ServiceDashboard):
    BINDINGS = ServiceDashboard.BINDINGS + [
        ('r', 'refresh', 'Refresh'), ('a', 'add_grant', 'Add grant'), ('d', 'remove_grant', 'Remove grant')]

    def __init__(self, admin):
        super().__init__(product='Atraxis', heading='Inventory read access',
                         columns=('ORGANIZATION', 'RECIPIENT', 'TARGETS'), mode='interactive',
                         subtitle='SCOPED INVENTORY ADMINISTRATION',
                         navigation=(('scope', 'Your permissions'), ('add', 'Add grant'),
                                     ('remove', 'Remove grant'), ('audit', 'Recent changes')))
        self.admin = admin
        self.data = None
        self.pending = None

    def on_mount(self):
        self.action_refresh()

    def failed(self, error):
        self.pending = self.data = None
        self.display_rows([])
        self.query_one(DetailPanel).show_details('Inventory read access', 'Refresh to check current access.')
        self.set_activity('No automatic retry. Check current grants and recent changes before trying again.')
        self.set_notice('error', 'Operation unconfirmed or denied. Refresh before trying again.')

    def action_refresh(self):
        if not self.busy and self.pending is None:
            self.perform(self.admin.inspect, self.render_view, self.failed)

    def render_view(self, data):
        self.data = data
        rows = []
        for org in data['organizations']:
            for grant in org['grants']:
                detail = 'Organization: ' + org['id'] + '\nRecipient: ' + grant['subject_kind'] + ': ' + grant['subject']
                detail += '\n\n' + describe_targets(grant['targets']) + '\n\nGrant ID: ' + grant['id']
                rows.append(TableRow(grant['id'], (org['id'], grant['subject_kind'] + ': ' + grant['subject'],
                                     str(len(grant['targets']))), 'Inventory grant', detail))
        self.display_rows(rows)
        self.set_notice('info', 'Account: ' + data['subject'])
        self.set_activity('a Add · d Remove · r Refresh\nEvery change requires review. Engine permissions remain separate.')
        self.show_scope()

    def show_scope(self):
        if self.data is not None:
            self.query_one(DetailPanel).show_details('Your administration scope', '\n\n'.join(
                o['name'] + ' (' + o['id'] + ')\n' + describe_targets(o['targets']) for o in self.data['organizations']))

    def show_audit(self):
        if self.data is not None:
            self.query_one(DetailPanel).show_details('Recent changes in your scope', '\n\n'.join(
                e['time'] + ' · ' + e['actor'] + '\n' + e['organization'] + ' · ' + e['operation'] +
                ' · ' + e['grant']['subject'] + '\n' + describe_targets(e['grant']['targets'])
                for e in reversed(self.data['audit'])) or 'No recorded changes in this scope.')

    def on_dashboard_app_navigation_requested(self, event):
        action = {'scope': self.show_scope, 'audit': self.show_audit,
                  'add': self.action_add_grant, 'remove': self.action_remove_grant}.get(event.key)
        if action:
            action()

    def action_add_grant(self):
        self.begin('grant.add')

    def action_remove_grant(self):
        self.begin('grant.remove')

    def begin(self, operation):
        if self.busy or self.pending is not None or self.data is None:
            return
        revision = self.data['revision']
        fields = GRANT_FORM_FIELDS if operation == 'grant.add' else (('grant', 'Exact grant ID from row details', ''),)
        def received(values):
            if values is None:
                return
            def prepare():
                request = request_from_fields(values) if operation == 'grant.add' else dict(operation=operation, **values)
                return self.admin.prepare(request, revision)
            self.perform(prepare, self.review, self.failed, label='Checking current inventory authority...')
        self.push_screen(GrantForm('Add read grant' if operation == 'grant.add' else 'Remove read grant',
            [(name, label, value, False) for name, label, value in fields],
            hint='Use IDs from your scope. Review follows; no changes yet.', submit='Review'), received)

    def review(self, plan):
        self.pending = plan
        grant = plan['grant']
        details = ('Acting account: ' + plan['identity']['subject'] + '\nOperation: ' + plan['request']['operation'] +
                   '\nOrganization: ' + grant['organization'] + '\nRecipient: ' + grant['subject_kind'] + ': ' + grant['subject'] +
                   '\n\n' + describe_targets(grant['targets']) +
                   '\n\nSource/server grants include all source resources and metadata.\nRemoving one grant preserves overlapping grants.')
        self.push_screen(Confirmation('Confirm inventory access change', details, confirm_label='Apply',
                         hint='Current rights and revisions are checked again when saving.'), self.confirm)

    def confirm(self, accepted):
        plan = self.pending
        self.pending = None
        if accepted and plan is not None:
            self.perform(lambda: self.admin.commit(plan), lambda result: self.action_refresh(), self.failed,
                         label='Saving inventory access change...')


def run(admin):
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise ValueError('interactive_terminal_required')
    InventoryAdminApp(admin).run()
    return 0
