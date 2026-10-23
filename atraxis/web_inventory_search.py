"""Accessible GET search; query and paging preserve explicit organization context."""
from urllib.parse import urlencode
from atraxis.web import CSS, HEADERS, text, value, details
from atraxis.inventory_search import FILTER_FIELDS

SEARCH_HEADERS = dict(HEADERS)
SEARCH_HEADERS['Content-Security-Policy'] = HEADERS['Content-Security-Policy'].replace("form-action 'none'", "form-action 'self'")
LABELS = {'q':'Name or identifier', 'state':'Observation state', 'status':'Reported state (exact)', 'kind':'Resource type',
          'source':'Source ID', 'server':'Server ID', 'group':'Resource group ID'}


def page(body):
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>Inventory search · Atraxis</title><style>'+CSS+'</style></head><body>'
            '<header><div class="brand">ATRA<span>XIS</span></div><nav><a href="/ui">Inventory</a></nav></header>'
            '<main><h1>Search inventory</h1>'+body+'<footer>Search covers authorized retained observations. '
            'Pages use current data; refresh may change their contents. Collection readiness is independent of the search.</footer></main></body></html>')


def render_selection(scope, query):
    body='<p>Select an organization to search its authorized inventory.</p>'
    for org in scope['organizations']:
        body+='<p><a href="/ui/search?'+text(urlencode(dict(query.parameters(),organization=org['id'])))+'">'+text(org['name'])+'</a></p>'
    return page(body)


def render_search(result):
    query=result['query'];context=result['context']
    organization=context['organization'] if context else None
    base={'organization':organization} if organization else {}
    body='<p>Organization: '+text(organization or 'Unscoped service')+'</p>'
    body+='<form method="get" action="/ui/search"><section class="source">'
    if organization:body+='<input type="hidden" name="organization" value="'+text(organization)+'">'
    body+='<div class="search-fields">'
    for key in FILTER_FIELDS:
        body+='<p'+(' class="search-query"' if key=='q' else '')+'><label>'+LABELS[key]+' '
        choices={'state':('all','current','historical'),'kind':('all','container','certificate','metric')}.get(key)
        if choices:
            body+='<select name="'+key+'">'+''.join('<option value="'+v+'"'+(' selected' if query[key]==v else '')+'>'+v.capitalize()+'</option>' for v in choices)+'</select>'
        else:
            body+='<input name="'+key+'" value="'+text(query[key])+'" maxlength="128" autocomplete="off">'
        body+='</label></p>'
    body+='</div>'
    body+='<input type="hidden" name="limit" value="'+str(query['limit'])+'"><button type="submit" class="button">Search</button> '
    body+='<a href="/ui/search'+('?' + text(urlencode(base)) if base else '')+'">Reset filters</a></section></form>'
    body+='<p role="status">'+str(result['total'])+' matching resources · Page '+str(query['page'])+'</p>'
    body+='<p>Collection: '+('Ready' if result['ready'] else text(result['reason']))+'</p>'
    if result['unavailable']:
        body+='<p class="notice">Some selected sources lack current observations. No matches does not establish that a resource is absent.</p><ul>'
        body+=''.join('<li>'+text(s['source_id'])+' · '+text(s['state'])+'</li>' for s in result['unavailable'])+'</ul>'
    if not result['items']:
        body+='<div class="empty">No matching resources on this page.</div>'
    else:
        body+='<section class="source"><div class="table-wrap"><table><thead><tr><th>Resource</th><th>Source</th><th>Type</th><th>Observation</th><th>Reported state</th></tr></thead><tbody>'
        for row in result['items']:
            fields=row['fields'];name=', '.join(fields['domains']) if row['kind']=='certificate' else fields['name']
            body+='<tr><td class="name">'+text(name)+details(row)+'</td><td>'+text(row['source_id'])+'</td><td>'+text(row['kind'])+'</td><td>'+text(row['state'])+'</td><td>'+value(fields.get('status'))+'</td></tr>'
        body+='</tbody></table></div></section>'
    params=dict(query,**base)
    body+='<nav aria-label="Search pages">'
    if query['page']>1:
        body+='<a class="button" href="/ui/search?'+text(urlencode(dict(params,page=query['page']-1)))+'">Previous</a> '
    if result['has_more']:
        body+='<a class="button" href="/ui/search?'+text(urlencode(dict(params,page=query['page']+1)))+'">Next</a>'
    return page(body+'</nav>')
