"""Read-only complete CertLord observations through Covenant and Prometheus."""

from urllib.parse import urlencode
from atraxis.prometheus_certlord import (
    KIND,
    selection,
    query_parameters,
    parse_prometheus_certlord,
)

ROUTE = "/api/v1/query"


def configure(document, *, max_age, timeout, max_resources):
    metric = selection(document.get("metric"))
    query_parameters(metric, max_age, timeout, max_resources)
    return metric


def read_route(document):
    return (
        ROUTE
        + "?"
        + urlencode(
            query_parameters(
                document.get("metric"),
                document["max_age"],
                document["timeout"],
                document["max_resources"],
            )
        )
    )


def decode(payload, context):
    return parse_prometheus_certlord(
        payload,
        metric=context.metric,
        max_clock_skew=context.max_clock_skew,
        **context.arguments(),
    )
