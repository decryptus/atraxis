# CertLord metrics through Covenant

Use `kind: prometheus_certlord` to read the existing CertLord metric contract from
Covenant through Prometheus. It preserves certificate UUIDs, lifecycle metadata,
stored certificate expiration, pending ACME retries and the last recorded TLS
verification outcome. Collection failure, incomplete inventory and stale collection
metadata invalidate the current view. The adapter is read-only.

**This is a metric projection, not the full CertLord observation API.** Domains,
certificate version, fingerprint, not-before date, per-certificate observation
time and full verification evidence are absent from the native metric endpoint.
They remain `null`; the UI uses the UUID when no domain name is available.
Freshness describes the collection and scrape, not each certificate's observation.
These resources do not supply live rule/intervention facts.

## Configure the chain

Merge the [Covenant endpoint example](https://github.com/decryptus/atraxis/blob/main/examples/covenant-certlord.yml)
into an existing Covenant configuration. It uses Covenant's existing `certlord`
plugin, one fixed `/api/certificates/metrics` source, a finite timeout, verified TLS
and explicit read credentials. Set Covenant's `general.result_timeout` above twice
the network timeout (for example 30 seconds for a 10-second timeout). Keep the
password file private and grant its CertLord user only read access. Protect the
exporter separately; certificate UUIDs and lifecycle states are operational data.

The plugin validates each complete source snapshot. A failure emits
`covenant_certlord_source_up=0` without cached certificate gauges. It needs no
custom `collects` filters. See [Covenant's CertLord guide](https://github.com/decryptus/covenant/blob/master/docs/certlord.md)
for its credential and transport settings.

Example Prometheus scrape configuration:

```yaml
scrape_configs:
  - job_name: covenant-certlord
    scrape_interval: 15s
    scrape_timeout: 10s
    scheme: https
    metrics_path: /metrics/certificates
    honor_timestamps: false
    static_configs:
      - targets: [covenant.example.org:443]
```

Add appropriate ingress authentication and CA settings. Do not give `up` different
`job`/`instance` labels from the certificate metrics. Use one source per scrape
target; do not aggregate unrelated CertLord installations into that target.

Atraxis source configuration:

```yaml
sources:
  - source_id: certificates-via-covenant
    kind: prometheus_certlord
    origin: https://prometheus.example.org
    credentials_file: credentials/prometheus.json
    metric:
      match:
        job: covenant-certlord
        instance: covenant.example.org:443
    timeout: 5
    poll_interval: 15
    max_age: 60
    max_clock_skew: 2
    max_resources: 100
```

Both matcher keys are mandatory and exact. Extra stable target labels are allowed;
certificate-specific labels are not target matchers. The metric names are fixed,
not operator-supplied PromQL. A browser source administrator can select a
host-approved connection containing this complete contract. Configure source
access/grants explicitly, validate with `atraxis check-config`, and restart the
daemon after review. No deployed setting changes automatically.

## What is preserved

| Native metric | Atraxis fields / meaning |
| --- | --- |
| `certlord_certificate_info` | UUID, origin, renewal owner, status, pending action and material state |
| `certlord_certificate_not_after_timestamp_seconds` | `not_after_timestamp_seconds` and UTC `not_after`; only for readable material |
| `certlord_certificate_issuance_retry_count` | Integer `issuance_retry_count` for currently pending ACME create/renew |
| `certlord_certificate_tls_verification` | `tls_verification_status`: `verified`, `failed` or `not_checked` |
| `certlord_certificate_tls_verified_at_timestamp_seconds` | UTC `tls_verified_at`, only for a recorded successful verification |
| `certlord_observation_completed_timestamp_seconds` | `collection_completed_at` and source collection freshness |
| `certlord_certificates_observed` | Completeness/empty-inventory check |

The projection is marked `projection: certlord_metrics`. Its `verification` field
stays `null`: the metric status lacks the fingerprints, error details and timing
needed for the full verification object. A historical successful TLS check does
not establish what every destination currently serves. Certificate expiry is not
observation freshness: an expired certificate can be observed in a fresh collection.
Retry zero remains zero; absent/inapplicable retry or expiry stays unknown.

Resource grants use `kind: certificate`, the source ID and the original UUID.
Web, CLI and Textual search support these resources by UUID, source and lifecycle
status. Domain search cannot find a name that was not exported. Collection-only
freshness is labelled in the inventory and search displays.

## Coherence and failure semantics

One bounded range query reads Prometheus `up`, Covenant's `source_up`, collection
completion/count and all seven CertLord metric families. The latest `up` sample
anchors the scrape. Both up values must be one; metadata timestamps must match
the anchor. Only certificate series from that same scrape are projected. Old
status labels, deleted UUIDs and previous successful TLS results cannot be reused
from earlier scrapes. Duplicate identities/guard series or incomplete required
expiry, retry or TLS data fail closed.

Collection and scrape age must fit `max_age`; future collection times outside
`max_clock_skew` are rejected. Age advances monotonically after receipt. A fresh
zero count with matching metadata is an authoritative empty inventory. Failed,
missing, stale or partial responses leave prior data historical. Recovery requires
a complete valid collection. Detection waits for the next scrape and Atraxis poll.

**Per-certificate freshness is unavailable.** A long collection can complete
recently even though a certificate was read earlier. The direct `certlord` adapter
has each certificate's `observed_at`; this metric endpoint does not. Keep the direct
adapter when you require those timestamps, domains, fingerprints or full verification
evidence. This adapter does not manufacture them or authorize interventions.

Limits include the configured response byte budget and certificate count,
`min(10000, 10 * max_resources + 4)` returned series including historical label
sets, 1,000 samples per series, 10,000 samples per response, 12 exact target
matchers, 256 characters per label value and an 8 KiB encoded query. Non-finite
values, warnings, informational annotations, truncated/ambiguous results and
excess budgets invalidate the response.
