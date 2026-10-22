"""Request-local organization routing and navigation for rule browser adapters."""
from functools import wraps
from html import escape
from html.parser import HTMLParser
import inspect
from atraxis.accounts import AccountError
from atraxis.config import ConfigurationError
from atraxis.organization_context import selected, split_path, scoped_url


def choices(mapping, username):
    from atraxis.web_rules import load_mapping
    from atraxis.account_store import require_membership
    entries=[]
    for entry in load_mapping(mapping):
        if entry['username']!=username: continue
        try: require_membership(mapping,entry.get('accounts_file'),entry['subject'],entry['organization'])
        except PermissionError: continue
        entries.append(entry)
    return entries


def navigation(entries, organization):
    links=' '.join('<a href="'+escape(scoped_url('/ui/rules',e['organization']),quote=True)+'">'+escape(e['organization'])+'</a>' for e in entries)
    return '<section class="source actor" aria-label="Organization context"><p>Organization: <strong>'+escape(organization or 'Select an organization')+'</strong></p><nav aria-label="Select organization">'+links+'</nav></section>'


class ScopedHTML(HTMLParser):
    """Scope only application navigation and forms; preserve text/style verbatim."""
    def __init__(self, organization):
        super().__init__(convert_charrefs=False);self.organization=organization;self.parts=[]

    def handle_starttag(self, tag, attrs):
        values=dict(attrs);key='href' if tag=='a' else 'action' if tag=='form' else None
        if key is None or key not in values:
            self.parts.append(self.get_starttag_text());return
        original=values[key];values[key]=scoped_url(original,self.organization)
        if original==values[key]:
            self.parts.append(self.get_starttag_text());return
        self.parts.append('<'+tag+''.join(' '+k+('="'+escape(v,quote=True)+'"' if v is not None else '') for k,v in values.items())+'>')
        if tag=='form' and values.get('method','get').lower()=='get':
            self.parts.append('<input type="hidden" name="organization" value="'+escape(self.organization,quote=True)+'">')

    def handle_endtag(self,tag):self.parts.append('</'+tag+'>')
    def handle_startendtag(self,tag,attrs):self.parts.append(self.get_starttag_text())
    def handle_data(self,data):self.parts.append(data)
    def handle_entityref(self,name):self.parts.append('&'+name+';')
    def handle_charref(self,name):self.parts.append('&#'+name+';')
    def handle_decl(self,decl):self.parts.append('<!'+decl+'>')
    def handle_comment(self,data):self.parts.append('<!--'+data+'-->')


def contextual(function):
    signature=inspect.signature(function)
    @wraps(function)
    def call(*args,**kwargs):
        bound=signature.bind(*args,**kwargs);values=bound.arguments
        controller=values.get('self');mapping=controller.mapping if controller is not None else values['mapping']
        attempted=False
        try:
            path,organization=split_path(values['path'])
            entries=choices(mapping,values['username'])
            if organization is None and len(entries)>1:
                from atraxis.web_rules import page
                result=(409,page(navigation(entries,None)))
                return result+({},) if controller is not None else result
            entry=selected(entries,organization)
            values['path']=path;values['organization']=entry['organization']
            attempted=values.get('method','GET')=='POST'
            result=function(*bound.args,**bound.kwargs)
            if result[0]>=400: return result
            # Selection and authority can be revoked while a reader/operation runs.
            current=choices(mapping,values['username'])
            if entry not in current: raise PermissionError('organization_access_changed')
            code,body=result[:2];parser=ScopedHTML(entry['organization']);parser.feed(body);parser.close()
            body=''.join(parser.parts).replace('<h1>',navigation(current,entry['organization'])+'<h1>',1)
            if controller is not None:
                extra=dict(result[2])
                if 'Location' in extra:extra['Location']=scoped_url(extra['Location'],entry['organization'])
                return code,body,extra
            return code,body
        except PermissionError:
            code,message=403,'Organization access denied. Refresh to check your access.'
        except (AccountError,ConfigurationError):
            code,message=503,'Organization context unavailable. Check current state before retrying.'
        except (ValueError,TypeError):
            code,message=400,'Invalid organization context or unavailable configuration.'
        except Exception:
            code,message=503,'Organization context unavailable. Check current state before retrying.'
        if attempted: message+=' The operation may already have completed. Check current state before retrying.'
        from atraxis.web_rules import page
        result=(code,page('<p role="alert">'+message+'</p>'))
        return result+({},) if controller is not None else result
    return call
