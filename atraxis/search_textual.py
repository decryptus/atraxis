"""Remote server-side inventory search in Textual; Enter submits the full query."""
from dataclasses import replace
import json
import hashlib
import sys
from textual.widgets import Input
from dwho.tui.textual import ServiceDashboard, TableRow, DetailPanel
from atraxis.account_textual import AccountForm
from atraxis.inventory_search import SearchQuery, FILTER_FIELDS
from atraxis.search_client import read_search


class InventorySearchApp(ServiceDashboard):
    BINDINGS=ServiceDashboard.BINDINGS+[
        ('r','refresh','Refresh'),('n','next_page','Next'),('p','previous_page','Previous'),('f','filters','Filters')]

    def __init__(self,config,query=None):
        super().__init__(product='Atraxis',heading='Inventory search',columns=('SOURCE','NAME','TYPE','OBSERVATION'),
                         mode='read_only',subtitle='AUTHORIZED INVENTORY SEARCH',
                         navigation=(('refresh','Refresh'),('filters','Filters'),('reset','Reset filters')))
        self.config=config;self.query=query or SearchQuery();self.data=None

    def on_mount(self):
        self.query_one(Input).placeholder='Search all authorized resources; Enter to submit'
        self.query_one(Input).value=self.query.q
        self.action_refresh()

    def on_input_changed(self,event):
        # Do not invoke the shared dashboard's loaded-page filter.
        if event.input.has_class('dw-search'):event.stop()

    def _render_rows(self):
        # The shared dashboard filters displayed cells using the text box. Server
        # matches also include native IDs, so neutralize that local filter.
        field=self.query_one(Input);value=field.value
        with field.prevent(Input.Changed):
            field.value=''
            super()._render_rows()
            field.value=value

    def on_input_submitted(self,event):
        if event.input.has_class('dw-search') and not self.busy:
            try:self.query=replace(self.query,q=event.value,page=1)
            except ValueError:self.failed(None);return
            self.action_refresh()

    def action_clear_search(self):
        if not self.busy:
            self.query=replace(self.query,q='',page=1)
            self.query_one(Input).value=''
            self.action_refresh()

    def action_refresh(self):
        if self.busy:return
        query=self.query
        def read():
            result,error=read_search(self.config,query)
            if error:raise ValueError(error)
            return result
        self.perform(read,self.render_result,self.failed,label='Searching authorized inventory...')

    def render_result(self,data):
        self.data=data
        rows=[]
        for row in data['items']:
            name=', '.join(row['fields']['domains']) if row['kind']=='certificate' else row['fields']['name']
            key=hashlib.sha256(json.dumps([row['source_id'],row['kind'],row['native_id']]).encode()).hexdigest()
            rows.append(TableRow(key,(row['source_id'],name,row['kind'],row['state']),name,json.dumps(row,indent=2,ensure_ascii=True)))
        self.display_rows(rows)
        self.set_notice('info',str(data['total'])+' matches · Page '+str(self.query.page)+' · Organization: '+str(data['context']['organization'] if data['context'] else 'unscoped'))
        self.set_activity(('Some sources have no current observations. No match is not proof of absence.\n' if data['unavailable'] else '')+
                          'Enter submits the complete query · f Filters · n/p Pages · r Refresh\nObservation state is reported at search time.')

    def failed(self,error):
        self.data=None;self.display_rows([])
        self.query_one(DetailPanel).show_details('Search unavailable','Check organization, current permissions and filters, then refresh.')
        self.set_notice('error','Search failed. No cached or partial results shown.')
        self.set_activity('No automatic retry. Use -o ID when more than one organization is available.')

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
            try:self.query=SearchQuery(**values,limit=self.query.limit)
            except (TypeError,ValueError):self.failed(None);return
            self.query_one(Input).value=self.query.q;self.action_refresh()
        self.push_screen(AccountForm('Inventory filters',[(k,k.capitalize(),getattr(self.query,k),False) for k in FILTER_FIELDS],
            hint='State: all/current/historical. Kind: all/container/certificate/metric. Other filters use IDs.',submit='Search'),received)

    def on_dashboard_app_navigation_requested(self,event):
        if event.key=='refresh':self.action_refresh()
        elif event.key=='filters':self.action_filters()
        elif event.key=='reset' and not self.busy:
            self.query=SearchQuery(limit=self.query.limit);self.query_one(Input).value='';self.action_refresh()


def run(config,query):
    if not sys.stdin.isatty() or not sys.stdout.isatty():raise ValueError('interactive_terminal_required')
    InventorySearchApp(config,query).run();return 0
