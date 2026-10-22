"""Explicit browser admission to engine-owned test workflows only."""
import json
import secrets
from urllib.parse import parse_qsl, urlencode, urlsplit

from atraxis.intervention_search import DIGEST
from atraxis.web import text
from atraxis.web_intervention_detail import marker, receipt
from atraxis.web_intervention_search import page as intervention_page
from atraxis.web_organization_context import contextual
from atraxis.web_rule_activation import confirmation
from atraxis.web_rule_editor import WebEditor, strict_json, FORM_BYTES
from atraxis.web_rules import RuleReader, NAME, IDENTIFIER, valid_revision, MAX_QUERY_BYTES

SIMULATION_ROUTES = ('simulate', 'simulate-confirm')
FACT_BYTES = 65536
MAX_BINDINGS = 128
MAX_BINDING_BYTES = 65536
MAX_ACTIONS = 128
TARGET_KINDS = ('server', 'container')
BINDING_FIELDS = frozenset({'id', 'rule', 'revision', 'workflow', 'actions', 'test_supported'})
FORM_FIELDS = frozenset({'token', 'binding', 'facts'})
ERRORS = {400: 'Invalid simulation form. Open a fresh form and check your input.',
          403: 'Simulation access denied or confirmation expired.',
          404: 'Rule unavailable.', 409: 'The approved workflow changed or is no longer accessible. Open a fresh form.',
          503: 'Simulation service unavailable. No automatic retry was attempted.'}


def page(body):
    return intervention_page(body, simulation=True)


class SimulationClient(RuleReader):
    methods = frozenset({'test_bindings', 'test_dispatch'})


def bindings(reader, name, target):
    rows = reader.call('test_bindings', name, target)
    if (type(rows) is not list or len(rows) > MAX_BINDINGS
            or len(json.dumps(rows, allow_nan=False).encode()) > MAX_BINDING_BYTES):
        raise ValueError('invalid_simulation_response')
    seen = set()
    for row in rows:
        if (type(row) is not dict or set(row) != BINDING_FIELDS
                or type(row['id']) is not str or not DIGEST.fullmatch(row['id'])
                or row['id'] in seen or row['rule'] != name or not valid_revision(row['revision'])
                or type(row['workflow']) is not str or not NAME.fullmatch(row['workflow'])
                or type(row['test_supported']) is not bool or type(row['actions']) is not list
                or not 1 <= len(row['actions']) <= MAX_ACTIONS
                or any(type(action) is not str or not NAME.fullmatch(action) for action in row['actions'])
                or len(set(row['actions'])) != len(row['actions'])):
            raise ValueError('invalid_simulation_response')
        seen.add(row['id'])
    return [row for row in rows if row['test_supported']]


def query(path):
    if len(path.encode()) > MAX_QUERY_BYTES:
        raise ValueError('invalid_rule_request')
    pairs = parse_qsl(urlsplit(path).query, keep_blank_values=True, strict_parsing=True,
                      max_num_fields=3, errors='strict')
    values = dict(pairs)
    if (len(values) != len(pairs) or set(values) not in (set(), {'name'}, {'name', 'kind', 'target'})
            or 'name' in values and not NAME.fullmatch(values['name'])
            or 'kind' in values and (values['kind'] not in TARGET_KINDS
                                    or not IDENTIFIER.fullmatch(values['target']))):
        raise ValueError('invalid_rule_request')
    return values


def fields(raw):
    if type(raw) is not bytes or not 0 < len(raw) <= FORM_BYTES:
        raise ValueError('invalid_rule_request')
    pairs = parse_qsl(raw.decode(), keep_blank_values=True, strict_parsing=True,
                      max_num_fields=3, errors='strict')
    values = dict(pairs)
    if (len(values) != len(pairs) or set(values) != FORM_FIELDS or len(values['token']) != 43
            or not DIGEST.fullmatch(values['binding']) or len(values['facts'].encode()) > FACT_BYTES):
        raise ValueError('invalid_rule_request')
    try:
        facts = strict_json(values['facts'])
        if type(facts) is not dict or len(json.dumps(facts, allow_nan=False).encode()) > FACT_BYTES:
            raise ValueError()
    except (ValueError, TypeError, RecursionError):
        raise ValueError('invalid_rule_request') from None
    return values, facts


def form_start(route, token):
    return ('<form class="editor" method="post" action="/ui/rules/' + route + '">'
            '<input type="hidden" name="token" value="' + text(token) + '">')


def search_link(name, event, organization):
    return '/ui/interventions?' + urlencode(dict(name=name, mode='test', q=event, organization=organization))


class WebSimulation(WebEditor):
    @contextual
    def handle(self, route, path, username, *, method='GET', headers=None, raw=b'', organization=None):
        attempted = False
        pending = None
        try:
            if route not in SIMULATION_ROUTES or urlsplit(path).path != '/ui/rules/' + route:
                raise ValueError('invalid_rule_request')
            reader = SimulationClient(self.mapping, username, self.factory, organization=organization)
            if method in ('GET', 'HEAD'):
                if route != 'simulate':
                    raise ValueError('invalid_rule_request')
                try:
                    values = query(path)
                except (ValueError, UnicodeError):
                    raise ValueError('invalid_rule_request') from None
                body = '<h2>Launch a test simulation</h2><p>Only approved workflows with registered simulators are available. Manual facts are test input, not trusted telemetry. No rule activation or real intervention is requested.</p>'
                body += '<form class="editor" method="get" action="/ui/rules/simulate"><label>Rule name<input name="name" required maxlength="64" value="' + text(values.get('name', '')) + '"></label><label>Target kind<select aria-label="Target kind" name="kind">' + ''.join('<option' + (' selected' if kind == values.get('kind', 'server') else '') + '>' + kind + '</option>' for kind in TARGET_KINDS) + '</select></label><label>Target ID<input name="target" required maxlength="128" value="' + text(values.get('target', '')) + '"></label><button type="submit">Find test workflows</button></form>'
                if 'target' in values:
                    name, target = values['name'], [values['kind'], values['target']]
                    rows = bindings(reader, name, target)
                    body += '<p>Selected target: <code>' + text(':'.join(target)) + '</code></p>'
                    if rows:
                        token = self.tokens.issue(reader, route, name, None, dict(target=target, bindings=rows))
                        body += form_start(route, token) + '<label>Approved test workflow<select aria-label="Approved test workflow" name="binding">'
                        body += ''.join('<option value="' + row['id'] + '">' + text(row['workflow'] + ' · revision ' + row['revision'] + ' · ' + row['id']) + '</option>' for row in rows)
                        body += '</select></label><label>Facts JSON<textarea aria-label="Facts JSON" name="facts" required rows="10" maxlength="' + str(FACT_BYTES) + '">{}</textarea></label><button type="submit">Review simulation</button></form>'
                    else:
                        body += '<p>No accessible workflow supports test execution for this target.</p>'
                reader.entry()
                return 200, page(body), {}
            headers = headers or {}
            if (method != 'POST' or path != '/ui/rules/' + route or headers.get('origin') != self.origin
                    or headers.get('sec-fetch-site', 'same-origin') != 'same-origin'):
                raise PermissionError('invalid_web_origin')
            if route == 'simulate':
                try:
                    values, facts = fields(raw)
                except (ValueError, UnicodeError):
                    raise ValueError('invalid_rule_request') from None
                pending = self.tokens.consume(values['token'], reader, route)
                context = pending['context']
                selected = next((row for row in context['bindings'] if row['id'] == values['binding']), None)
                if selected is None:
                    raise ValueError('invalid_rule_request')
                if selected not in bindings(reader, pending['name'], context['target']):
                    raise ValueError('rule_conflict')
                event = secrets.token_hex(16)
                context = dict(target=context['target'], binding=selected, facts=facts, event=event)
                token = self.tokens.issue(reader, 'simulate-confirm', pending['name'], selected['revision'], context)
                body = '<h2>Review test simulation</h2>'
                for label, value in [('Rule', pending['name']), ('Target', ':'.join(context['target'])),
                                     ('Workflow', selected['workflow']), ('Revision', selected['revision']),
                                     ('Binding', selected['id']), ('Actions', ', '.join(selected['actions'])), ('Event', event)]:
                    body += '<p>' + label + ': <code>' + text(value) + '</code></p>'
                body += '<pre>' + text(json.dumps(facts, indent=2, ensure_ascii=False)) + '</pre>'
                body += '<p>This review has not queued work. Confirmation evaluates these facts and queues a marked test only if they match the approved binding. Registered simulators run; an approved test alert receiver may be contacted. Receiver acceptance does not prove downstream delivery.</p>'
                body += '<p>Keep the event identifier to check the outcome if the response is lost.</p>'
                body += form_start('simulate-confirm', token) + '<label><input type="checkbox" name="confirm" value="simulate-confirm" required> I confirm this test simulation for the target and workflow shown above.</label><button type="submit">Confirm test simulation</button></form>'
                reader.entry()
                return 200, page(body), {}
            pending = self.tokens.consume(confirmation(raw, route), reader, route)
            context = pending['context']
            if context['binding'] not in bindings(reader, pending['name'], context['target']):
                raise ValueError('rule_conflict')
            attempted = True
            result = reader.call('test_dispatch', pending['name'], context['target'], context['facts'],
                                 context['binding']['id'], event_id=context['event'])
            if type(result) is not dict:
                raise ValueError('invalid_simulation_response')
            if result.get('status') == 'not_matched':
                if (result.get('simulation') is not True or result.get('matches_binding') is not False
                        or result.get('binding') != context['binding']['id'] or result.get('revision') != pending['revision']
                        or result.get('target') != context['target'] or type(result.get('test_context')) is not dict
                        or type(result['test_context'].get('id')) is not str
                        or not DIGEST.fullmatch(result['test_context']['id'])
                        or not marker(result['test_context'], result['test_context']['id'])):
                    raise ValueError('invalid_simulation_response')
                return 200, page('<h2>No matching test decision</h2><p>No test run was admitted for event <code>' + text(context['event']) + '</code>.</p>'), {}
            run_id = result.get('id')
            if type(run_id) is not str or not DIGEST.fullmatch(run_id):
                raise ValueError('invalid_simulation_response')
            receipt(result, dict(mode='test', id=run_id, rule=pending['name'], revision=pending['revision']))
            return 303, '', {'Location': '/ui/interventions/detail?' + urlencode(dict(name=pending['name'], mode='test', id=run_id, organization=organization))}
        except PermissionError:
            status = 403
        except Exception as error:
            status = {'invalid_rule_request': 400, 'rule_not_found': 404, 'rule_conflict': 409}.get(str(error), 503)
        body = '<p role="alert">' + ERRORS[status] + '</p>'
        if attempted:
            context = pending['context']
            body += '<p>Outcome unconfirmed. The test may already have been admitted. No automatic retry was attempted. Event: <code>' + text(context['event']) + '</code>.</p>'
            body += '<p><a href="' + text(search_link(pending['name'], context['event'], organization)) + '">Check this test event before submitting again</a></p>'
        return status, page(body), {}
