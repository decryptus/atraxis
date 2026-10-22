"""Browser rule reads, mapped to engine identities by a private server file."""
import base64
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import parse_qsl, urlencode, urlsplit

import yaml

from atraxis.account_storage import storage_config
from atraxis.config import ConfigurationError, _StrictLoader
from atraxis.rule_files import read_private
from atraxis.web import CSS, HEADERS, text

MAX_MAPPING_BYTES = 65536
MAX_IDENTITIES = 128
PAGE_SIZE = 20
MAX_OFFSET = 1000000
MAX_QUERY_BYTES = 1024
MAX_DOCUMENT_BYTES = 131072
NAME = re.compile(r'[A-Za-z][A-Za-z0-9_.-]{0,63}')
IDENTIFIER = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}')
REVISION = re.compile(r'[0-9a-f]{32}')
READ_METHODS = frozenset(('history', 'active', 'read'))
ERRORS = {400: 'Invalid rule query.', 403: 'Rule access denied.',
          404: 'Rule or revision not available.', 503: 'Rule service unavailable. Refresh to try again.'}
RULE_CSS = CSS + '''
.info{padding:16px 20px;border-left:3px solid var(--blue);background:#112b40;border-radius:0 8px 8px 0;color:#d2e9fa;overflow-wrap:anywhere}
nav{display:flex;flex-wrap:wrap;gap:24px}nav a{text-decoration:none}form{display:flex;gap:12px;align-items:end;flex-wrap:wrap;margin:24px 0}label{display:block;color:var(--muted);font-size:14px}input{display:block;background:var(--bg);color:#edf5ff;border:1px solid #567b9e;border-radius:8px;padding:12px;font:inherit;max-width:100%;margin-top:6px}button{font:inherit;color:var(--blue);cursor:pointer}input:focus-visible,button:focus-visible{outline:3px solid var(--amber);outline-offset:3px}pre{white-space:pre-wrap;overflow-wrap:anywhere;color:#d2e9fa;font-size:14px}code{overflow-wrap:anywhere}.revision{display:block;margin-bottom:8px}.pager{display:flex;gap:24px;margin-top:20px}.activation{display:block}.activation label{margin:22px 0;display:flex;align-items:center;gap:12px}.activation input[type=checkbox]{width:20px;height:20px;flex-shrink:0;margin:0}.actor{font-size:14px;color:var(--muted);overflow-wrap:anywhere}.state{overflow-wrap:anywhere}.notice{overflow-wrap:anywhere}.editor{display:block}.editor label{margin:18px 0}.editor textarea{display:block;box-sizing:border-box;width:100%;margin-top:8px;padding:14px;background:var(--bg);color:#edf5ff;border:1px solid #567b9e;border-radius:8px;font:14px/1.6 monospace;resize:vertical}.editor textarea:focus-visible{outline:3px solid var(--amber);outline-offset:3px}
'''
RULE_CSS += '.editor select{display:block;box-sizing:border-box;max-width:100%;padding:12px;background:var(--bg);color:#edf5ff;border:1px solid #567b9e;border-radius:8px;font:inherit}.editor select:focus-visible{outline:3px solid var(--amber);outline-offset:3px}'

RULE_HEADERS = dict(HEADERS, **{'Content-Security-Policy':
    "default-src 'none'; style-src 'sha256-" + base64.b64encode(hashlib.sha256(RULE_CSS.encode()).digest()).decode()
    + "'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"})


def load_mapping(path):
    try:
        data = yaml.load(read_private(path, MAX_MAPPING_BYTES), Loader=_StrictLoader)
        if (type(data) is not dict or not {'version', 'identities'} <= set(data) or not set(data) <= {'version', 'identities', 'accounts_file'}
                or type(data['version']) is not int or data['version'] != 1
                or type(data['identities']) is not list or len(data['identities']) > MAX_IDENTITIES):
            raise ValueError()
        if 'accounts_file' in data:
            storage_config(data['accounts_file'], Path(path).absolute().parent)
        seen = set(); subjects = {}
        for entry in data['identities']:
            if (type(entry) is not dict or set(entry) != {'username','subject','organization','atropos_client'}
                    or any(type(entry[key]) is not str or not IDENTIFIER.fullmatch(entry[key])
                           for key in ('username','subject','organization'))
                    or (entry['username'],entry['organization']) in seen or type(entry['atropos_client']) is not str
                    or not 0 < len(entry['atropos_client']) <= 4096
                    or not entry['atropos_client'].isprintable()):
                raise ValueError()
            if entry['username'] in subjects and subjects[entry['username']] != entry['subject']: raise ValueError()
            subjects[entry['username']] = entry['subject']
            seen.add((entry['username'],entry['organization']))
        if 'accounts_file' in data:
            for entry in data['identities']:
                entry['accounts_file'] = data['accounts_file']
        return data['identities']
    except (OSError, ValueError, TypeError, KeyError, UnicodeError, RecursionError, yaml.YAMLError):
        raise ConfigurationError('invalid_web_rule_mapping') from None


def client_factory(path, *, expected_actor):
    from atropos_client.client import RuleClient
    return RuleClient(path, expected_actor=expected_actor)


class RuleReader:
    methods = READ_METHODS
    def __init__(self, path, username, factory=client_factory, *, organization=None):
        self.path, self.username, self.factory = Path(path), username, factory
        self.organization = organization
        self.initial = self.entry()

    def entry(self):
        matches = [entry for entry in load_mapping(self.path) if entry['username'] == self.username]
        from atraxis.organization_context import selected
        entry = selected(matches, self.organization)
        from atraxis.account_store import require_membership
        require_membership(self.path, entry.get('accounts_file'), entry['subject'], entry['organization'])
        if hasattr(self, 'initial') and self.initial != entry:
            raise PermissionError('web_rule_identity_changed')
        return entry

    def call(self, method, *args, **kwargs):
        if method not in self.methods:
            raise PermissionError('web_rule_read_only')
        entry = self.entry()
        destination = Path(entry['atropos_client'])
        if not destination.is_absolute():
            destination = self.path.parent / destination
        client = self.factory(destination, expected_actor={key: entry[key] for key in ('subject','organization')})
        try:
            result = getattr(client, method)(*args, **kwargs)
            self.entry()  # Discard a result if mapping changed during the request.
            return result
        finally:
            client.close()


def query(path):
    if type(path) is not str or len(path.encode()) > MAX_QUERY_BYTES:
        raise ValueError('invalid_web_rule_query')
    pairs = parse_qsl(urlsplit(path).query, keep_blank_values=True, max_num_fields=3,
                      encoding='utf-8', errors='strict')
    values = dict(pairs)
    if len(values) != len(pairs) or not set(values) <= {'name','revision','offset'}:
        raise ValueError('invalid_web_rule_query')
    name = values.get('name', '')
    revision = values.get('revision')
    offset = values.get('offset', '0')
    if ((name and not NAME.fullmatch(name)) or (not name and values)
            or (revision is not None and not REVISION.fullmatch(revision))
            or not re.fullmatch(r'[0-9]{1,7}', offset) or int(offset) > MAX_OFFSET):
        raise ValueError('invalid_web_rule_query')
    return name, revision, int(offset)


def link(name, *, revision=None, offset=0):
    values = {'name':name, 'offset':offset}
    if revision is not None:
        values['revision'] = revision
    return '/ui/rules?' + text(urlencode(values))


def valid_revision(value):
    return type(value) is str and REVISION.fullmatch(value) is not None


def inspect_rule(reader, name, revision, offset):
    rows = reader.call('history', name, offset, PAGE_SIZE)
    state = reader.call('active', name)
    if (type(rows) is not list or len(rows) > PAGE_SIZE or type(state) is not dict
            or any(key not in state or state[key] is not None and not valid_revision(state[key])
                   for key in ('revision','generation'))):
        raise ValueError('invalid_web_rule_response')
    for row in rows:
        if (type(row) is not dict or not valid_revision(row.get('revision'))
                or type(row.get('author')) is not str or len(row['author']) > 128
                or type(row.get('message')) is not str or len(row['message']) > 256):
            raise ValueError('invalid_web_rule_response')
    record = None
    if revision is not None:
        record = reader.call('read', name, revision)
        if (type(record) is not dict or record.get('revision') != revision
                or type(record.get('document')) is not dict or record['document'].get('name') != name
                or len(json.dumps(record['document'], allow_nan=False).encode()) > MAX_DOCUMENT_BYTES):
            raise ValueError('invalid_web_rule_response')
    reader.entry()
    return rows, state, record


def page(body, name='', *, workbench=False, activation=False):
    intro = ('Edit drafts and evaluate scenarios. Permissions are checked by Atropos.' if workbench else 'Read versions and inspect the active revision. Permissions are checked by Atropos.')
    footer = ('Draft changes and scenario tests do not activate rules or execute actions.' if workbench else 'Read-only view · Opening a revision does not activate it. Refresh the page to check for changes.')
    if activation:
        intro = 'Review the exact revision, scope and active state before confirming a change.'
        footer = 'Lifecycle changes affect future evaluations. They do not cancel running actions or submit interventions.'
    form = '<form method="get" action="/ui/rules"><label>Rule name<input name="name" value="' + text(name) + '" required maxlength="64" autocomplete="off" placeholder="high-cpu"></label><button class="button" type="submit">Open rule</button></form>'
    return '<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Rules · Atraxis</title><style>' + RULE_CSS + '</style></head><body><a class="skip" href="#rules">Skip to rules</a><header><div class="brand">ATRA<span>XIS</span></div><nav aria-label="Main"><a href="/ui">Inventory</a><a href="/ui/rules" aria-current="page">Rules</a><a href="/ui/interventions">Interventions</a></nav></header><main id="rules"><div class="eyebrow">Atropos / Rule versions</div><h1>Rule explorer</h1><p class="muted">' + intro + '</p>' + form + body + '<footer>' + footer + '</footer></main></body></html>'


def render(reader, name, revision, offset, rows=None, state=None, record=None, *, editing=False, dispatching=False):
    actor = reader.initial
    body = '<p class="actor">Account: ' + text(actor['subject']) + ' · Organization: ' + text(actor['organization']) + '</p>'
    if editing:
        body += '<nav class="pager" aria-label="New rules"><a href="/ui/rules/create">Create from template</a><a href="/ui/rules/import">Import a new rule</a><a href="/ui/rules/simulate">Launch a test simulation</a></nav>'
    if dispatching:
        body += '<p><a href="/ui/rules/dispatch">Review a real intervention</a></p>'
    if not name:
        return page(body + '<div class="empty">Enter a rule name to view the revisions available to your account.</div>', workbench=editing)
    active = state['revision']
    active_link = 'Not active' if active is None else '<a href="' + link(name, revision=active) + '"><code>' + text(active) + '</code></a>'
    body += '<section class="source state"><h2>' + text(name) + '</h2><p>Active revision: ' + active_link + '</p><p class="muted">Generation: <code>' + text(state['generation'] or 'None') + '</code></p></section>'
    body += '<section class="source"><h2>Revision history</h2><p class="muted">Oldest first. Only revisions permitted by Atropos are shown.</p>'
    if not rows:
        body += '<p>No accessible revisions in this history window. Later windows may still contain accessible revisions.</p>'
    for row in rows:
        body += '<div class="revision"><a href="' + link(name, revision=row['revision'], offset=offset) + '"><code>' + text(row['revision']) + '</code></a><p class="muted">' + text(row['author']) + ' · ' + text(row['message']) + '</p></div>'
    # The engine filters history after paging; an empty page is not an end marker.
    body += '<nav class="pager" aria-label="History windows">'
    if offset:
        body += '<a href="' + link(name, offset=max(0,offset-PAGE_SIZE)) + '">Previous window</a>'
    if offset + PAGE_SIZE <= MAX_OFFSET:
        body += '<a href="' + link(name, offset=offset+PAGE_SIZE) + '">Next window</a>'
    body += '</nav><p class="muted">History window ' + str(offset//PAGE_SIZE+1) + '</p></section>'
    if record is not None:
        body += '<section class="source"><h2>Selected revision</h2><p><code>' + text(revision) + '</code></p><span class="badge ' + ('current' if revision == active else 'unknown') + '">' + ('Active at page load' if revision == active else 'Not the active revision') + '</span><h3>Rule document (YAML)</h3><pre>' + text(yaml.safe_dump(record['document'], sort_keys=False, allow_unicode=True)) + '</pre></section>'
    if editing:
        body += '<p>' + ' · '.join('<a href="/ui/rules/' + operation + '?' + text(urlencode({'name':name})) + '">Review ' + operation + '</a>' for operation in ('archive','unarchive','delete')) + '</p>'
    body += '<p><a href="/ui/rules/audit?' + text(urlencode({'name':name})) + '">Lifecycle audit</a></p>'
    if editing:
        if active is not None:
            body += '<p><a href="/ui/rules/deactivate?' + text(urlencode({'name':name})) + '">Review deactivation</a></p>'
        body += '<nav class="pager" aria-label="Rule workbench"><a href="/ui/rules/edit?' + text(urlencode({'name':name})) + '">Edit current draft</a>'
        if revision is not None:
            body += '<a href="/ui/rules/test?' + text(urlencode({'name':name, 'revision':revision})) + '">Test this revision</a>'
        body += '</nav>'
    return page(body, name, workbench=editing)


from atraxis.web_organization_context import contextual


@contextual
def handle(path, username, mapping, *, factory=client_factory, editing=False, dispatching=False, organization=None):
    try:
        name, revision, offset = query(path)
    except (ValueError, UnicodeError):
        return 400, page('<p class="notice" role="alert">' + ERRORS[400] + '</p>')
    try:
        reader = RuleReader(mapping, username, factory, organization=organization)
        values = inspect_rule(reader, name, revision, offset) if name else (None,None,None)
        return 200, render(reader, name, revision, offset, *values, editing=editing, dispatching=dispatching)
    except PermissionError:
        status = 403
    except Exception as error:
        # Only fixed client error codes affect the status; never echo exception text.
        status = 404 if str(error) == 'rule_not_found' else 503
    return status, page('<p class="notice" role="alert">' + ERRORS[status] + '</p>', name)
