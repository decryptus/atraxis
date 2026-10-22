"""Internal fixed-route read request; not a configurable source adapter kind."""
from dataclasses import dataclass, field

from atraxis.source_config import SOURCE_ROUTES


INVENTORY_KIND = 'atraxis_inventory'
SEARCH_KIND = 'atraxis_inventory_search'
READ_ROUTES = dict(SOURCE_ROUTES, **{INVENTORY_KIND: '/v1/status', SEARCH_KIND: '/v1/inventory/search'})


@dataclass(frozen=True)
class ReadRequest:
    source_id: str
    kind: str
    origin: str
    credentials_file: str = field(repr=False)
    ca_file: str | None = field(repr=False)
    timeout: int
    max_response_bytes: int
    organization: str | None = None
    search_query: dict | None = None
