"""Transport-independent observations and conservative snapshot retention."""
from dataclasses import dataclass
from enum import Enum
from math import isfinite


class Freshness(str, Enum):
    FRESH = 'fresh'
    STALE = 'stale'
    UNKNOWN = 'unknown'


@dataclass(frozen=True)
class Facts:
    """Allowlisted nested object, immutable like the containing snapshot."""
    fields: tuple


def public_value(value):
    if isinstance(value, Facts):
        return {key: public_value(item) for key, item in value.fields}
    if isinstance(value, tuple):
        return [public_value(item) for item in value]
    return value


def public_fields(fields):
    return public_value(Facts(fields))


@dataclass(frozen=True)
class Resource:
    source_id: str
    kind: str
    native_id: str
    fields: tuple

    @property
    def key(self):
        return self.source_id, self.kind, self.native_id


@dataclass(frozen=True)
class Snapshot:
    source_id: str
    source_kind: str
    received_at: float
    received_monotonic: float
    age_at_receipt: float | None
    max_age: float
    freshness: Freshness
    authoritative: bool
    resources: tuple[Resource, ...]
    metadata: tuple = ()

    def freshness_at(self, now_monotonic):
        if not isfinite(now_monotonic) or now_monotonic < self.received_monotonic:
            return Freshness.UNKNOWN
        if self.freshness != Freshness.FRESH:
            return self.freshness
        age = self.age_at_receipt + now_monotonic - self.received_monotonic
        return Freshness.FRESH if age <= self.max_age else Freshness.STALE


class SnapshotStore:
    """In-memory, single-owner store; previous data never masquerades as current."""

    def __init__(self):
        self._latest = {}
        self._previous = {}

    def accept(self, snapshot):
        self._latest[snapshot.source_id] = snapshot
        if snapshot.authoritative and snapshot.freshness == Freshness.FRESH:
            self._previous[snapshot.source_id] = snapshot

    def fail(self, source_id):
        self._latest[source_id] = None

    def view(self, source_id, now_monotonic):
        latest = self._latest.get(source_id)
        previous = self._previous.get(source_id)
        current = (latest is not None and latest is previous
                   and latest.freshness_at(now_monotonic) == Freshness.FRESH)
        return previous, bool(current)
