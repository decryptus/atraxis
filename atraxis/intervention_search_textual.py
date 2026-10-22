"""Read-only engine intervention search in Textual."""
import json
import hashlib
import sys
from textual.widgets import Input
from dwho.tui.textual import ServiceDashboard,TableRow,DetailPanel
from atraxis.account_search_textual import AccountSearchApp
from atraxis.account_textual import AccountForm
from atraxis.intervention_search import InterventionQuery,form_values,date_value,date_text

class InterventionSearchApp(AccountSearchApp):
    def __init__(self,service,query=None):
        ServiceDashboard.__init__(self,product='Atraxis',heading='Intervention search',
            columns=('MODE','STATUS','TARGET','WORKFLOW','CREATED UTC'),mode='read_only',
            subtitle='ENGINE INTERVENTION SEARCH',navigation=(('refresh','Refresh'),('filters','Filters'),('reset','Reset filters')))
        self.admin=service;self.query=query or InterventionQuery();self.data=None
        self.search_hint='Search your runs for '+service.name+'; Enter to submit'

    def action_refresh(self):
        if not self.busy:
            query=self.query
            self.perform(lambda:self.admin.search(query),self.render_result,self.failed,label='Searching engine interventions...')

    def render_result(self,data):
        self.data=data
        self.display_rows([TableRow(hashlib.sha256((r['mode']+r['id']).encode()).hexdigest(),
            tuple(r[k] for k in ('mode','status','target','workflow'))+(date_text(r['created_at']),),r['event_id'],json.dumps(r,indent=2)) for r in data['items']])
        self.set_notice('info',str(data['total'])+' matches · Page '+str(self.query.page)+' · '+data['organization']+' / '+data['rule'])
        self.set_activity('Enter submits · f Filters · n/p Pages · r Refresh\nYour own admitted runs; status observed at query time. Search never executes actions.')

    def action_filters(self):
        if self.busy:return
        values=form_values(self.query)
        def received(values):
            if values is None:return
            try:
                for key in ('since','until'):values[key]=date_value(values[key])
                self.query=InterventionQuery(**values,limit=self.query.limit)
            except (TypeError,ValueError):self.failed(None);return
            self.query_one(Input).value=self.query.q;self.action_refresh()
        self.push_screen(AccountForm('Intervention filters',[(k,k.capitalize()+(' (UTC)' if k in ('since','until') else ''),values[k],False) for k in ('q','mode','status','target','workflow','since','until')],
            hint='Mode: all/real/test. Dates: YYYY-MM-DDTHH:MM:SS. Empty filters match all.',submit='Search'),received)

    def failed(self,error):
        self.data=None;self.display_rows([])
        self.query_one(DetailPanel).show_details('Search unavailable','Check the rule, current permissions and filters, then refresh.')
        self.set_notice('error','Search unavailable. No cached or partial results shown.')

    def on_dashboard_app_navigation_requested(self,event):
        if event.key=='reset' and not self.busy:
            self.query=InterventionQuery(limit=self.query.limit);self.query_one(Input).value='';self.action_refresh()
        elif event.key=='refresh':self.action_refresh()
        elif event.key=='filters':self.action_filters()


def run(service,query):
    if not sys.stdin.isatty() or not sys.stdout.isatty():raise ValueError('interactive_terminal_required')
    InterventionSearchApp(service,query).run()
