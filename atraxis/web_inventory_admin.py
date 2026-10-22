"""Authenticated, reviewed inventory grant changes; no catalog or execution writes."""
from urllib.parse import parse_qsl, urlencode, urlsplit

from atraxis.accounts import AccountError
from atraxis.inventory_admin import InventoryAdmin, GRANT_FORM_FIELDS, request_from_fields
from atraxis.web import text
from atraxis.web_accounts import HEADERS, CSS, hidden, parse_form
from atraxis.web_rule_editor import FormTokens, validate_origin

FORM_BYTES = 32768
OPERATIONS = {'grant.add':'Add read grant','grant.remove':'Remove read grant'}


def page(body):
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
        '<title>Inventory access · Atraxis</title><style>'+CSS+'</style></head><body><header><div class="brand">ATRA<span>XIS</span></div>'
        '<nav aria-label="Main"><a href="/ui">Inventory</a> <a href="/ui/inventory-access">Inventory access</a></nav></header>'
        '<main><div class="eyebrow">Administration</div><h1>Inventory read access</h1>'+body+
        '<footer>These grants control inventory reading. Intervention and account-administration permissions are separate.</footer></main></body></html>')


def targets(items):
    return '<ul>'+''.join('<li>'+text(t['kind'])+' · <code>'+text(t.get('source',''))+(' / ' if 'source' in t else '')+text(t['id'])+'</code></li>' for t in items)+'</ul>'


def overview(data):
    body='<p>Acting account: '+text(data['subject'])+'</p>'
    for org in data['organizations']:
        query=urlencode(dict(organization=org['id']))
        body+='<section class="source"><h2>'+text(org['name'])+'</h2><h3>Your administration scope</h3>'+targets(org['targets'])
        body+='<p><a class="button" href="/ui/inventory-access/edit?'+query+'">Add read grant</a></p><h3>Manageable grants</h3>'
        for grant in org['grants']:
            body+='<p>'+text(grant['subject_kind'])+': <strong>'+text(grant['subject'])+'</strong></p>'+targets(grant['targets'])
            body+='<a href="/ui/inventory-access/remove?'+urlencode(dict(grant=grant['id']))+'">Remove this grant</a>'
        if not org['grants']:body+='<p>No manageable grants.</p>'
        body+='</section>'
    body+='<h2>Recent changes in your scope</h2>'
    for event in reversed(data['audit']):
        body+='<p>'+text(event['time'])+' · '+text(event['actor'])+' · '+text(OPERATIONS[event['operation']])+' · '+text(event['grant']['subject'])+'</p>'
    if not data['audit']:body+='<p>No recorded changes in this scope.</p>'
    return page(body)


class WebInventoryAdmin:
    def __init__(self,path,origin):
        self.path,self.origin=path,validate_origin(origin);self.tokens=FormTokens()

    def handle(self,path,username,*,method='GET',headers=None,raw=b''):
        headers=headers or {}
        try:
            if username is None:raise PermissionError('authentication_required')
            if len(path.encode())>2048:raise ValueError('invalid_path')
            admin=InventoryAdmin(self.path,username)
            parsed=urlsplit(path);route=parsed.path
            if method in ('GET','HEAD'):
                data=admin.inspect()
                if route=='/ui/inventory-access' and not parsed.query:return 200,overview(data),{}
                pairs=parse_qsl(parsed.query,keep_blank_values=True,strict_parsing=True,max_num_fields=1,errors='strict')
                if len(pairs)!=1:raise ValueError('invalid_query')
                if route=='/ui/inventory-access/edit' and pairs[0][0]=='organization':
                    org=next((o for o in data['organizations'] if o['id']==pairs[0][1]),None)
                    if org is None:raise PermissionError('organization_denied')
                    token=self.tokens.issue(admin,'inventory.edit',org['id'],data['revision'])
                    body='<h2>Add read grant</h2><p>Organization: '+text(org['name'])+'</p><h3>Maximum allowed scope</h3>'+targets(org['targets'])
                    body+='<form class="editor" method="post" action="/ui/inventory-access/review">'+hidden('token',token)
                    for name,label,value in GRANT_FORM_FIELDS[1:]:
                        body+='<label>'+text(label)+'<input name="'+name+'" value="'+text(value)+'" maxlength="128" autocomplete="off"></label>'
                    body+='<p>Use a resource-group target for several resources. Recipient membership and your maximum scope are checked before saving.</p><button class="button" type="submit">Review grant</button></form>'
                    return 200,page(body),{}
                if route=='/ui/inventory-access/remove' and pairs[0][0]=='grant':
                    plan=admin.prepare(dict(operation='grant.remove',grant=pairs[0][1]),data['revision'])
                    return self.review(admin,plan)
                raise ValueError('invalid_query')
            if (method!='POST' or parsed.query or route not in ('/ui/inventory-access/review','/ui/inventory-access/commit')
                    or headers.get('origin')!=self.origin or headers.get('sec-fetch-site') not in (None,'same-origin')
                    or headers.get('content-type')!='application/x-www-form-urlencoded'):
                raise PermissionError('origin_denied')
            fields=parse_form(raw);token=fields.pop('token',None)
            if route.endswith('/review'):
                binding=self.tokens.consume(token,admin,'inventory.edit')
                if set(fields)!={name for name,_,_ in GRANT_FORM_FIELDS[1:]}:raise ValueError('invalid_form')
                request=request_from_fields(dict(fields,organization=binding['name']))
                plan=admin.prepare(request,binding['revision'])
                return self.review(admin,plan)
            binding=self.tokens.consume(token,admin,'inventory.commit')
            if fields!={'confirm':'yes'}:raise PermissionError('confirmation_required')
            admin.commit(binding['context'])
            return 303,page('<p>Read grant change saved.</p>'),{'Location':'/ui/inventory-access'}
        except PermissionError:
            code,message=403,'Access denied or form expired. Open a fresh form.'
        except AccountError as error:
            code=409 if str(error)=='inventory_revision_conflict' else 400
            message='The configuration changed. Review the current grants.' if code==409 else 'Invalid grant, unavailable configuration or capacity reached. No change was saved.'
        except (ValueError,TypeError,KeyError,RecursionError):
            code,message=400,'Invalid inventory access request.'
        except Exception:
            code,message=503,'Outcome unconfirmed. Inspect current grants and recent changes before trying again. No automatic retry was attempted.'
        return code,page('<p class="notice" role="alert">'+message+'</p>'),{}

    def review(self,admin,plan):
        grant=plan['grant']
        token=self.tokens.issue(admin,'inventory.commit',plan['request']['operation'],plan['revision'],plan)
        body='<h2>Review: '+text(OPERATIONS[plan['request']['operation']])+'</h2><p>Acting account: '+text(plan['identity']['subject'])+'</p>'
        body+='<p>Organization: '+text(grant['organization'])+' · '+text(grant['subject_kind'])+': '+text(grant['subject'])+'</p>'+targets(grant['targets'])
        body+='<p>A source or server grant includes every resource and source-wide metadata. Removing one grant does not cancel other overlapping grants.</p>'
        body+='<form class="activation" method="post" action="/ui/inventory-access/commit">'+hidden('token',token)
        body+='<label><input type="checkbox" name="confirm" value="yes" required>I confirm this inventory access change.</label><button class="button" type="submit">Confirm access change</button></form>'
        return 200,page(body),{}
