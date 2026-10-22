"""Authenticated rule gateway; observation attestations are built server-side."""
import hashlib
import hmac
import json
import re
from pathlib import Path

from httpdis.authentication import Identity, AuthenticationDenied
from httpdis.httpdis import HttpReqHandler, HttpResponse, HttpServerContext

from atraxis.rule_operator import LocalRuleOperator, load_operator_config
from atraxis.rule_protocol import ROUTE, TOKEN_PATTERN, MAX_REQUEST_BYTES, MAX_RESPONSE_BYTES, command
from atraxis.rule_operator import RuleActor
from atropos_client.client import RemoteRuleError
from atropos_client.protocol import ENGINE_OBSERVATION_METHODS

LISTEN_ADDRESS = '127.0.0.1'
DEFAULT_PORT = 8081
SOCKET_TIMEOUT = 10
HEADERS = {'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'no-store',
           'X-Content-Type-Options': 'nosniff', 'Connection': 'close', 'X-Rule-Service': 'atraxis'}


def load_service_config(path):
    return load_operator_config(path, remote=True)


def mapped(document, digest):
    matches = [item for item in document['identities']
               if hmac.compare_digest(item['token_sha256'], digest)]
    if not matches:
        raise PermissionError('rule_access_denied')
    return RuleActor(matches[0]['subject'], matches[0]['organization'])


class TokenProvider:
    challenge = 'Bearer realm="Atraxis rules"'

    def __init__(self, path):
        self.path = path

    def authenticate(self, request):
        # This is a machine/terminal API, without cookies or browser sessions.
        if any(request.header(name) is not None for name in ('cookie', 'origin', 'sec-fetch-site')):
            raise AuthenticationDenied()
        authorization = request.header('authorization', '')
        if not authorization.startswith('Bearer '):
            raise AuthenticationDenied()
        token = authorization[7:]
        if re.fullmatch(TOKEN_PATTERN, token) is None:
            raise AuthenticationDenied()
        digest = hashlib.sha256(token.encode('ascii')).hexdigest()
        document = load_service_config(self.path)
        try:
            mapped(document, digest)
        except PermissionError:
            raise AuthenticationDenied() from None
        return Identity(digest, 'bearer')


class RuleSession(LocalRuleOperator):
    remote = True

    def __init__(self, path, identity, *, organization=None):
        if not isinstance(identity, Identity) or identity.method != 'bearer':
            raise PermissionError('rule_access_denied')
        self.digest = identity.principal
        super().__init__(path, organization=organization)


class RuleHandler(HttpReqHandler):
    _ALLOWED_CONTENT_TYPES = ['application/json']
    _ALLOWED_MULTIPART_FORM = False

    def setup(self):
        self.request.settimeout(SOCKET_TIMEOUT)
        super().setup()

    def do_OPTIONS(self):
        self.send_error_msg(405, 'Method not allowed')

    @staticmethod
    def parse_payload(data, charset):
        # Keep JSON parsing in the bounded protocol decoder, not the form parser.
        return None

    def data_from_payload(self, cmd):
        # Validate framing before HTTPdis reads any body. Never accept chunked,
        # duplicate lengths, compression or query aliases on the control route.
        lengths = self.headers.get_all('Content-Length', [])
        types = self.headers.get_all('Content-Type', [])
        if (self.path != ROUTE or len(lengths) != 1 or not lengths[0].isascii()
                or not lengths[0].isdigit() or self.headers.get_all('Transfer-Encoding')
                or self.headers.get_all('Content-Encoding') or len(types) != 1
                or types[0].lower() != 'application/json'
                or len(self.headers.get_all('X-Atraxis-Organization',[]))>1):
            raise self.req_error(400, 'Invalid rule request')
        return super().data_from_payload(cmd)


def build_context(path, port=DEFAULT_PORT):
    path = Path(path).absolute()
    load_service_config(path)
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError('invalid_rule_service_port')

    def handle(request):
        session = None
        try:
            method, values = command(request.get_payload().getvalue())
            identity = request.get_server_vars().get('HTTP_AUTH_IDENTITY')
            organization=request.headers.get('X-Atraxis-Organization')
            session = RuleSession(path, identity, organization=organization)
            if method in ENGINE_OBSERVATION_METHODS:
                raise PermissionError('engine_observation_method_not_public')
            if method == 'observe_dispatch_plan':
                result = session.observed_dispatch.plan(**values)
            elif method == 'observe_dispatch':
                result = session.observed_dispatch.dispatch(**values)
            elif method == 'observe':
                result = session.live.evaluate(values['name'], tuple(values['target']))
            else:
                if method == 'evaluate_active':
                    values['target'] = tuple(values['target'])
                result = getattr(session.application, method)(**values)
            body = json.dumps({'result': result}, ensure_ascii=True, allow_nan=False)
            if len(body.encode()) > MAX_RESPONSE_BYTES:
                raise RuntimeError('response_limit')
            return HttpResponse(code=200, data=body, headers=HEADERS.copy())
        except PermissionError:
            code, error = 403, 'rule_access_denied'
        except RemoteRuleError as failure:
            error = str(failure)
            codes = {'rule_queue_full':429, 'rule_conflict':409, 'rule_not_found':404, 'invalid_rule_request':400,
                     'rule_service_unavailable':503, 'rule_outcome_unknown':503}
            code = codes.get(error, 503)
            if error not in codes:
                error = 'rule_outcome_unknown'
        except KeyError:
            code, error = 404, 'rule_not_found'
        except (ValueError, TypeError, AttributeError, RecursionError):
            code, error = 400, 'invalid_rule_request'
        except Exception:
            # Storage/native errors may occur after a commit. Do not claim failure.
            code, error = 503, 'rule_outcome_unknown'
        finally:
            if session is not None:
                session.close()
        return HttpResponse(code=code, data=json.dumps({'error': error}), headers=HEADERS.copy())

    context = HttpServerContext()
    context.register(handle, op='POST', name=ROUTE.lstrip('/'), to_auth=True, to_log=False,
                     max_body_size=MAX_REQUEST_BYTES)
    context.init({'listen_addr': LISTEN_ADDRESS, 'listen_port': port, 'max_workers': 4,
                  'auth_provider': TokenProvider(path), 'max_body_size': MAX_REQUEST_BYTES,
                  'server_version': 'Atraxis-rules', 'sys_version': ''})
    return context


def serve(path, port=DEFAULT_PORT):
    from atraxis.server import run_context
    run_context(build_context(path, port), RuleHandler)
