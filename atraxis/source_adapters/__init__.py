"""Explicit built-in source registry; configuration never imports arbitrary code."""
from types import MappingProxyType
from typing import Mapping

from atraxis.parsing import InvalidPayload
from atraxis.source_adapters import certlord, monit_docker, prometheus, prometheus_monit, prometheus_certlord
from atraxis.source_adapters.contracts import DecodeContext, SourceAdapter

ADAPTERS: Mapping[str, SourceAdapter] = MappingProxyType({
    adapter.KIND: adapter for adapter in (monit_docker, certlord, prometheus, prometheus_monit, prometheus_certlord)
})
SOURCE_KINDS = tuple(ADAPTERS)
SOURCE_ROUTES = MappingProxyType({kind: adapter.ROUTE for kind, adapter in ADAPTERS.items()})


def get_adapter(kind):
    try:
        return ADAPTERS[kind]
    except (KeyError, TypeError):
        raise InvalidPayload() from None
