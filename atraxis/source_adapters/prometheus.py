"""Optional Prometheus source; exact bounded selections, never arbitrary PromQL."""
from urllib.parse import urlencode

from atraxis.prometheus import parse_prometheus, query_parameters, selection

KIND = 'prometheus'
ROUTE = '/api/v1/query'


def configure(document, *, max_age, timeout, max_resources):
    metric = selection(document.get('metric'))
    query_parameters(metric, max_age, timeout, max_resources)
    return metric


def read_route(document):
    return ROUTE + '?' + urlencode(query_parameters(document.get('metric'),
        document['max_age'], document['timeout'], document['max_resources']))


def decode(payload, context):
    return parse_prometheus(payload, metric=context.metric,
                            max_clock_skew=context.max_clock_skew,
                            **context.arguments())
