"""Explicit single-use rule archive/unarchive/delete confirmations."""
from urllib.parse import parse_qsl, urlencode, urlsplit

from atraxis.web import text
from atraxis.web_rules import RuleReader, NAME, MAX_QUERY_BYTES, valid_revision, page
from atraxis.web_rule_editor import WebEditor, record_checked, context_checked
from atraxis.web_rule_activation import confirmation

RETIREMENT_ROUTES = ('archive', 'unarchive', 'delete')
DESCRIPTIONS = {
    'archive': 'Archive this inactive rule while preserving every revision, test report and audit record.',
    'unarchive': 'Make this archived rule available again. It will remain inactive.',
    'delete': 'Permanently remove draft contents and test reports. Only never-activated rules without linked interventions can be deleted. Audit remains and the name cannot be reused.',
}
ERRORS = {400: 'Invalid lifecycle request.', 403: 'Access denied or confirmation expired.',
          404: 'Rule not available.', 409: 'Rule state changed or this operation is not permitted for its lifecycle. Review its status and audit.',
          503: 'Rule service unavailable. No automatic retry was attempted.'}


class RetirementClient(RuleReader):
    methods = frozenset(('draft', 'availability', 'archive', 'unarchive', 'delete'))


def checked(state):
    if (type(state) is not dict or state.get('status') not in ('available','archived','deleted')
            or 'generation' not in state or state['generation'] is not None and not valid_revision(state['generation'])):
        raise ValueError('invalid_web_rule_response')
    return state


def review(path, reader, tokens, operation):
    if type(path) is not str or len(path.encode()) > MAX_QUERY_BYTES:
        raise ValueError('invalid_rule_request')
    pairs = parse_qsl(urlsplit(path).query, keep_blank_values=True, max_num_fields=1, errors='strict')
    if len(pairs) != 1 or pairs[0][0] != 'name' or not NAME.fullmatch(pairs[0][1]):
        raise ValueError('invalid_rule_request')
    name = pairs[0][1]
    head = reader.call('draft', name)
    record_checked(head, name)
    context = context_checked(head.get('context'))
    state = checked(reader.call('availability', name))
    actor = reader.entry()
    body = '<h2>Review ' + operation + '</h2><p class="actor">Account: ' + text(actor['subject']) + ' · Organization: ' + text(actor['organization']) + '</p>'
    body += '<p>' + DESCRIPTIONS[operation] + '</p><section class="source"><h3>' + text(name) + '</h3><p>Status: ' + text(state['status']) + '</p>'
    body += '<p>Draft revision: <code>' + text(head['revision']) + '</code></p><h3>Current targets</h3><ul>'
    body += ''.join('<li>' + text(kind) + ': <code>' + text(value) + '</code></li>' for kind,value in context['targets'])
    body += '</ul><p>Atropos also checks this permission for all historical scopes before changing the rule.</p></section>'
    token = tokens.issue(reader, operation, name, head['revision'],
                         dict(expected_revision=head['revision'], expected_generation=state['generation']))
    body += '<form method="post" action="/ui/rules/' + operation + '"><input type="hidden" name="token" value="' + text(token) + '">'
    body += '<label><input type="checkbox" name="confirm" value="' + operation + '" required> I confirm ' + operation + ' for this rule.</label>'
    body += '<button class="button" type="submit">Confirm ' + operation + '</button></form>'
    body += '<p><a href="/ui/rules/audit?' + text(urlencode({'name':name})) + '">Rule audit</a></p>'
    reader.entry()
    return page(body, name, workbench=True)


from atraxis.web_organization_context import contextual

class WebRetirement(WebEditor):
    @contextual
    def handle(self, route, path, username, *, method='GET', headers=None, raw=b'', organization=None):
        attempted = False
        try:
            if route not in RETIREMENT_ROUTES: raise ValueError('invalid_rule_request')
            reader = RetirementClient(self.mapping, username, self.factory, organization=organization)
            if method in ('GET','HEAD'):
                return 200, review(path, reader, self.tokens, route), {}
            headers = headers or {}
            if (method != 'POST' or path != '/ui/rules/'+route or headers.get('origin') != self.origin
                    or headers.get('sec-fetch-site','same-origin') != 'same-origin'):
                raise PermissionError('invalid_web_origin')
            pending = self.tokens.consume(confirmation(raw, route), reader, route)
            attempted = True
            result = checked(reader.call(route, pending['name'], **pending['context']))
            if (result['status'] != {'archive':'archived','unarchive':'available','delete':'deleted'}[route]
                    or result['generation'] is None or result['generation'] == pending['context']['expected_generation']
                    or result.get('revision') != pending['revision']):
                raise ValueError('invalid_web_rule_response')
            return 303, '', {'Location':'/ui/rules/audit?'+urlencode({'name':pending['name']})}
        except PermissionError:
            status = 403
        except Exception as error:
            status = {'invalid_rule_request':400,'rule_not_found':404,'rule_conflict':409}.get(str(error),503)
        message = ERRORS[status]
        if attempted and status in (403,503):
            message = 'Outcome unconfirmed. Check rule availability and audit before any new attempt. No automatic retry was attempted.'
        return status, page('<p class="notice" role="alert">'+message+'</p>',workbench=True), {}
