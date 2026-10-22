# Read-only core prototype

Python 3.11 or 3.12. Pure models/parsers use the standard library; the application runtime
also uses DWho, HTTPdis and Sonicprobe.

`atraxis.parsing` projects already-decoded Python dictionaries into immutable
resources and snapshots. Nested filesystem/TLS objects use immutable `Facts`;
serialization creates independent dictionaries/lists. It performs no network, disk or subprocess operations.
Callers must supply source identity, receive time, monotonic time, freshness and
resource limits explicitly; there are no unvalidated operational defaults.

`parse_monit` reads API version 1 and projects container identity, name, state,
health, memory/CPU/I/O/network and PID/event-window measurements, sampled maintenance
and restart/manual-protection facts, and filesystem capacity/access observations. `parse_certlord` reads
observation schema 1 and projects certificate identity, domains, lifecycle,
material validity, fingerprint, retry count/date and allowlisted historical TLS
verification. Source cycle counters and timestamps are retained as snapshot metadata
and as metadata of the latest parsed attempt, even when it is not authoritative.
Additional upstream fields, manual request/actor history, PEM and private keys are
ignored. This covers the first read-only adapter projection, not every API of either tool.

Required malformed fields, duplicate resource identities or excessive record
counts reject the whole input with `InvalidPayload('invalid_payload')`. Missing
optional numerical measurements remain null. Absent optional policy flags and
filesystem/TLS objects also remain null, rather than implying false or empty success.
`max_resources` bounds both the resource count and, separately, the total filesystem
observations across the source. Duplicate filesystem group/path pairs are rejected. Resource identity includes source
identity; a source label or endpoint must not be reused for another installation.

Freshness is distinct from workload health: an expired certificate can be a fresh
observation. monit-docker's source age includes the whole measured request
duration conservatively, then advances using elapsed local monotonic time. CertLord uses source collection and record timestamps; future timestamps
beyond the explicit skew budget become unknown. A source clock behind Atraxis can
look stale; true clock accuracy cannot be established from the payload alone.

`SnapshotStore` is an in-memory, single-owner prototype. `view` returns
`(last_authoritative_snapshot, is_current)`. Consumers must use `is_current`:
historical data is not a current successful poll. On errors call `fail(source_id)`;
after valid parsing call `accept(snapshot)`. A fresh, authoritative empty snapshot
replaces previous inventory. Nothing is persisted across process restart. The
scheduler serializes collections per source and ReadService protects store access
with a lock. The store itself is not a concurrent or out-of-order event store.
Transport outcome metadata belongs to the application layer.

Validation:

```sh
python scripts/check-test-collection.py --runner unittest tests
python -m unittest discover -s tests -v
```

The collection guard is reused from monit-docker. CI runs the same checks on
Python 3.11 and 3.12. The [daemon foundation](docs-runtime.md) now supplies YAML, explicit serve/check-config commands and authenticated health/status routes. The [collection layer](docs/collection-inventory.md) now connects bounded HTTP transport,
source credentials, scheduling and authenticated inventory. The API-backed [CLI](docs/cli.md) is implemented. Durable storage, TUI, live acceptance
and deployment remain absent. The transport
enforces response byte/time limits before the domain parser; the record limit here
is not a byte limit.
