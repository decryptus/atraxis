# CertLord observations through Covenant

Use `kind: prometheus_certlord` for the same read-only certificate inventory as
Atraxis's direct CertLord source. This uses `/api/certificates/observations`, not
the limited `/api/certificates/metrics` endpoint. Domains, UUIDs, versions,
fingerprints, validity dates, lifecycle/retry fields, full stored TLS verification
and per-certificate observation times are preserved. Native parsing and freshness
rules are reused, including freshness of the oldest certificate observation.

## Configure the chain

Use Covenant's optional `certlord_observation` plugin and the
[endpoint example](https://github.com/decryptus/atraxis/blob/main/examples/covenant-certlord.yml).
It performs one fixed, authenticated, read-only JSON request per collection.
Set `general.result_timeout` above twice the source network timeout. Use HTTPS,
verified TLS and a private password/token file with source read permission only.
Literal loopback HTTP is supported for local testing. Redirects and dynamic
source targets are refused. Failures emit `covenant_certlord_observation_up=0`
without any previously collected fields. The existing Covenant `certlord` plugin
and its legacy numeric metrics remain available for their existing uses.

Example Prometheus scrape (add ingress authentication and CA settings):

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

Atraxis configuration:

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
    max_response_bytes: 4194304
```

Exactly one target is selected. Both matchers are mandatory; include all additional
target labels. Do not combine unrelated source installations. Host-approved
connections, source/resource grants and domain search work as with direct sources.
Validate with `atraxis check-config` before restarting the daemon.

## Equivalent result

| Native observation | Covenant / Prometheus result |
| --- | --- |
| Certificate UUID | Same stable native identity, including renewal |
| Domains, version, SHA-256 fingerprint | Same values and domain order; searchable/displayable domains |
| Lifecycle, origin, renewal owner, pending action | Same states |
| Not-before, not-after, expiry epoch | Same dates and precision; missing material remains null |
| Retry count and last retry time | Same values, including zero versus unknown |
| Verification status, check time, error, expected/observed/target hashes | Full stored verification object or null |
| Per-certificate observation time | Same value; oldest observation controls freshness |
| Started/completed times and completion epoch | Same source metadata |
| Empty, stale, future or invalid snapshot | Same native interpretation, with additional transport guards |

A recent collection containing an old certificate observation is stale. Expired
certificate material can still be a fresh observation: expiry and data age are
separate concepts. Stored TLS verification describes the last recorded check; it
is not a new probe. Certificate observation does not grant live intervention rights.

The same complete-scrape, schema/count/digest and transport checks described in the
[Monit-Docker guide](prometheus-monit.md#freshness-and-completeness) apply here.
No old label generation is reused after a change or deletion. No missing domain or
fingerprint is invented. Limits and Prometheus storage/cardinality considerations
are documented under [capacity and operation](prometheus-monit.md#capacity-and-operation).
Protect the exported domains and fingerprints as operational inventory data.
Neither PEM material nor unrecognized source fields are exported.
