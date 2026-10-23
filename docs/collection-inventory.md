# Periodic collection and authenticated inventory

Experimental prototype. Atraxis reads configured monit-docker, CertLord and
Prometheus sources, keeps their last valid inventory in memory and exposes it
through `GET /v1/status`. Collection performs no business mutation, restart,
certificate change or job submission. Stopping Atraxis leaves local tools autonomous.

## Lifecycle and scheduling

`check-config` stays offline. `serve` initializes inbound authentication and binds
its listener before starting collection. Each source is initially due immediately;
at most four reads run concurrently across at most 32 configured sources. Waiting
sources are considered oldest-due first. There is no overlapping read for a source,
no unbounded executor queue and no catch-up burst. A source's next poll is due
`poll_interval` seconds after its previous result has been processed, whether that
result succeeded or failed. This is a minimum pause, not a fixed-rate promise;
collection time, admission contention and scheduler overhead increase the interval.
The scheduler wakes at most every 50 ms when idle. HTTP request workers and outbound
collection slots have separate limits; `general.max_workers` controls inbound HTTP.

A failed source does not discard or change another source's inventory. Four busy
sources can occupy all outbound slots until they finish or time out; admission
is bounded, not a per-source latency guarantee. The transport's total timeout and
cancellation include DNS and slow responses; see [transport](http-transport.md).
Unexpected adapter exceptions produce the fixed `collector_error` code and the
next attempt waits for the normal interval. An unexpected coordinator failure
stops collection, makes readiness false and emits a fixed safe log message.

SIGINT/SIGTERM request shutdown outside the signal handler: the collector stops
admission, cancels in-flight workers, kills/reaps transport subprocesses and joins
its threads before the HTTP server closes. Cleanup is also performed after a
server error. OS scheduling and process cleanup can add overhead; there is no
claim of a hard real-time shutdown bound.

## Read API

All routes require inbound Basic authentication. Remote access requires the
configured TLS reverse proxy. Without an inventory access profile, authenticated
readers share the configured sources. With a profile, current organization
membership and explicit read grants filter the response; see
[access and scope](web-inventory.md#access-and-scope). `GET` and `HEAD` are
supported on these read routes; mutation methods are not.

| Route | Meaning |
| --- | --- |
| `/healthz` | 200 while the HTTP process can respond; no source-health claim |
| `/readyz` | 200 only while collection runs and every configured source has a current authoritative snapshot; otherwise 503 |
| `/v1/status` | 200 with inventory and degradation details, even if some or all sources are unavailable |

An empty source list is not ready (`collectors_not_configured`). With sources,
readiness reports `collector_stopped` if collection is not running, otherwise
`sources_not_current` until every source is current. Success uses `reason: null`.
Readiness is about completeness/freshness of observations, not healthy workloads:
an expired certificate or unhealthy container can be a current observation.

The status envelope contains `schema_version: 1`, `ready`, `reason`, `collecting`
and `sources`. Each source entry contains:

| Field | Meaning |
| --- | --- |
| `source_id`, `kind` | Configured installation identity and adapter kind |
| `availability` | Result of the last completed attempt: `unknown` before the first, `available` for a parsed response, `unavailable` on error; not a continuous connectivity probe |
| `current` | Latest observation is authoritative and still fresh, and collection is running |
| `last_attempt` | Null initially; otherwise fixed `outcome`, UTC Unix-second `attempted_at` / `completed_at`, initial `observation_freshness`, and allowlisted `source_metadata` (null if parsing failed) |
| `last_valid_snapshot` | Null until the first fresh authoritative result; otherwise receive time, advancing age, `max_age_seconds`, freshness, source metadata and projected resources |

`last_attempt.observation_freshness` describes receipt time. In contrast,
`last_valid_snapshot.freshness` and `age_seconds` are recomputed using monotonic
elapsed time on every read. monit-docker age conservatively includes the whole
source request duration, measured monotonically, so a slow transfer cannot make
an old observation appear newly fresh. This can overestimate age because the
source may generate its response partway through the request. Each resource has `source_id`, `kind`, `native_id`
and projected `fields`; identical native IDs in different sources stay separate.
URLs, credential paths, passwords, raw response bodies and exception text are not
included. Nullable measurements remain null.

A failed, malformed, stale or unknown response never erases the previous valid
snapshot. The snapshot may still have `freshness: fresh` by age after a recent
failure, but `current: false` makes clear it is historical. Consumers must check
`current`, not just the presence of resources or the age label. A fresh,
authoritative empty result intentionally replaces the previous list with `[]`.
A never-observed source has `last_valid_snapshot: null`, not an empty-success list.
For Prometheus, an empty query is unknown and does not replace the previous valid
snapshot. Prometheus sample age is not original-data age or source health; series
can remain visible within the configured window after scraping stops. See the
[metric guide](prometheus.md) for this distinct freshness contract.

For example: if monitor A times out while CertLord B still responds, A retains its
previous resources with `current: false`; B remains current; `/readyz` returns 503
and `/v1/status` still returns both entries. A's next successful fresh response
restores its current state without a restart.

## Limits and validation

Storage is in memory only and resets on restart. Retention is limited to one last
valid snapshot plus the latest parsed snapshot per source, not a metrics history.
Projections include filesystem/protection facts and historical TLS verification;
see [core semantics](../docs-core.md). Manual request history, actor metadata and
CertLord operational queues are outside this first inventory projection.
Source clock accuracy cannot be proven from an API response. Large configured
inventories can consume substantial memory; the body/resource bounds are not
benchmarked total-RSS limits, and inventory pagination is not implemented.

Synthetic tests cover all three source kinds, stale/unknown/malformed results, retention,
authoritative empty inventories, source isolation, bounded admission, scheduling,
recovery, inbound/outbound authentication and real daemon SIGTERM with a blocked
source. They do not establish production capacity, leak absence, sustained-load
behavior or successful integration with deployed sources. The [API-backed CLI](cli.md) is implemented. Synthetic Docker acceptance is covered in the [validation guide](contributing.md).
The [Textual interface](textual.md) is available. Durable inventory storage and
live-source qualification remain separate work; these tests use synthetic sources.
