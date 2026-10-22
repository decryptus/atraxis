"""Trusted-host setup diagnostics with explicit, bounded source reads."""
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
from pathlib import Path
import signal
import sys
import threading

from sonicprobe.libs import xys

from atraxis.config import CONFIG_SCHEMA, load_document, parse_config
from atraxis.source_config import parse_sources

PROFILE_GROUPS = {
    'rules': ('rules_web_file', 'rules_web_origin'),
    'accounts': ('accounts_web_file', 'accounts_web_origin'),
    'inventory_access': ('inventory_access_file', 'inventory_admin_origin'),
    'configuration_admin': ('configuration_web_file', 'configuration_web_origin'),
}
PROFILE_FIELDS = frozenset(field for fields in PROFILE_GROUPS.values() for field in fields)
ADVICE = {
    'invalid_document': 'Check the YAML syntax, duplicate keys, supported fields and file size.',
    'invalid_runtime': 'Check the listener address/port, TLS proxy setting and readable nonempty htpasswd file.',
    'invalid_sources': 'Check the source count, unique IDs, origins, credential/CA files and timing limits.',
    'invalid_profile': 'Check the private profile format, origin and referenced account/client files.',
    'invalid_accounts': 'Check the current account database, file permissions and format. No database was initialized.',
    'external_accounts_unchecked': 'This offline check only opens SQLite/JSON account adapters; qualify the installed adapter separately.',
    'client_unavailable': 'Install the matching independent Atropos client and check each private client/token/CA file.',
    'no_sources': 'Add a monit-docker or CertLord source; an empty configuration is never ready.',
    'shared_inventory': 'All authenticated users share inventory. Configure inventory_access_file before adding scoped users.',
    'not_configured': 'Optional component is not configured.',
    'configured': 'Configuration is valid; connection and current observations have not been checked.',
    'profile_valid': 'Local profile and supported referenced files are valid; remote authority is not verified.',
    'runtime_valid': 'Listener settings and authentication file are valid; actual login and TLS proxy are not tested.',
    'fresh': 'Current observations were validated; this does not authorize an intervention.',
    'not_current': 'The source responded, but observations are stale, unavailable or not authoritative.',
    'authentication_error': 'Check the source username/password file and source-side authentication.',
    'forbidden': 'Check source-side read permissions.',
    'tls_error': 'Check the CA bundle, hostname, certificate validity and TLS listener.',
    'redirect_refused': 'Configure the final HTTPS origin; redirects are refused.',
    'endpoint_unavailable': 'Check the component version and its supported observation route.',
    'invalid_payload': 'Check the source API version and observation format; response content is not displayed.',
    'timeout': 'Check DNS, routing and source response time against the configured timeout.',
    'response_too_large': 'Review the source response size and configured observation limits.',
    'configuration_error': 'Recheck the source credential/CA files; they may have changed since validation.',
    'cancelled': 'The read was cancelled; no retry was made.',
    'source_unavailable': 'Check the source service and network; no redirect or retry was attempted.',
}


def row(component, state, code):
    return dict(component=component, state=state, code=code, advice=ADVICE[code])


def check_account_store(config):
    from atraxis.account_store import AccountStore
    if config['backend'] not in ('sqlite', 'json'):
        return 'external_accounts_unchecked'
    AccountStore(config).snapshot(audit=False)
    return None


def inspect_profile(kind, config):
    if kind == 'configuration_admin':
        from atraxis.configuration_admin import load_profile
        _, store, _ = load_profile(config.configuration_web_file)
        return check_account_store(store)
    if kind == 'accounts':
        from atraxis.account_admin import load_admin_profile
        _, store, _ = load_admin_profile(config.accounts_web_file)
        return check_account_store(store)
    if kind == 'inventory_access':
        from atraxis.inventory_access import load_profile
        _, store, _ = load_profile(config.inventory_access_file)
        return check_account_store(store)
    from atraxis.web_rules import load_mapping
    from atraxis.account_storage import storage_config
    from atropos_client.client import RuleClient
    path = Path(config.rules_web_file)
    unchecked = None
    for entry in load_mapping(path):
        if 'accounts_file' in entry:
            unchecked = check_account_store(storage_config(entry['accounts_file'], path.parent)) or unchecked
        client = RuleClient(path.parent / entry['atropos_client'],
                            expected_actor={key: entry[key] for key in ('subject', 'organization')})
        client.close()  # Construction validates files only; no rule RPC is made.
    return unchecked


def diagnose(path, *, probe=False, cancel=None):
    """No implicit network: probe reads only configured source observation routes."""
    report = dict(version=1, probe_requested=probe, configuration_valid=False,
                  sources_ready=None, checks=[])
    checks = report['checks']
    cancel = cancel if cancel is not None else threading.Event()
    try:
        document = load_document(path)
        if not xys.validate(document, CONFIG_SCHEMA):
            raise ValueError()
        if 'configuration_history' in document:
            from atraxis.configuration_admin import validate_history
            validate_history(document['configuration_history'])
    except (OSError, ValueError, TypeError, RecursionError):
        checks.append(row('configuration', 'error', 'invalid_document'))
        return report, 1
    base = {key: value for key, value in document['general'].items() if key not in PROFILE_FIELDS}
    try:
        parse_config(dict(general=base, sources=[]), path)
        checks.append(row('runtime', 'passed', 'runtime_valid'))
    except (OSError, ValueError, TypeError, RecursionError):
        checks.append(row('runtime', 'error', 'invalid_runtime'))
    source_documents = document.get('sources', [])
    try:
        sources = parse_sources(source_documents, path)
        if not sources:
            checks.append(row('sources', 'warning', 'no_sources'))
    except (OSError, ValueError, TypeError, RecursionError):
        sources = ()
        checks.append(row('sources', 'error', 'invalid_sources'))
    for kind, fields in PROFILE_GROUPS.items():
        if not any(field in document['general'] for field in fields):
            checks.append(row(kind, 'warning' if kind == 'inventory_access' else 'not_checked',
                              'shared_inventory' if kind == 'inventory_access' else 'not_configured'))
            continue
        try:
            general = dict(base, **{field: document['general'][field] for field in fields if field in document['general']})
            config = parse_config(dict(general=general, sources=[]), path)
        except (OSError, ValueError, TypeError, RecursionError):
            checks.append(row(kind, 'error', 'invalid_profile'))
            continue
        try:
            unchecked = inspect_profile(kind, config)
            checks.append(row(kind, 'warning' if unchecked else 'passed', unchecked or 'profile_valid'))
        except (ImportError, OSError, ValueError, TypeError, RecursionError):
            checks.append(row(kind, 'error', 'client_unavailable' if kind == 'rules' else 'invalid_accounts'))
    report['configuration_valid'] = not any(check['state'] == 'error' for check in checks)
    if not probe or not report['configuration_valid']:
        checks.extend(row('source:' + source.source_id, 'not_checked', 'configured') for source in sources)
        return report, 1 if not report['configuration_valid'] else 2 if any(c['state'] == 'warning' for c in checks) else 0
    from atraxis.service import ReadService
    from atraxis.transport import HTTPTransport, DEFAULT_CONCURRENCY
    service = ReadService(sources)
    service.set_running(True)
    transport = HTTPTransport()
    try:
        with ThreadPoolExecutor(max_workers=DEFAULT_CONCURRENCY) as pool:
            try:
                futures = {pool.submit(transport.fetch, source, cancel=cancel): source for source in sources}
                for future in as_completed(futures):
                    source = futures[future]
                    service.record(source.source_id, future.result())
            except BaseException:
                cancel.set()  # Reap transport workers on interruption before executor shutdown.
                raise
        snapshot = service.status()
        report['sources_ready'] = snapshot['ready']
        for source in snapshot['sources']:
            outcome = source['last_attempt']['outcome']
            code = 'fresh' if source['current'] else 'not_current' if outcome == 'success' else outcome if outcome in ADVICE else 'source_unavailable'
            checks.append(row('source:' + source['source_id'], 'passed' if source['current'] else 'warning', code))
    finally:
        service.set_running(False)
    return report, 0 if report['sources_ready'] and not any(c['state'] == 'warning' for c in checks) else 2


def add_parser(commands):
    parser = commands.add_parser('doctor', help='diagnose local setup; source network reads require --probe-sources')
    parser.add_argument('--config', required=True)
    parser.add_argument('--probe-sources', action='store_true')
    parser.add_argument('--json', action='store_true')


def main(options):
    cancel = threading.Event()
    terminated = threading.Event()
    previous = None
    def terminate(signum, frame):
        terminated.set(); cancel.set()
    try:
        if threading.current_thread() is threading.main_thread():
            previous = signal.signal(signal.SIGTERM, terminate)
        report, code = diagnose(options.config, probe=options.probe_sources, cancel=cancel)
        if options.json:
            print(json.dumps(report, ensure_ascii=True, allow_nan=False, indent=2))
        else:
            print('Source readiness: ' + ('not checked' if report['sources_ready'] is None else 'ready' if report['sources_ready'] else 'not ready'))
            for check in report['checks']:
                print('%s %s: %s' % (check['state'].upper(), json.dumps(check['component'], ensure_ascii=True), check['advice']))
        return 143 if terminated.is_set() else code
    except KeyboardInterrupt:
        cancel.set()
        return 130
    except (OSError, ValueError, TypeError, RecursionError):
        print('Setup diagnosis failed; check local configuration and referenced files.', file=sys.stderr)
        return 1
    finally:
        cancel.set()
        if previous is not None:
            signal.signal(signal.SIGTERM, previous)
