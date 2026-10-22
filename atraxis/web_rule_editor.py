"""Bounded browser forms over the independent Atropos client.

Only form nonces and immutable operation metadata live here. Rule documents,
validation, version checks, authorization and reports remain engine-owned.
"""
from copy import deepcopy
from ipaddress import ip_address
import json
import secrets
import threading
import time
from urllib.parse import parse_qsl, urlencode, urlsplit

import yaml

from atraxis.config import _StrictLoader
from atraxis.web import text
from atraxis.web_rules import (RuleReader, NAME, MAX_DOCUMENT_BYTES, MAX_QUERY_BYTES,
                               valid_revision, page, query, IDENTIFIER)

FORM_BYTES = 524288
CASE_BYTES = 65536
REPORT_BYTES = 262144
TOKEN_TTL = 900
MAX_TOKENS = 1024
MAX_USER_TOKENS = 16
EDITOR_METHODS = frozenset(('draft', 'read', 'save_draft', 'test', 'test_report'))
ERRORS = {400: 'Invalid form or rule data. Open a fresh form and check your input.',
          403: 'Access denied or form expired. Open a fresh form to continue.',
          404: 'Rule or revision not available.',
          409: 'The draft changed. Open the latest draft before editing again.',
          503: 'Rule service unavailable. No automatic retry was attempted.'}
UNCERTAIN = ('Outcome unconfirmed. The operation may have been saved by Atropos. '
             'Check the revision history or test reports before submitting again. No automatic retry was attempted.')


def validate_origin(value):
    if type(value) is not str or not 0 < len(value) <= 2048:
        raise ValueError('invalid_web_rule_origin')
    parsed = urlsplit(value)
    if (parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username
            or parsed.password or parsed.path or parsed.query or parsed.fragment
            or value != parsed.scheme + '://' + parsed.netloc
            or not value.isascii() or any(c.isspace() for c in value)):
        raise ValueError('invalid_web_rule_origin')
    if parsed.port is not None and not 1 <= parsed.port <= 65535:
        raise ValueError('invalid_web_rule_origin')
    if parsed.scheme == 'http' and not ip_address(parsed.hostname).is_loopback:
        raise ValueError('invalid_web_rule_origin')
    return value


class FormTokens:
    """Single-use, actor-bound CSRF tokens; bounded, process-local, not sessions."""
    def __init__(self, clock=time.monotonic):
        self.clock, self.lock, self.entries = clock, threading.Lock(), {}

    def issue(self, reader, operation, name, revision, context=None):
        with self.lock:
            now = self.clock()
            self.entries = {key: item for key, item in self.entries.items() if item['expires'] > now}
            owned = [key for key, item in self.entries.items() if item['username'] == reader.username]
            for key in owned[:max(0, len(owned) - MAX_USER_TOKENS + 1)]:
                del self.entries[key]
            if len(self.entries) >= MAX_TOKENS:
                raise ValueError('web_form_capacity')
            token = secrets.token_urlsafe(32)
            self.entries[token] = dict(expires=now + TOKEN_TTL, username=reader.username,
                identity=deepcopy(reader.initial), operation=operation, name=name,
                revision=revision, context=deepcopy(context))
            return token

    def consume(self, token, reader, operation):
        with self.lock:
            item = self.entries.pop(token, None)
        if (item is None or item['expires'] <= self.clock() or item['username'] != reader.username
                or item['identity'] != reader.entry() or item['operation'] != operation):
            raise PermissionError('invalid_web_form')
        return item


class EditorClient(RuleReader):
    methods = EDITOR_METHODS


def record_checked(record, name, revision=None):
    if (type(record) is not dict or not valid_revision(record.get('revision'))
            or revision is not None and record['revision'] != revision
            or type(record.get('document')) is not dict or record['document'].get('name') != name
            or len(json.dumps(record, allow_nan=False).encode()) > REPORT_BYTES):
        raise ValueError('invalid_web_rule_response')
    return record


def context_checked(context):
    if (type(context) is not dict or set(context) != {'version', 'rule_kind', 'targets'}
            or type(context['version']) is not int or context['version'] != 1
            or type(context['rule_kind']) is not str or type(context['targets']) is not list
            or not 1 <= len(context['targets']) <= 128
            or any(type(target) is not list or len(target) != 2
                   or any(type(value) is not str or not IDENTIFIER.fullmatch(value) for value in target)
                   for target in context['targets'])):
        raise ValueError('invalid_web_rule_response')
    return context


def report_checked(report, revision, report_id=None):
    if (type(report) is not dict or report.get('revision') != revision
            or not valid_revision(report.get('id')) or type(report.get('passed')) is not bool
            or report_id is not None and report['id'] != report_id
            or type(report.get('results')) is not list or not 1 <= len(report['results']) <= 100
            or len(json.dumps(report, allow_nan=False).encode()) > REPORT_BYTES):
        raise ValueError('invalid_web_rule_response')
    for result in report['results']:
        if (type(result) is not dict or type(result.get('name')) is not str
                or len(result['name']) > 128 or type(result.get('passed')) is not bool):
            raise ValueError('invalid_web_rule_response')
    if report['passed'] != all(item['passed'] for item in report['results']):
        raise ValueError('invalid_web_rule_response')
    return report


def form(action, token, label, value, button, *, message=False):
    field = 'document' if action == 'edit' else 'cases'
    max_length = MAX_DOCUMENT_BYTES if action == 'edit' else CASE_BYTES
    return ('<form class="editor" method="post" action="/ui/rules/' + action + '">'
        '<input type="hidden" name="token" value="' + text(token) + '">'
        '<label>' + label + '<textarea name="' + field + '" rows="20" required maxlength="' + str(max_length)
        + '" spellcheck="false">' + text(value) + '</textarea></label>'
        + ('<label>Revision message<input name="message" maxlength="256" autocomplete="off"></label>' if message else '')
        + '<button class="button" type="submit">' + button + '</button></form>')


def location(route, **values):
    return '/ui/rules/' + route + '?' + urlencode(values)


def read_page(route, path, reader, tokens):
    if route == 'edit':
        try:
            name, revision, offset = query(path)
        except (ValueError, UnicodeError):
            raise ValueError('invalid_rule_request') from None
        if not name or revision is not None or offset:
            raise ValueError('invalid_rule_request')
        record = record_checked(reader.call('draft', name), name)
        context = record.get('context')
        context_checked(context)
        token = tokens.issue(reader, route, name, record['revision'], context)
        body = '<h2>Edit current draft</h2><p>Base revision: <code>' + text(record['revision']) + '</code></p>'
        body += '<p>Scope stays unchanged:</p><pre>' + text(yaml.safe_dump(context, sort_keys=False)) + '</pre>'
        body += '<p>Saving creates a new draft. It does not change the active revision.</p>'
        body += form(route, token, 'Rule YAML', yaml.safe_dump(record['document'], sort_keys=False,
                     allow_unicode=True), 'Save new draft', message=True)
    else:
        if len(path.encode()) > MAX_QUERY_BYTES:
            raise ValueError('invalid_rule_request')
        try:
            pairs = parse_qsl(urlsplit(path).query, keep_blank_values=True, max_num_fields=3, errors='strict')
        except (ValueError, UnicodeError):
            raise ValueError('invalid_rule_request') from None
        values = dict(pairs)
        if (len(pairs) != len(values) or not {'name', 'revision'} <= set(values)
                or not set(values) <= {'name', 'revision', 'report'}
                or not NAME.fullmatch(values['name']) or not valid_revision(values['revision'])
                or 'report' in values and not valid_revision(values['report'])):
            raise ValueError('invalid_rule_request')
        name, revision = values['name'], values['revision']
        record_checked(reader.call('read', name, revision), name, revision)
        body = '<h2>Test this revision</h2><p>Revision: <code>' + text(revision) + '</code></p>'
        body += '<p>Scenario evaluation only. No activation, intervention or notification is performed.</p>'
        if 'report' in values:
            report = report_checked(reader.call('test_report', name, revision, values['report']), revision, values['report'])
            body += '<section class="source"><h3>Test report</h3><p class="badge ' + ('current' if report['passed'] else 'unknown') + '">' + ('All scenarios passed' if report['passed'] else 'Some scenarios failed') + '</p>'
            body += '<p>Report: <code>' + text(report['id']) + '</code></p><pre>' + text(json.dumps(report['results'], indent=2, ensure_ascii=False)) + '</pre></section>'
        if 'report' in values and report['passed']:
            body += '<p><a class="button" href="' + text(location('activate', **values)) + '">Review activation</a> · <a href="' + text(location('rollback', **values)) + '">Review rollback</a></p><p class="muted">Rollback is available only for a previously active revision; Atropos verifies eligibility on confirmation.</p>'
        token = tokens.issue(reader, route, name, revision)
        body += '<p>Supply a JSON array of scenarios with name, input and expected (decision and matched_rule).</p>'
        body += form(route, token, 'Scenarios JSON', '', 'Run scenarios')
    reader.entry()
    return page(body, name, workbench=True)


def parse_form(raw, route):
    if type(raw) is not bytes or not 0 < len(raw) <= FORM_BYTES:
        raise ValueError('invalid_rule_request')
    pairs = parse_qsl(raw.decode('utf-8'), keep_blank_values=True, strict_parsing=True,
                      max_num_fields=3, encoding='utf-8', errors='strict')
    values = dict(pairs)
    expected = {'token', 'document', 'message'} if route == 'edit' else {'token', 'cases'}
    if len(values) != len(pairs) or set(values) != expected or len(values['token']) != 43:
        raise ValueError('invalid_rule_request')
    return values


def strict_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('invalid_rule_request')
            result[key] = value
        return result
    def invalid(_):
        raise ValueError('invalid_rule_request')
    value = json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid)
    json.dumps(value, allow_nan=False)
    return value


def form_document(values, name):
    try:
        if len(values['document'].encode()) > MAX_DOCUMENT_BYTES or len(values['message']) > 256:
            raise ValueError()
        document = yaml.load(values['document'], Loader=_StrictLoader)
        if type(document) is not dict or document.get('name') != name:
            raise ValueError()
        # Only convert the data format; rule grammar validation belongs to Atropos.
        document = strict_json(json.dumps(document, allow_nan=False))
        if len(json.dumps(document).encode()) > MAX_DOCUMENT_BYTES:
            raise ValueError()
        return document
    except (ValueError, TypeError, UnicodeError, RecursionError, yaml.YAMLError):
        raise ValueError('invalid_rule_request') from None


def form_cases(values):
    try:
        if len(values['cases'].encode()) > CASE_BYTES:
            raise ValueError()
        cases = strict_json(values['cases'])
        if type(cases) is not list or not 1 <= len(cases) <= 100:
            raise ValueError()
        return cases
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise ValueError('invalid_rule_request') from None


from atraxis.web_organization_context import contextual

class WebEditor:
    def __init__(self, mapping, origin, factory):
        self.mapping, self.origin, self.factory = mapping, validate_origin(origin), factory
        self.tokens = FormTokens()

    @contextual
    def handle(self, route, path, username, *, method='GET', headers=None, raw=b'', organization=None):
        attempted = False
        try:
            reader = EditorClient(self.mapping, username, self.factory, organization=organization)
            if method in ('GET', 'HEAD'):
                return 200, read_page(route, path, reader, self.tokens), {}
            headers = headers or {}
            if (method != 'POST' or path != '/ui/rules/' + route
                    or headers.get('origin') != self.origin
                    or headers.get('sec-fetch-site', 'same-origin') != 'same-origin'):
                raise PermissionError('invalid_web_origin')
            try:
                values = parse_form(raw, route)
            except (ValueError, UnicodeError):
                raise ValueError('invalid_rule_request') from None
            pending = self.tokens.consume(values['token'], reader, route)
            name, revision = pending['name'], pending['revision']
            if route == 'edit':
                document = form_document(values, name)
                attempted = True
                result = reader.call('save_draft', document, rule_kind=pending['context']['rule_kind'],
                    selections=pending['context']['targets'], expected_revision=revision, message=values['message'])
                result = record_checked(result, name)
                destination = '/ui/rules?' + urlencode(dict(name=name, revision=result['revision']))
            else:
                cases = form_cases(values)
                attempted = True
                report = report_checked(reader.call('test', name, revision, cases), revision)
                destination = location('test', name=name, revision=revision, report=report['id'])
            return 303, '', {'Location': destination}
        except PermissionError:
            status = 403
        except Exception as error:
            code = str(error)
            if code in ('rule_conflict', 'rule_not_found', 'invalid_rule_request'):
                status = {'rule_conflict':409, 'rule_not_found':404, 'invalid_rule_request':400}[code]
            elif not attempted and isinstance(error, (UnicodeError, RecursionError)):
                status = 400
            else:
                status = 503
        message = UNCERTAIN if attempted and status == 503 else ERRORS[status]
        if attempted and status == 403:
            message += ' If access changed during the request, an operation may already have completed.'
        return status, page('<p class="notice" role="alert">' + message + '</p>', workbench=True), {}
