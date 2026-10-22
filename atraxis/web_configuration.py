"""Explicit source setup forms; no secret fields, probes or implicit restarts."""
from urllib.parse import parse_qsl, urlencode, urlsplit

from atraxis.accounts import AccountError
from atraxis.configuration_admin import ConfigurationAdmin, SETTINGS, OPERATIONS
from atraxis.web import text
from atraxis.web_accounts import HEADERS, CSS, FORM_BYTES, hidden, parse_form
from atraxis.web_rule_editor import FormTokens, validate_origin

LABELS={'source.add':'Add source','source.update':'Edit source','source.remove':'Remove source'}
FIELD_LABELS={'timeout':'Request timeout (seconds)','poll_interval':'Collection interval (seconds)','max_age':'Maximum observation age (seconds)'}


def page(body):
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
        '<title>Source configuration · Atraxis</title><style>'+CSS+'</style></head><body><a class="skip" href="#configuration">Skip to configuration</a>'
        '<header><div class="brand">ATRA<span>XIS</span></div><nav aria-label="Main"><a href="/ui">Inventory</a> '
        '<a href="/ui/configuration">Configuration</a></nav></header><main id="configuration"><div class="eyebrow">Installation administration</div>'
        '<h1>Source configuration</h1>'+body+'<footer>Changes take effect after an explicit daemon restart. Saving never contacts sources or runs an intervention.</footer></main></body></html>')


def summary(value):
    if value is None:return '<p>None</p>'
    return '<dl>'+''.join('<dt>'+text(label)+'</dt><dd>'+text(str(value.get(key) or 'Host-managed'))+'</dd>' for key,label in
        [('source_id','Source ID'),('connection','Approved connection'),('kind','Type'),('origin','Origin'),*FIELD_LABELS.items()])+'</dl>'


class WebConfiguration:
    def __init__(self,profile_path,config_path,origin,active_sources):
        self.profile_path,self.config_path=profile_path,config_path
        self.origin=validate_origin(origin);self.active_sources=active_sources;self.tokens=FormTokens()

    def handle(self,path,username,*,method='GET',headers=None,raw=b''):
        headers=headers or {}
        try:
            if username is None:raise PermissionError('authentication_required')
            if len(path.encode())>2048:raise ValueError('invalid_path')
            admin=ConfigurationAdmin(self.profile_path,self.config_path,username)
            parsed=urlsplit(path);route=parsed.path
            if method in ('GET','HEAD'):
                data=admin.inspect(self.active_sources)
                if route=='/ui/configuration' and not parsed.query:
                    pending=data['restart_required']
                    body='<p>Acting account: '+text(data['identity']['subject'])+' · Installation-wide source administration</p>'
                    body+='<p class="notice">'+('Saved changes are pending. Restart the daemon explicitly to apply them.' if pending else 'Saved sources match the running configuration.')+'</p>'
                    body+='<p><a class="button" href="/ui/configuration/edit?operation=source.add">Add source</a></p>'
                    for source in data['sources']:
                        body+='<section class="source"><h2>'+text(source['source_id'])+'</h2>'+summary(source)
                        for operation in ('source.update','source.remove'):
                            body+='<a class="button" href="/ui/configuration/edit?'+urlencode(dict(operation=operation,source=source['source_id']))+'">'+LABELS[operation]+'</a> '
                        body+='</section>'
                    if not data['sources']:body+='<p>No saved sources. Inventory will not be ready.</p>'
                    body+='<h2>Recent configuration changes</h2>'
                    body+=''.join('<p>'+text(e['time'])+' · '+text(e['actor'])+' · '+text(LABELS[e['operation']])+' · '+text(e['source_id'])+'</p>' for e in reversed(data['audit'])) or '<p>No recorded changes.</p>'
                    return 200,page(body),{}
                pairs=parse_qsl(parsed.query,keep_blank_values=True,strict_parsing=True,max_num_fields=2,errors='strict')
                query=dict(pairs)
                if route!='/ui/configuration/edit' or len(query)!=len(pairs) or query.get('operation') not in OPERATIONS:
                    raise ValueError('invalid_configuration_query')
                operation=query['operation'];source_id=query.get('source','')
                if set(query)!=({'operation'} if operation=='source.add' else {'operation','source'}):raise ValueError('invalid_configuration_query')
                if operation=='source.remove':return self.review(admin,admin.prepare(dict(operation=operation,source_id=source_id),data['revision']))
                source=next((s for s in data['sources'] if s['source_id']==source_id),None)
                if operation=='source.update' and source is None:raise ValueError('unknown_source')
                source=source or dict(timeout=10,poll_interval=60,max_age=180,connection='')
                token=self.tokens.issue(admin,'configuration.edit',source_id,data['revision'],dict(operation=operation))
                body='<h2>'+LABELS[operation]+'</h2><form class="editor" method="post" action="/ui/configuration/review">'+hidden('token',token)
                body+=('<label>Source ID<input name="source_id" required maxlength="128" autocomplete="off"></label>' if operation=='source.add'
                       else '<p>Source ID: <strong>'+text(source_id)+'</strong></p>')
                body+='<label for="connection">Approved connection</label><select id="connection" name="connection" required><option value="">Choose a connection</option>'
                for connection in data['connections']:
                    body+='<option value="'+text(connection['id'])+'"'+(' selected' if connection['id']==source['connection'] else '')+'>'+text(connection['id']+' · '+connection['kind']+' · '+connection['origin'])+'</option>'
                body+='</select><p>Connections and credentials are provisioned on the server by the installation administrator.</p>'
                for field in SETTINGS:
                    body+='<label>'+FIELD_LABELS[field]+'<input type="number" min="1" max="'+('60' if field=='timeout' else '3600' if field=='poll_interval' else '86400')+'" name="'+field+'" value="'+str(source[field])+'" required></label>'
                body+='<p>Timeout ≤ collection interval ≤ maximum age.</p><button class="button" type="submit">Review source change</button></form>'
                return 200,page(body),{}
            if (method!='POST' or parsed.query or route not in ('/ui/configuration/review','/ui/configuration/commit')
                    or headers.get('origin')!=self.origin or headers.get('sec-fetch-site') not in (None,'same-origin')
                    or headers.get('content-type')!='application/x-www-form-urlencoded'):
                raise PermissionError('origin_denied')
            fields=parse_form(raw);token=fields.pop('token',None)
            if route.endswith('/review'):
                binding=self.tokens.consume(token,admin,'configuration.edit');operation=binding['context']['operation']
                expected={'connection',*SETTINGS}|({'source_id'} if operation=='source.add' else set())
                if set(fields)!=expected:raise ValueError('invalid_configuration_form')
                request=dict(operation=operation,source_id=fields.get('source_id',binding['name']),connection=fields['connection'])
                for field in SETTINGS:
                    value=fields[field]
                    if not value.isascii() or not value.isdigit() or len(value)>5:raise ValueError('invalid_collection_limit')
                    request[field]=int(value)
                return self.review(admin,admin.prepare(request,binding['revision']))
            binding=self.tokens.consume(token,admin,'configuration.commit')
            if fields!={'confirm':'yes'}:raise PermissionError('confirmation_required')
            admin.commit(binding['context'])
            return 303,page('<p>Source configuration saved. Restart the daemon explicitly to apply it.</p>'),{'Location':'/ui/configuration'}
        except PermissionError:
            code,message=403,'Access denied or form expired. Open a fresh form.'
        except AccountError as error:
            code=409 if str(error)=='configuration_revision_conflict' else 400
            message='Configuration or permissions changed. Review the current configuration.' if code==409 else 'Invalid configuration or capacity reached. No change was saved.'
        except (ValueError,TypeError,KeyError,RecursionError):
            code,message=400,'Invalid source configuration request.'
        except Exception:
            code,message=503,'Outcome unconfirmed. Inspect the saved configuration and recent changes before trying again. No automatic retry was attempted.'
        return code,page('<p class="notice" role="alert">'+message+'</p>'),{}

    def review(self,admin,plan):
        token=self.tokens.issue(admin,'configuration.commit',plan['request']['source_id'],plan['revision'],plan)
        body='<h2>Review: '+LABELS[plan['request']['operation']]+'</h2><p>Acting account: '+text(plan['identity']['subject'])+'</p>'
        body+='<h3>Current saved source</h3>'+summary(plan['before'])+'<h3>Proposed source</h3>'+summary(plan['after'])
        body+='<p>This changes collection for the whole installation after a daemon restart. Source read grants and intervention bindings are managed separately.</p>'
        body+='<form class="activation" method="post" action="/ui/configuration/commit">'+hidden('token',token)
        body+='<label><input type="checkbox" name="confirm" value="yes" required>I confirm this source configuration change.</label><button class="button" type="submit">Save configuration</button></form>'
        return 200,page(body),{}
