"""Browser intervention search forwarded to the independent Atropos client."""
from urllib.parse import urlencode
from atraxis.web import text
from atraxis.web_rules import RuleReader,RULE_CSS,client_factory
from atraxis.intervention_search import parse_query,checked,form_values,date_text,STATUSES


def page(body, *, simulation=False, dispatch=False):
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
        '<title>Interventions · Atraxis</title><style>'+RULE_CSS+'</style></head><body><header><div class="brand">ATRA<span>XIS</span></div>'
        '<nav aria-label="Main"><a href="/ui">Inventory</a><a href="/ui/rules">Rules</a></nav></header><main><div class="eyebrow">Atropos</div><h1>Interventions</h1>'
        +body+'<footer>'+('Real interventions require explicit confirmation and fresh observations.' if dispatch else 'Test simulations require explicit confirmation. Permissions are checked by Atropos.' if simulation else 'Read-only search · Permissions are checked by Atropos.')+'</footer></main></body></html>')

class SearchReader(RuleReader):
    methods=frozenset({'search_interventions'})


def render(name,query,data=None):
    values=form_values(query)
    body='<h2>Search interventions</h2><p>Search your own admitted runs for one rule. Test runs remain explicitly labelled.</p>'
    body+='<form class="editor" method="get" action="/ui/interventions">'
    for key,label,value in [('name','Rule name',name),('q','Text',query.q),('target','Target',query.target),('workflow','Workflow',query.workflow)]:
        body+='<label>'+label+'<input name="'+key+'" value="'+text(value)+'" maxlength="128"'+(' required' if key=='name' else '')+'></label>'
    for key,label,options in [('mode','Mode',('all','real','test')),('status','Status',('',*sorted(STATUSES)))]:
        body+='<label>'+label+'<select aria-label="'+label+'" name="'+key+'">'
        body+=''.join('<option value="'+option+'"'+(' selected' if values[key]==option else '')+'>'+text(option or 'All statuses')+'</option>' for option in options)
        body+='</select></label>'
    for key,label in [('since','From (UTC)'),('until','Until (UTC)')]:
        body+='<label>'+label+'<input type="datetime-local" step="1" name="'+key+'" value="'+values[key]+'"></label>'
    body+='<button class="button" type="submit">Search</button></form><p><a href="/ui/interventions?'+text(urlencode(dict(name=name)))+'">Reset filters</a></p>'
    if data is not None:
        body+='<p>'+str(data['total'])+' matches · Page '+str(query.page)+'</p><div class="table-wrap"><table><thead><tr><th>Event</th><th>Mode</th><th>Status</th><th>Target</th><th>Workflow</th><th>Created (UTC)</th></tr></thead><tbody>'
        for row in data['items']:
            href='/ui/interventions/detail?'+urlencode(dict(name=name,mode=row['mode'],id=row['id']))
            body+='<tr><td><a href="'+text(href)+'">'+text(row['event_id'])+'</a></td>'+''.join('<td>'+text(row[k])+'</td>' for k in ('mode','status','target','workflow'))+'<td>'+text(date_text(row['created_at']))+'</td></tr>'
        body+='</tbody></table></div>'
        if not data['items']:body+='<p>No matches on this page.</p>'
        body+='<nav class="pager" aria-label="Search pages">'
        for label,number,show in [('Previous',query.page-1,query.page>1),('Next',query.page+1,data['has_more'])]:
            if show:body+='<a href="/ui/interventions?'+text(urlencode(dict(values,name=name,page=number)))+'">'+label+'</a>'
        body+='</nav><p>Status is observed at query time. Refresh to see changes. Search never executes or retries a run.</p>'
    return page(body)


from atraxis.web_organization_context import contextual

@contextual
def handle(path,username,mapping,*,factory=client_factory,organization=None):
    try:
        name,query=parse_query(path)
    except (TypeError,ValueError):
        return 400,page('<p role="alert">Invalid intervention search query.</p>')
    try:
        reader=SearchReader(mapping,username,factory,organization=organization);actor=reader.entry()
        if not name:return 200,render(name,query)
        data=checked(reader.call('search_interventions',name,**query.parameters()),name,query,actor)
        return 200,render(name,query,data)
    except PermissionError:return 403,page('<p role="alert">Intervention search access denied.</p>')
    except Exception:return 503,page('<p role="alert">Intervention search unavailable. No partial results shown. Refresh to try again.</p>')
