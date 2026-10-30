"""Complete native observation through the versioned Covenant contract."""

from atraxis.prometheus_observation import (
    selection as _selection,
    query_parameters as _query,
    parse_observation,
)

KIND = "prometheus_monit"


def selection(document):
    return _selection(document, "monit")


def query_parameters(metric, max_age, timeout, max_resources):
    return _query(metric, max_age, timeout, max_resources, "monit")


def parse_prometheus_monit(payload, **kwargs):
    return parse_observation(payload, kind="monit", **kwargs)
