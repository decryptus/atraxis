"""Explicit new drafts over the independent client, without activation."""
import json
from urllib.parse import parse_qsl, urlencode

from atraxis.web import text
from atraxis.web_rules import RuleReader, NAME, IDENTIFIER, MAX_DOCUMENT_BYTES, page
from atraxis.web_rule_editor import (FormTokens, FORM_BYTES, REPORT_BYTES, ERRORS,
    UNCERTAIN, validate_origin, record_checked, context_checked, form_document)
from atraxis.web_organization_context import contextual

CREATION_ROUTES = ('create', 'import')
MAX_TEMPLATES = 128
MAX_DESCRIPTION = 512
FIELDS = frozenset(('token', 'name', 'rule_kind', 'target_kind', 'target_id'))
RULE_KINDS = ('intervention', 'supervision', 'alerting')
TARGET_KINDS = ('server', 'container')


class CreationClient(RuleReader):
    methods = frozenset(('catalog', 'create_from_templates', 'import_draft'))


def catalog_checked(value):
    if (type(value) is not dict or type(value.get('catalog_version')) is not int
            or value['catalog_version'] < 1 or type(value.get('templates')) is not list
            or not 1 <= len(value['templates']) <= MAX_TEMPLATES
            or len(json.dumps(value, allow_nan=False).encode()) > REPORT_BYTES):
        raise ValueError('invalid_web_rule_response')
    seen = set()
    for item in value['templates']:
        if (type(item) is not dict or type(item.get('id')) is not str
                or not IDENTIFIER.fullmatch(item['id']) or item['id'] in seen
                or type(item.get('description')) is not str
                or len(item['description']) > MAX_DESCRIPTION):
            raise ValueError('invalid_web_rule_response')
        seen.add(item['id'])
    return value


def read_page(route, reader, tokens):
    catalog = catalog_checked(reader.call('catalog')) if route == 'create' else None
    context = dict(catalog_version=catalog['catalog_version'],
                   templates=[item['id'] for item in catalog['templates']]) if catalog else None
    token = tokens.issue(reader, route, None, None, context)
    body = '<h2>' + ('Create from template' if catalog else 'Import a new rule') + '</h2>'
    body += '<p>This creates a new draft only. Review its thresholds, test it, then separately review activation. Existing rules are never overwritten.</p>'
    body += '<form class="editor" method="post" action="/ui/rules/' + route + '">'
    body += '<input type="hidden" name="token" value="' + text(token) + '">'
    for label, name, limit, default in (
            ('New rule name', 'name', 64, ''), ('Target ID', 'target_id', 128, '')):
        body += '<label>' + label + '<input name="' + name + '" required maxlength="' + str(limit) + '" value="' + default + '" autocomplete="off"></label>'
    for label, name, options in (('Rule kind', 'rule_kind', RULE_KINDS), ('Target kind', 'target_kind', TARGET_KINDS)):
        body += '<label for="creation-' + name + '">' + label + '</label><select id="creation-' + name + '" name="' + name + '" required>'
        body += ''.join('<option value="' + option + '">' + option + '</option>' for option in options) + '</select>'
    body += '<p>Atropos checks your current permission for this rule kind and target.</p>'
    if catalog:
        body += '<label for="creation-template">Template</label><select id="creation-template" name="template" required>'
        for item in catalog['templates']:
            body += '<option value="' + text(item['id']) + '">' + text(item['description']) + '</option>'
        body += '</select>'
    else:
        body += '<label>Rule YAML<textarea name="document" rows="20" required maxlength="' + str(MAX_DOCUMENT_BYTES) + '" spellcheck="false"></textarea></label>'
        body += '<p>The document name must match the new rule name above.</p>'
    body += '<button class="button" type="submit">Create draft</button></form>'
    reader.entry()
    return page(body, workbench=True)


def parse_form(raw, route):
    if type(raw) is not bytes or not 0 < len(raw) <= FORM_BYTES:
        raise ValueError('invalid_rule_request')
    try:
        pairs = parse_qsl(raw.decode('utf-8'), keep_blank_values=True, strict_parsing=True,
                          max_num_fields=6, encoding='utf-8', errors='strict')
        values = dict(pairs)
        expected = FIELDS | {'template' if route == 'create' else 'document'}
        if len(values) != len(pairs) or set(values) != expected or len(values['token']) != 43:
            raise ValueError()
        if not NAME.fullmatch(values['name']) or any(
                not IDENTIFIER.fullmatch(values[key]) for key in ('rule_kind', 'target_kind', 'target_id')):
            raise ValueError()
        if values['rule_kind'] not in RULE_KINDS or values['target_kind'] not in TARGET_KINDS:
            raise ValueError()
        return values
    except (ValueError, UnicodeError):
        raise ValueError('invalid_rule_request') from None


class WebCreation:
    def __init__(self, mapping, origin, factory):
        self.mapping, self.origin, self.factory = mapping, validate_origin(origin), factory
        self.tokens = FormTokens()

    @contextual
    def handle(self, route, path, username, *, method='GET', headers=None, raw=b'', organization=None):
        attempted = False
        try:
            if route not in CREATION_ROUTES or path != '/ui/rules/' + route:
                raise ValueError('invalid_rule_request')
            reader = CreationClient(self.mapping, username, self.factory, organization=organization)
            if method in ('GET', 'HEAD'):
                return 200, read_page(route, reader, self.tokens), {}
            headers = headers or {}
            if (method != 'POST' or headers.get('origin') != self.origin
                    or headers.get('sec-fetch-site', 'same-origin') != 'same-origin'):
                raise PermissionError('invalid_web_origin')
            values = parse_form(raw, route)
            pending = self.tokens.consume(values['token'], reader, route)
            name = values['name']
            selections = [[values['target_kind'], values['target_id']]]
            if route == 'create':
                if values['template'] not in pending['context']['templates']:
                    raise ValueError('invalid_rule_request')
                attempted = True
                result = reader.call('create_from_templates', [values['template']], name=name,
                    rule_kind=values['rule_kind'], selections=selections,
                    catalog_version=pending['context']['catalog_version'])
            else:
                document = form_document(dict(document=values['document'], message=''), name)
                attempted = True
                result = reader.call('import_draft', json.dumps(document, allow_nan=False),
                    format='json', rule_kind=values['rule_kind'], selections=selections,
                    expected_revision=None, message='Imported through browser')
            result = record_checked(result, name)
            context_checked(result.get('context'))
            if result['context'] != dict(version=1, rule_kind=values['rule_kind'], targets=selections):
                raise ValueError('invalid_web_rule_response')
            return 303, '', {'Location': '/ui/rules?' + urlencode(dict(name=name, revision=result['revision']))}
        except PermissionError:
            status = 403
        except Exception as error:
            code = str(error)
            if code in ('rule_conflict', 'rule_not_found', 'invalid_rule_request'):
                status = {'rule_conflict': 409, 'rule_not_found': 404, 'invalid_rule_request': 400}[code]
            else:
                status = 503
        message = UNCERTAIN if attempted and status == 503 else ERRORS[status]
        if status == 409:
            message = 'The rule already exists. Open its current draft; this form never overwrites it.'
        if attempted and status == 403:
            message += ' If access changed during the request, an operation may already have completed.'
        return status, page('<p class="notice" role="alert">' + message + '</p>', workbench=True), {}
