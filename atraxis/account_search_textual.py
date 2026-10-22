"""Local authenticated account search; no filtering of only loaded rows."""
from dataclasses import replace
import hashlib
import json
import sys
from textual.widgets import Input
from dwho.tui.textual import ServiceDashboard,TableRow,DetailPanel
from atraxis.account_search import AccountQuery
from atraxis.account_textual import AccountForm

class AccountSearchApp(ServiceDashboard):
    BINDINGS=ServiceDashboard.BINDINGS+[
        ('r','refresh','Refresh'),('n','next_page','Next'),('p','previous_page','Previous'),('f','filters','Filters')]

    def __init__(self,admin,query=None):
        super().__init__(product='Atraxis',heading='Account search',columns=('ORGANIZATION','TYPE','ID','NAME'),
            mode='read_only',subtitle='AUTHORIZED ACCOUNT SEARCH',navigation=(('refresh','Refresh'),('filters','Filters'),('reset','Reset filters')))
        self.admin=admin;self.query=query or AccountQuery();self.data=None

    def on_mount(self):
        self.query_one(Input).placeholder=getattr(self,'search_hint','Search your complete account scope; Enter to submit')
        self.query_one(Input).value=self.query.q;self.action_refresh()

    def on_input_changed(self,event):
        if event.input.has_class('dw-search'):event.stop()

    def _render_rows(self):
        field=self.query_one(Input);value=field.value
        with field.prevent(Input.Changed):
            field.value='';super()._render_rows();field.value=value

    def on_input_submitted(self,event):
        if event.input.has_class('dw-search') and not self.busy:
            try:self.query=replace(self.query,q=event.value,page=1)
            except ValueError:self.failed(None);return
            self.action_refresh()

    def action_refresh(self):
        if not self.busy:
            query=self.query
            self.perform(lambda:self.admin.search(query),self.render_result,self.failed,label='Searching authorized accounts...')

    def render_result(self,data):
        self.data=data
        self.display_rows([TableRow(hashlib.sha256(json.dumps(row,sort_keys=True).encode()).hexdigest(),
            tuple(row[k] for k in ('organization','kind','id','name')),row['name'],json.dumps(row,indent=2)) for row in data['items']])
        self.set_notice('info',str(data['total'])+' matches · Page '+str(self.query.page)+' · Account: '+data['subject'])
        self.set_activity('Enter searches the complete scope · f Filters · n/p Pages · r Refresh\nUser IDs; group and organization IDs/names. Current delegation checked on each query.')

    def failed(self,error):
        self.data=None;self.display_rows([])
        self.query_one(DetailPanel).show_details('Search unavailable','Check current permissions and filters, then refresh.')
        self.set_notice('error','Search failed. No cached or partial results shown.')

    def action_clear_search(self):
        if not self.busy:
            self.query=replace(self.query,q='',page=1);self.query_one(Input).value='';self.action_refresh()

    def action_next_page(self):
        if not self.busy and self.data and self.data['has_more']:
            self.query=replace(self.query,page=self.query.page+1);self.action_refresh()

    def action_previous_page(self):
        if not self.busy and self.query.page>1:
            self.query=replace(self.query,page=self.query.page-1);self.action_refresh()

    def action_filters(self):
        if self.busy:return
        def received(values):
            if values is None:return
            try:self.query=AccountQuery(**values,limit=self.query.limit)
            except (TypeError,ValueError):self.failed(None);return
            self.query_one(Input).value=self.query.q;self.action_refresh()
        self.push_screen(AccountForm('Account filters',[(k,k.capitalize(),getattr(self.query,k),False) for k in ('q','kind','organization')],
            hint='Type: all/tenant/user/group/organization. Organization uses an exact ID.',submit='Search'),received)

    def on_dashboard_app_navigation_requested(self,event):
        if event.key=='refresh':self.action_refresh()
        elif event.key=='filters':self.action_filters()
        elif event.key=='reset' and not self.busy:
            self.query=AccountQuery(limit=self.query.limit);self.query_one(Input).value='';self.action_refresh()


def run(admin,query):
    if not sys.stdin.isatty() or not sys.stdout.isatty():raise ValueError('interactive_terminal_required')
    AccountSearchApp(admin,query).run();return 0
