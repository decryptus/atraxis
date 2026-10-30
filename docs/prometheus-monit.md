# Monit-Docker observations through Covenant

Use `kind: prometheus_monit` for the same read-only container inventory as the
native Monit-Docker source. Covenant reads `/v1/status` once and exports the
complete allowlisted observation. Atraxis reconstructs it and uses its native
Monit-Docker parser. Full Docker IDs, every container and filesystem field,
source metadata, action counters, nulls, booleans and integer precision survive.
The nine supported rule inputs retain their native units and authorization rules.

## Configure the chain

Use the optional `monit_observation` Covenant plugin and the
[endpoint example](https://github.com/decryptus/atraxis/blob/main/examples/covenant-monit.yml).
It collects every container in the source response; no hardcoded container or
filesystem selection is required. This plugin is required: the generic HTTP
filters and legacy partial metric contracts do not supply complete observations.
Set `general.result_timeout` above twice the source network timeout. Use HTTPS,
verified TLS and explicit read credentials; only literal loopback IPs permit HTTP
for local testing. Credentials are read from private files and never exported.
The destination is fixed, redirects are refused, and failed reads emit only
`covenant_monit_observation_up=0`, with no cached observation fields.

Example Prometheus target (add ingress authentication and CA settings):

```yaml
scrape_configs:
  - job_name: covenant-monit
    scrape_interval: 15s
    scrape_timeout: 10s
    scheme: https
    metrics_path: /metrics/monit
    honor_timestamps: false
    static_configs:
      - targets: [covenant.example.org:443]
```

Atraxis configuration:

```yaml
sources:
  - source_id: containers-via-covenant
    kind: prometheus_monit
    origin: https://prometheus.example.org
    credentials_file: credentials/prometheus.json
    metric:
      prefix: covenant_monit_observation_
      match:
        job: covenant-monit
        instance: covenant.example.org:443
    timeout: 5
    poll_interval: 15
    max_age: 60
    max_clock_skew: 2
    max_resources: 100
    max_response_bytes: 4194304
```

The prefix is fixed. Match exactly one target, with mandatory `job` and `instance`.
Include any additional target labels in `match`; never aggregate upstream sources
or rewrite labels differently between `up` and the observation families.
Configure grants explicitly and validate with `atraxis check-config` before
restarting the daemon. Source administrators can select a host-approved connection;
they cannot supply arbitrary PromQL or redefine the transport contract.

## Freshness and completeness

One bounded instant query returns values and their actual scrape timestamps.
Prometheus's query evaluation time is never treated as a sample time. The exporter
manifest, every field, collection timing and both availability guards must belong
to the same successful scrape. The schema, field count and SHA-256 content digest
must match. Missing or altered metrics invalidate the whole observation; null is
an explicit source value and is never replaced by zero.

Monit's own `age_seconds` remains authoritative, as in the direct adapter.
Source HTTP duration and subsequent retention time are added to observation age.
The source's reported metadata is preserved separately. A new scrape cannot reset
that age. Empty inventories and unready sources retain the native meaning. Failed
reads keep any prior valid snapshot historical, and cannot supply current facts.

This transports the same Atraxis inventory, not action/audit endpoints or arbitrary
source keys. It does not submit jobs, activate rules or change Monit-Docker.
Detection still occurs on the next scrape/poll, not synchronously with a source change.

## Capacity and operation

The closed transport accepts at most 10,000 typed field rows and 4 MiB per source
response/export; Atraxis also applies its configured resource/response limits.
Exceeding a limit rejects the whole observation, never a truncated inventory.
Strings are bounded to 4,096 characters. Large integers use exact decimal text;
other numeric facts remain gauge values. No arbitrary JSON blob is placed in a label.

Text fields, including timestamps and the digest, create label generations when
changed. Size Prometheus retention and cardinality for the inventory and scrape
rate; qualify production capacity before switching. Instant queries avoid returning
historical label generations, but those generations still consume storage.
Do not drop or transform these families with metric relabeling. Protect their
read access like the native inventory. Keep exporter/Prometheus/Atraxis clocks
synchronized within `max_clock_skew`; clock uncertainty invalidates the current view.
