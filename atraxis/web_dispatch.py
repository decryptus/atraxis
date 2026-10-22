"""Explicit real admission with freshly signed observations and one confirmation."""
import hashlib
import json
import secrets
from urllib.parse import parse_qsl, urlencode, urlsplit

from atraxis.intervention_search import DIGEST, date_text
from atraxis.web import text
from atraxis.web_intervention_detail import receipt, timestamp
from atraxis.web_intervention_search import page as intervention_page
from atraxis.web_observations import ObservationReader, WebObservationSession
from atraxis.web_organization_context import contextual
from atraxis.web_rule_activation import confirmation
from atraxis.web_rule_editor import WebEditor
from atraxis.web_rules import NAME, IDENTIFIER, valid_revision, MAX_QUERY_BYTES

DISPATCH_ROUTES = ('dispatch', 'dispatch-confirm')
FORM_BYTES = 4096
MAX_PLAN_BYTES = 524288
MAX_STEPS = 128
MAX_FACT_BYTES = 65536
FORM_FIELDS = frozenset({'token', 'name', 'kind', 'target', 'binding'})
PLAN_FIELDS = frozenset({'binding', 'decision', 'target', 'workflow', 'workflow_revision',
                         'input', 'max_age_seconds', 'effects', 'facts', 'observation'})
ERRORS = {400: 'Invalid intervention form. Open a fresh form and check your input.',
          403: 'Intervention access denied or confirmation expired.',
          404: 'Rule unavailable.',
          409: 'The active rule or decision changed. Review current observations again.',
          503: 'Intervention service or current observations unavailable. No automatic retry was attempted.'}


def page(body):
    return intervention_page(body, dispatch=True)


def form_start(route, token):
    return ('<form class="editor" method="post" action="/ui/rules/' + route + '">'
            '<input type="hidden" name="token" value="' + text(token) + '">')


def fields(raw):
    if type(raw) is not bytes or not 0 < len(raw) <= FORM_BYTES:
        raise ValueError('invalid_rule_request')
    pairs = parse_qsl(raw.decode(), keep_blank_values=True, strict_parsing=True,
                      max_num_fields=5, errors='strict')
    values = dict(pairs)
    if (len(values) != len(pairs) or set(values) != FORM_FIELDS or len(values['token']) != 43
            or not NAME.fullmatch(values['name']) or values['kind'] != 'container'
            or not IDENTIFIER.fullmatch(values['target']) or not DIGEST.fullmatch(values['binding'])):
        raise ValueError('invalid_rule_request')
    return values


def checked_plan(data, target, binding, actor, source):
    if (type(data) is not dict or set(data) != PLAN_FIELDS
            or len(json.dumps(data, allow_nan=False).encode()) > MAX_PLAN_BYTES
            or data['binding'] != binding or data['target'] != '/'.join([actor['organization'], *target])
            or type(data['input']) is not dict or type(data['facts']) is not dict
            or len(json.dumps(data['facts'], allow_nan=False).encode()) > 65536
            or type(data['max_age_seconds']) is not int or not 1 <= data['max_age_seconds'] <= 300):
        raise ValueError('invalid_dispatch_response')
    decision, workflow, observation = data['decision'], data['workflow'], data['observation']
    if (type(decision) is not dict or set(decision) != {'revision', 'generation', 'target', 'result'}
            or not valid_revision(decision['revision']) or not valid_revision(decision['generation'])
            or decision['target'] != target or type(decision['result']) is not dict
            or set(decision['result']) != {'decision', 'matched_rule'}
            or any(type(v) is not str or not 1 <= len(v) <= 128 for v in decision['result'].values())
            or type(workflow) is not dict or type(workflow.get('name')) is not str
            or not NAME.fullmatch(workflow['name']) or type(workflow.get('steps')) is not dict
            or not 1 <= len(workflow['steps']) <= MAX_STEPS
            or data['workflow_revision'] != hashlib.sha256(json.dumps(workflow, allow_nan=False,
                sort_keys=True, separators=(',', ':')).encode()).hexdigest()):
        raise ValueError('invalid_dispatch_response')
    for step, value in workflow['steps'].items():
        if (not NAME.fullmatch(step) or type(value) is not dict or type(value.get('action')) is not str
                or not NAME.fullmatch(value['action'])):
            raise ValueError('invalid_dispatch_response')
    expected = dict(source_id=source.source_id, native_id=source.native_id,
                    filesystem=list(source.filesystem) if source.filesystem is not None else None)
    if (type(observation) is not dict or set(observation) != set(expected) | {'expires_at'}
            or any(observation[key] != value for key, value in expected.items())
            or not timestamp(observation['expires_at'])):
        raise ValueError('invalid_dispatch_response')
    return data


class WebDispatch(WebEditor):
    def __init__(self, mapping, origin, factory, profile, access, service):
        super().__init__(mapping, origin, factory)
        self.profile, self.access, self.service = profile, access, service

    @contextual
    def handle(self, route, path, username, *, method='GET', headers=None, raw=b'', organization=None):
        session = None
        pending = None
        try:
            if route not in DISPATCH_ROUTES or urlsplit(path).path != '/ui/rules/' + route:
                raise ValueError('invalid_rule_request')
            reader = ObservationReader(self.mapping, username, self.factory, organization=organization)
            if method in ('GET', 'HEAD'):
                if route != 'dispatch' or len(path.encode()) > MAX_QUERY_BYTES or urlsplit(path).query:
                    raise ValueError('invalid_rule_request')
                token = self.tokens.issue(reader, route, '', None)
                body = '<h2>Review a real intervention</h2><p>Supply the observation-required binding ID approved by your administrator. Current measurements are collected under your inventory access. This form accepts no manual facts or action parameters.</p>'
                body += form_start(route, token)
                for key, label, limit in [('name', 'Rule name', 64), ('target', 'Container target ID', 128), ('binding', 'Approved binding ID', 64)]:
                    body += '<label>' + label + '<input name="' + key + '" required maxlength="' + str(limit) + '"></label>'
                body += '<input type="hidden" name="kind" value="container"><button type="submit">Review current observations</button></form>'
                reader.entry()
                return 200, page(body), {}
            headers = headers or {}
            if (method != 'POST' or path != '/ui/rules/' + route or headers.get('origin') != self.origin
                    or headers.get('sec-fetch-site', 'same-origin') != 'same-origin'):
                raise PermissionError('invalid_web_origin')
            if route == 'dispatch':
                try:
                    values = fields(raw)
                except (ValueError, UnicodeError):
                    raise ValueError('invalid_rule_request') from None
                self.tokens.consume(values['token'], reader, route)
                name, target, binding = values['name'], [values['kind'], values['target']], values['binding']
                session = WebObservationSession(reader, self.profile, self.access, self.service)
                plan = checked_plan(session.dispatcher.plan(name, target, binding), target,
                                    binding, reader.initial, session.binding(target))
                session.guard()
                event = secrets.token_hex(16)
                context = dict(target=target, binding=binding, event=event,
                               generation=plan['decision']['generation'], profile=session.revision)
                token = self.tokens.issue(reader, 'dispatch-confirm', name, plan['decision']['revision'], context)
                body = '<h2>Confirm a real intervention</h2>'
                for label, value in [('Rule', name), ('Target', plan['target']),
                                     ('Revision', plan['decision']['revision']), ('Generation', context['generation']),
                                     ('Workflow', plan['workflow']['name']), ('Workflow revision', plan['workflow_revision']),
                                     ('Binding', binding), ('Event', event), ('Source', plan['observation']['source_id']),
                                     ('Native container', plan['observation']['native_id']),
                                     ('Preview evidence expires (UTC)', date_text(plan['observation']['expires_at']))]:
                    body += '<p>' + label + ': <code>' + text(value) + '</code></p>'
                body += '<h3>Approved steps</h3><ul>' + ''.join('<li>' + text(step + ': ' + value['action']) + '</li>' for step, value in plan['workflow']['steps'].items()) + '</ul>'
                body += '<h3>Current facts</h3><pre>' + text(json.dumps(plan['facts'], indent=2, ensure_ascii=False)) + '</pre>'
                body += '<p>This review has not queued work. Confirmation collects current measurements again and pins this binding and active generation. Fixed action parameters remain engine-approved; this page cannot edit them.</p><p>Real actions and notifications may follow admission. Keep the event identifier. Queued does not mean completed; an uncertain effect must be reconciled before a new submission.</p>'
                body += form_start('dispatch-confirm', token) + '<label><input type="checkbox" name="confirm" value="dispatch-confirm" required> I authorize this real intervention for the target and approved workflow shown above.</label><button type="submit">Confirm real intervention</button></form>'
                session.guard()
                return 200, page(body), {}
            pending = self.tokens.consume(confirmation(raw, route), reader, route)
            context = pending['context']
            session = WebObservationSession(reader, self.profile, self.access, self.service)
            if session.revision != context['profile']:
                raise ValueError('rule_conflict')
            result = session.dispatcher.dispatch(pending['name'], context['target'], context['binding'],
                expected_generation=context['generation'], event_id=context['event'])
            if type(result) is not dict or type(result.get('id')) is not str or not DIGEST.fullmatch(result['id']):
                raise ValueError('invalid_dispatch_response')
            receipt(result, dict(mode='real', id=result['id'], rule=pending['name'], revision=pending['revision'],
                    event_id=context['event'], target='/'.join([organization, *context['target']])))
            if (result['generation'] != context['generation'] or result['binding'] != context['binding']
                    or 'observation' not in result):
                raise ValueError('invalid_dispatch_response')
            return 303, '', {'Location': '/ui/interventions/detail?' + urlencode(dict(
                name=pending['name'], mode='real', id=result['id'], organization=organization))}
        except PermissionError:
            status = 403
        except Exception as error:
            status = {'invalid_rule_request': 400, 'rule_not_found': 404, 'rule_conflict': 409,
                      'active_version_changed': 409}.get(str(error), 503)
        body = '<p role="alert">' + ERRORS[status] + '</p>'
        if session is not None and session.submitted and pending is not None:
            context = pending['context']
            body += '<p>Outcome unconfirmed. The real intervention may already have been admitted. No automatic retry was attempted. Event: <code>' + text(context['event']) + '</code>.</p>'
            link = '/ui/interventions?' + urlencode(dict(name=pending['name'], mode='real', q=context['event'], organization=organization))
            body += '<p><a href="' + text(link) + '">Check this event before submitting again</a></p>'
        return status, page(body), {}
