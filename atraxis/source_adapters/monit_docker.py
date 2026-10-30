"""Optional Monit-Docker inventory source; no execution capabilities."""
from atraxis.parsing import parse_monit
from atraxis.source_adapters.contracts import inventory_options

KIND = 'monit_docker'
ROUTE = '/v1/status'
configure = inventory_options


def read_route(document):
    return ROUTE


def decode(payload, context):
    return parse_monit(payload, transport_elapsed=context.transport_elapsed,
                       **context.arguments())
