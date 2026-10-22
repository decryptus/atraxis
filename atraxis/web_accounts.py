"""Authenticated, scoped account administration with single-use review forms."""
import json
import base64
import hashlib
from urllib.parse import parse_qsl, urlencode, urlsplit

from atraxis.account_admin import AccountAdmin, request_from_fields
from atraxis.account_delegation import OPERATIONS
from atraxis.accounts import AccountError
from atraxis.web import text
from atraxis.web_rules import RULE_CSS, RULE_HEADERS
from atraxis.web_rule_editor import FormTokens, validate_origin

CSS = RULE_CSS + '\n.account-actions{flex-wrap:wrap}.account-actions a{max-width:100%;overflow-wrap:anywhere}main p,main dd{overflow-wrap:anywhere}\n'
HEADERS = dict(RULE_HEADERS, **{'Referrer-Policy':'same-origin'})
HEADERS['Content-Security-Policy'] = RULE_HEADERS['Content-Security-Policy'].replace(
    base64.b64encode(hashlib.sha256(RULE_CSS.encode()).digest()).decode(),
    base64.b64encode(hashlib.sha256(CSS.encode()).digest()).decode())
FORM_BYTES = 32768
from atraxis.account_forms import LABELS, FIELDS
from atraxis.account_entities import ALL_OPERATIONS
ACCOUNT_OPERATIONS = OPERATIONS | ALL_OPERATIONS


def page(body):
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
        '<title>Accounts · Atraxis</title><style>'+CSS+'</style></head><body><a class="skip" href="#accounts">Skip to accounts</a>'
        '<header><div class="brand">ATRA<span>XIS</span></div><nav aria-label="Main"><a href="/ui">Inventory</a>'
        '<a href="/ui/accounts" aria-current="page">Accounts</a></nav></header><main id="accounts"><div class="eyebrow">Account administration</div>'
        '<h1>People &amp; delegations</h1><p class="muted">Your authorized organizations and groups. Changes require review and confirmation.</p>'
        +body+'<footer>Account administration does not grant implicit intervention rights. Atropos checks operational permissions separately.</footer></main></body></html>')


def hidden(name,value):
    return '<input type="hidden" name="'+text(name)+'" value="'+text(value)+'">'


def overview(data):
    body='<p class="actor">Account: '+text(data['subject'])+'</p><p>Revision: <code>'+text(data['revision'])+'</code></p>'
    body+='<p><a href="/ui/accounts/search">Search accounts</a></p>'
    body+='<nav class="pager account-actions" aria-label="Account operations">'+' '.join('<a class="button" href="/ui/accounts/edit?'+urlencode({'operation':op})+'">'+label+'</a>' for op,label in LABELS.items())+'</nav>'
    for org in data['organizations']:
        body+='<section class="source"><div class="eyebrow">Company group · '+text(org['tenant'])+'</div><h2>'+text(org['name'])+'</h2><p>Organization: <code>'+text(org['id'])+'</code></p>'
        body+='<h3>Managed groups</h3><p>'+(', '.join(text(g['name'])+' (<code>'+text(g['id'])+'</code>)' for g in org['groups']) or 'None')+'</p>'
        body+='<h3>Memberships</h3><div class="table-wrap"><table><thead><tr><th>User</th><th>Scope</th></tr></thead><tbody>'
        body+=''.join('<tr><td>'+text(m['user'])+'</td><td>'+text(m['group'] or 'Organization membership')+'</td></tr>' for m in org['memberships'])
        body+='</tbody></table></div><h3>Your administration permissions</h3>'
        for grant in org['grants']:
            body+='<p><code>'+text(grant['id'])+'</code> · '+text(', '.join(LABELS[o] for o in grant['operations']))+'<br>Groups: '+text(', '.join(grant['groups']) or 'None')+' · Organization membership: '+('Yes' if grant['organization_membership'] else 'No')+' · Further delegation: '+('Yes' if grant['delegable'] else 'No')+'</p>'
        body+='<h3>Delegations you issued</h3>'
        body+=''.join('<p><code>'+text(g['id'])+'</code> · '+text(g['subject'])+' · '+('Enabled' if g['enabled'] else 'Revoked')+'</p>' for g in org['delegated']) or '<p>None</p>'
        body+='</section>'
    entity_data = data.get('entities', {})
    if data.get('entity_grants'):
        body += '<h2>Entity administration</h2><p>Explicit entity permissions; user identities are global.</p>'
        for kind, entities in entity_data.items():
            body += '<section class="source"><h3>'+text(kind.capitalize())+'</h3><div class="table-wrap"><table><thead><tr><th>ID</th><th>Name</th><th>State</th><th>Scope</th></tr></thead><tbody>'
            for entity in entities:
                body += '<tr><td>'+text(entity['id'])+'</td><td>'+text(entity['name'])+'</td><td>'+('Enabled' if entity['enabled'] else 'Disabled')+'</td><td>'+text(entity.get('organization',entity.get('tenant','Global')))+('</td></tr>')
            body += '</tbody></table></div></section>'
        for grant in data['entity_grants']:
            body += '<p><code>'+text(grant['id'])+'</code> · '+text(grant['scope']['kind'])+' '+text(grant['scope']['id'] or '')+' · '+text(', '.join(grant['operations']))+'</p>'
        for grant in data['entity_delegated']:
            body += '<p>Delegated: <code>'+text(grant['id'])+'</code> · '+text(grant['subject'])+' · '+('Enabled' if grant['enabled'] else 'Revoked')+'</p>'
    return page(body)


def parse_form(raw):
    if type(raw) is not bytes or not 0<len(raw)<=FORM_BYTES:raise AccountError('invalid_account_form')
    pairs=parse_qsl(raw.decode('utf-8'),keep_blank_values=True,strict_parsing=True,max_num_fields=12,errors='strict')
    fields=dict(pairs)
    if len(fields)!=len(pairs):raise AccountError('invalid_account_form')
    return fields


class WebAccounts:
    def __init__(self,profile,origin):
        self.profile,self.origin=profile,validate_origin(origin)
        self.tokens=FormTokens()

    def handle(self,path,username,*,method='GET',headers=None,raw=b''):
        headers=headers or {}
        try:
            if type(path) is not str or len(path.encode())>2048:raise AccountError('invalid_account_path')
            route=urlsplit(path).path
            admin=AccountAdmin(self.profile,username)
            if username is None:raise PermissionError('account_authentication_required')
            if method in ('GET','HEAD'):
                if route=='/ui/accounts/search':
                    from atraxis.web_account_search import render
                    from atraxis.account_search import parse_query
                    return 200,render(admin.search(parse_query(path))),{}
                data=admin.inspect()
                if route=='/ui/accounts' and not urlsplit(path).query:return 200,overview(data),{}
                pairs=parse_qsl(urlsplit(path).query,keep_blank_values=True,max_num_fields=1,errors='strict')
                if route!='/ui/accounts/edit' or len(pairs)!=1 or pairs[0][0]!='operation' or pairs[0][1] not in ACCOUNT_OPERATIONS:
                    raise AccountError('invalid_account_path')
                operation=pairs[0][1]
                token=self.tokens.issue(admin,'accounts.edit',operation,data['revision'])
                body='<h2>'+text(LABELS[operation])+'</h2><p>Only the permissions shown on your accounts page can be used. Identifiers are fixed; updates preserve entity ownership.</p>'
                body+='<form class="editor" method="post" action="/ui/accounts/review">'+hidden('token',token)+hidden('operation',operation)
                for name,label,value in FIELDS[operation]:
                    body+='<label>'+text(label)+'<input name="'+name+'" value="'+text(value)+'" maxlength="8192" autocomplete="off"></label>'
                return 200,page(body+'<button class="button" type="submit">Review change</button></form>'),{}
            if (method!='POST' or urlsplit(path).query or route not in ('/ui/accounts/review','/ui/accounts/commit')
                    or headers.get('origin')!=self.origin or headers.get('sec-fetch-site') not in (None,'same-origin')
                    or headers.get('content-type')!='application/x-www-form-urlencoded'):
                raise PermissionError('account_origin_or_method_denied')
            fields=parse_form(raw)
            token=fields.pop('token',None)
            if route.endswith('/review'):
                binding=self.tokens.consume(token,admin,'accounts.edit')
                if fields.get('operation')!=binding['name']:raise PermissionError('account_operation_changed')
                plan=admin.prepare(request_from_fields(fields),binding['revision'])
                token=self.tokens.issue(admin,'accounts.commit',binding['name'],plan['revision'],plan)
                body='<h2>Review: '+text(LABELS[plan['request']['operation']])+'</h2><p>Account: '+text(plan['audit']['subject'])+' · Organization: '+text(plan['audit']['organization'] or 'Global accounts')+'</p>'
                body+='<dl>'+''.join('<dt>'+text(key.replace('_',' ').capitalize())+'</dt><dd>'+text(', '.join(value) if type(value) is list else value if value is not None else 'Organization membership')+'</dd>' for key,value in plan['request'].items())+'</dl>'
                body+='<p>Membership changes can enable or revoke existing Atropos group grants.</p><p>Expected revision: <code>'+text(plan['revision'])+'</code></p><form class="activation" method="post" action="/ui/accounts/commit">'+hidden('token',token)
                body+='<label><input type="checkbox" name="confirm" value="yes" required>I confirm this account change.</label><button class="button" type="submit">Confirm change</button></form>'
                return 200,page(body),{}
            binding=self.tokens.consume(token,admin,'accounts.commit')
            if fields!={'confirm':'yes'}:raise PermissionError('account_confirmation_required')
            admin.commit(binding['context'])
            return 303,page('<p>Account change saved.</p>'),{'Location':'/ui/accounts'}
        except PermissionError:
            status,message=403,'Access denied or form expired. Open a fresh form.'
        except AccountError as error:
            status=409 if str(error)=='account_revision_conflict' else 400
            message='The directory changed. Review the current version.' if status==409 else 'Invalid account data or unavailable private configuration.'
        except (ValueError,TypeError,KeyError,RecursionError):
            status,message=400,'Invalid account request.'
        except Exception:
            status,message=503,'Outcome unconfirmed. Check the directory before trying again; no automatic retry was attempted.'
        return status,page('<p class="notice" role="alert">'+message+'</p>'),{}
