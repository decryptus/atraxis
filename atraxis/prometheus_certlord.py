"""CertLord's existing Covenant metric contract, with explicit unavailable facts."""

from copy import deepcopy
from datetime import datetime, timezone
import json
from urllib.parse import urlencode
from uuid import UUID

from atraxis.models import Freshness, Resource, Snapshot
from atraxis.parsing import InvalidPayload, number, require, settings, timestamp, choice
from atraxis.prometheus import MAX_QUERY_BYTES, label_name, label_value
from atraxis.prometheus_bundle import matrix_series

KIND = "prometheus_certlord"
PROJECTION = "certlord_metrics"
PREFIX = "certlord_"
UPSTREAM = "covenant_certlord_source_up"
COMPLETED = "observation_completed_timestamp_seconds"
COUNT = "certificates_observed"
INFO = "certificate_info"
EXPIRY = "certificate_not_after_timestamp_seconds"
RETRY = "certificate_issuance_retry_count"
TLS = "certificate_tls_verification"
VERIFIED = "certificate_tls_verified_at_timestamp_seconds"
SUFFIXES = (COMPLETED, COUNT, INFO, EXPIRY, RETRY, TLS, VERIFIED)
NAMES = {"up": "up", UPSTREAM: "source_up", **{PREFIX + k: k for k in SUFFIXES}}
ORIGINS = frozenset(("external", "acme", "provider-managed", "unknown"))
STATES = frozenset(
    (
        "create",
        "exists",
        "invalid-dns",
        "delete",
        "renew",
        "processing",
        "generated",
        "deployed",
        "unknown",
    )
)
MATERIAL = frozenset(("readable", "missing", "invalid"))
TLS_STATES = frozenset(("verified", "failed", "not_checked"))
INFO_LABELS = ("origin", "renewal_owner", "status", "pending_action", "material_state")
RESOURCE_LABELS = frozenset(("certificate_id", *INFO_LABELS))
UNKNOWN_FIELDS = frozenset(
    (
        "domains",
        "version",
        "not_before",
        "fingerprint_sha256",
        "last_issuance_retry_at",
        "verification",
        "observed_at",
    )
)
FIELDS = UNKNOWN_FIELDS | frozenset(
    (
        "projection",
        *INFO_LABELS,
        "not_after",
        "not_after_timestamp_seconds",
        "issuance_retry_count",
        "tls_verification_status",
        "tls_verified_at",
        "collection_completed_at",
    )
)
MAX_SERIES = 10000
MAX_MATCHERS = 12


def selection(document):
    require(type(document) is dict and set(document) == {"match"})
    match = document["match"]
    require(
        type(match) is dict
        and 2 <= len(match) <= MAX_MATCHERS
        and {"job", "instance"} <= set(match)
    )
    require(all(label_name(k) and label_value(v) and v for k, v in match.items()))
    require(not RESOURCE_LABELS & set(match))
    return deepcopy(document)


def series_limit(max_resources):
    return min(MAX_SERIES, 10 * max_resources + 4)


def query_parameters(metric, max_age, timeout, max_resources):
    spec = selection(metric)
    require(type(max_age) is int and 1 <= max_age <= 86400)
    require(type(timeout) is int and 1 <= timeout <= 60)
    require(type(max_resources) is int and 1 <= max_resources <= 10000)
    match = ",".join(
        k + "=" + json.dumps(v, ensure_ascii=False)
        for k, v in sorted(spec["match"].items())
    )
    query = (
        "{__name__=~"
        + json.dumps("|".join(NAMES))
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


def iso(value):
    number(value)
    try:
        return datetime.fromtimestamp(value, timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        raise InvalidPayload() from None


def identity(value):
    require(type(value) is str and len(value) == 36)
    try:
        require(str(UUID(value)) == value)
    except ValueError:
        raise InvalidPayload() from None
    return value


def validate_fields(fields, native_id):
    """Also used by API/search clients; unknown domain data cannot be fabricated."""
    identity(native_id)
    require(
        type(fields) is dict
        and set(fields) == FIELDS
        and fields["projection"] == PROJECTION
    )
    require(all(fields[k] is None for k in UNKNOWN_FIELDS))
    choice(fields["origin"], ORIGINS)
    choice(fields["renewal_owner"], ORIGINS)
    choice(fields["status"], STATES)
    choice(fields["pending_action"], STATES | {"none"})
    choice(fields["material_state"], MATERIAL)
    timestamp(fields["collection_completed_at"])
    if fields["material_state"] == "readable":
        require(
            abs(
                timestamp(fields["not_after"])
                - number(fields["not_after_timestamp_seconds"])
            )
            <= 0.001
        )
    else:
        require(
            fields["not_after"] is None
            and fields["not_after_timestamp_seconds"] is None
        )
    retry = fields["issuance_retry_count"]
    needs_retry = fields["renewal_owner"] == "acme" and fields["pending_action"] in (
        "create",
        "renew",
    )
    require(type(retry) is int and retry >= 0 if needs_retry else retry is None)
    choice(fields["tls_verification_status"], TLS_STATES)
    if fields["tls_verification_status"] == "verified":
        timestamp(fields["tls_verified_at"])
    else:
        require(fields["tls_verified_at"] is None)
    return deepcopy(fields)


def parse_prometheus_certlord(
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
    grouped = matrix_series(payload, NAMES, spec["match"], series_limit(max_resources))

    def unavailable(age=None, freshness=Freshness.UNKNOWN):
        return Snapshot(
            source_id, KIND, received_at, monotonic, age, max_age, freshness, False, ()
        )

    guards = ("up", "source_up", COMPLETED, COUNT)
    if any(len(grouped[k]) != 1 for k in guards):
        return unavailable()
    anchor, up, _ = grouped["up"][0]
    _, source_up, _ = grouped["source_up"][0]
    _, completed, _ = grouped[COMPLETED][0]
    _, count, _ = grouped[COUNT][0]
    if (
        up != 1
        or source_up != 1
        or any(grouped[k][0][0] != anchor for k in guards)
        or completed <= 0
        or anchor > received_at + max_clock_skew
        or completed > min(anchor, received_at) + max_clock_skew
    ):
        return unavailable()
    age = max(0, received_at - min(anchor, completed))
    if age > max_age:
        return unavailable(age, Freshness.STALE)
    require(count >= 0 and count.is_integer() and count <= max_resources)
    require(all(stamp <= anchor for rows in grouped.values() for stamp, _, _ in rows))
    current = {}
    for suffix in (INFO, EXPIRY, RETRY, TLS, VERIFIED):
        rows = {}
        for stamp, value, labels in grouped[suffix]:
            if stamp != anchor:
                continue
            uid = identity(labels.get("certificate_id"))
            require(uid not in rows)
            rows[uid] = (value, labels)
        current[suffix] = rows
    infos = current[INFO]
    require(len(infos) == int(count))
    require(all(set(rows) <= set(infos) for rows in current.values()))
    readable = set()
    retry_ids = set()
    verified_ids = set()
    resources = []
    require(set(current[TLS]) == set(infos))
    for uid, (value, labels) in sorted(infos.items()):
        require(value == 1 and all(k in labels for k in INFO_LABELS))
        fields = {k: None for k in UNKNOWN_FIELDS}
        fields.update({k: labels[k] for k in INFO_LABELS})
        fields.update(
            projection=PROJECTION,
            not_after=None,
            not_after_timestamp_seconds=None,
            issuance_retry_count=None,
            tls_verified_at=None,
            collection_completed_at=iso(completed),
        )
        if fields["material_state"] == "readable":
            readable.add(uid)
            require(uid in current[EXPIRY])
            expiry = current[EXPIRY][uid][0]
            fields.update(not_after=iso(expiry), not_after_timestamp_seconds=expiry)
        if fields["renewal_owner"] == "acme" and fields["pending_action"] in (
            "create",
            "renew",
        ):
            retry_ids.add(uid)
            require(uid in current[RETRY])
            retry = current[RETRY][uid][0]
            require(retry >= 0 and retry.is_integer())
            fields["issuance_retry_count"] = int(retry)
        tls, tls_labels = current[TLS][uid]
        require(tls == 1 and tls_labels.get("status") in TLS_STATES)
        fields["tls_verification_status"] = tls_labels["status"]
        if tls_labels["status"] == "verified":
            verified_ids.add(uid)
            require(uid in current[VERIFIED])
            verified = current[VERIFIED][uid][0]
            require(verified <= min(completed, received_at) + max_clock_skew)
            fields["tls_verified_at"] = iso(verified)
        validate_fields(fields, uid)
        resources.append(Resource(source_id, "certificate", uid, tuple(fields.items())))
    require(
        set(current[EXPIRY]) == readable
        and set(current[RETRY]) == retry_ids
        and set(current[VERIFIED]) == verified_ids
    )
    return Snapshot(
        source_id,
        KIND,
        received_at,
        monotonic,
        age,
        max_age,
        Freshness.FRESH,
        True,
        tuple(resources),
        (("completed_timestamp_seconds", completed),),
    )
