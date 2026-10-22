"""Rule CLI presentation. Operator/application/observation services own behavior."""
import argparse
import json
import sys
from pathlib import Path

from atraxis.rule_inputs import read_text, read_json

MAX_INPUT_BYTES = 65536
KINDS = ('supervision', 'intervention', 'alerting')


def _nullable(value):
    return None if value == 'none' else value


def _target(value):
    kind, separator, identity = value.partition(':')
    if not separator or kind not in ('server', 'container', 'server_group', 'container_group') or not identity:
        raise argparse.ArgumentTypeError('Expected kind:identity')
    return kind, identity


def add_parser(commands):
    parser = commands.add_parser('rules', help='local or authenticated remote rule commands')
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--operator-config', help='private local administrator configuration')
    source.add_argument('--remote-config', help='private remote rule client configuration')
    source.add_argument('--remote', action='store_true', help='remote connection from ATRAXIS_* environment and explicit options')
    parser.add_argument('-o','--organization', help='explicit organization; required for a multi-organization identity')
    parser.add_argument('--server-url', help='Atraxis HTTPS service URL')
    parser.add_argument('--token-file', help='private bearer token file; never pass the token value as an argument')
    parser.add_argument('--ca-file', help='CA bundle for the remote service')
    parser.add_argument('--request-timeout', type=int, help='request deadline in seconds (1–60)')
    commands = parser.add_subparsers(dest='rule_command', required=True)
    commands.add_parser('catalog')
    search=commands.add_parser('search-interventions',help='search your admitted runs for one rule')
    search.add_argument('name');search.add_argument('-q','--query',default='')
    search.add_argument('--mode',choices=('all','real','test'),default='all')
    search.add_argument('--status',default='');search.add_argument('--target',default='');search.add_argument('--workflow',default='')
    search.add_argument('--since',default='',help='UTC date, YYYY-MM-DDTHH:MM:SS')
    search.add_argument('--until',default='',help='UTC date, YYYY-MM-DDTHH:MM:SS')
    search.add_argument('--page',type=int,default=1);search.add_argument('--limit',type=int,default=50)
    search.add_argument('--tui',action='store_true')
    bindings = commands.add_parser('test-bindings', help='list authorized test workflow metadata')
    bindings.add_argument('name')
    bindings.add_argument('--target', type=_target, required=True)
    create = commands.add_parser('create')
    create.add_argument('templates', nargs='+')
    create.add_argument('--name', required=True)
    create.add_argument('--match', choices=('all', 'any'), default='any')
    create.add_argument('--decision', default='review')
    create.add_argument('--parameters')
    imported = commands.add_parser('import')
    imported.add_argument('file')
    imported.add_argument('--expected-revision', type=_nullable, required=True, help='exact draft head, or none for a new rule')
    for item in (create, imported):
        item.add_argument('--kind', choices=KINDS, required=True)
        item.add_argument('--target', type=_target, action='append', required=True)
    for command in ('read', 'export', 'test', 'activate', 'rollback'):
        item = commands.add_parser(command)
        item.add_argument('name')
        item.add_argument('revision')
        if command == 'test':
            item.add_argument('--cases', required=True)
        if command in ('activate', 'rollback'):
            item.add_argument('--test-id', required=True)
            item.add_argument('--expected-generation', type=_nullable, required=True)
    for command in ('archive', 'unarchive', 'delete'):
        item = commands.add_parser(command)
        item.add_argument('name')
        item.add_argument('--expected-revision', required=True)
        item.add_argument('--expected-generation', type=_nullable, required=True,
                          help='availability generation, or none; separate from activation')
        item.add_argument('--confirm', action='store_true', required=True)
    for command in ('availability', 'active', 'draft', 'deactivate', 'history', 'audit', 'evaluate', 'observe'):
        item = commands.add_parser(command)
        item.add_argument('name')
        if command == 'deactivate':
            item.add_argument('--expected-generation', type=_nullable, required=True)
        if command in ('history', 'audit'):
            item.add_argument('--offset', type=int, default=0)
            item.add_argument('--limit', type=int, default=20)
        if command in ('evaluate', 'observe'):
            item.add_argument('--target', type=_target, required=True)
        if command == 'evaluate':
            item.add_argument('--input', required=True)
    for command in ('dispatch-plan', 'dispatch', 'simulate-dispatch', 'observe-dispatch-plan', 'observe-dispatch', 'test-dispatch'):
        item = commands.add_parser(command)
        item.add_argument('name')
        item.add_argument('--target', type=_target, required=True)
        if not command.startswith('observe-'):
            item.add_argument('--input', required=True, help='explicit evaluation facts, not workflow parameters')
        item.add_argument('--binding', required=True, help='reviewed immutable binding ID')
        if command == 'test-dispatch':
            item.add_argument('--event-id', required=True, help='stable test identity')
        if command in ('dispatch', 'observe-dispatch'):
            item.add_argument('--expected-generation', required=True)
            item.add_argument('--event-id', required=True, help='stable event identity for recovery and deduplication')
            item.add_argument('--confirm', action='store_true', required=True, help='confirm the reviewed workflow submission')
    status = commands.add_parser('dispatch-status')
    status.add_argument('name'); status.add_argument('--event-id', required=True)
    test_status = commands.add_parser('test-dispatch-status')
    test_status.add_argument('name'); test_status.add_argument('--event-id', required=True)
    report = commands.add_parser('report')
    report.add_argument('name'); report.add_argument('revision'); report.add_argument('test_id')
    tui = commands.add_parser('tui', help='terminal rule workbench; requires a TTY')
    tui.add_argument('--name', required=True)
    tui.add_argument('--ui', choices=('curses', 'textual'), default='curses')
    tui.add_argument('--cases', help='synthetic scenario JSON used by the test key')






def run_command(options, operator):
    app = operator.application
    command = options.rule_command
    if command == 'test-bindings':
        return app.test_bindings(options.name, options.target)
    if command == 'catalog':
        return app.catalog()
    if command == 'create':
        return app.create_from_templates(options.templates, name=options.name, rule_kind=options.kind,
                 selections=options.target, match=options.match, decision=options.decision,
                 parameters=read_json(options.parameters) if options.parameters else None)
    if command == 'import':
        suffix = Path(options.file).suffix.lower()
        if suffix not in ('.yaml', '.yml', '.json'):
            raise ValueError('invalid_rule_file_extension')
        return app.import_draft(read_text(options.file), format='json' if suffix == '.json' else 'yaml',
                   rule_kind=options.kind, selections=options.target, expected_revision=options.expected_revision)
    if command in ('history', 'audit'):
        return getattr(app, command)(options.name, options.offset, options.limit)
    if command in ('archive', 'unarchive', 'delete'):
        return getattr(app, command)(options.name, expected_revision=options.expected_revision,
                                     expected_generation=options.expected_generation)
    if command=='search-interventions':
        from atraxis.intervention_search import InterventionQuery,SearchService,date_value
        query=InterventionQuery(q=options.query,mode=options.mode,status=options.status,target=options.target,
            workflow=options.workflow,since=date_value(options.since),until=date_value(options.until),page=options.page,limit=options.limit)
        service=SearchService(app,options.name)
        if options.tui:
            from atraxis.intervention_search_textual import run
            run(service,query);return None
        return service.search(query)
    if command in ('availability', 'active', 'draft'):
        return getattr(app, command)(options.name)
    if command == 'read':
        return app.read(options.name, options.revision)
    if command == 'export':
        return app.export_yaml(options.name, options.revision)
    if command == 'report':
        return app.test_report(options.name, options.revision, options.test_id)
    if command == 'test':
        return app.test(options.name, options.revision, read_json(options.cases))
    if command in ('activate', 'rollback'):
        return getattr(app, command)(options.name, options.revision, test_id=options.test_id,
                                     expected_generation=options.expected_generation)
    if command == 'deactivate':
        return app.deactivate(options.name, expected_generation=options.expected_generation)
    if command == 'evaluate':
        return app.evaluate_active(options.name, options.target, read_json(options.input))
    if command == 'test-dispatch':
        return app.test_dispatch(options.name, options.target, read_json(options.input), options.binding, event_id=options.event_id)
    if command == 'test-dispatch-status':
        return app.test_dispatch_status(options.name, options.event_id)
    if command == 'simulate-dispatch':
        return app.simulate_dispatch(options.name, options.target, read_json(options.input), options.binding)
    if command == 'observe-dispatch-plan':
        return operator.observed_dispatch.plan(options.name, options.target, options.binding)
    if command == 'observe-dispatch':
        if not options.confirm:
            raise ValueError('dispatch_confirmation_required')
        return operator.observed_dispatch.dispatch(options.name, options.target, options.binding,
                            expected_generation=options.expected_generation, event_id=options.event_id)
    if command == 'dispatch-plan':
        return app.plan_dispatch(options.name, options.target, read_json(options.input), options.binding)
    if command == 'dispatch':
        if not options.confirm:
            raise ValueError('dispatch_confirmation_required')
        return app.dispatch(options.name, options.target, read_json(options.input), options.binding,
                            expected_generation=options.expected_generation, event_id=options.event_id)
    if command == 'dispatch-status':
        return app.dispatch_status(options.name, options.event_id)
    if command == 'observe':
        return operator.live.evaluate(options.name, options.target)
    if command == 'tui':
        if getattr(options, 'ui', 'curses') == 'textual':
            from atraxis.rule_textual import run
        else:
            from atraxis.rule_tui import run
        run(operator, options.name, options.cases)
        return None
    raise ValueError('unknown_rule_command')


def main(options):
    try:
        from atraxis.rule_remote import RemoteRuleError, RuleClientConfigurationError
    except ImportError:
        print('{"error":"rule_client_required"}', file=sys.stderr)
        return 2
    operator = None
    try:
        overrides = {key: value for key, value in (
            ('origin', getattr(options, 'server_url', None)),
            ('token_file', getattr(options, 'token_file', None)),
            ('ca_file', getattr(options, 'ca_file', None)),
            ('timeout', getattr(options, 'request_timeout', None))) if value is not None}
        if options.operator_config and overrides:
            raise ValueError('remote_options_with_operator_config')
        context = {} if getattr(options,'organization',None) is None else dict(organization=options.organization)
        if options.remote_config or getattr(options, 'remote', False):
            from atraxis.rule_remote import RemoteRuleOperator
            operator = RemoteRuleOperator(options.remote_config, overrides=overrides, **context)
        else:
            from atraxis.rule_operator import LocalRuleOperator
            operator = LocalRuleOperator(options.operator_config, **context)
        result = run_command(options, operator)
        if result is not None:
            print(result if options.rule_command == 'export' else json.dumps(result, ensure_ascii=True, allow_nan=False, indent=2))
        return 1 if options.rule_command == 'test' and not result['passed'] else 0
    except RuleClientConfigurationError:
        print('{"error":"invalid_rule_client_configuration"}', file=sys.stderr)
        return 2
    except RemoteRuleError as error:
        print(json.dumps({'error': str(error)}), file=sys.stderr)
        return 2
    except PermissionError:
        print('{"error":"rule_access_denied"}', file=sys.stderr)
        return 3
    except ImportError:
        print('{"error":"rule_client_required"}', file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
    except BrokenPipeError:
        return 2
    except Exception:
        # Do not expose Redis credentials, paths, upstream data or native engine errors.
        print('{"error":"rule_operation_failed_refresh_state_before_retry"}', file=sys.stderr)
        return 2
    finally:
        if operator is not None:
            operator.close()
