"""Read-only, identity-bound projection of one engine-owned intervention."""
import json
import math
from urllib.parse import parse_qsl, urlencode, urlsplit

from atraxis.intervention_search import InterventionQuery, checked, date_text, DIGEST, STATUSES
from atraxis.web import text
from atraxis.web_rules import RuleReader, NAME, REVISION, IDENTIFIER, client_factory
from atraxis.web_intervention_search import page
from atraxis.web_organization_context import contextual

MAX_QUERY_BYTES = 1024
MAX_RECEIPT_BYTES = 524288
MAX_HISTORY = 1000
ERROR_TEXT = {
    'execution_authorization_denied': 'Execution authorization denied.',
    'action_failed': 'Action failed.',
    'action_outcome_unknown': 'Action outcome unknown.',
    'unmatched_decision': 'No matching decision branch.',
    'invalid_step_input_or_definition': 'Invalid step input or definition.',
    'worker lease expired; action outcome requires reconciliation':
        'Worker lease expired; reconciliation required.',
}
COMMON = frozenset({'id', 'status', 'rule', 'revision', 'history', 'error'})
REAL = COMMON | {'generation', 'binding', 'event_id', 'target'}
TEST = COMMON | {'test_context', 'results'}
RECONCILIATION_FIELDS = frozenset({'author', 'organization', 'outcome', 'evidence', 'expected_state', 'created_at'})


class DetailReader(RuleReader):
    methods = frozenset({'search_interventions', 'dispatch_status', 'test_dispatch_status'})


class InterventionUnavailable(Exception):
    pass


def query(path):
    if type(path) is not str or len(path.encode()) > MAX_QUERY_BYTES:
        raise ValueError('invalid_intervention_query')
    pairs = parse_qsl(urlsplit(path).query, keep_blank_values=True,
                      strict_parsing=True, max_num_fields=4, errors='strict')
    values = dict(pairs)
    if (len(values) != len(pairs) or set(values) != {'name', 'mode', 'id'}
            or not NAME.fullmatch(values['name']) or values['mode'] not in ('real', 'test')
            or not DIGEST.fullmatch(values['id'])):
        raise ValueError('invalid_intervention_query')
    return values


def marker(value, run_id):
    return (type(value) is dict and set(value) == {'test', 'id'}
            and value['test'] is True and value['id'] == run_id)


def timestamp(value):
    return type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 253402300799


def receipt(data, row):
    is_test = row['mode'] == 'test'
    required = TEST if is_test else REAL
    if (type(data) is not dict or not required <= set(data)
            or set(data) - required - ({'reconciliation'} if is_test else {'observation', 'reconciliation'})
            or len(json.dumps(data, allow_nan=False).encode()) > MAX_RECEIPT_BYTES
            or any(data[key] != row[key] for key in ('id', 'rule', 'revision'))
            or type(data['status']) is not str or data['status'] not in STATUSES
            or data['error'] is not None and type(data['error']) is not str
            or type(data['history']) is not list or len(data['history']) > MAX_HISTORY):
        raise ValueError('invalid_intervention_receipt')
    if is_test:
        if not marker(data['test_context'], row['id']) or type(data['results']) is not dict:
            raise ValueError('invalid_intervention_receipt')
        # Results can contain action output; never render them or remote errors.
    elif (any(data[key] != row[key] for key in ('event_id', 'target'))
            or type(data['generation']) is not str or not REVISION.fullmatch(data['generation'])
            or type(data['binding']) is not str or not DIGEST.fullmatch(data['binding'])):
        raise ValueError('invalid_intervention_receipt')
    if 'observation' in data:
        observation = data['observation']
        if (type(observation) is not dict or set(observation) != {'verified_at_admission', 'expires_at'}
                or observation['verified_at_admission'] is not True or not timestamp(observation['expires_at'])):
            raise ValueError('invalid_intervention_receipt')
    if 'reconciliation' in data:
        reconciliation_checked(data['reconciliation'], data['status'], row['target'].split('/')[0])
    for entry in data['history']:
        fields = {'step', 'status', 'at'} | ({'test_context'} if is_test else set())
        if (type(entry) is not dict or not fields <= set(entry) or set(entry) - fields - {'error'}
                or type(entry['step']) is not str or not NAME.fullmatch(entry['step'])
                or type(entry['status']) is not str or entry['status'] not in STATUSES
                or not timestamp(entry['at'])
                or entry.get('error') is not None and type(entry['error']) is not str
                or is_test and not marker(entry['test_context'], row['id'])):
            raise ValueError('invalid_intervention_receipt')
    return data


def reconciliation_checked(value, status, organization):
    if (type(value) is not dict or set(value) != RECONCILIATION_FIELDS
            or status not in ('succeeded', 'failed') or value['outcome'] != status
            or value['organization'] != organization
            or type(value['author']) is not str or not IDENTIFIER.fullmatch(value['author'])
            or type(value['expected_state']) is not str or not DIGEST.fullmatch(value['expected_state'])
            or not timestamp(value['created_at'])
            or type(value['evidence']) is not str or not value['evidence'].strip()
            or value['evidence'] != value['evidence'].strip() or len(value['evidence'].encode()) > 2048
            or any(ord(char) < 32 or ord(char) == 127 for char in value['evidence'])):
        raise ValueError('invalid_intervention_receipt')
    return value


def lookup(reader, values, actor):
    filters = InterventionQuery(q=values['id'], mode=values['mode'])
    data = checked(reader.call('search_interventions', values['name'], **filters.parameters()),
                   values['name'], filters, actor)
    rows = [row for row in data['items'] if row['id'] == values['id'] and row['mode'] == values['mode']]
    if len(rows) != 1 or data['has_more']:
        raise InterventionUnavailable('intervention_unavailable')
    return rows[0]


def render(row, data, *, reconciliation_enabled=False):
    mode = 'Test simulation' if row['mode'] == 'test' else 'Real intervention'
    body = '<h2>Intervention details</h2><p class="notice">' + mode + '</p>'
    for label, value in [('Status', data['status']), ('Rule', row['rule']),
                         ('Event', row['event_id']), ('Run ID', row['id']),
                         ('Revision', row['revision']), ('Target', row['target']),
                         ('Workflow', row['workflow'])]:
        body += '<p class="state">' + label + ': <code>' + text(value) + '</code></p>'
    if data['status'] == 'uncertain':
        body += '<p role="alert">Outcome unknown. An external effect may already have occurred. Reconcile before any new intervention; refreshing this page never retries it.</p>'
        if reconciliation_enabled:
            body += '<p><a href="/ui/rules/reconcile?' + text(urlencode(dict(name=row['rule'], mode=row['mode'], id=row['id']))) + '">Review reconciliation</a> — requires current scoped reconciliation rights.</p>'
    if 'reconciliation' in data:
        annotation = data['reconciliation']
        body += '<h3>Operator-confirmed outcome</h3><p>This run was closed by ' + text(annotation['author']) + ' at ' + text(date_text(annotation['created_at'])) + '. This attestation does not prove every workflow step ran.</p><p>Verification: ' + text(annotation['evidence']) + '</p>'
    if data['error'] is not None:
        body += '<p>' + ERROR_TEXT.get(data['error'], 'The engine reported an error. Consult authorized diagnostics.') + '</p>'
    body += '<h3>Execution history</h3><p>Each status describes the workflow after that event, not a separate action result.</p>'
    body += '<div class="table-wrap"><table><thead><tr><th>Step</th><th>Workflow status</th><th>Time (UTC)</th><th>Diagnostic</th></tr></thead><tbody>'
    for entry in data['history']:
        diagnostic = '' if entry.get('error') is None else ERROR_TEXT.get(entry['error'], 'Engine error; consult authorized diagnostics.')
        body += '<tr>' + ''.join('<td>' + text(value) + '</td>' for value in
                               (entry['step'], entry['status'], date_text(entry['at']), diagnostic)) + '</tr>'
    body += '</tbody></table></div>'
    if not data['history']:
        body += '<p>No execution history yet. Admission does not prove execution.</p>'
    body += '<p>Alert receiver acceptance does not prove downstream notification delivery. Action output and raw remote errors are not shown.</p>'
    body += '<p><a href="/ui/interventions?' + text(urlencode(dict(name=row['rule']))) + '">Back to intervention search</a></p>'
    body += '<p>Read-only view. Refresh to check current state. No execution or retry controls.</p>'
    return page(body)


@contextual
def handle(path, username, mapping, *, factory=client_factory, organization=None, reconciliation_enabled=False):
    try:
        values = query(path)
    except (TypeError, ValueError):
        return 400, page('<p role="alert">Invalid intervention detail query.</p>')
    try:
        reader = DetailReader(mapping, username, factory, organization=organization)
        actor = reader.entry()
        row = lookup(reader, values, actor)
        method = 'test_dispatch_status' if values['mode'] == 'test' else 'dispatch_status'
        data = receipt(reader.call(method, values['name'], row['event_id']), row)
        # Recheck engine rights and immutable metadata after the status read.
        current = lookup(reader, values, actor)
        if any(current[key] != row[key] for key in row if key != 'status'):
            raise ValueError('intervention_identity_changed')
        return 200, render(row, data, reconciliation_enabled=reconciliation_enabled)
    except PermissionError:
        return 403, page('<p role="alert">Intervention access denied.</p>')
    except InterventionUnavailable:
        return 404, page('<p role="alert">Intervention unavailable.</p>')
    except ValueError as error:
        if str(error) == 'rule_not_found':
            return 404, page('<p role="alert">Intervention unavailable.</p>')
        return 503, page('<p role="alert">Intervention details unavailable. No partial results shown.</p>')
    except Exception:
        return 503, page('<p role="alert">Intervention details unavailable. No partial results shown.</p>')
