"""Explicit browser lifecycle review; Atropos owns admission and audit."""
import re
from urllib.parse import parse_qsl, urlencode, urlsplit

import yaml

from atraxis.web import text
from atraxis.web_rules import RuleReader, NAME, MAX_QUERY_BYTES, valid_revision, page
from atraxis.web_rule_editor import (WebEditor, record_checked, report_checked,
                                     context_checked, location)

ACTIVATION_BYTES = 4096
ACTIVATION_METHODS = frozenset(('read', 'active', 'test_report', 'activate'))
LIFECYCLE_ROUTES = ('activate', 'rollback', 'deactivate')
LABELS = {'activate': 'activation', 'rollback': 'rollback', 'deactivate': 'deactivation'}
HASH = re.compile(r'[0-9a-f]{64}')
MESSAGES = {
    400: 'The request or test evidence is no longer valid. For rollback, the revision must have been active before. Run scenarios and review again.',
    403: 'Lifecycle access denied or review expired. Open a fresh review to continue.',
    404: 'Rule, revision or test report not available.',
    409: 'The active version changed. Open a fresh review before confirming again.',
    422: 'This revision needs a passing report for the same content and scope.',
    503: 'Rule service unavailable. No automatic retry was attempted.',
}
UNKNOWN = ('Lifecycle outcome unconfirmed. Check the active revision and the engine audit '
           'before trying again. No automatic retry was attempted.')


class ActivationClient(RuleReader):
    methods = ACTIVATION_METHODS


class RollbackClient(RuleReader):
    methods = frozenset(('read', 'active', 'test_report', 'rollback'))


class DeactivationClient(RuleReader):
    methods = frozenset(('read', 'active', 'deactivate'))


CLIENTS = {'activate': ActivationClient, 'rollback': RollbackClient, 'deactivate': DeactivationClient}


def activation_query(path):
    try:
        if type(path) is not str or len(path.encode()) > MAX_QUERY_BYTES:
            raise ValueError()
        pairs = parse_qsl(urlsplit(path).query, keep_blank_values=True, max_num_fields=3, errors='strict')
        values = dict(pairs)
        if (len(pairs) != len(values) or set(values) != {'name', 'revision', 'report'}
                or not NAME.fullmatch(values['name']) or not valid_revision(values['revision'])
                or not valid_revision(values['report'])):
            raise ValueError()
        return values
    except (ValueError, UnicodeError):
        raise ValueError('invalid_rule_request') from None


def active_checked(state):
    if (type(state) is not dict or any(key not in state or state[key] is not None
            and not valid_revision(state[key]) for key in ('revision', 'generation'))
            or state['revision'] is not None and state['generation'] is None):
        raise ValueError('invalid_web_rule_response')
    return {key: state[key] for key in ('revision', 'generation')}


def evidence_checked(record, report):
    # Presentation guard only. Atropos checks latest report, content, scope and compiler at commit.
    if not report['passed']:
        raise ValueError('activation_not_ready')
    for field in ('content_sha256', 'context_sha256'):
        if (type(record.get(field)) is not str or not HASH.fullmatch(record[field])
                or report.get(field) != record[field]):
            raise ValueError('activation_not_ready')


def review(path, reader, tokens, operation='activate'):
    label = LABELS[operation]
    values = activation_query(path)
    name, revision, report_id = values['name'], values['revision'], values['report']
    record = record_checked(reader.call('read', name, revision), name, revision)
    context = context_checked(record.get('context'))
    report = report_checked(reader.call('test_report', name, revision, report_id), revision, report_id)
    evidence_checked(record, report)
    state = active_checked(reader.call('active', name))
    actor = reader.entry()
    body = '<h2>Review ' + label + '</h2><p class="actor">Account: ' + text(actor['subject']) + ' · Organization: ' + text(actor['organization']) + '</p>'
    body += '<p>This makes the selected revision active for future evaluations. This confirmation submits no intervention or notification.</p>'
    if operation == 'rollback':
        body += '<p>Restore a previously active revision. Atropos checks prior activation, the latest passing report and current permissions when you confirm.</p>'
    body += '<section class="source"><h3>' + text(name) + '</h3><p>Currently active: <code>' + text(state['revision'] or 'None') + '</code></p>'
    body += '<p>Selected revision: <code>' + text(revision) + '</code></p><p class="muted">Expected generation: <code>' + text(state['generation'] or 'None') + '</code></p>'
    body += '<p>Passing test report: <a href="' + text(location('test', **values)) + '"><code>' + text(report_id) + '</code></a></p>'
    body += '<p>Rule kind: ' + text(context['rule_kind']) + '</p><h3>Targets</h3><ul>'
    body += ''.join('<li>' + text(kind) + ': <code>' + text(identifier) + '</code></li>' for kind, identifier in context['targets'])
    body += '</ul><details><summary>Review rule YAML</summary><pre>' + text(yaml.safe_dump(record['document'], sort_keys=False, allow_unicode=True)) + '</pre></details></section>'
    if state['revision'] == revision:
        body += '<p class="notice">This revision is already active. No confirmation is needed.</p>'
    else:
        token = tokens.issue(reader, operation, name, revision,
            dict(test_id=report_id, expected_generation=state['generation']))
        body += '<form class="activation" method="post" action="/ui/rules/' + operation + '"><input type="hidden" name="token" value="' + text(token) + '">'
        body += '<label><input type="checkbox" name="confirm" value="' + operation + '" required> I confirm ' + label + ' of this revision for the targets shown above.</label>'
        body += '<button class="button" type="submit">Confirm ' + label + '</button></form>'
    body += '<p><a href="' + text(location('test', **values)) + '">Back to test report</a></p>'
    reader.entry()
    return page(body, name, workbench=True, activation=True)


def deactivation_review(path, reader, tokens):
    try:
        if type(path) is not str or len(path.encode()) > MAX_QUERY_BYTES:
            raise ValueError()
        pairs = parse_qsl(urlsplit(path).query, keep_blank_values=True, max_num_fields=1, errors='strict')
        if len(pairs) != 1 or pairs[0][0] != 'name' or not NAME.fullmatch(pairs[0][1]):
            raise ValueError()
        name = pairs[0][1]
    except (ValueError, UnicodeError):
        raise ValueError('invalid_rule_request') from None
    state = active_checked(reader.call('active', name))
    actor = reader.entry()
    body = '<h2>Review deactivation</h2><p class="actor">Account: ' + text(actor['subject']) + ' · Organization: ' + text(actor['organization']) + '</p>'
    body += '<p>Stop future active evaluations of this rule. Existing drafts, reports and history are preserved. Running actions are not cancelled.</p>'
    if state['revision'] is None:
        body += '<p class="notice">This rule is already inactive. No confirmation is needed.</p>'
    else:
        record = record_checked(reader.call('read', name, state['revision']), name, state['revision'])
        context = context_checked(record.get('context'))
        body += '<section class="source"><h3>' + text(name) + '</h3><p>Deactivate revision: <code>' + text(state['revision']) + '</code></p>'
        body += '<p class="muted">Expected generation: <code>' + text(state['generation']) + '</code></p>'
        body += '<p>Rule kind: ' + text(context['rule_kind']) + '</p><h3>Targets</h3><ul>'
        body += ''.join('<li>' + text(kind) + ': <code>' + text(identifier) + '</code></li>' for kind, identifier in context['targets'])
        body += '</ul></section>'
        token = tokens.issue(reader, 'deactivate', name, state['revision'], dict(expected_generation=state['generation']))
        body += '<form class="activation" method="post" action="/ui/rules/deactivate"><input type="hidden" name="token" value="' + text(token) + '">'
        body += '<label><input type="checkbox" name="confirm" value="deactivate" required> I confirm deactivation of this rule for the targets shown above.</label>'
        body += '<button class="button" type="submit">Confirm deactivation</button></form>'
    body += '<p><a href="/ui/rules?' + text(urlencode(dict(name=name))) + '">Back to rule</a></p>'
    reader.entry()
    return page(body, name, workbench=True, activation=True)


def confirmation(raw, operation='activate'):
    try:
        if type(raw) is not bytes or not 0 < len(raw) <= ACTIVATION_BYTES:
            raise ValueError()
        pairs = parse_qsl(raw.decode('utf-8'), keep_blank_values=True, strict_parsing=True,
                          max_num_fields=2, encoding='utf-8', errors='strict')
        values = dict(pairs)
        if (len(values) != len(pairs) or set(values) != {'token', 'confirm'}
                or len(values['token']) != 43 or values['confirm'] != operation):
            raise ValueError()
        return values['token']
    except (ValueError, UnicodeError):
        raise ValueError('invalid_rule_request') from None


from atraxis.web_organization_context import contextual

class WebActivation(WebEditor):
    @contextual
    def handle(self, route, path, username, *, method='GET', headers=None, raw=b'', organization=None):
        attempted = False
        try:
            if route not in LIFECYCLE_ROUTES:
                raise ValueError('invalid_rule_request')
            reader = CLIENTS[route](self.mapping, username, self.factory, organization=organization)
            if method in ('GET', 'HEAD'):
                body = deactivation_review(path, reader, self.tokens) if route == 'deactivate' else review(path, reader, self.tokens, route)
                return 200, body, {}
            headers = headers or {}
            if (method != 'POST' or path != '/ui/rules/' + route
                    or headers.get('origin') != self.origin
                    or headers.get('sec-fetch-site', 'same-origin') != 'same-origin'):
                raise PermissionError('invalid_web_origin')
            pending = self.tokens.consume(confirmation(raw, route), reader, route)
            attempted = True
            args = (pending['name'],) if route == 'deactivate' else (pending['name'], pending['revision'])
            result = reader.call(route, *args, **pending['context'])
            state = active_checked(result)
            expected_revision = None if route == 'deactivate' else pending['revision']
            if (state['revision'] != expected_revision or state['generation'] is None
                    or state['generation'] == pending['context']['expected_generation']
                    or result.get('test_id') != pending['context'].get('test_id')):
                raise ValueError('invalid_web_rule_response')
            return 303, '', {'Location': '/ui/rules?' + urlencode(dict(name=pending['name'], revision=pending['revision']))}
        except PermissionError:
            status = 403
        except Exception as error:
            status = {'invalid_rule_request':400, 'rule_not_found':404, 'rule_conflict':409,
                      'activation_not_ready':422}.get(str(error), 503)
        message = UNKNOWN if attempted and status == 503 else MESSAGES[status]
        if attempted and status == 403:
            message += ' If access changed during confirmation, the change may already have completed.'
        return status, page('<p class="notice" role="alert">' + message + '</p>', workbench=True, activation=True), {}
