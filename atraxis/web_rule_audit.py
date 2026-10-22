"""Read-only bounded presentation of engine-authorized lifecycle events."""
from datetime import datetime, timezone
import json
import re
from urllib.parse import parse_qsl, urlencode, urlsplit

from atraxis.web import text
from atraxis.web_rules import (RuleReader, NAME, IDENTIFIER, MAX_QUERY_BYTES,
                               PAGE_SIZE, MAX_OFFSET, valid_revision, page, client_factory)

AUDIT_BYTES = 262144
MAX_RETIREMENT_REVISIONS = 128
EVENT_FIELDS = frozenset(('generation', 'previous_generation', 'operation', 'author',
    'organization', 'created_at', 'previous_revision', 'revision', 'test_id', 'scope_revisions'))
DISPATCH_FIELDS = frozenset(('generation', 'operation', 'author', 'organization',
    'created_at', 'revision', 'scope_revisions', 'binding', 'run_id'))
TEST_RUN_FIELDS = frozenset(('operation', 'author', 'organization', 'created_at',
    'revision', 'scope_revisions', 'test_context', 'run_id'))
RETIREMENT_FIELDS = frozenset(('operation', 'generation', 'previous_generation', 'author',
    'organization', 'created_at', 'revision', 'scope_revisions'))
RECONCILIATION_FIELDS = frozenset(('operation', 'mode', 'run_id', 'revision', 'scope_revisions',
    'author', 'organization', 'created_at', 'outcome', 'evidence', 'expected_state'))
DIGEST = re.compile(r'[a-f0-9]{64}')
OPERATIONS = {'activate': 'Activation', 'rollback': 'Rollback', 'deactivate': 'Deactivation',
              'archive': 'Rule archived', 'unarchive': 'Rule made available', 'delete': 'Draft contents deleted',
              'dispatch': 'Intervention admitted', 'test_run': 'Test run admitted', 'reconcile': 'Operator-confirmed outcome'}
EVENT_SCHEMAS = {'activate': EVENT_FIELDS, 'rollback': EVENT_FIELDS, 'deactivate': EVENT_FIELDS,
                 'archive': RETIREMENT_FIELDS, 'unarchive': RETIREMENT_FIELDS, 'delete': RETIREMENT_FIELDS,
                 'dispatch': DISPATCH_FIELDS, 'test_run': TEST_RUN_FIELDS, 'reconcile': RECONCILIATION_FIELDS}
ERRORS = {400: 'Invalid audit query.', 403: 'Audit access denied.',
          404: 'Rule not available.', 503: 'Audit service unavailable. Refresh to try again.'}


class AuditClient(RuleReader):
    methods = frozenset(('audit',))


def audit_query(path):
    if type(path) is not str or len(path.encode()) > MAX_QUERY_BYTES:
        raise ValueError('invalid_rule_request')
    pairs = parse_qsl(urlsplit(path).query, keep_blank_values=True, max_num_fields=2, errors='strict')
    values = dict(pairs)
    if (len(pairs) != len(values) or not {'name'} <= set(values) <= {'name', 'offset'}
            or not NAME.fullmatch(values['name'])):
        raise ValueError('invalid_rule_request')
    offset = values.get('offset', '0')
    if not offset.isascii() or not offset.isdigit() or len(offset) > 7 or int(offset) > MAX_OFFSET:
        raise ValueError('invalid_rule_request')
    return values['name'], int(offset)


def events_checked(events, organization):
    if (type(events) is not list or len(events) > PAGE_SIZE
            or len(json.dumps(events, allow_nan=False).encode()) > AUDIT_BYTES):
        raise ValueError('invalid_web_rule_response')
    for event in events:
        if (type(event) is not dict or type(event.get('operation')) is not str
                or set(event) != EVENT_SCHEMAS.get(event['operation'])
                or any(type(event[key]) is not str or not IDENTIFIER.fullmatch(event[key])
                       for key in ('author', 'organization'))
                or event['organization'] != organization
                or type(event['created_at']) not in (int, float)
                or not 0 <= event['created_at'] <= 253402300799
                or type(event['scope_revisions']) is not list or not 1 <= len(event['scope_revisions']) <= (MAX_RETIREMENT_REVISIONS if event['operation'] in ('archive','unarchive','delete') else 2)
                or any(not valid_revision(value) for value in event['scope_revisions'])):
            raise ValueError('invalid_web_rule_response')
        operation = event['operation']
        if operation == 'reconcile':
            from atraxis.web_intervention_detail import reconciliation_checked, RECONCILIATION_FIELDS as ANNOTATION_FIELDS
            if (event['mode'] not in ('real', 'test') or not valid_revision(event['revision'])
                    or event['scope_revisions'] != [event['revision']]
                    or type(event['run_id']) is not str or not DIGEST.fullmatch(event['run_id'])):
                raise ValueError('invalid_web_rule_response')
            reconciliation_checked({k: event[k] for k in ANNOTATION_FIELDS}, event['outcome'], organization)
            continue
        if operation in ('archive', 'unarchive', 'delete'):
            if (not valid_revision(event['revision']) or not valid_revision(event['generation'])
                    or event['previous_generation'] is not None and not valid_revision(event['previous_generation'])
                    or event['revision'] not in event['scope_revisions']):
                raise ValueError('invalid_web_rule_response')
            continue
        if operation in ('dispatch', 'test_run'):
            if (not valid_revision(event['revision']) or event['scope_revisions'] != [event['revision']]
                    or type(event['run_id']) is not str or not DIGEST.fullmatch(event['run_id'])):
                raise ValueError('invalid_web_rule_response')
            if operation == 'dispatch':
                if (not valid_revision(event['generation']) or type(event['binding']) is not str
                        or not DIGEST.fullmatch(event['binding'])):
                    raise ValueError('invalid_web_rule_response')
            else:
                context = event['test_context']
                if (type(context) is not dict or set(context) != {'test', 'id'}
                        or context['test'] is not True or context['id'] != event['run_id']):
                    raise ValueError('invalid_web_rule_response')
            continue
        if (not valid_revision(event['generation'])
                or any(event[key] is not None and not valid_revision(event[key])
                       for key in ('previous_generation', 'previous_revision', 'revision', 'test_id'))):
            raise ValueError('invalid_web_rule_response')
        if operation == 'deactivate':
            if event['revision'] is not None or event['test_id'] is not None or event['previous_revision'] is None:
                raise ValueError('invalid_web_rule_response')
        elif event['revision'] is None or event['test_id'] is None:
            raise ValueError('invalid_web_rule_response')
    return events


def audit_link(name, offset):
    return '/ui/rules/audit?' + text(urlencode(dict(name=name, offset=offset)))


def render(reader, name, offset, *, reports_enabled=False):
    events = events_checked(reader.call('audit', name, offset, PAGE_SIZE), reader.initial['organization'])
    actor = reader.entry()
    body = '<h2>Lifecycle audit</h2><p class="actor">Account: ' + text(actor['subject']) + ' · Organization: ' + text(actor['organization']) + '</p>'
    body += '<p>Lifecycle changes and intervention/test admissions recorded by Atropos. Admission does not confirm execution success. Oldest first; only events permitted for your account are shown.</p>'
    if not events:
        body += '<p class="notice">No accessible events in this audit window. Later windows may still contain accessible events.</p>'
    for event in events:
        timestamp = datetime.fromtimestamp(event['created_at'], timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
        body += '<section class="source"><h3>' + OPERATIONS[event['operation']] + '</h3><p>' + text(timestamp) + ' · ' + text(event['author']) + '</p>'
        admitted = event['operation'] in ('dispatch', 'test_run')
        retired = event['operation'] in ('archive', 'unarchive', 'delete')
        reconciled = event['operation'] == 'reconcile'
        revisions = [('Rule revision', 'revision')] if admitted or retired or reconciled else [
            ('Previous revision', 'previous_revision'), ('Resulting revision', 'revision')]
        for label, key in revisions:
            value = event[key]
            target = '<a href="/ui/rules?' + text(urlencode(dict(name=name, revision=value))) + '"><code>' + text(value) + '</code></a>' if value else 'None'
            body += '<p>' + label + ': ' + target + '</p>'
        if reconciled:
            body += '<p>' + text(event['mode']) + ' run: <code>' + text(event['run_id']) + '</code></p>'
            body += '<p>Attested outcome: ' + text(event['outcome']) + '</p><p>Verification: ' + text(event['evidence']) + '</p><p>Operator attestation. No action was replayed and no remaining step was executed by reconciliation.</p>'
        elif admitted:
            body += '<p class="info">Request admitted. Execution outcome is not recorded in this event.</p>'
            body += '<p>Run: <code>' + text(event['run_id']) + '</code></p>'
            if event['operation'] == 'dispatch':
                body += '<p>Binding: <code>' + text(event['binding']) + '</code></p>'
                body += '<p>Generation: <code>' + text(event['generation']) + '</code></p>'
            else:
                body += '<p>Test mode · Test ID: <code>' + text(event['test_context']['id']) + '</code></p>'
        else:
            body += '<p class="muted">Generation: <code>' + text(event['generation']) + '</code></p>'
            body += '<p class="muted">Previous generation: <code>' + text(event['previous_generation'] or 'None') + '</code></p>'
            if retired:
                body += '<p>Rule lifecycle change. No activation or intervention was submitted. Audit references survive draft deletion.</p>'
            if event.get('test_id') and reports_enabled:
                body += '<p><a href="/ui/rules/test?' + text(urlencode(dict(name=name, revision=event['revision'], report=event['test_id']))) + '">Passing test report</a></p>'
            elif event.get('test_id'):
                body += '<p>Test report: <code>' + text(event['test_id']) + '</code></p>'
        body += '</section>'
    body += '<nav class="pager" aria-label="Audit windows">'
    if offset:
        body += '<a href="' + audit_link(name, max(0, offset-PAGE_SIZE)) + '">Previous window</a>'
    if offset+PAGE_SIZE <= MAX_OFFSET:
        body += '<a href="' + audit_link(name, offset+PAGE_SIZE) + '">Next window</a>'
    body += '</nav><p class="muted">Audit window ' + str(offset//PAGE_SIZE+1) + '</p>'
    body += '<p><a href="/ui/rules?' + text(urlencode(dict(name=name))) + '">Back to rule</a></p>'
    reader.entry()
    return page(body, name)


from atraxis.web_organization_context import contextual

@contextual
def handle(path, username, mapping, *, factory=client_factory, reports_enabled=False, organization=None):
    try:
        name, offset = audit_query(path)
    except (ValueError, UnicodeError):
        return 400, page('<p class="notice" role="alert">' + ERRORS[400] + '</p>')
    try:
        return 200, render(AuditClient(mapping, username, factory, organization=organization), name, offset, reports_enabled=reports_enabled)
    except PermissionError:
        status = 403
    except Exception as error:
        status = 404 if str(error) == 'rule_not_found' else 503
    return status, page('<p class="notice" role="alert">' + ERRORS[status] + '</p>', name)
