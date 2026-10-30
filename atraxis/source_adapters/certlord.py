"""Optional CertLord certificate observation source; no issuance capabilities."""
from atraxis.parsing import parse_certlord
from atraxis.source_adapters.contracts import inventory_options

KIND = 'certlord'
ROUTE = '/api/certificates/observations'
configure = inventory_options


def read_route(document):
    return ROUTE


def decode(payload, context):
    return parse_certlord(payload, max_clock_skew=context.max_clock_skew,
                          **context.arguments())
