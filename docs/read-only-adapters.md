# Read-only source adapter contract

Read-only adapter design. The current prototype connects bounded HTTP collection and authenticated inventory; the first inventory projection and API-backed CLI are implemented (see core limits and collection documentation). Scope: one organization, multiple configured sources; no business mutations, job submission or automatic discovery of network targets.

## Evidence reviewed

- monit-docker tree `2e1908b3da8cf352fcbe2346f575651f0fcff6ed`: [HTTP contract](https://github.com/decryptus/monit-docker/blob/master/docs/http-api-contract.md), `monit_docker/adapters/http_routes.py`.
- CertLord tree `0a4dfe34c89c0b818ec7308c3d517282fd9d907f`: [supervision contract](https://github.com/decryptus/certlord/blob/main/docs/supervision.md), [identity and client contract](https://github.com/decryptus/certlord/blob/main/docs/certificate-identity.md), `modules/route_auth.py`, `modules/ssl_certs.py`, `services/api_access.py`, `services/certificates.py`, `services/observations.py`.
- These are repository reviews, not live deployment acceptance. Route configuration and installed versions must be checked when configuring a real source.

Additional source review on 4 October 2026: monit-docker `64b7a337c04d2565a147aa50afbd095950873d64` (`docs/http-api-contract.md`, `monit_docker/service.py`) and CertLord `91dc3f7089de5b8e7ae33abd12205e1e42313fc3` (`certlord/services/observations.py`, `certlord/services/verification.py`).

## Existing interfaces

| Adapter | Single primary request | Authorization | Important semantics |
| --- | --- | --- | --- |
| monit-docker | GET /v1/status | No native credential for this read route; remote integration needs a protected HTTPS ingress | Cached snapshot; HTTP 200 can mean not ready; containers are withheld when not ready |
| CertLord | GET /api/certificates/observations | HTTPdis Basic identity with read permission; no write/deploy rights | Fresh synchronous Vault/Redis reads; backend failure fails the request; empty successful collection is valid |

No need to change either public project for the initial data adapter. The remote monit-docker authentication boundary must be provisioned separately; an action token does not secure GET /v1/status. Do not assume the public demo proxy forwards every agent route. CertLord observation routes must be present in the installed route configuration. Do not read Vault or Redis directly from Atraxis.

## Neutral result model

The built-in adapters live in `atraxis/source_adapters/`, with separate modules
for Monit-Docker, CertLord and Prometheus. Each implements the same `SourceAdapter`
contract:

- `configure(document, *, max_age, timeout, max_resources)` validates source-specific
  selection options offline.
- `read_route(document)` returns a fixed read route and, where needed, bounded
  query parameters. The shared transport remains GET-only and owns explicit
  credentials, TLS, deadlines, cancellation and response-size limits.
- `decode(payload, DecodeContext) -> Snapshot` reuses the pure source parsers and
  returns the existing immutable domain projection. The context carries identity,
  receipt times, freshness/resource limits and selection, without credentials or URLs.

The immutable built-in registry supplies the configured kinds and routes. Both
configuration and collection use it; configuration cannot load arbitrary Python
modules. The scheduler and `ReadService` do not select source-specific parsers.
`TransportResult` still represents the request outcome; `Snapshot` represents the
validated observation. No new public API or configuration format is introduced.

Monit-Docker and CertLord remain supported optional adapters, not temporary migration
paths. Multiple Prometheus instances can be configured with distinct `source_id`
values and their own origins, credentials and selections. Covenant metrics already
stored in Prometheus use the Prometheus adapter; a dedicated Covenant adapter is
not needed for that path. The modules ship inside Atraxis rather than as separately
versioned packages for this first extraction.

This is collection abstraction, not resource equivalence: Prometheus still projects
`metric` resources, Monit-Docker `container` resources and CertLord `certificate`
resources. Live rule observations still require the existing Monit-Docker container
contract. Switching that path to Prometheus requires an explicit mapping and parity
validation of identity, units, missing values and freshness; changing `kind` alone
is not supported. Action admission and execution are unchanged.

A transport/observation result contains:
- source_id: stable configured identity for one installation, independent of its mutable URL or display name.
- source_kind: monit_docker, certlord or prometheus.
- attempted_at and received_at: UTC timestamps measured by Atraxis.
- outcome: success, timeout, transport_error, authentication_error, forbidden, endpoint_unavailable, source_error, invalid_payload or response_too_large.
- freshness: fresh, stale or unknown; separate from transport outcome and resource health.
- source timestamps and monotonic receipt time for local aging.
- inventory_authoritative: whether this collection is a complete usable inventory.
- resources: typed domain projections from an explicit allowlist.
- safe error_code: no response-body dump, credentials or raw exception text.

Resource identity is the tuple (source_id, resource_kind, native_id). Use the full Docker ID or CertLord UUID, never a container name, domain name, short prefix or hash of a mutable endpoint. Atraxis must not invent replacement certificate UUIDs. Cross-source copies remain distinct. Prometheus resource IDs derive from the selected metric and identity labels; source identity remains part of the full resource key.

The application retains a last valid snapshot separately from the latest attempt. A failed or non-authoritative attempt cannot delete that snapshot or silently serve it as fresh. Any retained resources must show their age and previous-observation status. The implemented cache is in memory and is lost on restart; it is not a durable history.

## monit-docker projection

Validate api_version == 1 and documented types; ignore additional fields. Use ready, last_cycle_success, last_success_at, last_cycle_finished_at, age_seconds, cycles_total and errors_total without interpreting HTTP 200 as health.

- ready == false: inventory is not authoritative, including containers == []; do not infer removals.
- ready == true: a validated empty list can be authoritative for that agent's configured selection, not all containers on the host.
- running == true can coexist with ready == true; it does not prove progress.
- Use the agent's monotonic age_seconds plus elapsed local monotonic time since receipt for aging; enforce Atraxis's configured freshness budget as well as ready. Missing/inconsistent freshness evidence becomes unknown/invalid, never fresh.
- Resource fields include full id, name, Docker status, health and available numerical measurements. Null means unknown/unrequested, never zero. CPU may exceed 100%.
- Maintenance and protection fields are sampled metadata. manual_actions_protected protects manual actions only.
- Counters can reset after process restart; names can be reused by replacement containers.
- Exclude manual request history and actor metadata from the initial projection; no action or audit credentials are needed.

## CertLord projection

Validate schema_version == 1, collection start/completion times, completed_timestamp_seconds and records. Preserve per-record observed_at, certificate_id, domains, version, lifecycle status, origin, renewal_owner, pending_action, material_state, leaf validity, fingerprint and optional verification.

- Collection and per-record timestamps are source-clock observations. Require timezone-aware finite values, sensible ordering and bounded clock skew; otherwise freshness is unknown and inventory is not authoritative.
- Evaluate freshness per record as well as per collection: a long collection can finish recently while its earliest observations are old.
- The full inventory is synchronous, not paginated and not an atomic transaction across Vault and Redis.
- Reject duplicate native identities or malformed required records as a failed collection; do not silently return a successful partial inventory.
- Successful zero records is authoritative; a backend error is not.
- Missing/invalid material retains null validity/fingerprint fields.
- Stored certificate readability does not establish trust, current validity or what a destination serves. Verification is the last recorded deployment check, not continuous health.
- Retry count is current pending-work state, not a cumulative failure counter.
- Store no PEM, private key or Vault credential. API identity is separate from Vault identity.

## Transport requirements

Configured destinations only. HTTPS with certificate and hostname validation remotely; plain HTTP allowed only for explicitly configured literal loopback fixtures. No URL userinfo, fragments, arbitrary per-request URLs, redirects, ambient proxies or netrc credentials. Bind credentials to a source; never forward them to a redirect destination.

Allow only the fixed GET routes above in the initial adapters. Keep credential files outside inventory output, audit and logs. Basic authentication may be used for CertLord and an explicitly configured monit-docker ingress; do not pretend the latter is native agent authentication.

Bound source count, concurrency, connect/read timeouts, overall collection duration, decompressed response bytes and resource count. Make limits explicit in configuration and documentation; values require fixture/load validation before becoming defaults. Oversized/slow responses fail with an explicit outcome; never truncate into a successful collection. Avoid overlapping polls of one source; cancellation and failed polls must not block other sources indefinitely.

An authenticated Atraxis read interface is a separate boundary from credentials used to read sources. This milestone must not imply multi-organization isolation has already been implemented.

## Required implementation checks

Use unittest and verify discovery before execution:
- Two sources with identical native IDs remain distinct.
- monit-docker 200/not-ready does not remove previous inventory; ready/empty is distinct.
- A replacement container receives a new resource identity.
- CertLord empty success differs from 503; malformed records/duplicates fail the collection.
- Null values, CPU above 100%, source clock skew and stale per-record timestamps.
- Wrong credentials, forbidden access, TLS failure, redirect refusal, timeout, payload limit and recovery.
- No request to any mutation endpoint; no secret in result/error output.
- Slow or failed source does not prevent another source from updating.

## Remaining work

The first projection, bounded transport, polling, authenticated inventory and explicit CLI reads are implemented and covered by synthetic tests. The milestone review and synthetic Docker runtime acceptance are recorded in [validation guide](contributing.md). Live-source acceptance requires explicit authorization and must record deployed versions and ingress permissions. Select durable storage and additional UI only after the contract is exercised. No live credentials, deployment changes or container actions were used for this work.
