"""Operator attestation with exact-state review and a single-use confirmation."""
import json
from urllib.parse import parse_qsl, urlencode, urlsplit

from atraxis.web import text
from atraxis.web_rules import RuleReader
from atraxis.intervention_search import DIGEST
from atraxis.web_intervention_detail import query, lookup, receipt, InterventionUnavailable
from atraxis.web_intervention_search import page
from atraxis.web_organization_context import contextual
from atraxis.web_rule_activation import confirmation
from atraxis.web_rule_editor import WebEditor

RECONCILIATION_ROUTES = ('reconcile', 'reconcile-confirm')
FORM_BYTES = 16384
MAX_EVIDENCE_BYTES = 2048
MAX_PLAN_BYTES = 524288
OUTCOMES = frozenset({'succeeded', 'failed'})
ERRORS = {400: 'Invalid reconciliation form. Open a fresh review.',
          403: 'Reconciliation access denied or confirmation expired.',
          404: 'Intervention unavailable.',
          409: 'The intervention state changed. Review it again before confirming.',
          503: 'Reconciliation service unavailable. No automatic retry was attempted.'}


class ReconciliationReader(RuleReader):
    methods = frozenset({'search_interventions', 'plan_reconciliation', 'reconcile'})


def evidence_text(value):
    if (type(value) is not str or not value.strip() or value != value.strip()
            or len(value.encode()) > MAX_EVIDENCE_BYTES
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise ValueError('invalid_rule_request')
    return value


def fields(raw):
    if type(raw) is not bytes or not 0 < len(raw) <= FORM_BYTES:
        raise ValueError('invalid_rule_request')
    pairs = parse_qsl(raw.decode(), keep_blank_values=True, strict_parsing=True,
                     max_num_fields=3, errors='strict')
    values = dict(pairs)
    if (len(values) != len(pairs) or set(values) != {'token', 'outcome', 'evidence'}
            or len(values['token']) != 43 or values['outcome'] not in OUTCOMES):
        raise ValueError('invalid_rule_request')
    evidence_text(values['evidence'])
    return values


def checked_plan(data, row):
    if (type(data) is not dict or set(data) != {'mode', 'expected_state', 'receipt'}
            or len(json.dumps(data, allow_nan=False).encode()) > MAX_PLAN_BYTES
            or data['mode'] != row['mode'] or type(data['expected_state']) is not str
            or not DIGEST.fullmatch(data['expected_state'])):
        raise ValueError('invalid_reconciliation_response')
    if receipt(data['receipt'], row)['status'] != 'uncertain':
        raise ValueError('rule_conflict')
    return data


def detail_link(row, organization):
    return '/ui/interventions/detail?' + urlencode(dict(name=row['rule'], mode=row['mode'],
                                                        id=row['id'], organization=organization))


def description(row):
    body = '<h2>Reconcile an uncertain outcome</h2><p class="notice">' + ('Test simulation' if row['mode'] == 'test' else 'Real intervention') + '</p>'
    for label, key in [('Rule', 'rule'), ('Run ID', 'id'), ('Event', 'event_id'),
                       ('Target', 'target'), ('Workflow', 'workflow'), ('Revision', 'revision')]:
        body += '<p>' + label + ': <code>' + text(row[key]) + '</code></p>'
    body += '<p>An external effect may already have occurred. Verify it outside this form before confirming. Your attestation closes the entire run and releases its target lock. It does not retry an action, run remaining steps, or prove that every step completed.</p>'
    return body


def form_start(route, token):
    return '<form class="editor" method="post" action="/ui/rules/' + route + '"><input type="hidden" name="token" value="' + text(token) + '">'


class WebReconciliation(WebEditor):
    @contextual
    def handle(self, route, path, username, *, method='GET', headers=None, raw=b'', organization=None):
        submitted = False
        pending = None
        try:
            if route not in RECONCILIATION_ROUTES or urlsplit(path).path != '/ui/rules/' + route:
                raise ValueError('invalid_rule_request')
            reader = ReconciliationReader(self.mapping, username, self.factory, organization=organization)
            if method in ('GET', 'HEAD'):
                if route != 'reconcile':
                    raise ValueError('invalid_rule_request')
                try:
                    values = query(path)
                except (ValueError, UnicodeError):
                    raise ValueError('invalid_rule_request') from None
                row = lookup(reader, values, reader.initial)
                plan = checked_plan(reader.call('plan_reconciliation', values['name'], row['event_id'], mode=row['mode']), row)
                token = self.tokens.issue(reader, route, row['rule'], row['revision'],
                                          dict(row=row, expected_state=plan['expected_state']))
                body = description(row) + form_start(route, token)
                body += '<label for="reconciliation-outcome">Externally verified outcome</label><select id="reconciliation-outcome" name="outcome" required><option value="">Choose an outcome</option><option value="succeeded">Succeeded — operator confirmed</option><option value="failed">Failed — operator confirmed</option></select>'
                body += '<label>Verification reference or note<input name="evidence" required maxlength="2048"></label><p>Use a concise reference without secrets or credentials. Atropos retains it in the run and scoped audit.</p><button type="submit">Review attestation</button></form>'
                reader.entry()
                return 200, page(body), {}
            headers = headers or {}
            if (method != 'POST' or path != '/ui/rules/' + route or headers.get('origin') != self.origin
                    or headers.get('sec-fetch-site', 'same-origin') != 'same-origin'):
                raise PermissionError('invalid_web_origin')
            if route == 'reconcile':
                try:
                    values = fields(raw)
                except (ValueError, UnicodeError):
                    raise ValueError('invalid_rule_request') from None
                pending = self.tokens.consume(values['token'], reader, route)
                context = pending['context']
                row = context['row']
                plan = checked_plan(reader.call('plan_reconciliation', row['rule'], row['event_id'], mode=row['mode']), row)
                if plan['expected_state'] != context['expected_state']:
                    raise ValueError('rule_conflict')
                context = dict(context, outcome=values['outcome'], evidence=values['evidence'])
                token = self.tokens.issue(reader, 'reconcile-confirm', row['rule'], row['revision'], context)
                body = description(row)
                body += '<p>Attested outcome: <strong>' + text(context['outcome']) + '</strong></p><p>Verification: ' + text(context['evidence']) + '</p>'
                body += form_start('reconcile-confirm', token)
                body += '<label><input type="checkbox" name="confirm" value="reconcile-confirm" required> I verified the external outcome and confirm closing this run with this attestation.</label><button type="submit">Confirm reconciliation</button></form>'
                reader.entry()
                return 200, page(body), {}
            pending = self.tokens.consume(confirmation(raw, route), reader, route)
            context = pending['context']; row = context['row']
            reader.entry()
            submitted = True
            result = receipt(reader.call('reconcile', row['rule'], row['event_id'], mode=row['mode'],
                expected_state=context['expected_state'], outcome=context['outcome'], evidence=context['evidence']), row)
            annotation = result.get('reconciliation', {})
            expected = dict(author=reader.initial['subject'], organization=reader.initial['organization'],
                expected_state=context['expected_state'], outcome=context['outcome'], evidence=context['evidence'])
            if result['status'] != context['outcome'] or any(annotation.get(k) != v for k, v in expected.items()):
                raise ValueError('invalid_reconciliation_response')
            return 303, '', {'Location': detail_link(row, organization)}
        except Exception as error:
            if submitted:
                body = '<p role="alert">Reconciliation outcome unconfirmed. It may already have been recorded. Check intervention details and the engine audit before any new confirmation. No automatic retry was attempted.</p>'
                body += '<p><a href="' + text(detail_link(pending['context']['row'], organization)) + '">Check intervention details</a></p>'
                return 503, page(body), {}
            code = 403 if isinstance(error, PermissionError) else 404 if isinstance(error, InterventionUnavailable) or str(error) == 'rule_not_found' else 409 if str(error) == 'rule_conflict' else 400 if str(error) == 'invalid_rule_request' else 503
            return code, page('<p role="alert">' + ERRORS[code] + '</p>'), {}
