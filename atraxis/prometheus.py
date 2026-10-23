"""Bounded read-only metric selection and pure Prometheus sample projection."""
from copy import deepcopy
import hashlib
import json
import math
import re
from urllib.parse import urlencode

from atraxis.models import Facts, Freshness, Resource, Snapshot
from atraxis.parsing import InvalidPayload, number, require, settings

METRIC_NAME = re.compile(r'[a-zA-Z_:][a-zA-Z0-9_:]{0,127}')
LABEL_NAME = re.compile(r'[a-zA-Z_][a-zA-Z0-9_]{0,63}')
SAMPLE_VALUE = re.compile(r'[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?')
METRIC_ID = re.compile(r'[0-9a-f]{64}')
MAX_LABELS = 16
MAX_LABEL_VALUE = 256
MAX_QUERY_BYTES = 8192
MAX_SERIES_SAMPLES = 1000
MAX_TOTAL_SAMPLES = 10000
METRIC_FIELDS = frozenset(('name', 'value', 'sampled_at', 'labels'))


def label_name(value):
    return (type(value) is str and LABEL_NAME.fullmatch(value) is not None
            and not value.startswith('__'))


def label_value(value):
    return (type(value) is str and len(value) <= MAX_LABEL_VALUE
            and (not value or value.isprintable()))


def selection(document):
    """Configuration accepts one named metric and exact matchers, not PromQL."""
    require(type(document) is dict and set(document) == {'name', 'match', 'labels'})
    require(type(document['name']) is str and METRIC_NAME.fullmatch(document['name']) is not None)
    match, labels = document['match'], document['labels']
    require(type(match) is dict and 1 <= len(match) <= MAX_LABELS)
    require(all(label_name(k) and label_value(v) and v for k, v in match.items()))
    require(type(labels) is list and 1 <= len(labels) <= MAX_LABELS)
    require(all(label_name(k) for k in labels) and len(set(labels)) == len(labels))
    require(set(match) <= set(labels))
    return deepcopy(document)


def query_parameters(metric, max_age, timeout, max_resources):
    spec = selection(metric)
    require(type(max_age) is int and 1 <= max_age <= 86400)
    require(type(timeout) is int and 1 <= timeout <= 60)
    require(type(max_resources) is int and 1 <= max_resources <= 10000)
    match = ','.join(k + '=' + json.dumps(v, ensure_ascii=False)
                     for k, v in sorted(spec['match'].items()))
    # A range selector preserves actual sample timestamps. An instant vector's
    # timestamp is the query evaluation time, not the original sample time.
    query = spec['name'] + '{' + match + '}[' + str(max_age) + 's]'
    parameters = dict(query=query, timeout=str(timeout) + 's', limit=max_resources + 1)
    require(len(urlencode(parameters).encode()) <= MAX_QUERY_BYTES)
    return parameters


def identity(name, labels):
    encoded = json.dumps([name, labels], ensure_ascii=True,
                         sort_keys=True, separators=(',', ':')).encode()
    return hashlib.sha256(encoded).hexdigest()


def validate_fields(fields, native_id):
    """Validate only the public projection, including signed numeric gauges."""
    require(type(fields) is dict and set(fields) == METRIC_FIELDS)
    name = fields['name']
    require(type(name) is str and METRIC_NAME.fullmatch(name) is not None)
    value = fields['value']
    require(type(value) in (int, float))
    try:
        require(math.isfinite(value))
    except OverflowError:
        raise InvalidPayload() from None
    number(fields['sampled_at'])
    labels = fields['labels']
    require(type(labels) is list and 1 <= len(labels) <= MAX_LABELS)
    require(all(type(row) is dict and set(row) == {'name', 'value'}
                and label_name(row['name']) and label_value(row['value']) for row in labels))
    names = [row['name'] for row in labels]
    require(names == sorted(set(names)))
    require(type(native_id) is str and METRIC_ID.fullmatch(native_id) is not None
            and native_id == identity(name, labels))
    return deepcopy(fields)


def parse_prometheus(payload, *, metric, source_id, received_at, monotonic,
                     max_age, max_resources, max_clock_skew):
    settings(source_id, received_at, monotonic, max_age, max_resources)
    spec = selection(metric)
    require(type(payload) is dict and payload.get('status') == 'success')
    # Warnings can accompany partial results; do not present them as complete.
    require(not payload.get('warnings') and not payload.get('infos'))
    data = payload.get('data')
    require(type(data) is dict and data.get('resultType') == 'matrix')
    result = data.get('result')
    require(type(result) is list and len(result) <= max_resources)
    resources, ages, seen = [], [], set()
    total = 0
    future = False
    for series in result:
        require(type(series) is dict and set(series) == {'metric', 'values'})
        labels = series['metric']
        require(type(labels) is dict and labels.get('__name__') == spec['name'])
        require(all(labels.get(k) == v for k, v in spec['match'].items()))
        require(all(k in labels and label_value(labels[k]) for k in spec['labels']))
        visible = [dict(name=k, value=labels[k]) for k in sorted(spec['labels'])]
        native_id = identity(spec['name'], visible)
        # Distinct series differing only in hidden labels are ambiguous, not merged.
        require(native_id not in seen)
        seen.add(native_id)
        samples = series['values']
        require(type(samples) is list and 1 <= len(samples) <= MAX_SERIES_SAMPLES)
        total += len(samples)
        require(total <= MAX_TOTAL_SAMPLES)
        previous = -1
        for sample in samples:
            require(type(sample) is list and len(sample) == 2)
            stamp, raw = sample
            number(stamp)
            require(stamp > previous)
            previous = stamp
            require(type(raw) is str and len(raw) <= 64 and SAMPLE_VALUE.fullmatch(raw) is not None)
            value = float(raw)
            require(math.isfinite(value))
        ages.append(max(0, received_at - stamp))
        future = future or stamp > received_at + max_clock_skew
        fields = (('name', spec['name']), ('value', value), ('sampled_at', stamp),
                  ('labels', tuple(Facts(tuple(row.items())) for row in visible)))
        resources.append(Resource(source_id, 'metric', native_id, fields))
    age = max(ages) if ages else None
    freshness = (Freshness.UNKNOWN if not ages or future else
                 Freshness.STALE if age > max_age else Freshness.FRESH)
    return Snapshot(source_id, 'prometheus', received_at, monotonic, age, max_age,
                    freshness, freshness == Freshness.FRESH,
                    tuple(sorted(resources, key=lambda resource: resource.native_id)))
