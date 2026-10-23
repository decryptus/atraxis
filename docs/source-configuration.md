# Source configuration

`atraxis check-config` validates up to 32 source definitions without DNS resolution
or requests. `atraxis serve` periodically reads configured sources and exposes
their inventory through the authenticated API; see [collection](collection-inventory.md).
With `sources: []`, the daemon serves health/status but is not ready. Valid
configuration is not evidence of connectivity or API permission.

```yaml
general:
  auth_basic_file: users.htpasswd  # Inbound Atraxis authentication.
sources:
  - source_id: host-a-monitor
    kind: monit_docker
    origin: https://monitor.example.org
    credentials_file: credentials/host-a.json
  - source_id: certificate-service
    kind: certlord
    origin: https://certificates.example.org
    credentials_file: credentials/certificates.json
    timeout: 10
    poll_interval: 60
    max_age: 180
```

`source_id` is a stable installation identity (1–128 characters, no control
characters or leading/trailing whitespace). It can contain a name or UUID and must
be unique in the file. Keep it when an installation's URL changes. Resource identity
also includes source identity; identical native IDs on two sources stay distinct.

Accepted kinds are `monit_docker`, `certlord` and `prometheus`. Their GET routes
are fixed to `/v1/status`, `/api/certificates/observations` and `/api/v1/query`.
Prometheus requires an explicit `metric` selection; see the
[Covenant/Prometheus guide](prometheus.md) for configuration and sample semantics.
`origin` contains just the scheme,
host and optional port, with an optional trailing slash. Userinfo, arbitrary paths,
query strings, fragments, whitespace and percent escapes are rejected. Unicode DNS
names are normalized to IDNA. Custom proxy path prefixes are not supported yet.

Remote origins require HTTPS. For local fixtures only, explicit
`allow_loopback_http: true` permits literal loopback addresses such as
`http://127.0.0.1:9810` or `http://[::1]:9810`; names like `localhost`, abbreviated
IPv4 and other destinations are rejected. Do not set this flag on HTTPS origins.

`credentials_file` is a separate UTF-8 JSON file, at most 8 KiB, containing exactly
`username` and `password` strings. Empty values, control characters, a colon in the
username, duplicate keys and extra fields are rejected. Use a read-only API identity
(or the protected HTTPS ingress for monit-docker). This is **not** an htpasswd file:
outbound Basic authentication needs the credential, not its hash. File paths resolve
relative to the main YAML; keep them readable only by the service identity/operator.
Passwords are not retained in the SourceConfig value or printed in errors/repr.
The HTTP transport reloads credentials explicitly for each request and never uses ambient netrc.

Optional `ca_file` points to a readable CA PEM bundle (at most 1 MiB) for HTTPS.
Its certificate syntax is checked offline. There is no option to disable TLS
verification. System trust is the default; a configured CA bundle supplies explicit trust roots.
Certificate and hostname checks are exercised by the transport tests.

| Setting | Default | Accepted range |
| --- | ---: | --- |
| `poll_interval` | 60 s | 1–3600 s |
| `timeout` | 10 s | 1–60 s |
| `max_age` | 180 s | 1–86400 s |
| `max_response_bytes` | 1048576 | 1024–4194304 |
| `max_resources` | 1000 | 1–10000 |
| `max_clock_skew` | 30 s | 0–300 s |

`max_resources` bounds the container/certificate/metric-series count and separately the total
filesystem observations in a monit-docker response. Oversized collections fail
without replacing the previous valid snapshot.

All limits are strict integers, not booleans or numeric strings. Require
`timeout <= poll_interval <= max_age`. These initial budgets are configuration
contracts, not measured capacity claims. The [HTTP transport](http-transport.md) enforces response-size, deadline and
no-overlap constraints at runtime. Configuration validation alone cannot guarantee
connectivity. Polling uses a minimum pause after completion rather than a fixed
wall-clock rate; see [scheduling and freshness](collection-inventory.md).
