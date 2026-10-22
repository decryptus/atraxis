"""Read-only scoped account search form."""
from urllib.parse import urlencode
from atraxis.web import text
from atraxis.web_accounts import page


def render(data):
    query=data['query']
    body='<h2>Search accounts</h2><form class="editor" method="get" action="/ui/accounts/search">'
    for key,label in [('q','Text (user ID, group or organization ID/name)'),('organization','Organization ID')]:
        body+='<label>'+label+'<input name="'+key+'" value="'+text(query[key])+'" maxlength="128"></label>'
    body+='<label>Type<select aria-label="Type" name="kind">'
    for kind in ('all','tenant','user','group','organization'):
        body+='<option value="'+kind+'"'+(' selected' if query['kind']==kind else '')+'>'+kind.capitalize()+'</option>'
    body+='</select></label><button class="button" type="submit">Search</button></form><p><a href="/ui/accounts/search">Reset filters</a></p>'
    body+='<p>'+str(data['total'])+' matches · Page '+str(query['page'])+'</p><div class="table-wrap"><table><thead><tr><th>Organization</th><th>Type</th><th>ID</th><th>Name</th></tr></thead><tbody>'
    body+=''.join('<tr>'+''.join('<td>'+text(row[k])+'</td>' for k in ('organization','kind','id','name'))+'</tr>' for row in data['items'])
    body+='</tbody></table></div>'
    if not data['items']: body+='<p>No matches on this page.</p>'
    body+='<nav class="pager" aria-label="Search pages">'
    for label,number,show in [('Previous',query['page']-1,query['page']>1),('Next',query['page']+1,data['has_more'])]:
        if show: body+='<a href="/ui/accounts/search?'+text(urlencode(dict(query,page=number)))+'">'+label+'</a>'
    return page(body+'</nav><p>Only your current administration scope is searched. Users are matched by ID.</p>')
