"""Explicit Monit-Docker metric contract via a single read-only Prometheus query."""

from urllib.parse import urlencode

from atraxis.prometheus_monit import (
    KIND,
    parse_prometheus_monit,
    query_parameters,
    selection,
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
    return parse_prometheus_monit(
        payload,
        metric=context.metric,
        max_clock_skew=context.max_clock_skew,
        **context.arguments(),
    )
