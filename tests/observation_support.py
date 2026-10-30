"""Synthetic full native records and Prometheus's paired instant-vector shape."""

from copy import deepcopy
from dataclasses import replace
from atraxis.observation_codec import encode
from atraxis.prometheus_observation import parse_observation, METRIC_LABEL, CLOCK_LABEL
from atraxis.parsing import parse_monit, parse_certlord
from test_observations import NOW, COMMON, iso, monit, certlord

MATCH = dict(job="fixture", instance="exporter:9118")


def spec(kind):
    return dict(
        match=MATCH,
        **({"prefix": "covenant_monit_observation_"} if kind == "monit" else {}),
    )


def native(kind):
    raw = monit() if kind == "monit" else certlord()
    if kind == "monit":
        raw.update(
            last_error_code=0,
            actions=dict(
                executed=5,
                cooldown=0,
                pending=2,
                **{"restart-limit": 3, "maintenance": 4, "dry-run": 1},
            ),
        )
        item = raw["containers"][0]
        item.update(
            name="fixture",
            health="healthy",
            mem_usage=2**54 + 1,
            mem_limit=2**55 + 3,
            mem_percent=92.5,
            io_read=17,
            io_write=18,
            net_rx=19,
            net_tx=20,
            pids_percent=42.5,
            maintenance_until=NOW + 10,
            pid=123,
            oom_events=0,
            starts_recent=2,
            pids_current=5,
            pids_limit=10,
            event_window_seconds=60,
            event_window_end=int(NOW),
            restart_attempts=2,
            restart_limit=5,
            event_history_complete=1,
            manual_actions_protected=True,
            maintenance_active=False,
            filesystems=[
                dict(
                    group="data",
                    path="/data",
                    disk_usage=120,
                    disk_available=80,
                    disk_total=200,
                    disk_percent=60,
                    inode_usage=5,
                    inode_available=5,
                    inode_total=10,
                    inode_percent=50,
                    fs_mode="rw",
                    fs_readable=1,
                    fs_writable=0,
                    fs_executable=1,
                )
            ],
        )
    else:
        raw["certificates"][0].update(
            domains=["example.test", "*.example.test", "tést.example"],
            version=2**54 + 1,
            origin="acme",
            renewal_owner="acme",
            pending_action="renew",
            material_state="readable",
            not_before=iso(-86400),
            not_after=iso(86400),
            not_after_timestamp_seconds=NOW + 86400,
            fingerprint_sha256="a" * 64,
            issuance_retry_count=0,
            last_issuance_retry_at=iso(-15),
            verification=dict(
                status="verified",
                checked_at=iso(-12),
                error_code=None,
                expected_fingerprint_sha256="a" * 64,
                observed_fingerprint_sha256="a" * 64,
                target_sha256="b" * 64,
            ),
        )
    return raw


def vector(kind, raw=None, stamp=NOW, collected=NOW, elapsed=0):
    fields, sha = encode(native(kind) if raw is None else raw, kind)
    prefix = "covenant_" + kind + "_observation_"
    metrics = [
        ("up", {}, 1),
        (prefix + "up", {}, 1),
        (prefix + "manifest", dict(sha256=sha, version="1"), len(fields)),
        (prefix + "collected_at_seconds", {}, collected),
        (prefix + "collection_elapsed_seconds", {}, elapsed),
    ]
    metrics.extend(
        (prefix + "value", dict(path=p, type=t, text=s), v) for p, t, s, v in fields
    )
    rows = []
    for name, labels, value in metrics:
        labels = {
            k: v
            for k, v in dict(labels, **MATCH, **{METRIC_LABEL: name}).items()
            if v != ""
        }
        rows.append(dict(metric=dict(labels, __name__=name), value=[NOW, str(value)]))
        rows.append(
            dict(metric=dict(labels, **{CLOCK_LABEL: "1"}), value=[NOW, str(stamp)])
        )
    return dict(status="success", data=dict(resultType="vector", result=rows))


def parse(kind, raw=None, response=None, **changes):
    args = dict(COMMON, max_clock_skew=2, metric=spec(kind), **changes)
    return parse_observation(
        vector(kind, raw) if response is None else response, kind=kind, **args
    )


def direct(kind, raw, elapsed=0, **changes):
    args = dict(COMMON, **changes)
    return (
        parse_monit(raw, transport_elapsed=elapsed, **args)
        if kind == "monit"
        else parse_certlord(raw, max_clock_skew=2, **args)
    )


def row(payload, suffix, path=None, clock=False):
    return next(
        r
        for r in payload["data"]["result"]
        if r["metric"][METRIC_LABEL].endswith(suffix)
        and (path is None or r["metric"].get("path") == path)
        and (CLOCK_LABEL in r["metric"]) == clock
    )
