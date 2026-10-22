"""Optional Textual account workbench sharing the scoped administration service."""
import json
import sys
from dwho.tui.textual import ServiceDashboard,TableRow,InputForm,Confirmation,DetailPanel
from atraxis.account_admin import request_from_fields
from atraxis.account_forms import FIELDS,LABELS


class AccountForm(InputForm):
    DEFAULT_CSS = '''
    AccountForm .dw-form { height: 90%; max-height: 38; }
    AccountForm .dw-fields { height: 1fr; }
    '''


class AccountApp(ServiceDashboard):
    BINDINGS=ServiceDashboard.BINDINGS+[
        ('r','refresh','Refresh'),('a','add_member','Add member'),('d','remove_member','Remove member'),
        ('g','delegate','Delegate'),('x','revoke','Revoke delegation'),('e','entities','Entities')]

    def __init__(self,admin):
        super().__init__(product='Atraxis',heading='Account administration',columns=('ORGANIZATION','USER','MEMBERSHIP'),
                         mode='interactive',subtitle='SCOPED ACCOUNT ADMINISTRATION',
                         navigation=(('scope','Your permissions'),('add','Add member'),('remove','Remove member'),
                                     ('delegate','Delegate'),('revoke','Revoke delegation'),('entities','Manage entities')))
        self.admin=admin;self.data=None;self.pending=None

    def on_mount(self):self.action_refresh()

    def failed(self,error):
        self.pending=None;self.data=None;self.display_rows([])
        self.query_one(DetailPanel).show_details('Account administration','Refresh to check current access.')
        self.set_notice('error','Operation unconfirmed or denied. Refresh before trying again; no automatic retry.')

    def action_refresh(self):
        if not self.busy and self.pending is None:self.perform(self.admin.inspect,self.render_view,self.failed)

    def render_view(self,data):
        self.data=data;rows=[]
        for org in data['organizations']:
            scope=self.describe_scope(org)
            for i,m in enumerate(org['memberships']):
                rows.append(TableRow(org['id']+':'+str(i),(org['id'],m['user'],m['group'] or 'Organization'),m['user'],'Organization: '+org['name']+'\nMembership: '+(m['group'] or 'Organization')+'\n\n'+scope))
        for kind, entities in data.get('entities', {}).items():
            for entity in entities:
                rows.append(TableRow('entity:'+kind+':'+entity['id'],(entity.get('organization',entity.get('tenant','Global')),entity['id'],kind),entity['name'],json.dumps(entity,indent=2)))
        self.display_rows(rows)
        self.set_notice('info','Account: '+data['subject']+' · Revision: '+data['revision'])
        self.set_activity('a Add · d Remove · g Delegate · x Revoke\nEvery change requires review; operational permissions remain in Atropos.')
        self.show_scope()

    def describe_scope(self,org):
        lines=['Company group: '+org['tenant'],'Organization: '+org['name'],'Your permissions:']
        for grant in org['grants']:
            lines.extend([grant['id']+': '+', '.join(LABELS[o] for o in grant['operations']),
                'Groups: '+(', '.join(grant['groups']) or 'None'),
                'Organization membership: '+('Yes' if grant['organization_membership'] else 'No'),
                'May delegate: '+('Yes' if grant['delegable'] else 'No')])
        lines.append('Delegations you issued:')
        lines.extend(g['id']+' → '+g['subject']+' ('+('enabled' if g['enabled'] else 'revoked')+')' for g in org['delegated'])
        return '\n'.join(lines)

    def show_scope(self):
        if self.data is not None:
            self.query_one(DetailPanel).show_details('Your administration scope','\n\n'.join(self.describe_scope(o) for o in self.data['organizations'])+'\n\nEntity permissions:\n'+json.dumps(self.data.get('entity_grants',[]),indent=2))

    def on_dashboard_app_navigation_requested(self,event):
        if event.key=='scope':self.show_scope()
        else:
            action={'add':self.action_add_member,'remove':self.action_remove_member,'delegate':self.action_delegate,'revoke':self.action_revoke,'entities':self.action_entities}.get(event.key)
            if action:action()

    def begin(self,operation):
        if self.busy or self.pending is not None or self.data is None:return
        revision=self.data['revision']
        def received(values):
            if values is None:return
            def prepare():return self.admin.prepare(request_from_fields(dict(values,operation=operation)),revision)
            self.perform(prepare,self.review,self.failed,label='Checking current delegation...')
        self.push_screen(AccountForm(LABELS[operation],[(name,label,value,False) for name,label,value in FIELDS[operation]],
            hint='Use IDs from your scope. Entity ownership and current permissions are checked. Review follows.',submit='Review'),received)

    def review(self,plan):
        self.pending=plan
        details='\n'.join(key.replace('_',' ').capitalize()+': '+(', '.join(value) if type(value) is list else str(value) if value is not None else 'Organization membership') for key,value in plan['request'].items())
        self.push_screen(Confirmation('Confirm account change',details,
            confirm_label='Apply',hint='Revision: '+plan['revision']),self.confirm)

    def confirm(self,accepted):
        plan=self.pending;self.pending=None
        if accepted and plan is not None:
            self.perform(lambda:self.admin.commit(plan),lambda result:self.action_refresh(),self.failed,label='Saving account change...')

    def action_entities(self):
        if self.busy or self.pending is not None or self.data is None:return
        from atraxis.account_entities import ALL_OPERATIONS
        def selected(values):
            if values is None:return
            operation=values['operation']
            if operation not in ALL_OPERATIONS:
                self.set_notice('error','Unknown entity operation.');return
            self.begin(operation)
        self.push_screen(AccountForm('Manage entities',[('operation','Operation','group.create',False)],
            hint='tenant, organization, group or user: .create / .update. Also entity_delegation.create / .revoke.',submit='Continue'),selected)

    def action_add_member(self):self.begin('membership.add')
    def action_remove_member(self):self.begin('membership.remove')
    def action_delegate(self):self.begin('delegation.create')
    def action_revoke(self):self.begin('delegation.revoke')


def run(admin):
    if not sys.stdin.isatty() or not sys.stdout.isatty():raise ValueError('interactive_terminal_required')
    AccountApp(admin).run();return 0
