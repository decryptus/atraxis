"""Coherent, source-timed Monit-Docker observations from a bounded metric bundle."""

from copy import deepcopy
from dataclasses import replace
import json
from urllib.parse import urlencode

from atraxis.models import Freshness, Snapshot
from atraxis.prometheus_bundle import matrix_series
from atraxis.parsing import number, parse_monit, require, settings
from atraxis.prometheus import (
    MAX_QUERY_BYTES,
    METRIC_NAME,
    label_name,
    label_value,
)

KIND = "prometheus_monit"
GUARDS = ("source_ready", "observed_at_seconds", "containers")
SCALARS = {
    "cpu_percent": "cpu_percent",
    "memory_percent": "mem_percent",
    "maintenance": "maintenance_active",
    "health_status": "health",
    "restart_attempts": "restart_attempts",
}
FILESYSTEMS = {
    "disk_percent": "disk_percent",
    "inode_percent": "inode_percent",
    "readable": "fs_readable",
    "writable": "fs_writable",
}
SUFFIXES = (*GUARDS, "container_info", *SCALARS, *FILESYSTEMS)
MAX_PREFIX = 80
MAX_MATCHERS = 12
MAX_SERIES = 10000


def selection(document):
    require(type(document) is dict and set(document) == {"prefix", "match"})
    prefix, match = document["prefix"], document["match"]
    require(
        type(prefix) is str
        and 1 <= len(prefix) <= MAX_PREFIX
        and METRIC_NAME.fullmatch(prefix) is not None
    )
    require(
        type(match) is dict
        and 2 <= len(match) <= MAX_MATCHERS
        and {"job", "instance"} <= set(match)
    )
    require(all(label_name(k) and label_value(v) and v for k, v in match.items()))
    require(not {"id", "name", "status", "health", "group", "path"} & set(match))
    return deepcopy(document)


def series_limit(max_resources):
    # Allow two generations of six container/four filesystem series, plus guards.
    return min(
        MAX_SERIES, 2 * max_resources * (1 + len(SCALARS) + len(FILESYSTEMS)) + 4
    )


def query_parameters(metric, max_age, timeout, max_resources):
    spec = selection(metric)
    require(type(max_age) is int and 1 <= max_age <= 86400)
    require(type(timeout) is int and 1 <= timeout <= 60)
    require(type(max_resources) is int and 1 <= max_resources <= 10000)
    names = ["up", *(spec["prefix"] + suffix for suffix in SUFFIXES)]
    match = ",".join(
        k + "=" + json.dumps(v, ensure_ascii=False)
        for k, v in sorted(spec["match"].items())
    )
    # One matrix query: no independent reads, arbitrary PromQL or interpolated regex.
    # Allowed metric-name characters have no regex operators. PromQL anchors =~.
    query = (
        "{__name__=~"
        + json.dumps("|".join(names))
        + ","
        + match
        + "}["
        + str(max_age)
        + "s]"
    )
    result = dict(
        query=query, timeout=str(timeout) + "s", limit=series_limit(max_resources) + 1
    )
    require(len(urlencode(result).encode()) <= MAX_QUERY_BYTES)
    return result


def _series(payload, spec, max_resources):
    names = {"up": "up", **{spec["prefix"] + suffix: suffix for suffix in SUFFIXES}}
    return matrix_series(payload, names, spec["match"], series_limit(max_resources))


def parse_prometheus_monit(
    payload,
    *,
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
    spec = selection(metric)
    grouped = _series(payload, spec, max_resources)

    def unavailable(age=None, freshness=Freshness.UNKNOWN):
        return Snapshot(
            source_id, KIND, received_at, monotonic, age, max_age, freshness, False, ()
        )

    # A latest up=0 invalidates retained exporter values immediately after the
    # failed scrape. Missing or duplicate guard series never establish readiness.
    if any(len(grouped[key]) != 1 for key in ("up", *GUARDS)):
        return unavailable()
    anchor, up, _ = grouped["up"][0]
    ready_stamp, ready, _ = grouped["source_ready"][0]
    observed_stamp, observed, _ = grouped["observed_at_seconds"][0]
    count_stamp, count, _ = grouped["containers"][0]
    if (
        up != 1
        or ready != 1
        or any(stamp != anchor for stamp in (ready_stamp, observed_stamp, count_stamp))
        or observed <= 0
        or anchor > received_at + max_clock_skew
        or observed > min(anchor, received_at) + max_clock_skew
    ):
        return unavailable()
    age = max(0, received_at - min(anchor, observed))
    if age > max_age:
        return unavailable(age, Freshness.STALE)
    require(count >= 0 and count.is_integer() and count <= max_resources)
    # Explicit timestamps or mixed scrape generations must never be combined.
    require(all(stamp <= anchor for rows in grouped.values() for stamp, _, _ in rows))
    active = {
        key: [(value, labels) for stamp, value, labels in rows if stamp == anchor]
        for key, rows in grouped.items()
    }
    containers = {}
    for value, labels in active["container_info"]:
        native_id = labels.get("id")
        require(value == 1 and type(native_id) is str and native_id not in containers)
        containers[native_id] = dict(
            id=native_id,
            name=labels.get("name"),
            status=labels.get("status"),
            health="unknown",
            filesystems=[],
        )
    require(len(containers) == int(count))
    seen, filesystems = set(), {}
    for suffix in (*SCALARS, *FILESYSTEMS):
        for value, labels in active[suffix]:
            native_id = labels.get("id")
            require(type(native_id) is str and native_id in containers)
            target = containers[native_id]
            if suffix in FILESYSTEMS:
                group, path = labels.get("group"), labels.get("path")
                require(type(group) is str and group and type(path) is str and path)
                key = (native_id, group, path)
                if key not in filesystems:
                    require(len(filesystems) < max_resources)
                    filesystems[key] = dict(group=group, path=path)
                    target["filesystems"].append(filesystems[key])
                target = filesystems[key]
                identity = (suffix, *key)
                field = FILESYSTEMS[suffix]
            else:
                identity = (suffix, native_id)
                field = SCALARS[suffix]
            require(identity not in seen)
            seen.add(identity)
            require(value >= 0)
            if suffix == "health_status":
                require(value == 1 and labels.get("health") in ("healthy", "unhealthy"))
                value = labels["health"]
            elif suffix in ("maintenance", "readable", "writable"):
                require(value in (0, 1))
                value = bool(value) if suffix == "maintenance" else int(value)
            elif suffix == "restart_attempts":
                require(value.is_integer())
                value = int(value)
            target[field] = value
    # Reuse the native container/FS validation, without inventing unknown facts.
    # The synthetic envelope only enters the pure parser; its counters are discarded.
    projected = parse_monit(
        dict(
            api_version=1,
            ready=True,
            running=True,
            last_cycle_success=True,
            cycles_total=0,
            errors_total=0,
            age_seconds=age,
            last_success_at=observed,
            last_cycle_finished_at=observed,
            containers=[containers[key] for key in sorted(containers)],
        ),
        source_id=source_id,
        received_at=received_at,
        monotonic=monotonic,
        max_age=max_age,
        max_resources=max_resources,
    )
    return replace(
        projected,
        source_kind=KIND,
        metadata=(("last_success_at", observed), ("age_seconds", age)),
    )
