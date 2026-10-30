"""Complete Covenant observations with native parsing and scrape-time joins."""

from copy import deepcopy
from dataclasses import replace
import json
import math
from urllib.parse import urlencode

from atraxis.models import Freshness, Snapshot
from atraxis.parsing import (
    InvalidPayload,
    number,
    parse_monit,
    parse_certlord,
    require,
    settings,
)
from atraxis.prometheus import (
    MAX_QUERY_BYTES,
    LABEL_NAME,
    label_name,
    label_value,
    SAMPLE_VALUE,
)
from atraxis.observation_codec import MAX_ROWS, MAX_TEXT, InvalidObservation, decode

METRIC_LABEL = "covenant_metric"
CLOCK_LABEL = "covenant_sample_time"
RESERVED = frozenset(
    ("__name__", METRIC_LABEL, CLOCK_LABEL, "path", "type", "text", "sha256", "version")
)
SUFFIXES = (
    "up",
    "value",
    "manifest",
    "collected_at_seconds",
    "collection_elapsed_seconds",
)
MAX_MATCHERS = 12
MAX_VECTOR = 2 * (MAX_ROWS + len(SUFFIXES) + 1)


def selection(document, kind):
    keys = {"match", "prefix"} if kind == "monit" else {"match"}
    require(type(document) is dict and set(document) == keys)
    # This is a versioned contract, not arbitrary metrics or user-supplied PromQL.
    if kind == "monit":
        require(document["prefix"] == "covenant_monit_observation_")
    match = document["match"]
    require(
        type(match) is dict
        and 2 <= len(match) <= MAX_MATCHERS
        and {"job", "instance"} <= set(match)
    )
    require(all(label_name(k) and label_value(v) and v for k, v in match.items()))
    require(not RESERVED.intersection(match))
    return deepcopy(document)


def query_parameters(metric, max_age, timeout, max_resources, kind):
    spec = selection(metric, kind)
    require(type(max_age) is int and 1 <= max_age <= 86400)
    require(type(timeout) is int and 1 <= timeout <= 60)
    require(type(max_resources) is int and 1 <= max_resources <= 10000)
    prefix = "covenant_" + kind + "_observation_"
    names = ["up"] + [prefix + key for key in SUFFIXES]
    match = ",".join(
        k + "=" + json.dumps(v, ensure_ascii=False)
        for k, v in sorted(spec["match"].items())
    )
    selector = "{__name__=~" + json.dumps("|".join(names)) + "," + match + "}"
    # Instant vectors avoid historical label generations/cardinality. Their API
    # timestamp is evaluation time, so obtain real sample clocks in the SAME query.
    values = (
        "label_replace(" + selector + ',"' + METRIC_LABEL + '","$1","__name__","(.+)")'
    )
    # timestamp MUST consume the raw selector: label_replace first would reset
    # its internal sample time to evaluation time. timestamp drops __name__, so
    # give each fixed family its name only AFTER extracting its sample clock.
    clocks = []
    for name in names:
        raw = "{__name__=" + json.dumps(name) + "," + match + "}"
        named = (
            "label_replace(timestamp("
            + raw
            + '),"'
            + METRIC_LABEL
            + '",'
            + json.dumps(name)
            + ',"","")'
        )
        clocks.append("label_replace(" + named + ',"' + CLOCK_LABEL + '","1","","")')
    result = dict(
        query=" or ".join([values, *clocks]),
        timeout=str(timeout) + "s",
        limit=MAX_VECTOR + 1,
    )
    require(len(urlencode(result).encode()) <= MAX_QUERY_BYTES)
    return result


def vector_rows(payload, kind, match):
    require(type(payload) is dict and payload.get("status") == "success")
    require(not payload.get("warnings") and not payload.get("infos"))
    data = payload.get("data")
    require(type(data) is dict and data.get("resultType") == "vector")
    rows = data.get("result")
    require(type(rows) is list and len(rows) <= MAX_VECTOR)
    prefix = "covenant_" + kind + "_observation_"
    names = {"up": "scrape_up", **{prefix + key: key for key in SUFFIXES}}
    pairs = {}
    evaluation = None
    for row in rows:
        require(type(row) is dict and set(row) == {"metric", "value"})
        labels, sample = row["metric"], row["value"]
        require(type(labels) is dict and len(labels) <= 32)
        require(
            all(
                type(k) is str
                and LABEL_NAME.fullmatch(k)
                and type(v) is str
                and len(v) <= MAX_TEXT
                and not any(ord(c) < 32 or ord(c) == 127 for c in v)
                for k, v in labels.items()
            )
        )
        require(all(labels.get(k) == v for k, v in match.items()))
        name = labels.get(METRIC_LABEL)
        require(name in names)
        is_clock = CLOCK_LABEL in labels
        require(not is_clock or labels[CLOCK_LABEL] == "1")
        require(
            labels.get("__name__") in (None, name)
            if is_clock
            else labels.get("__name__") == name
        )
        require(type(sample) is list and len(sample) == 2)
        stamp, raw = sample
        number(stamp)
        require(evaluation is None or evaluation == stamp)
        evaluation = stamp
        require(type(raw) is str and len(raw) <= 64 and SAMPLE_VALUE.fullmatch(raw))
        value = float(raw)
        require(math.isfinite(value))
        clean = {
            k: v
            for k, v in labels.items()
            if k not in ("__name__", CLOCK_LABEL, METRIC_LABEL)
        }
        identity = (name, tuple(sorted(clean.items())))
        pair = pairs.setdefault(identity, {})
        require(is_clock not in pair)
        pair[is_clock] = value
    grouped = {key: [] for key in names.values()}
    for (name, labels), pair in pairs.items():
        require(set(pair) == {False, True})
        number(pair[True])
        grouped[names[name]].append((pair[True], pair[False], dict(labels)))
    return grouped


def parse_observation(
    payload,
    *,
    kind,
    metric,
    source_id,
    received_at,
    monotonic,
    max_age,
    max_resources,
    max_clock_skew,
):
    settings(source_id, received_at, monotonic, max_age, max_resources)
    number(max_clock_skew)
    spec = selection(metric, kind)
    grouped = vector_rows(payload, kind, spec["match"])
    source_kind = "prometheus_" + kind

    def unavailable():
        return Snapshot(
            source_id,
            source_kind,
            received_at,
            monotonic,
            None,
            max_age,
            Freshness.UNKNOWN,
            False,
            (),
        )

    guards = (
        "scrape_up",
        "up",
        "manifest",
        "collected_at_seconds",
        "collection_elapsed_seconds",
    )
    if any(len(grouped[key]) != 1 for key in guards):
        return unavailable()
    anchor, up, _ = grouped["scrape_up"][0]
    if up != 1 or grouped["up"][0][1] != 1:
        return unavailable()
    if any(stamp != anchor for rows in grouped.values() for stamp, _, _ in rows):
        return unavailable()
    collected, elapsed = (
        grouped[key][0][1]
        for key in ("collected_at_seconds", "collection_elapsed_seconds")
    )
    if (
        anchor > received_at + max_clock_skew
        or received_at - anchor > max_age
        or collected > received_at + max_clock_skew
        or collected < anchor - max_clock_skew
        or elapsed < 0
    ):
        return unavailable()
    for key in (
        "scrape_up",
        "up",
        "collected_at_seconds",
        "collection_elapsed_seconds",
    ):
        require(set(grouped[key][0][2]) == set(spec["match"]))
    _, count, manifest = grouped["manifest"][0]
    require(set(manifest) == set(spec["match"]) | {"sha256", "version"})
    require(manifest["version"] == "1" and count == len(grouped["value"]))
    records = []
    for _, value, labels in grouped["value"]:
        require(set(labels) - {"text"} == set(spec["match"]) | {"path", "type"})
        records.append((labels["path"], labels["type"], labels.get("text", ""), value))
    try:
        document = decode(records, kind, manifest["sha256"])
    except InvalidObservation:
        raise InvalidPayload() from None
    arguments = dict(
        source_id=source_id,
        received_at=received_at,
        monotonic=monotonic,
        max_age=max_age,
        max_resources=max_resources,
    )
    if kind == "monit":
        # Source age already includes time since the Monit cycle. Add source HTTP
        # duration and subsequent retention, never reset age on a fresh scrape.
        snapshot = parse_monit(
            document,
            transport_elapsed=elapsed + max(0, received_at - collected),
            **arguments,
        )
    else:
        snapshot = parse_certlord(document, max_clock_skew=max_clock_skew, **arguments)
    return replace(snapshot, source_kind=source_kind)
