"""Source decoding contract: bounded observations, without I/O or credentials."""
from dataclasses import dataclass
from typing import Protocol

from atraxis.models import Snapshot
from atraxis.parsing import require


@dataclass(frozen=True)
class DecodeContext:
    source_id: str
    received_at: float
    monotonic: float
    max_age: int
    max_resources: int
    max_clock_skew: int
    transport_elapsed: float
    metric: dict | None = None

    def arguments(self):
        return dict(source_id=self.source_id, received_at=self.received_at,
                    monotonic=self.monotonic, max_age=self.max_age,
                    max_resources=self.max_resources)


class SourceAdapter(Protocol):
    KIND: str
    ROUTE: str

    def configure(self, document: dict, *, max_age: int, timeout: int,
                  max_resources: int) -> dict | None: ...

    def read_route(self, document: dict) -> str: ...

    def decode(self, payload: dict, context: DecodeContext) -> Snapshot: ...


def inventory_options(document, **limits):
    require('metric' not in document)
    return None
