"""HTTPdis adapter; authentication and process lifecycle stay outside the core."""
import json
import signal
import threading

from httpdis.httpdis import HttpReqHandler, HttpResponse, HttpServerContext

from atraxis.service import ReadService
from atraxis.collection import PollingCoordinator


_HEADERS = {'Content-Type': 'application/json; charset=utf-8',
            'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'}
_METHODS = ('GET', 'HEAD')
_ROUTES = (('healthz', 'health'), ('readyz', 'readiness'), ('v1/status', 'status'))
_SOCKET_TIMEOUT = 5.0


class _Handler(HttpReqHandler):
    _ALLOWED_MULTIPART_FORM = False

    def data_from_payload(self, cmd):
        if self.path.startswith(('/ui/accounts/','/ui/inventory-access/','/ui/configuration/')) or self.path.split('?', 1)[0] in ('/ui/rules/create', '/ui/rules/import', '/ui/rules/edit', '/ui/rules/test', '/ui/rules/activate', '/ui/rules/rollback', '/ui/rules/deactivate', '/ui/rules/simulate', '/ui/rules/simulate-confirm', '/ui/rules/dispatch', '/ui/rules/dispatch-confirm', '/ui/rules/reconcile', '/ui/rules/reconcile-confirm'):
            lengths = self.headers.get_all('Content-Length', [])
            types = self.headers.get_all('Content-Type', [])
            if (len(lengths) != 1 or not lengths[0].isascii() or not lengths[0].isdigit()
                    or self.headers.get_all('Transfer-Encoding') or self.headers.get_all('Content-Encoding')
                    or len(types) != 1 or types[0].lower() != 'application/x-www-form-urlencoded'
                    or len(self.headers.get_all('Origin', [])) != 1
                    or len(self.headers.get_all('Sec-Fetch-Site', [])) > 1):
                raise self.req_error(400, 'Invalid rule form')
        return super().data_from_payload(cmd)

    def setup(self):
        self.request.settimeout(_SOCKET_TIMEOUT)
        super().setup()


def build_context(config, service=None, *, rule_client_factory=None):
    service = service if service is not None else ReadService(config.sources)
    collector = PollingCoordinator(config.sources, service)
    context = HttpServerContext()

    from atraxis.web import HEADERS as WEB_HEADERS, render_inventory

    from atraxis.accounts import AccountError
    from atraxis.inventory_access import InventoryAccess, requested_organization
    from atraxis.web import render_context_selection
    access = InventoryAccess(config.inventory_access_file) if config.inventory_access_file else None

    def configuration_link(body):
        return body.replace('</header>', '<a href="/ui/configuration">Configuration</a></header>', 1) if config.configuration_web_origin else body

    def scoped_inventory(request, readiness=False, html=False):
        try:
            scope, payload = access.snapshot(request.get_server_vars().get('HTTP_AUTH_USER'),
                requested_organization(request.path), service)
            if payload is None:
                if html:
                    return HttpResponse(code=200, data=render_context_selection(scope), headers=WEB_HEADERS.copy())
                value = dict(error='organization_required', organizations=scope['organizations'])
                code = 409
            elif html:
                return HttpResponse(code=200, data=configuration_link(render_inventory(payload,
                    rules_enabled=bool(config.rules_web_file), accounts_enabled=bool(config.accounts_web_file), inventory_admin_enabled=bool(config.inventory_admin_origin))), headers=WEB_HEADERS.copy())
            else:
                value = {k: payload[k] for k in ('ready','reason','context')} if readiness else payload
                code = 503 if readiness and not value['ready'] else 200
        except PermissionError:
            code, value = 403, dict(error='inventory_access_denied')
        except AccountError:
            code, value = 503, dict(error='inventory_access_unavailable')
        except (ValueError, TypeError):
            code, value = 400, dict(error='invalid_inventory_context')
        return HttpResponse(code=code, data=json.dumps(value)+'\n', headers=_HEADERS.copy())

    def unconfigured_context(request):
        try:
            if requested_organization(request.path) is not None:
                raise ValueError('inventory_context_unavailable')
        except (ValueError,TypeError):
            return HttpResponse(code=400,data=json.dumps(dict(error='inventory_context_unavailable')),headers=_HEADERS.copy())
        return None

    def inventory_page(request):
        if access:
            return scoped_inventory(request, html=True)
        denied=unconfigured_context(request)
        if denied is not None:return denied
        return HttpResponse(code=200, data=configuration_link(render_inventory(service.status(), rules_enabled=bool(config.rules_web_file), accounts_enabled=bool(config.accounts_web_file), inventory_admin_enabled=bool(config.inventory_admin_origin))),
                            headers=WEB_HEADERS.copy())

    for verb in _METHODS:
        context.register(inventory_page, op=verb, name='ui', to_auth=True, to_log=False)

    from atraxis.inventory_search import parse_query, search_inventory
    from atraxis.web_inventory_search import SEARCH_HEADERS, render_search, render_selection

    def inventory_search_page(request):
        html = request.path.split('?', 1)[0] == '/ui/search'
        try:
            organization, query = parse_query(request.path)
            if access:
                scope, result = access.search(request.get_server_vars().get('HTTP_AUTH_USER'), organization, query, service)
                if result is None:
                    if html:
                        return HttpResponse(code=200, data=render_selection(scope, query), headers=SEARCH_HEADERS.copy())
                    return HttpResponse(code=409, data=json.dumps(dict(error='organization_required')), headers=_HEADERS.copy())
            else:
                if organization is not None:
                    raise ValueError('invalid_inventory_search')
                result = search_inventory(service.status(), query)
            return HttpResponse(code=200, data=render_search(result) if html else json.dumps(result, allow_nan=False),
                                headers=SEARCH_HEADERS.copy() if html else _HEADERS.copy())
        except PermissionError:
            code, error = 403, 'inventory_access_denied'
        except AccountError:
            code, error = 503, 'inventory_access_unavailable'
        except (ValueError, TypeError):
            code, error = 400, 'invalid_inventory_search'
        return HttpResponse(code=code, data=json.dumps(dict(error=error)), headers=_HEADERS.copy())

    for name in ('ui/search', 'v1/inventory/search'):
        for verb in _METHODS:
            context.register(inventory_search_page, op=verb, name=name, to_auth=True, to_log=False)

    if config.rules_web_file is not None:
        from atraxis.web_rules import RULE_HEADERS, client_factory, handle, load_mapping
        load_mapping(config.rules_web_file)
        if rule_client_factory is None:
            # Enabling this integration requires the separately installed client.
            try:
                import atropos_client.client
            except ImportError:
                raise ValueError('rule_client_required') from None
        factory = rule_client_factory or client_factory

        def rules_page(request):
            username = request.get_server_vars().get('HTTP_AUTH_USER')
            code, body = handle(request.path, username, config.rules_web_file, factory=factory, editing=bool(config.rules_web_origin), dispatching=bool(config.rules_web_observations))
            return HttpResponse(code=code, data=body, headers=RULE_HEADERS.copy())

        from atraxis.web_rule_audit import handle as audit_handle

        def audit_page(request):
            username = request.get_server_vars().get('HTTP_AUTH_USER')
            code, body = audit_handle(request.path, username, config.rules_web_file, factory=factory, reports_enabled=bool(config.rules_web_origin))
            return HttpResponse(code=code, data=body, headers=RULE_HEADERS.copy())

        from atraxis.web_intervention_search import handle as intervention_handle
        from atraxis.web_intervention_detail import handle as intervention_detail_handle
        def intervention_detail_page(request):
            code,body=intervention_detail_handle(request.path,request.get_server_vars().get('HTTP_AUTH_USER'),config.rules_web_file,factory=factory,reconciliation_enabled=bool(config.rules_web_origin))
            return HttpResponse(code=code,data=body,headers=RULE_HEADERS.copy())

        def intervention_page(request):
            code,body=intervention_handle(request.path,request.get_server_vars().get('HTTP_AUTH_USER'),config.rules_web_file,factory=factory)
            return HttpResponse(code=code,data=body,headers=RULE_HEADERS.copy())

        for verb in _METHODS:
            context.register(intervention_detail_page,op=verb,name='ui/interventions/detail',to_auth=True,to_log=False)
            context.register(intervention_page,op=verb,name='ui/interventions',to_auth=True,to_log=False)
            context.register(audit_page, op=verb, name='ui/rules/audit', to_auth=True, to_log=False)
            context.register(rules_page, op=verb, name='ui/rules', to_auth=True, to_log=False)

    if config.rules_web_origin is not None:
        from atraxis.web_rule_editor import WebEditor, FORM_BYTES
        from atraxis.web_rule_creation import WebCreation, CREATION_ROUTES
        from atraxis.web_rule_activation import WebActivation, ACTIVATION_BYTES, LIFECYCLE_ROUTES
        from atraxis.web_rule_retirement import WebRetirement, RETIREMENT_ROUTES
        from atraxis.web_simulation import WebSimulation, SIMULATION_ROUTES
        from atraxis.web_reconciliation import WebReconciliation, RECONCILIATION_ROUTES, FORM_BYTES as RECONCILIATION_BYTES
        reconciliation = WebReconciliation(config.rules_web_file, config.rules_web_origin, factory)
        simulation = WebSimulation(config.rules_web_file, config.rules_web_origin, factory)
        retirement = WebRetirement(config.rules_web_file, config.rules_web_origin, factory)
        editor = WebEditor(config.rules_web_file, config.rules_web_origin, factory)
        creation = WebCreation(config.rules_web_file, config.rules_web_origin, factory)
        from atraxis.web_dispatch import WebDispatch, DISPATCH_ROUTES, FORM_BYTES as DISPATCH_BYTES
        dispatch_routes = DISPATCH_ROUTES if config.rules_web_observations else ()
        dispatch = None
        if dispatch_routes:
            dispatch = WebDispatch(config.rules_web_file, config.rules_web_origin, factory,
                                   config.rules_web_observations, access, service)
            dispatch.tokens = editor.tokens
        reconciliation.tokens = editor.tokens
        creation.tokens = editor.tokens
        simulation.tokens = editor.tokens
        activation = WebActivation(config.rules_web_file, config.rules_web_origin, factory)
        retirement.tokens = editor.tokens
        activation.tokens = editor.tokens  # One bounded form store for all browser operations.

        def editor_page(route):
            def handle_editor(request):
                username = request.get_server_vars().get('HTTP_AUTH_USER')
                payload = request.get_payload()
                controller = reconciliation if route in RECONCILIATION_ROUTES else dispatch if route in dispatch_routes else simulation if route in SIMULATION_ROUTES else creation if route in CREATION_ROUTES else retirement if route in RETIREMENT_ROUTES else activation if route in LIFECYCLE_ROUTES else editor
                code, body, extra = controller.handle(route, request.path, username,
                    method=request.command,
                    headers={key.lower(): value for key, value in request.headers.items()},
                    raw=payload.getvalue() if payload else b'')
                return HttpResponse(code=code, data=body, headers=dict(RULE_HEADERS, **{'Referrer-Policy': 'same-origin'}, **extra))
            return handle_editor

        for route_name in CREATION_ROUTES + ('edit', 'test') + LIFECYCLE_ROUTES + RETIREMENT_ROUTES + SIMULATION_ROUTES + dispatch_routes + RECONCILIATION_ROUTES:
            for verb in ('GET', 'HEAD', 'POST'):
                context.register(editor_page(route_name), op=verb, name='ui/rules/' + route_name,
                    to_auth=True, to_log=False, max_body_size=RECONCILIATION_BYTES if route_name in RECONCILIATION_ROUTES else DISPATCH_BYTES if route_name in dispatch_routes else ACTIVATION_BYTES if route_name in LIFECYCLE_ROUTES + RETIREMENT_ROUTES else FORM_BYTES)

    if config.accounts_web_file is not None:
        from atraxis.web_accounts import WebAccounts, FORM_BYTES as ACCOUNT_FORM_BYTES, HEADERS as ACCOUNT_HEADERS
        accounts = WebAccounts(config.accounts_web_file, config.accounts_web_origin)
        def accounts_page(request):
            payload = request.get_payload()
            code, body, extra = accounts.handle(request.path, request.get_server_vars().get('HTTP_AUTH_USER'),
                method=request.command, headers={key.lower():value for key,value in request.headers.items()},
                raw=payload.getvalue() if payload else b'')
            return HttpResponse(code=code,data=body,headers=dict(ACCOUNT_HEADERS,**extra))
        for name in ('ui/accounts','ui/accounts/search','ui/accounts/edit','ui/accounts/review','ui/accounts/commit'):
            verbs = ('POST',) if name.endswith(('/review','/commit')) else ('GET','HEAD')
            for verb in verbs:
                context.register(accounts_page,op=verb,name=name,to_auth=True,to_log=False,max_body_size=ACCOUNT_FORM_BYTES)

    if config.inventory_admin_origin is not None:
        from atraxis.web_inventory_admin import WebInventoryAdmin, HEADERS as INVENTORY_ADMIN_HEADERS, FORM_BYTES as INVENTORY_ADMIN_BYTES
        inventory_admin = WebInventoryAdmin(config.inventory_access_file,config.inventory_admin_origin)
        def inventory_admin_page(request):
            payload=request.get_payload()
            code,body,extra=inventory_admin.handle(request.path,request.get_server_vars().get('HTTP_AUTH_USER'),
                method=request.command,headers={key.lower():value for key,value in request.headers.items()},
                raw=payload.getvalue() if payload else b'')
            return HttpResponse(code=code,data=body,headers=dict(INVENTORY_ADMIN_HEADERS,**extra))
        for name in ('ui/inventory-access','ui/inventory-access/edit','ui/inventory-access/remove','ui/inventory-access/review','ui/inventory-access/commit'):
            verbs=('POST',) if name.endswith(('/review','/commit')) else ('GET','HEAD')
            for verb in verbs:
                context.register(inventory_admin_page,op=verb,name=name,to_auth=True,to_log=False,max_body_size=INVENTORY_ADMIN_BYTES)

    if config.configuration_web_origin is not None:
        from atraxis.web_configuration import WebConfiguration, HEADERS as CONFIG_HEADERS, FORM_BYTES as CONFIG_FORM_BYTES
        configuration = WebConfiguration(config.configuration_web_file, config.configuration_path,
                                         config.configuration_web_origin, config.sources)
        def configuration_page(request):
            payload = request.get_payload()
            code, body, extra = configuration.handle(request.path, request.get_server_vars().get('HTTP_AUTH_USER'),
                method=request.command, headers={key.lower(): value for key,value in request.headers.items()},
                raw=payload.getvalue() if payload else b'')
            return HttpResponse(code=code,data=body,headers=dict(CONFIG_HEADERS,**extra))
        for name in ('ui/configuration','ui/configuration/edit','ui/configuration/review','ui/configuration/commit'):
            for verb in (('POST',) if name.endswith(('/review','/commit')) else ('GET','HEAD')):
                context.register(configuration_page,op=verb,name=name,to_auth=True,to_log=False,max_body_size=CONFIG_FORM_BYTES)

    def route(method, readiness=False, inventory=False):
        def handle(request):
            if access and (readiness or inventory):
                return scoped_inventory(request, readiness=readiness)
            if readiness or inventory:
                denied=unconfigured_context(request)
                if denied is not None:return denied
            value = method()
            code = 503 if readiness and not value['ready'] else 200
            return HttpResponse(code=code, data=json.dumps(value, allow_nan=False) + '\n',
                                headers=_HEADERS.copy())
        return handle

    for name, method in _ROUTES:
        for verb in _METHODS:
            lifecycle = name == 'v1/status' and verb == 'GET'
            context.register(route(getattr(service, method), name == 'readyz', name == 'v1/status'),
                op=verb, name=name, to_auth=True, to_log=False,
                at_start=(lambda options: collector.start()) if lifecycle else None,
                at_stop=collector.stop if lifecycle else None)
    context.init({'listen_addr': config.listen_addr, 'listen_port': config.listen_port,
                  'max_workers': config.max_workers, 'auth_basic': 'Atraxis',
                  'auth_basic_file': config.auth_basic_file, 'max_body_size': 1024,
                  'server_version': 'Atraxis', 'sys_version': ''})
    if not context.auth or not context.auth.users:
        raise ValueError('empty_authentication_file')
    return context


def serve(config):
    run_context(build_context(config), _Handler)


def run_context(context, handler):
    previous = {}
    shutdown = threading.Event()
    cleanup_errors = []

    def stop():
        shutdown.wait()
        try:
            context.stop()
        except Exception:
            cleanup_errors.append(True)

    # Signal handlers only request shutdown; they must not re-enter a startup
    # lock or join a worker interrupted halfway through Thread.start().
    watcher = threading.Thread(target=stop, name='atraxis-shutdown')
    watcher.start()
    try:
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous[signum] = signal.signal(signum, lambda signum, frame: shutdown.set())
        context.run(http_req_handler=handler)
    finally:
        shutdown.set()
        watcher.join()
        for signum, handler in previous.items():
            signal.signal(signum, handler)
    if cleanup_errors:
        raise ValueError('server_shutdown_failed')
